"""Settings, all read from environment variables (or a local .env file)."""

import os

from dotenv import load_dotenv

load_dotenv()


def _db_url() -> str:
    url = os.getenv("DATABASE_URL", "sqlite:///buydips.db")
    # Some hosts hand out "postgres://", which SQLAlchemy no longer accepts.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


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
