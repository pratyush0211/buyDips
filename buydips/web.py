"""The website: sign in with an emailed link, pick what to watch, unsubscribe."""

import logging
import re
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, abort, redirect, render_template, request, send_from_directory, session, url_for

from . import buckets as bucketmath, card, catalog, config, db, emails, links, markets
from .data import YahooProvider
from .mailer import send_email

log = logging.getLogger("buyDips.web")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)


def _background(fn, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True).start()


def _announce_new_user(email: str) -> None:
    """Tell the site owner someone new has signed in. Never lets a failure affect the new user."""
    if not config.ADMIN_EMAIL:
        return
    try:
        send_email(
            config.ADMIN_EMAIL,
            "buyDips: a new user just joined",
            f"{email} just signed in to buyDips for the first time.\n\n"
            f"People with an account: {db.user_count()}\n\n"
            "See everyone: the users table in Supabase, or run admin_users.py on your Mac.",
        )
    except Exception as exc:  # noqa: BLE001
        log.error("Could not send new-user notice: %s", exc)


def _presets() -> list:
    """Quick-pick buttons on the watchlist page: (label, 'SYM|SYM|...')."""
    india = [i["symbol"] for i in catalog.ITEMS if i["type"] == "index" and i["group"] == "India"]
    us = [i["symbol"] for i in catalog.ITEMS if i["type"] == "index" and i["group"] == "US"]
    return [("Indian indexes", "|".join(india)), ("US indexes", "|".join(us))]


_price_pool = ThreadPoolExecutor(2)


def _fetch_quote(symbol: str):
    """One live price for a newly added stock, or None if the provider is slow or down (the next sync fills it in)."""
    try:
        return _price_pool.submit(YahooProvider().fetch, [symbol]).result(timeout=6).get(symbol)
    except Exception:  # noqa: BLE001
        return None


def _starting_price(symbol: str):
    """The price to measure a new bucket stock from: a recent synced price, else a live look, else unknown."""
    cached = db.quotes_for([symbol]).get(symbol)
    if cached and datetime.utcnow() - cached["updated_at"] < timedelta(hours=6):
        return cached["price"]
    q = _fetch_quote(symbol)
    if q is None:
        return None
    db.save_quotes([q])
    return q.price


def _synced_text(quotes: dict) -> str:
    stamps = [q["updated_at"] for q in quotes.values()]
    if not stamps:
        return "Prices sync while markets are open"
    when = max(stamps).replace(tzinfo=timezone.utc).astimezone(markets.IST)
    return f"Prices synced {when.strftime('%a %d %b, ')}{markets._clock(when)} IST"


def _alert_groups(user_id: int) -> list:
    """The user's alert history as [{day, rows}], newest first, with times in Indian Standard Time."""
    now = datetime.now(timezone.utc).astimezone(markets.IST)
    groups = []
    for r in db.alerts_for_user(user_id):
        when = r["sent_at"].replace(tzinfo=timezone.utc).astimezone(markets.IST)
        gap = (now.date() - when.date()).days
        day = "Today" if gap == 0 else "Yesterday" if gap == 1 else when.strftime("%a %d %b")
        cur = catalog.currency_symbol(r["symbol"])
        row = {
            "name": catalog.name_of(r["symbol"]), "symbol": r["symbol"],
            "time": markets._clock(when) + " IST",
            "drop": None if r["change_pct"] is None else f"\u2212{abs(r['change_pct']):.2f}%",
            "detail": None if r["price"] is None else f"{cur}{r['price']:,.2f} vs {cur}{r['prev_close']:,.2f} close",
            "level": None if r["threshold_pct"] is None else f"{r['threshold_pct']:g}%",
        }
        if not groups or groups[-1]["day"] != day:
            groups.append({"day": day, "rows": []})
        groups[-1]["rows"].append(row)
    return groups


