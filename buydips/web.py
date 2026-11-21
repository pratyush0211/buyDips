"""The website: sign in with an emailed link, pick what to watch, unsubscribe."""

import logging
import re
import secrets

from flask import Flask, abort, redirect, render_template, request, session, url_for

from . import catalog, config, db, links
from .mailer import send_email

log = logging.getLogger("buyDips.web")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")


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

    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.after_request
    def security_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"
        )
        return resp

    def page(title: str, message: str, status: int = 200):
        return render_template("message.html", title=title, message=message, user=current_user()), status

    # ---------- routes ----------

    @app.route("/")
    def index():
        return render_template("index.html", user=current_user(), disclaimer=config.DISCLAIMER)

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
            sent = send_email(
                email,
                "Your buyDips sign-in link",
                f"Click to sign in to buyDips (valid for {config.LOGIN_LINK_MINUTES} minutes):\n\n"
                f"{links.login_url(email)}\n\n"
                "If you didn't ask for this, ignore this email.",
            )
            if not sent:
                return page("Couldn't send the email",
                            "Something went wrong on our side while sending your sign-in link. "
                            "Please try again in a few minutes.", 503)
            db.mark_login_link_sent(email)
        return page("Check your inbox", f"If {email} is valid, a sign-in link is on its way. It expires in "
                                        f"{config.LOGIN_LINK_MINUTES} minutes.")

    @app.route("/auth/<token>")
    def auth(token):
        email = links.read_login_token(token)
        if not email:
            return page("Link expired", "That sign-in link is invalid or has expired. Request a new one.", 400)
        user = db.get_or_create_user(email)
        session.clear()
        session["uid"] = user["id"]
        return redirect(url_for("watchlist"))

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
                                  f"{catalog.MAX_THRESHOLD:g}%")
                    continue
                chosen[symbol] = round(pct, 2)
            if len(chosen) > config.MAX_WATCHLIST_ITEMS:
                errors.append(f"You can watch up to {config.MAX_WATCHLIST_ITEMS} items.")
            if errors:
                return page("Couldn't save", " ".join(errors), 400)
            db.set_subscriptions(user["id"], chosen)
            return redirect(url_for("watchlist", saved=1))

        return render_template(
            "watchlist.html", user=user, sections=catalog.grouped(), subs=db.get_subscriptions(user["id"]),
            default_threshold=catalog.default_threshold, saved=bool(request.args.get("saved")),
            limit=config.MAX_WATCHLIST_ITEMS, active=user["active"],
        )

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
