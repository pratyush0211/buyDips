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

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
# Address shown in the From header (defaults to the SMTP login)
MAIL_FROM = os.getenv("MAIL_FROM", "") or SMTP_USER

LOGIN_LINK_MINUTES = 15
LOGIN_LINK_COOLDOWN_SECONDS = 60
MAX_WATCHLIST_ITEMS = 25
# If more than this share of symbols fail to load in one run, the run is marked failed.
MAX_FETCH_FAILURE_RATIO = 0.5

DISCLAIMER = (
    "This is an automated price alert for information only. It is not investment advice. "
    "Prices come from a third-party source and may be delayed or inaccurate."
)
