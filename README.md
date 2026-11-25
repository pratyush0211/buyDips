# buyDips 📉

Pick the indexes, stocks and gold and silver ETFs you follow (Nifty 500, Nasdaq-100, Gold BeES and Silver ETF), set how far they must fall, and get an email when they do.

## How it works

- **Website** (`buydips/web.py`, Flask): passwordless sign-in by emailed link or 6-digit code, a watchlist page, a daily AI brief (researched once each morning for followed stocks that triggered a technical signal, shown only when notable, with sources, charts, share cards and an optional email) and a performance page, one-click unsubscribe, account deletion.
- **Scheduled job** (`run_check.py`, started by `.github/workflows/check.yml` on a 20-minute GitHub schedule, Mon–Fri, which GitHub often delays or skips, so an external trigger (cron-job.org) also starts it about hourly; in practice, expect a check about once an hour): fetches each distinct watched symbol once, then emails every user whose alert level was crossed. Each (user, symbol, trading session) alerts at most once. The run fails loudly if most prices can't be fetched.
- **Data** (`buydips/data.py`): Yahoo Finance via `yfinance`, behind a small provider interface so it can be swapped for a licensed source.
- **Catalog** (`buydips/catalog.json`): the curated, Yahoo-verified list users can choose from.
- **Storage**: Postgres (Supabase) in production, SQLite locally. Tables: `users`, `subscriptions`, `alerts_sent`.
- **Email** (`buydips/mailer.py`): Resend/Brevo over HTTPS, or SMTP.

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
.venv/bin/flask --app wsgi run --port 5000     # http://127.0.0.1:5000
.venv/bin/python run_check.py                  # one alert run
.venv/bin/python -m pytest                     # tests
```

With no email configured, sign-in links and codes are printed to the server log instead of sent.

## Deploy

- **Website**: Render (`render.yaml`). Set `DATABASE_URL`, `SECRET_KEY`, `EMAIL_API_KEY`, `MAIL_FROM`, `EMAIL_PROVIDER`.
- **Scheduled job**: GitHub Actions. Secrets: `DATABASE_URL`, `SECRET_KEY`, `EMAIL_API_KEY`, and `ANTHROPIC_API_KEY` for the daily brief (the AI research runs in this job, not on the website). Repository variable: `BASE_URL`.
- `SECRET_KEY` must be **identical** in both places: it signs the unsubscribe links in emails.

## Notes

- Alerts are for information only, not investment advice. Prices come from a third-party source and may be delayed.
- Yahoo data is unofficial and fine for a small beta; use a licensed provider before a wide public launch.

## Market holidays (refresh every December)

`buydips/holidays.json` lists weekday closures for India (from NSE's "Trading holidays" circular) and the US
(from NYSE's hours-and-calendars page). Alerts are skipped on those days. If the current year has no dates,
each run logs a warning. Weekends need no entry. Early closes and Diwali Muhurat sessions are not special-cased.
