"""buyDips: watches major stock indexes and sends a WhatsApp message when one dips."""

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
logging.getLogger("twilio").setLevel(logging.WARNING)

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
    """Send to every configured channel. True if at least one succeeded."""
    sent_whatsapp = send_whatsapp(body)
    sent_email = send_email(body)
    return sent_whatsapp or sent_email


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


# ---------- WhatsApp ----------

def send_whatsapp(body: str) -> bool:
    provider = os.getenv("WHATSAPP_PROVIDER", "callmebot").lower()
    if provider == "meta":
        return send_via_meta(body)
    if provider == "callmebot":
        return send_via_callmebot(body)
    return send_via_twilio(body)


def send_via_meta(body: str) -> bool:
    """WhatsApp Cloud API (Meta). Uses an approved template if META_TEMPLATE_NAME is set,
    otherwise free-form text (which only works within 24h of you messaging the number)."""
    token = os.getenv("META_ACCESS_TOKEN")
    phone_id = os.getenv("META_PHONE_NUMBER_ID")
    to = (os.getenv("META_TO") or "").lstrip("+").replace(" ", "")
    template = os.getenv("META_TEMPLATE_NAME")
    lang = os.getenv("META_TEMPLATE_LANG", "en")
    version = os.getenv("META_API_VERSION", "v23.0")
    if not token or not phone_id or not to or token == "your_access_token":
        log.warning("Meta WhatsApp not configured; would have sent: %s", body)
        return False

    payload = {"messaging_product": "whatsapp", "to": to}
    if template:
        tpl = {"name": template, "language": {"code": lang}}
        if template != "hello_world":  # hello_world has no variables
            # Template parameters may not contain newlines or tabs.
            text = " ".join(body.split())
            tpl["components"] = [{"type": "body", "parameters": [{"type": "text", "text": text}]}]
        payload.update(type="template", template=tpl)
    else:
        payload.update(type="text", text={"body": body})

    try:
        import requests

        r = requests.post(
            f"https://graph.facebook.com/{version}/{phone_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=30,
        )
        if r.ok:
            log.info("WhatsApp sent via Meta: %s", body)
            return True
        log.error("Meta send failed (%s): %s", r.status_code, r.text[:500])
    except Exception as exc:  # noqa: BLE001
        log.error("Meta send failed: %s", exc)
    return False


def send_via_callmebot(body: str) -> bool:
    phone = os.getenv("CALLMEBOT_PHONE")
    apikey = os.getenv("CALLMEBOT_APIKEY")
    if not phone or not apikey or apikey == "your_api_key":
        log.warning("CallMeBot not configured; would have sent: %s", body)
        return False
    try:
        import requests

        r = requests.get(
            "https://api.callmebot.com/whatsapp.php",
            params={"phone": phone, "text": body, "apikey": apikey},
            timeout=30,
        )
        text = r.text.lower()
        if r.ok and "error" not in text and "invalid" not in text:
            log.info("WhatsApp sent via CallMeBot: %s", body)
            return True
        log.error("CallMeBot send failed (%s): %s", r.status_code, r.text[:300])
    except Exception as exc:  # noqa: BLE001
        log.error("CallMeBot send failed: %s", exc)
    return False


def send_via_twilio(body: str) -> bool:
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    sender = os.getenv("TWILIO_WHATSAPP_FROM")
    to = os.getenv("WHATSAPP_TO")
    if not all([sid, token, sender, to]) or "xxxx" in sid or to.endswith("+10000000000"):
        log.warning("Twilio not configured; would have sent: %s", body)
        return False
    try:
        from twilio.rest import Client

        Client(sid, token).messages.create(body=body, from_=sender, to=to)
        log.info("WhatsApp sent: %s", body)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("WhatsApp send failed: %s", exc)
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
