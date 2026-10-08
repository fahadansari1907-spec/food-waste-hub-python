"""Food Waste Hub — a small Flask app for mindful food use."""

from __future__ import annotations

import math
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, render_template, request
from flask import g, redirect, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FOOD_WASTE_SECRET_KEY") or secrets.token_hex(32)
app.config["DATABASE"] = str(Path(app.instance_path) / "food_waste_hub.sqlite3")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
Path(app.instance_path).mkdir(parents=True, exist_ok=True)

# Per-food serving estimates. Water is available only where a comparable source
# and a documented serving conversion are available.
FOOD_IMPACTS = {
    # The rice footprint is a global average for dry rice. The estimate function
    # converts cooked weight to an approximate dry-rice equivalent (3:1).
    # Other foods intentionally have no numeric footprint until a suitable,
    # comparable source and serving conversion are available.
    "Cooked rice": {"water_l_per_kg": 2500, "grams_per_meal": 180},
    "Roti": {"water_l_per_kg": None, "grams_per_meal": 60},
    "Dal": {"water_l_per_kg": None, "grams_per_meal": 180},
    "Vegetables": {"water_l_per_kg": None, "grams_per_meal": 150},
    "Other": {"water_l_per_kg": None, "grams_per_meal": 150},
}

FOOD_GUIDE = {
    "rice": ("Rice gives your body carbohydrates for energy. Pair it with vegetables and a protein such as dal for a more filling meal.", "A very large portion may leave less room for other food groups. Brown and white rice differ in fibre; neither is automatically right for everyone."),
    "roti": ("Roti is mainly a source of carbohydrates. Whole-wheat atta usually has more fibre than refined flour.", "What goes into the dough and what you eat alongside it affect the overall meal. Add a protein and vegetables when you can."),
    "dal": ("Dal provides plant protein and fibre, which can help make a meal satisfying.", "Recipes vary in salt, oil, and thickness. Dal works well with grains and vegetables as part of a varied meal."),
    "lentil": ("Lentils provide plant protein, fibre, and useful minerals. They can be a filling part of a meal.", "Preparation and portion matter, as with any food. Rinse and cook them well; pair with other foods you enjoy."),
    "banana": ("A banana provides carbohydrates, fibre, and potassium. It is an easy fruit to take on the go.", "It is one food, not a complete meal by itself for everyone. Pair it with other foods if you need a longer-lasting snack."),
    "vegetable": ("Vegetables offer fibre and a mix of vitamins and minerals. Different colours bring different nutrients.", "The amount of oil, salt, and cooking method can change a dish. Frozen vegetables can also be a useful choice."),
    "noodles": ("Noodles provide energy from carbohydrates and can be part of a meal.", "Some instant noodles are high in salt and low in fibre or protein. Add vegetables or an egg, tofu, or beans, and use less seasoning if you prefer."),
    "egg": ("Eggs provide protein and several nutrients. They can be one convenient part of a meal.", "Your overall eating pattern matters more than judging one ingredient. Cook eggs safely and consider your own health needs."),
    "apple": ("Apples provide fibre and carbohydrates, and are an easy fruit to carry.", "An apple is one useful option among many; variety across foods helps cover different needs."),
}


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def init_db() -> None:
    db = sqlite3.connect(app.config["DATABASE"])
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS donations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            food_type TEXT NOT NULL,
            portions INTEGER NOT NULL,
            prepared_at TEXT NOT NULL,
            storage TEXT NOT NULL,
            allergens TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Pending partner verification',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS community_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            display_name TEXT NOT NULL,
            story TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    """)
    db.commit()
    db.close()


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def load_user_and_check_csrf():
    user_id = session.get("user_id")
    g.user = get_db().execute(
        "SELECT id, email, display_name FROM users WHERE id = ?", (user_id,)
    ).fetchone() if user_id else None
    if request.method == "POST":
        supplied = request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        if not expected or not secrets.compare_digest(supplied, expected):
            return "Invalid or expired form token. Refresh the page and try again.", 400


def safe_next_path(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or not value.startswith("/") or value.startswith("//"):
        return None
    return value


init_db()


def estimate_waste(food: str, amount: float, unit: str) -> dict:
    grams = amount * 1000 if unit == "kg" else amount * 180 if unit == "serving" else amount
    impact = FOOD_IMPACTS.get(food, FOOD_IMPACTS["Other"])
    kg = grams / 1000
    # A cooked serving of rice contains substantial added water; compare its
    # estimated uncooked-rice equivalent with the published dry-rice footprint.
    source_kg = kg / 3 if food == "Cooked rice" else None
    water = round(source_kg * impact["water_l_per_kg"]) if source_kg is not None else None
    return {
        "grams": round(grams),
        "meals": round(grams / impact["grams_per_meal"], 1),
        "water": water,
        "food": food,
    }


def calorie_estimate(age: int, weight: float, height: float, equation_group: str,
                     activity: str) -> dict[str, int | float]:
    """Estimate resting energy and a rough activity-adjusted daily amount."""
    # Mifflin–St Jeor estimates resting energy expenditure. Multipliers are a
    # common planning shorthand for activity, not a measured personal need.
    base = 10 * weight + 6.25 * height - 5 * age
    resting = base + (5 if equation_group == "male" else -161)
    factors = {"low": 1.2, "light": 1.375, "active": 1.55}
    factor = factors[activity]
    return {
        "resting": max(0, int(round(resting / 50) * 50)),
        "daily": max(0, int(round(resting * factor / 50) * 50)),
        "factor": factor,
    }


@app.route("/", methods=["GET", "POST"])
def index():
    page = request.args.get("page", "waste")
    impact_result = None
    calorie_result = None
    food_result = None
    message = None
    posts = []
    donations = []

    if request.method == "POST":
        action = request.form.get("action")
        page = {"impact": "waste", "calories": "portion", "food_check": "check", "donation": "donate", "community": "community"}.get(action, page)
        try:
            if action == "impact":
                amount = float(request.form.get("amount", ""))
                if not math.isfinite(amount) or amount <= 0 or amount > 100000:
                    raise ValueError
                food = request.form.get("food", "Other")
                if food not in FOOD_IMPACTS:
                    food = "Other"
                unit = request.form.get("unit", "g")
                if unit not in {"g", "kg", "serving"}:
                    unit = "g"
                impact_result = estimate_waste(food, amount, unit)
            elif action == "calories":
                age = int(request.form.get("age", ""))
                weight = float(request.form.get("weight", ""))
                height = float(request.form.get("height", ""))
                equation_group = request.form.get("equation_group", "")
                if age < 19 or age > 78:
                    message = "This adult estimate is intended for ages 19–78. It does not calculate calorie needs for children or teens."
                elif (not math.isfinite(weight) or weight < 10 or weight > 500
                        or not math.isfinite(height) or height < 100 or height > 250
                        or equation_group not in {"female", "male"}):
                    raise ValueError
                else:
                    activity = request.form.get("activity", "low")
                    if activity not in {"low", "light", "active"}:
                        raise ValueError
                    calorie_result = calorie_estimate(age, weight, height, equation_group, activity)
            elif action == "food_check":
                query = request.form.get("food_name", "").strip()
                if not query:
                    message = "Enter a food name to get a simple overview."
                else:
                    words = set(re.findall(r"[a-z]+", query.lower()))
                    aliases = {"lentils": "lentil", "vegetables": "vegetable", "eggs": "egg",
                               "apples": "apple", "bananas": "banana", "noodle": "noodles"}
                    words.update(aliases[word] for word in tuple(words) if word in aliases)
                    match = next((v for k, v in FOOD_GUIDE.items() if k in words), None)
                    food_result = {"name": query, "good": match[0], "consider": match[1]} if match else {
                        "name": query,
                        "good": "A food's nutrients depend on its ingredients and preparation. Look at the label or recipe to see what it contributes.",
                        "consider": "No single food is simply healthy or unhealthy for everyone. Portion, overall eating pattern, and personal needs matter.",
                    }
            elif action == "donation":
                if not g.user:
                    return redirect(url_for("login", next="/?page=donate"))
                food_type = request.form.get("food_type", "")
                portions = request.form.get("portions", "")
                prepared_at = request.form.get("prepared_at", "")
                storage = request.form.get("storage", "")
                allergens = request.form.get("allergens", "").strip()[:500]
                if food_type not in {"Cooked meal", "Fresh produce", "Packaged food"}:
                    raise ValueError
                portions = int(portions)
                if portions < 1 or portions > 10000:
                    raise ValueError
                datetime.strptime(prepared_at, "%H:%M")
                if storage not in {"Refrigerated", "Kept hot", "Room temperature"}:
                    raise ValueError
                db = get_db()
                db.execute("""INSERT INTO donations
                    (user_id, food_type, portions, prepared_at, storage, allergens, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (g.user["id"], food_type, portions, prepared_at, storage, allergens,
                     datetime.now(timezone.utc).isoformat()))
                db.commit()
                message = "Offer recorded as pending partner verification. No pickup has been booked. A verified local partner must review safety and confirm collection."
            elif action == "community":
                if not g.user:
                    return redirect(url_for("login", next="/?page=community"))
                story = request.form.get("story", "").strip()
                display_name = request.form.get("display_name", "").strip()[:80] or g.user["display_name"]
                if not story or len(story) > 2000:
                    raise ValueError
                db = get_db()
                db.execute("INSERT INTO community_posts (user_id, display_name, story, created_at) VALUES (?, ?, ?, ?)",
                           (g.user["id"], display_name, story, datetime.now(timezone.utc).isoformat()))
                db.commit()
                message = "Your story has been shared with the community. Please be kind and avoid posting private information."
        except (ValueError, TypeError):
            message = "Please check the values and try again."

    if page == "community":
        posts = get_db().execute("SELECT display_name, story, created_at FROM community_posts ORDER BY id DESC LIMIT 50").fetchall()
    if page == "donate" and g.user:
        donations = get_db().execute("SELECT food_type, portions, status, created_at FROM donations WHERE user_id = ? ORDER BY id DESC LIMIT 20", (g.user["id"],)).fetchall()
    return render_template("index.html", page=page, impact=impact_result,
                           calories=calorie_result, food_result=food_result, message=message,
                           user=g.user, posts=posts, donations=donations)