def create_app() -> Flask:
    local = config.BASE_URL.startswith("http://127.0.0.1")
    if not local and (config.SECRET_KEY == "dev-only-change-me" or len(config.SECRET_KEY) < 32):
        raise RuntimeError(
            "SECRET_KEY is missing or too short. Set it to the same long random value (32+ characters) "
            "used by the GitHub SECRET_KEY secret."
        )

    app = Flask(__name__)
    app.secret_key = config.SECRET_KEY
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=config.BASE_URL.startswith("https://"),
        MAX_CONTENT_LENGTH=64 * 1024,
    )
    db.init_db()

    # ---------- helpers ----------

    def csrf_token() -> str:
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return session["csrf"]

    def check_csrf() -> None:
        if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "x")):
            abort(400, "Your session expired. Go back, refresh the page and try again.")

    def current_user():
        uid = session.get("uid")
        return db.get_user(uid) if uid else None

    def safe_user():
        try:
            return current_user()
        except Exception:  # noqa: BLE001  (error pages must render even if the database is down)
            return None

    app.jinja_env.globals.update(
        csrf_token=csrf_token, year=datetime.utcnow().year, contact_email=config.CONTACT_EMAIL,
        base_url=config.BASE_URL
    )

    @app.after_request
    def security_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = CSP
        return resp

    def page(title: str, message: str, status: int = 200, icon: str = None):
        bad = status >= 400
        return render_template(
            "message.html", title=title, message=message, bad=bad,
            icon=icon or ("!" if bad else "✓"), user=safe_user(),
        ), status

    def error_page(code: int, title: str, message: str):
        return render_template("error.html", code=code, title=title, message=message, user=safe_user()), code

    @app.errorhandler(400)
    def bad_request(e):
        return error_page(400, "Something went wrong", getattr(e, "description", "") or "Please go back and try again.")

    @app.errorhandler(404)
    def not_found(e):
        return error_page(404, "Page not found", "That page doesn't exist, or the link is out of date.")

    @app.errorhandler(413)
    def too_large(e):
        return error_page(413, "Request too large", "That request was bigger than we accept.")

    @app.errorhandler(500)
    def server_error(e):
        return error_page(500, "Something broke on our side", "We've been notified in the logs. Please try again in a moment.")

    # ---------- routes ----------

    @app.route("/")
    def index():
        if safe_user():  # signed in: open straight on the app, like an iPhone app would
            return redirect(url_for("watchlist"))
        indexes = [i for i in catalog.ITEMS if i["type"] == "index"]
        examples = ["Nifty 50", "Sensex", "Nifty Bank", "S&P 500", "Nasdaq", "Reliance", "TCS", "HDFC Bank", "Infosys", "Apple", "NVIDIA", "Microsoft"]
        return render_template(
            "index.html", user=safe_user(), n_indexes=len(indexes),
            n_india=sum(i["type"] == "stock" and i["group"] == "India" for i in catalog.ITEMS),
            n_us=sum(i["type"] == "stock" and i["group"] == "US" for i in catalog.ITEMS), examples=examples,
        )

    @app.route("/privacy")
    def privacy():
        return render_template("privacy.html", user=safe_user())

    @app.route("/login", methods=["POST"])
    def login():
        check_csrf()
        email = db.normalize_email(request.form.get("email", ""))
        if not EMAIL_RE.match(email) or len(email) > 254:
            return page("Check your email address", "That doesn't look like a valid email address.", 400)

        # Same response whether or not the address is known, and at most one link per minute.
        wait = db.seconds_since_login_link(email)
        if wait is None or wait >= config.LOGIN_LINK_COOLDOWN_SECONDS:
            db.get_or_create_user(email)
            code = db.create_login_code(email, config.LOGIN_LINK_MINUTES)
            subject, text, html = emails.signin(links.login_url(code), config.LOGIN_LINK_MINUTES)
            sent = send_email(email, subject, text, html=html)
            if not sent:
                return page("Couldn't send the email",
                            "Something went wrong on our side while sending your sign-in link. "
                            "Please try again in a few minutes.", 503)
            db.mark_login_link_sent(email)
        return page("Check your inbox", f"If {email} is valid, a sign-in link is on its way. It expires in "
                                        f"{config.LOGIN_LINK_MINUTES} minutes.", icon="✉")

    @app.route("/signin/<code>")
    @app.route("/go/<code>")  # earlier short-link format; kept so links already emailed still work
    def signin(code):
        email = db.email_for_login_code(code)
        if not email:
            return page("Link expired", "That sign-in link is invalid or has expired. Request a new one.", 400)
        user = db.get_or_create_user(email)
        if db.mark_first_signin(user["id"]):
            _background(_announce_new_user, email)
        session.clear()
        session["uid"] = user["id"]
        return redirect(url_for("watchlist"))

    @app.route("/auth/<token>")
    def legacy_auth(token):  # long-format links from before 22 Nov 2026; they expire in 15 minutes anyway
        return page("Link expired", "That sign-in link is out of date. Request a new one.", 400)

    @app.route("/watchlist", methods=["GET", "POST"])
    def watchlist():
        user = current_user()
        if not user:
            return redirect(url_for("index"))

        if request.method == "POST":
            check_csrf()
            chosen, errors = {}, []
            for symbol in request.form.getlist("sym"):
                if symbol not in catalog.BY_SYMBOL:
                    continue
                raw = request.form.get(f"th_{symbol}", "")
                try:
                    pct = float(raw)
                except ValueError:
                    pct = catalog.default_threshold(symbol)
                if not catalog.MIN_THRESHOLD <= pct <= catalog.MAX_THRESHOLD:
                    errors.append(f"{catalog.name_of(symbol)}: choose between {catalog.MIN_THRESHOLD:g}% and "
                                  f"{catalog.MAX_THRESHOLD:g}%.")
                    continue
                chosen[symbol] = round(pct, 2)
            if len(chosen) > config.MAX_WATCHLIST_ITEMS:
                errors.append(f"You can watch up to {config.MAX_WATCHLIST_ITEMS} items.")
            if errors:
                return page("Couldn't save", " ".join(errors), 400)
            db.set_subscriptions(user["id"], chosen)
            return redirect(url_for("watchlist", saved=1))

        return render_template(
            "watchlist.html", user=user, tab="watchlist", sections=catalog.grouped(), totals=catalog.tab_totals(), subs=db.get_subscriptions(user["id"]),
            default_threshold=catalog.default_threshold, saved=bool(request.args.get("saved")),
            limit=config.MAX_WATCHLIST_ITEMS, active=user["active"], presets=_presets(),
        )

    # ---------- buckets ----------

    def _clean_name(raw: str) -> str:
        return " ".join(raw.split())[:40]

    def _owns(b: dict) -> bool:
        u = current_user()
        return bool(u and u["id"] == b["user_id"])

    def _bucket_or_404(user, bucket_id: int) -> dict:
        b = db.get_bucket(user["id"], bucket_id)
        if not b:
            abort(404)
        return b

    @app.route("/buckets", methods=["GET", "POST"])
    def buckets_tab():
        user = current_user()
        if not user:
            return redirect(url_for("index"))
        if request.method == "POST":
            check_csrf()
            name = _clean_name(request.form.get("name", ""))
            if not name:
                return page("Name your bucket", "Give the bucket a name, like \u201cBanks\u201d or \u201cUS tech\u201d.", 400)
            if db.count_buckets(user["id"]) >= config.MAX_BUCKETS:
                return page("Too many buckets", f"You can have up to {config.MAX_BUCKETS} buckets.", 400)
            return redirect(url_for("bucket_page", bucket_id=db.create_bucket(user["id"], name)))
        rows = db.list_buckets(user["id"])
        items = db.bucket_items_for([b["id"] for b in rows])
        quotes = db.quotes_for({i["symbol"] for v in items.values() for i in v})
        cards = [{**b, **bucketmath.view(items[b["id"]], quotes)} for b in rows]
        return render_template("buckets.html", user=user, tab="buckets", cards=cards,
                               flag_pct=f"{config.BUCKET_FLAG_PCT:g}", limit=config.MAX_BUCKETS)

    @app.route("/buckets/<int:bucket_id>")
    def bucket_page(bucket_id):
        user = current_user()
        if not user:
            return redirect(url_for("index"))
        b = _bucket_or_404(user, bucket_id)
        items = db.bucket_items_for([b["id"]])[b["id"]]
        quotes = db.quotes_for([i["symbol"] for i in items])
        q = " ".join(request.args.get("q", "").lower().split())[:40]
        have = {i["symbol"] for i in items}
        results = []
        if q:
            results = [i for i in catalog.ITEMS if i["type"] == "stock" and i["symbol"] not in have
                       and q in catalog.search_text(i)][:12]
        return render_template("bucket.html", user=user, tab="buckets", b=b, v=bucketmath.view(items, quotes),
                               synced=_synced_text(quotes), q=q, results=results, flag_pct=f"{config.BUCKET_FLAG_PCT:g}",
                               full=len(items) >= config.MAX_BUCKET_ITEMS, max_items=config.MAX_BUCKET_ITEMS,
                               changed=request.args.get("changed"),
                               share_url=f"{config.BASE_URL}/b/{b['share_token']}" if b.get("share_token") else None)

    @app.route("/buckets/<int:bucket_id>/add", methods=["POST"])
    def bucket_add(bucket_id):
        user = current_user()
        if not user:
            return redirect(url_for("index"))
        check_csrf()
        b = _bucket_or_404(user, bucket_id)
        symbol = request.form.get("symbol", "")
        item = catalog.BY_SYMBOL.get(symbol)
        if not item or item["type"] != "stock":
            return page("Couldn't add that", "That stock isn't available.", 400)
        if len(db.bucket_items_for([b["id"]])[b["id"]]) >= config.MAX_BUCKET_ITEMS:
            return page("Bucket is full", f"A bucket can hold up to {config.MAX_BUCKET_ITEMS} stocks.", 400)
        if symbol not in {i["symbol"] for i in db.bucket_items_for([b["id"]])[b["id"]]}:
            db.add_bucket_item(b["id"], symbol, _starting_price(symbol))
        return redirect(url_for("bucket_page", bucket_id=b["id"], changed="added"))

    @app.route("/buckets/<int:bucket_id>/remove", methods=["POST"])
    def bucket_remove(bucket_id):
        user = current_user()
        if not user:
            return redirect(url_for("index"))
        check_csrf()
        b = _bucket_or_404(user, bucket_id)
        db.remove_bucket_item(b["id"], request.form.get("symbol", ""))
        return redirect(url_for("bucket_page", bucket_id=b["id"], changed="removed"))

    @app.route("/buckets/<int:bucket_id>/rename", methods=["POST"])
    def bucket_rename(bucket_id):
        user = current_user()
        if not user:
            return redirect(url_for("index"))
        check_csrf()
        b = _bucket_or_404(user, bucket_id)
        name = _clean_name(request.form.get("name", ""))
        if not name:
            return page("Name your bucket", "A bucket needs a name.", 400)
        db.rename_bucket(b["id"], name)
        return redirect(url_for("bucket_page", bucket_id=b["id"], changed="renamed"))

    @app.route("/buckets/<int:bucket_id>/share", methods=["POST"])
    def bucket_share(bucket_id):
        user = current_user()
        if not user:
            return redirect(url_for("index"))
        check_csrf()
        b = _bucket_or_404(user, bucket_id)
        action = request.form.get("action", "")
        if action == "off":
            db.set_share(b["id"], None, False)
        elif action in ("on", "perf"):
            token = b.get("share_token") or secrets.token_urlsafe(9)
            perf = bool(b.get("share_perf")) if action == "on" else request.form.get("perf") == "1"
            db.set_share(b["id"], token, perf)
        else:
            abort(400)
        return redirect(url_for("bucket_page", bucket_id=b["id"], changed="shared" if action != "off" else "unshared"))

    def _shared_model(b: dict) -> dict:
        """Exactly what a shared bucket reveals: name, stocks and (only if the owner chose) the average."""
        items = db.bucket_items_for([b["id"]])[b["id"]]
        stocks = [{"name": catalog.name_of(i["symbol"]), "market": "IN" if markets.market_of(i["symbol"]) == "India" else "US"}
                  for i in items]
        m = {"name": b["name"], "n": len(stocks), "stocks": stocks}
        if b.get("share_perf"):
            v = bucketmath.view(items, db.quotes_for([i["symbol"] for i in items]))
            if v["n_priced"]:
                m.update(avg_text=v["average_text"], tone=v["tone"], n_priced=v["n_priced"])
        return m

    def _shared_or_404(token: str) -> dict:
        b = db.get_shared_bucket(token)
        if not b:
            abort(404)
        return b

    @app.route("/b/<token>")
    def shared_bucket(token):
        b = _shared_or_404(token)
        m = _shared_model(b)
        resp = app.make_response(render_template("shared.html", user=current_user(), tab="buckets" if _owns(b) else None, m=m, token=token, base=config.BASE_URL, mine=_owns(b)))
        resp.headers["X-Robots-Tag"] = "noindex"
        return resp

    @app.route("/b/<token>/<kind>.png")
    def shared_card(token, kind):
        if kind not in ("story", "link"):
            abort(404)
        png = card.render(_shared_model(_shared_or_404(token)), kind)
        resp = Response(png, mimetype="image/png")
        resp.headers["Cache-Control"] = "public, max-age=300"
        resp.headers["X-Robots-Tag"] = "noindex"
        if request.args.get("download"):
            resp.headers["Content-Disposition"] = 'attachment; filename="buydips-bucket.png"'
        return resp

    @app.route("/buckets/<int:bucket_id>/delete", methods=["POST"])
    def bucket_delete(bucket_id):
        user = current_user()
        if not user:
            return redirect(url_for("index"))
        check_csrf()
        db.delete_bucket(_bucket_or_404(user, bucket_id)["id"])
        return redirect(url_for("buckets_tab"))

    @app.route("/alerts")
    def alerts():
        user = current_user()
        if not user:
            return redirect(url_for("index"))
        return render_template("alerts.html", user=user, tab="alerts", groups=_alert_groups(user["id"]),
                               active=user["active"], n_watched=len(db.get_subscriptions(user["id"])))

    @app.route("/markets")
    def markets_tab():
        user = current_user()
        if not user:
            return redirect(url_for("index"))
        now = datetime.now(timezone.utc)
        cards = [{**markets.market_status(m, now), "holidays": markets.upcoming_holidays(m, now)}
                 for m in ("India", "US")]
        return render_template("markets.html", user=user, tab="markets", cards=cards)

    @app.route("/settings")
    def settings():
        user = current_user()
        if not user:
            return redirect(url_for("index"))
        return render_template("settings.html", user=user, tab="settings", active=user["active"],
                               n_watched=len(db.get_subscriptions(user["id"])),
                               changed=request.args.get("changed"))

    @app.route("/settings/alerts", methods=["POST"])
    def settings_alerts():
        user = current_user()
        if not user:
            return redirect(url_for("index"))
        check_csrf()
        on = request.form.get("on") == "1"
        db.set_active(user["id"], on)
        return redirect(url_for("settings", changed="on" if on else "off"))

    @app.route("/manifest.webmanifest")
    def manifest():
        resp = send_from_directory(app.static_folder, "manifest.webmanifest", mimetype="application/manifest+json")
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp

    @app.route("/unsubscribe/<token>", methods=["GET", "POST"])
    def unsubscribe(token):
        user_id = links.read_unsubscribe_token(token)
        if user_id is None or not db.get_user(user_id):
            return page("Invalid link", "This unsubscribe link isn't valid.", 400)
        if request.method == "POST":  # also used by mail clients' one-click unsubscribe
            db.unsubscribe_user(user_id)
            return page("Unsubscribed", "You won't get any more buyDips emails. You can sign in again any time "
                                        "to turn alerts back on.")
        return render_template("unsubscribe.html", token=token, user=None)

    @app.route("/delete-account", methods=["POST"])
    def delete_account():
        user = current_user()
        if not user:
            return redirect(url_for("index"))
        check_csrf()
        db.delete_user(user["id"])
        session.clear()
        return page("Account deleted", "Your email address and watchlist have been permanently deleted.")

    @app.route("/logout", methods=["POST"])
    def logout():
        check_csrf()
        session.clear()
        return redirect(url_for("index"))

    @app.route("/healthz")
    def healthz():
        return "ok"

    return app
