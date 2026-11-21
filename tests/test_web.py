import re

import pytest

from buydips import db, links, web


@pytest.fixture()
def client(monkeypatch):
    sent = []
    monkeypatch.setattr(web, "send_email", lambda *a, **k: sent.append(a) or True)
    app = web.create_app()
    app.testing = True
    c = app.test_client()
    c.sent = sent
    return c


def csrf(client, path="/"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def sign_in(client, email="a@x.com"):
    token = csrf(client)
    client.post("/login", data={"email": email, "csrf": token})
    client.get(links.login_url(email).split("http://127.0.0.1:5000")[1])


def test_login_requires_csrf(client):
    assert client.post("/login", data={"email": "a@x.com"}).status_code == 400


def test_login_sends_link_and_rate_limits(client):
    t = csrf(client)
    client.post("/login", data={"email": "a@x.com", "csrf": t})
    client.post("/login", data={"email": "a@x.com", "csrf": t})
    assert len(client.sent) == 1
    assert "/auth/" in client.sent[0][2]


def test_invalid_email_rejected(client):
    r = client.post("/login", data={"email": "nope", "csrf": csrf(client)})
    assert r.status_code == 400


def test_bad_login_token_rejected(client):
    assert client.get("/auth/garbage").status_code == 400


def test_watchlist_requires_login(client):
    assert client.get("/watchlist").status_code == 302


def test_save_watchlist_validates(client):
    sign_in(client)
    t = csrf(client, "/watchlist")
    r = client.post("/watchlist", data={"csrf": t, "sym": ["^NSEI", "FAKE.NS", "RELIANCE.NS"],
                                        "th_^NSEI": "1", "th_RELIANCE.NS": "3.5"})
    assert r.status_code == 302
    uid = db.get_or_create_user("a@x.com")["id"]
    assert db.get_subscriptions(uid) == {"^NSEI": 1.0, "RELIANCE.NS": 3.5}  # unknown symbol dropped


def test_out_of_range_threshold_rejected(client):
    sign_in(client)
    t = csrf(client, "/watchlist")
    r = client.post("/watchlist", data={"csrf": t, "sym": ["^NSEI"], "th_^NSEI": "0.01"})
    assert r.status_code == 400


def test_unsubscribe_needs_post_and_pauses_alerts(client):
    u = db.get_or_create_user("a@x.com")
    url = links.unsubscribe_url(u["id"]).replace("http://127.0.0.1:5000", "")
    client.get(url)  # a link-scanner's GET must not unsubscribe anyone
    assert db.get_user(u["id"])["active"]
    client.post(url)
    assert not db.get_user(u["id"])["active"]


def test_delete_account_removes_everything(client):
    sign_in(client)
    uid = db.get_or_create_user("a@x.com")["id"]
    db.set_subscriptions(uid, {"^NSEI": 1.0})
    client.post("/delete-account", data={"csrf": csrf(client, "/watchlist")})
    assert db.get_user(uid) is None
    assert db.get_subscriptions(uid) == {}


def test_production_refuses_weak_secret_key(monkeypatch):
    from buydips import config
    monkeypatch.setattr(config, "BASE_URL", "https://example.com")
    for bad in ("", "short", "dev-only-change-me"):
        monkeypatch.setattr(config, "SECRET_KEY", bad)
        with pytest.raises(RuntimeError):
            web.create_app()


def test_login_reports_failure_when_email_cannot_be_sent(monkeypatch):
    monkeypatch.setattr(web, "send_email", lambda *a, **k: False)
    app = web.create_app(); app.testing = True
    c = app.test_client()
    r = c.post("/login", data={"email": "a@x.com", "csrf": csrf(c)})
    assert r.status_code == 503 and "on our side" in r.get_data(as_text=True)
    # a failed send must not start the 60-second cooldown
    assert db.seconds_since_login_link("a@x.com") is None


def test_pages_render_and_csp_forbids_inline_code(client):
    for path in ("/", "/privacy"):
        r = client.get(path)
        assert r.status_code == 200
        csp = r.headers["Content-Security-Policy"]
        assert "unsafe-inline" not in csp and "script-src 'self'" in csp
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/favicon.svg").status_code == 200


def test_unknown_page_gets_a_styled_404(client):
    r = client.get("/nope")
    assert r.status_code == 404 and "Page not found" in r.get_data(as_text=True)


def test_templates_have_no_inline_scripts_or_styles():
    import pathlib
    for f in pathlib.Path(web.__file__).parent.glob("templates/*.html"):
        html = f.read_text()
        assert " style=" not in html, f.name
        assert "<style" not in html, f.name
        assert " onsubmit=" not in html and " onclick=" not in html, f.name
        assert "<script>" not in html, f.name