@app.route("/register", methods=["GET", "POST"])
def register():
    error = None
    next_path = safe_next_path(request.values.get("next")) or "/?page=waste"
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        display_name = request.form.get("display_name", "").strip()[:80]
        password = request.form.get("password", "")
        if (not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 254
                or not display_name or len(password) < 10 or len(password) > 256):
            error = "Enter a valid email and display name, and choose a password with at least 10 characters."
        else:
            try:
                db = get_db()
                cur = db.execute("INSERT INTO users (email, password_hash, display_name, created_at) VALUES (?, ?, ?, ?)",
                                 (email, generate_password_hash(password), display_name,
                                  datetime.now(timezone.utc).isoformat()))
                db.commit()
                session.clear()
                session["user_id"] = cur.lastrowid
                return redirect(next_path)
            except sqlite3.IntegrityError:
                error = "An account with that email already exists."
    return render_template("auth.html", mode="register", error=error, next_path=next_path)


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    next_path = safe_next_path(request.values.get("next")) or "/?page=waste"
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = get_db().execute("SELECT id, password_hash FROM users WHERE email = ?", (email,)).fetchone()
        if user and len(password) <= 256 and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            return redirect(safe_next_path(request.form.get("next")) or "/?page=waste")
        error = "Email or password was not recognized."
    return render_template("auth.html", mode="login", error=error, next_path=next_path)


@app.post("/logout")
def logout():
    session.clear()
    return redirect("/?page=waste")


if __name__ == "__main__":
    app.run(debug=False)
