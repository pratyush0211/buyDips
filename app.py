"""buyDips: watches major stock indexes and sends an email alert when one dips."""

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

import yfinance as yf
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, url_for

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("buyDips")

DIP_THRESHOLD_PCT = float(os.getenv("DIP_THRESHOLD_PCT", "1.0"))
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
DEFAULT_INDEXES = "^GSPC=S&P 500,^IXIC=Nasdaq Composite,^DJI=Dow Jones"
STATE_FILE = Path(__file__).parent / "alerts_state.json"


def parse_indexes(raw: str) -> Dict[str, str]:
    indexes = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        symbol, _, name = pair.partition("=")
        indexes[symbol.strip()] = name.strip() or symbol.strip()
    return indexes


INDEXES = parse_indexes(os.getenv("INDEXES", DEFAULT_INDEXES))

# In-memory snapshot shown on the dashboard
quotes: Dict[str, dict] = {}
last_check: Optional[datetime] = None
lock = threading.Lock()


# ---------- alert state (so we alert at most once per index per trading day) ----------

def load_state() -> Dict[str, str]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: Dict[str, str]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


alert_state = load_state()


# ---------- notifications ----------

def notify(body: str) -> bool:
    """Send an alert. Returns True if it was delivered."""
    return send_email(body)


def send_email(body: str) -> bool:
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    to = os.getenv("EMAIL_TO") or user
    if not user or not password or password == "your_app_password":
        log.info("Email not configured; skipping")
        return False
    try:
        import smtplib
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["Subject"] = body.split(":")[0] if body.startswith("📉") else "buyDips"
        msg["From"] = user
        msg["To"] = to
        msg.set_content(body)
        host = os.getenv("SMTP_HOST", "smtp.gmail.com")
        port = int(os.getenv("SMTP_PORT", "465"))
        with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
            smtp.login(user, password.replace(" ", ""))
            smtp.send_message(msg)
        log.info("Email sent to %s: %s", to, body)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("Email send failed: %s", exc)
        return False


# ---------- price checking ----------

def fetch_quote(symbol: str) -> Optional[dict]:
    """Latest daily bar vs the one before it. `session` is the trading day the price belongs to."""
    try:
        hist = yf.Ticker(symbol).history(period="7d", interval="1d")["Close"].dropna()
        if len(hist) < 2:
            raise ValueError("not enough price history")
        price, prev = float(hist.iloc[-1]), float(hist.iloc[-2])
        session = hist.index[-1].date()
        change_pct = (price - prev) / prev * 100
        return {"price": price, "prev_close": prev, "change_pct": change_pct, "session": session.isoformat()}
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to fetch %s: %s", symbol, exc)
        return None


def check_indexes() -> None:
    global last_check
    for symbol, name in INDEXES.items():
        q = fetch_quote(symbol)
        if q is None:
            continue
        q["name"] = name
        q["dip"] = q["change_pct"] <= -DIP_THRESHOLD_PCT
        # One alert per index per trading session (not per calendar day), so a Friday
        # drop isn't re-sent on Saturday or Monday morning before the market opens.
        q["alerted_today"] = alert_state.get(symbol) == q["session"]

        if q["dip"] and not q["alerted_today"]:
            msg = (
                f"📉 buyDips alert: {name} is down {abs(q['change_pct']):.2f}% "
                f"on {datetime.fromisoformat(q['session']).strftime('%a %d %b')} "
                f"({q['price']:,.2f} vs prev close {q['prev_close']:,.2f}). "
                f"Time to buy the dip?"
            )
            if notify(msg):
                alert_state[symbol] = q["session"]
                save_state(alert_state)
                q["alerted_today"] = True

        with lock:
            quotes[symbol] = q
    last_check = datetime.now()


def monitor_loop() -> None:
    while True:
        log.info("Checking %d indexes...", len(INDEXES))
        check_indexes()
        time.sleep(CHECK_INTERVAL_SECONDS)


# ---------- web ----------

app = Flask(__name__)


@app.route("/")
def index():
    with lock:
        rows = [dict(symbol=s, **q) for s, q in quotes.items()]
    return render_template(
        "index.html",
        rows=rows,
        last_check=last_check,
        threshold=DIP_THRESHOLD_PCT,
        interval=CHECK_INTERVAL_SECONDS,
    )


@app.route("/api/quotes")
def api_quotes():
    with lock:
        return jsonify(
            {"last_check": last_check.isoformat() if last_check else None, "quotes": quotes}
        )


@app.route("/check", methods=["POST"])
def check_now():
    check_indexes()
    return redirect(url_for("index"))


@app.route("/test-alert", methods=["POST"])
def test_alert():
    notify("✅ buyDips test message: alerts are working.")
    return redirect(url_for("index"))


if __name__ == "__main__":
    threading.Thread(target=monitor_loop, daemon=True).start()
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=False)
