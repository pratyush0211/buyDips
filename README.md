# buyDips 📉

Tracks major stock indexes and emails you when one falls 1% or more from its previous close.

## Run in the cloud (GitHub Actions)

`.github/workflows/check.yml` runs `check.py` every 20 minutes, Mon–Fri, 00:00–21:59 UTC.

1. Add repository secrets (**Settings → Secrets and variables → Actions**): `SMTP_USER`, `SMTP_PASSWORD`, and optionally `EMAIL_TO`.
2. Test it: **Actions → buyDips check → Run workflow**, tick **Send a test alert**.

Alert state (`alerts_state.json`) is committed back to the repo after each alert so the next run doesn't repeat it.

## Run locally (with dashboard)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then edit .env
.venv/bin/python app.py
```

Open http://127.0.0.1:5000.

## Email (Gmail, free)

1. Turn on 2-Step Verification for your Google account.
2. Create an app password at https://myaccount.google.com/apppasswords (name it "buyDips").
3. Set `SMTP_USER` to your Gmail address and `SMTP_PASSWORD` to the 16-character app password.

## How it works

- Prices come from Yahoo Finance (latest daily close vs. the previous one).
- If an index is down `DIP_THRESHOLD_PCT` (default 1%) or more, it sends an email alert.
- At most one alert per index per trading session.
- Edit `INDEXES` to change which indexes are tracked (Yahoo Finance symbols).
