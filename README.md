# Food Waste Hub

A Python-first Flask app inspired by the supplied Foodwise reference: warm off-white canvas, forest-green accents, responsive sidebar, and simple cards. It includes a food-waste impact estimator, calorie and portion guide, plain-language food guide, account registration/login, persisted food offers, and a community feed that starts empty and only shows user-submitted stories.

## Run locally

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000.

The first visit to registration creates the local SQLite database at `instance/food_waste_hub.sqlite3`. For sessions to remain valid across app restarts, set a persistent secret key before running:

```powershell
$env:FOOD_WASTE_SECRET_KEY = "paste-a-long-random-secret-here"
python app.py
```

Generate one with `py -c "import secrets; print(secrets.token_hex(32))"`. Keep it private and do not commit it.

## Scope and estimates

The rice water figure uses a global dry-rice water-footprint estimate from the Water Footprint Network and an approximate 3:1 cooked-to-dry weight conversion. It is not local or product-specific; the app leaves the water figure blank for other foods until comparable reference data is available. Meal counts use rough serving weights (180 g rice, 60 g roti, 180 g dal, 150 g vegetables/other).

The calorie guide displays a resting-energy estimate using the Mifflin–St Jeor equation, plus a separate rough daily estimate using activity multipliers of 1.2, 1.375, or 1.55. These multipliers are planning shorthand and do not measure personal energy needs. It requires age, height, weight, and the equation coefficient; it is not medical advice or a prescription.

Accounts, password hashes, offers, and community stories are stored in local SQLite. Offers remain pending partner verification; this app does not dispatch a gig worker or arrange pickup. Community stories are visible to anyone who opens the community page, so users are told not to include private information. There is no moderation queue, email verification, or password reset yet. This local development server is not configured for public production use; a public launch needs HTTPS, a persistent secret, rate limiting, account recovery, moderation, and a real partner workflow.
