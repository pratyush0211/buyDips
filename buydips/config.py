"""Settings, all read from environment variables (or a local .env file)."""

import os
import re

from dotenv import load_dotenv

load_dotenv()


def _db_url() -> str:
    # Tolerate stray whitespace/quotes from copy-paste.
    url = os.getenv("DATABASE_URL", "sqlite:///buydips.db").strip().strip("\"'").strip()
    # Hosts hand out "postgres://" or "postgresql+psycopg://" (a driver we don't install); normalise
    # every Postgres style to the psycopg2 driver we do install.
    url = re.sub(r"^postgres(ql)?(\+\w+)?://", "postgresql+psycopg2://", url)
    _check_db_url(url)
    return url


def _check_db_url(url: str) -> None:
    """Fail at startup with a plain-English reason (never echoing the URL, which holds a password)."""
    from sqlalchemy.engine import make_url

    problem = None
    if "[" in url or "]" in url or "YOUR-" in url.upper():
        problem = "it still contains the [YOUR-PASSWORD] placeholder (remove the square brackets and use your real password)"
    elif not url.startswith(("postgresql://", "postgresql+", "sqlite:")):
        problem = "it must start with postgresql://"
    else:
        try:
            make_url(url)
        except Exception:  # noqa: BLE001
            problem = ("it can't be parsed; a password containing @ / # ? or % must be URL-encoded, or reset to "
                       "letters and numbers only")
    if problem:
        raise RuntimeError(
            "DATABASE_URL is invalid: " + problem + ". It should look like "
            "postgresql://USER:PASSWORD@HOST:5432/postgres"
        )


DATABASE_URL = _db_url()
SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
# Public address used in emailed links. On Render, RENDER_EXTERNAL_URL is set automatically;
# set BASE_URL explicitly once a custom domain is attached.
BASE_URL = (os.getenv("BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "http://127.0.0.1:5000").rstrip("/")

# How email is sent: "smtp" (Gmail etc.), or an HTTPS API: "brevo" / "resend".
# HTTPS APIs are needed on hosts that block outgoing SMTP, such as Render's free plan.
EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "smtp").strip().lower()
EMAIL_API_KEY = os.getenv("EMAIL_API_KEY", "").strip()

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
# Address shown in the From header (defaults to the SMTP login)
MAIL_FROM = os.getenv("MAIL_FROM", "") or SMTP_USER

# Where "a new user joined" notices go (leave empty to disable).
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip()

# Optional public contact address shown in the footer and privacy page.
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "").strip()

LOGIN_LINK_MINUTES = 15
LOGIN_LINK_COOLDOWN_SECONDS = 60
LOGIN_PIN_TRIES = 5   # wrong guesses allowed on one emailed 6-digit code before it is thrown away
MAX_WATCHLIST_ITEMS = 25
# If more than this share of symbols fail to load in one run, the run is marked failed.
MAX_FETCH_FAILURE_RATIO = 0.5

DISCLAIMER = (
    "This is an automated price alert for information only. It is not investment advice or a recommendation to buy "
    "or sell anything, and buyDips is not a SEBI-registered investment adviser or research analyst. "
    "Prices come from a third-party source and may be delayed or inaccurate."
)

# Buckets: named groups of stocks whose performance is tracked from the day each stock was added.
BUCKET_FLAG_PCT = float(os.getenv("BUCKET_FLAG_PCT", "10"))  # a stock this far below its added price gets a red flag
MAX_BUCKETS = 10
MAX_BUCKET_ITEMS = 25
# Alert levels offered on the first-run screen (percent fall from the previous close).
START_LEVELS = (2.0, 3.0, 5.0)

# The daily brief: once each morning, Claude researches the few followed stocks that triggered a technical signal.
# With no key set nothing is researched and pages simply have no brief.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
BRIEF_MODEL = os.getenv("BRIEF_MODEL", "claude-haiku-5-5").strip()   # the cheapest model; set claude-sonnet-5-5 for sharper judgement at ~20x the token price
BRIEF_MAX_PER_DAY = int(os.getenv("BRIEF_MAX_PER_DAY", "25"))   # hard ceiling on stocks researched per day (the cost cap)
BRIEF_MAX_SEARCHES = int(os.getenv("BRIEF_MAX_SEARCHES", "1"))             # web pages read are most of the cost: one search is usually enough
BRIEF_FORCE = os.getenv("BRIEF_FORCE", "").strip().lower() in ("1", "true")   # a manual run only: research again even if today is done
BRIEF_STRICT_NUMBERS = os.getenv("BRIEF_STRICT_NUMBERS", "").strip().lower() in ("1", "true")   # drop any figure we cannot find in a quoted passage or headline
BRIEF_MIN_SIGNAL = 2                                              # a stock needs a signal at least this strong (1 minor to 3 major), or a dip
BRIEF_MIN_MATERIAL = 6                                            # 0-10 significance a brief needs before anyone sees it
BRIEF_RUN_AFTER_UTC_HOUR = 3                                      # first job run at or after this hour (08:30 IST) does the day's research
BRIEF_TIME_BUDGET_SECONDS = 480                                   # stop researching after this long in one run (the job has a 10 minute limit); the next run resumes
BRIEF_MAX_FAILURES = 3                                            # stop for the hour after this many failed requests in a row
