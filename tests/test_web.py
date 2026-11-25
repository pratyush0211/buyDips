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
    if path == "/watchlist":
        path = "/watchlist?start=india"   # a brand-new account sees the first-run screen at plain /watchlist
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def sign_in(client, email="a@x.com"):
    with client.session_transaction() as sess:  # start signed out: "/" sends signed-in visitors to the app
        sess.clear()
    token = csrf(client)
    client.post("/login", data={"email": email, "csrf": token})
    code = db.create_login_code(email, 15)
    client.post(f"/signin/{code}", data={"csrf": csrf(client, f"/signin/{code}")})


def test_login_requires_csrf(client):
    assert client.post("/login", data={"email": "a@x.com"}).status_code == 400


def test_login_sends_link_and_rate_limits(client):
    t = csrf(client)
    client.post("/login", data={"email": "a@x.com", "csrf": t})
    client.post("/login", data={"email": "a@x.com", "csrf": t})
    assert len(client.sent) == 1
    assert "/signin/" in client.sent[0][2]


def test_invalid_email_rejected(client):
    r = client.post("/login", data={"email": "nope", "csrf": csrf(client)})
    assert r.status_code == 400


def test_bad_login_code_rejected(client):
    assert client.get("/signin/garbage").status_code == 400
    assert client.get("/go/garbage").status_code == 400
    assert client.get("/auth/anything").status_code == 400  # old long-format links


def test_login_link_is_short_and_reveals_nothing(client):
    client.post("/login", data={"email": "secret.person@x.com", "csrf": csrf(client)})
    link = re.search(r"http://127\.0\.0\.1:5000/signin/(\S+)", client.sent[0][2]).group(0)
    assert len(link) < 50 and re.fullmatch(r"http://127\.0\.0\.1:5000/signin/[a-z2-7]{16}", link)
    assert "secret" not in link and "x.com" not in link


def test_login_code_expires_and_is_not_stored_in_clear(client):
    code = db.create_login_code("a@x.com", 15)
    assert db.email_for_login_code(code) == "a@x.com"
    from sqlalchemy import select
    with db.engine().connect() as c:
        stored = [r[0] for r in c.execute(select(db.login_codes.c.code_hash))]
    assert code not in stored
    expired = db.create_login_code("a@x.com", -1)
    assert db.email_for_login_code(expired) is None


def test_watchlist_requires_login(client):
    assert client.get("/watchlist?start=india").status_code == 302


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


def test_home_page_declares_its_canonical_address(client, monkeypatch):
    from buydips import config
    html = client.get("/").get_data(as_text=True)
    assert f'<link rel="canonical" href="{config.BASE_URL}/">' in html


def test_old_go_links_still_sign_you_in(client):
    code = db.create_login_code("a@x.com", 15)
    assert client.get(f"/go/{code}").status_code == 200                      # shows the Continue button
    assert client.post(f"/go/{code}", data={"csrf": csrf(client, f"/go/{code}")}).status_code == 302


def test_owner_is_told_once_when_someone_signs_in_for_the_first_time(client, monkeypatch):
    from buydips import config
    monkeypatch.setattr(config, "ADMIN_EMAIL", "owner@x.com")
    monkeypatch.setattr(web, "_background", lambda fn, *a: fn(*a))  # run inline, not in a thread
    client.sent.clear()
    for _ in range(2):  # second sign-in must not notify again
        sign_in(client, "newbie@x.com")
    notices = [m for m in client.sent if m[0] == "owner@x.com"]
    assert len(notices) == 1
    assert "newbie@x.com" in notices[0][2] and "signin" not in notices[0][2]  # says who, leaks no link


def test_no_notice_when_admin_email_not_set(client, monkeypatch):
    from buydips import config
    monkeypatch.setattr(config, "ADMIN_EMAIL", "")
    monkeypatch.setattr(web, "_background", lambda fn, *a: fn(*a))
    client.sent.clear()
    sign_in(client, "newbie@x.com")
    assert client.sent and all(m[1].endswith(" is your buyDips sign-in code") for m in client.sent)   # no admin notice among them


def test_failed_notice_never_breaks_sign_in(client, monkeypatch):
    from buydips import config
    monkeypatch.setattr(config, "ADMIN_EMAIL", "owner@x.com")
    monkeypatch.setattr(web, "_background", lambda fn, *a: fn(*a))
    def boom(to, *a, **k):
        if to == "owner@x.com":
            raise RuntimeError("mail down")
        return True
    monkeypatch.setattr(web, "send_email", boom)
    code = db.create_login_code("newbie@x.com", 15)
    assert client.post(f"/signin/{code}", data={"csrf": csrf(client, f"/signin/{code}")}).status_code == 302


def test_watchlist_page_has_tabs_for_each_market(client):
    sign_in(client, "tabs@x.com")
    html = client.get("/watchlist?start=india").get_data(as_text=True)
    for tab in ("indexes", "india", "us", "selected"):
        assert f'data-tab="{tab}"' in html
    assert 'class="group" data-tab="india"' in html and 'class="group" data-tab="us"' in html
    assert 'id="view-note"' in html and 'id="empty"' in html
    assert 'id="jump"' in html and 'class="navbar"' in html  # A-Z bar, filled in by watchlist.js


# ---------- the iOS-style app shell: two tabs (Watchlist, Buckets), Alerts and Settings as header icons ----------

PAGES = ["/watchlist", "/buckets", "/alerts", "/markets", "/settings"]
TABS = PAGES


def test_tab_pages_need_sign_in(client):
    for path in PAGES:
        r = client.get(path)
        assert r.status_code == 302 and r.headers["Location"].endswith("/"), path


def _bar(html):
    return html.split('<nav class="tabbar"')[1].split("</nav>")[0]


def test_the_bar_has_two_tabs_and_alerts_and_settings_are_header_icons(client):
    sign_in(client)
    selected = {"/watchlist": "/watchlist", "/markets": "/watchlist", "/buckets": "/buckets"}  # Markets is a sub-page of Watchlist
    for path in PAGES:
        html = client.get(path).get_data(as_text=True)
        assert 'class="app"' in html and 'class="tabbar"' in html, path
        bar = _bar(html)
        assert bar.count("<a ") == 2 and 'href="/watchlist"' in bar and 'href="/buckets"' in bar, path
        assert 'href="/alerts"' not in bar and 'href="/settings"' not in bar and 'href="/markets"' not in bar
        header = html.split("</header>")[0]
        assert 'class="iconbtn"' in header and 'href="/alerts"' in header and 'href="/settings"' in header, path
        if path in selected:
            assert 'aria-current="page"' in bar and f'href="{selected[path]}"' in bar.split('aria-current="page"')[0].rsplit("<a", 1)[1] + '"', path
        else:
            assert 'aria-current="page"' not in bar, path          # Alerts/Settings: the header icon is selected, not a tab
            assert html.count('aria-current="page"') == 1, path


def test_watchlist_shows_market_status_that_leads_to_hours_and_holidays(client):
    sign_in(client)
    html = client.get("/watchlist?start=india").get_data(as_text=True)
    assert 'class="mstrip"' in html and 'href="/markets"' in html and "India" in html and "US" in html
    assert 'class="backlink" href="/watchlist"' in client.get("/markets").get_data(as_text=True)


def test_signed_out_pages_have_no_tab_bar_but_every_signed_in_page_does(client):
    for path in ("/", "/privacy"):
        assert 'class="tabbar"' not in client.get(path).get_data(as_text=True), path
    sign_in(client)
    html = client.get("/privacy").get_data(as_text=True)
    assert 'class="tabbar"' in html and 'class="app"' in html
    assert 'aria-current="page"' not in html  # no tab is selected on a page that isn't one of them
    assert "Sign out</button>" not in html.split("</header>")[0]  # the old header with its Sign out button is gone


def test_signed_in_visitors_open_straight_on_the_watchlist(client):
    assert client.get("/").status_code == 200
    sign_in(client)
    r = client.get("/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/watchlist")


def test_alerts_tab_empty_then_lists_history_newest_first(client):
    sign_in(client)
    html = client.get("/alerts").get_data(as_text=True)
    assert "No alerts yet" in html and "Choose what to watch" in html
    uid = db.get_or_create_user("a@x.com")["id"]
    db.record_alerts(uid, [("^NSEI", "2026-11-20", {"change_pct": -2.5, "price": 24100.0, "prev_close": 24720.0,
                                                    "threshold_pct": 1.0}),
                           ("RELIANCE.NS", "2026-11-20")])  # an older row with no details
    html = client.get("/alerts").get_data(as_text=True)
    assert "No alerts yet" not in html and "Nifty 50" in html and "Reliance" in html
    assert "−2.50%" in html and "level 1%" in html and "Alerted" in html  # the old row degrades gracefully


def test_alerts_history_is_private_to_each_user(client):
    sign_in(client, "a@x.com")
    other = db.get_or_create_user("b@x.com")["id"]
    db.record_alerts(other, [("^GSPC", "2026-11-20", {"change_pct": -4.0, "price": 1.0, "prev_close": 2.0,
                                                      "threshold_pct": 1.0})])
    assert "S&amp;P 500" not in client.get("/alerts").get_data(as_text=True)


def test_markets_tab_describes_both_markets_without_prices(client):
    sign_in(client)
    html = client.get("/markets").get_data(as_text=True)
    assert "India (NSE)" in html and "United States" in html and "Upcoming holidays" in html
    assert ">Open<" in html or ">Closed<" in html
    assert "Local time" in html and "Hours" in html and "$" not in html.split("<main>")[1].split("</main>")[0]


def test_settings_toggle_pauses_and_resumes_alerts(client):
    sign_in(client)
    uid = db.get_or_create_user("a@x.com")["id"]
    assert db.get_user(uid)["active"]
    r = client.post("/settings/alerts", data={"csrf": csrf(client, "/settings")})  # switch off: no "on" field
    assert r.status_code == 302 and not db.get_user(uid)["active"]
    assert "Alerts are paused" in client.get("/settings?changed=off").get_data(as_text=True)
    client.post("/settings/alerts", data={"csrf": csrf(client, "/settings"), "on": "1"})
    assert db.get_user(uid)["active"]


def test_settings_actions_require_csrf(client):
    sign_in(client)
    assert client.post("/settings/alerts", data={"on": "1"}).status_code == 400


def test_manifest_and_icons_are_served(client):
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200 and r.mimetype == "application/manifest+json"
    data = r.get_json()
    assert data["display"] == "standalone" and data["start_url"] == "/watchlist"
    for icon in data["icons"] + [{"src": "/static/icons/apple-touch-icon.png"}]:
        assert client.get(icon["src"]).status_code == 200, icon["src"]
    assert 'rel="manifest"' in client.get("/").get_data(as_text=True)


# ---------- buckets ----------

from buydips.data import Quote  # noqa: E402


def new_bucket(client, name="Banks"):
    r = client.post("/buckets", data={"csrf": csrf(client, "/buckets"), "name": name})
    assert r.status_code == 302
    return int(r.headers["Location"].rsplit("/", 1)[1])


def test_create_bucket_and_see_it_on_the_list(client):
    sign_in(client)
    assert "No buckets yet" in client.get("/buckets").get_data(as_text=True)
    bid = new_bucket(client, "  US   tech ")
    html = client.get("/buckets").get_data(as_text=True)
    assert "US tech" in html and f"/buckets/{bid}" in html


def test_bucket_name_rules_and_limit(client, monkeypatch):
    from buydips import config
    sign_in(client)
    assert client.post("/buckets", data={"csrf": csrf(client, "/buckets"), "name": "   "}).status_code == 400
    assert client.post("/buckets", data={"name": "x"}).status_code == 400  # no csrf
    monkeypatch.setattr(config, "MAX_BUCKETS", 2)
    new_bucket(client, "One"); new_bucket(client, "Two")
    assert client.post("/buckets", data={"csrf": csrf(client, "/buckets"), "name": "Three"}).status_code == 400


def test_adding_a_stock_records_its_starting_price_and_shows_performance(client, monkeypatch):
    sign_in(client)
    monkeypatch.setattr(web, "_fetch_quotes", lambda syms: {sym: Quote(sym, 100.0, 99.0, 1.0, "2026-11-20") for sym in syms})
    bid = new_bucket(client)
    t = csrf(client, f"/buckets/{bid}")
    assert client.post(f"/buckets/{bid}/add", data={"csrf": t, "symbol": "HDFCBANK.NS"}).status_code == 302
    assert client.post(f"/buckets/{bid}/add", data={"csrf": t, "symbol": "HDFCBANK.NS"}).status_code == 302  # twice is harmless
    item = db.bucket_items_for([bid])[bid]
    assert len(item) == 1 and item[0]["added_price"] == 100.0
    db.save_quotes([Quote("HDFCBANK.NS", 90.0, 91.0, -1.0, "2026-11-20")])   # a later sync: down 10%
    html = client.get(f"/buckets/{bid}").get_data(as_text=True)
    assert "−10.00%" in html and "Down 10%+" in html and "HDFC Bank" in html
    assert "−10.00%" in client.get("/buckets").get_data(as_text=True) and "1 flagged" in client.get("/buckets").get_data(as_text=True)


def test_adding_when_the_price_source_is_down_still_works_and_waits(client, monkeypatch):
    sign_in(client)
    monkeypatch.setattr(web, "_fetch_quotes", lambda syms: {})
    bid = new_bucket(client)
    client.post(f"/buckets/{bid}/add", data={"csrf": csrf(client, f"/buckets/{bid}"), "symbol": "TCS.NS"})
    assert db.bucket_items_for([bid])[bid][0]["added_price"] is None
    assert "waiting for a price" in client.get(f"/buckets/{bid}").get_data(as_text=True)


def test_average_is_equal_weighted(client):
    sign_in(client)
    bid = new_bucket(client)
    db.add_bucket_item(bid, "TCS.NS", 100.0); db.add_bucket_item(bid, "INFY.NS", 100.0); db.add_bucket_item(bid, "WIPRO.NS", None)
    db.save_quotes([Quote("TCS.NS", 120.0, 1, 0, "d"), Quote("INFY.NS", 100.0, 1, 0, "d")])  # +20% and 0%; Wipro has no price yet
    html = client.get(f"/buckets/{bid}").get_data(as_text=True)
    assert "+10.00%" in html and "1 still waiting for a price" in html


def test_only_real_stocks_can_be_added_and_bucket_size_is_limited(client, monkeypatch):
    from buydips import config
    sign_in(client)
    monkeypatch.setattr(web, "_fetch_quotes", lambda syms: {})
    bid = new_bucket(client)
    t = csrf(client, f"/buckets/{bid}")
    assert client.post(f"/buckets/{bid}/add", data={"csrf": t, "symbol": "^NSEI"}).status_code == 400   # an index, not a stock
    assert client.post(f"/buckets/{bid}/add", data={"csrf": t, "symbol": "NOPE"}).status_code == 400
    monkeypatch.setattr(config, "MAX_BUCKET_ITEMS", 1)
    client.post(f"/buckets/{bid}/add", data={"csrf": t, "symbol": "TCS.NS"})
    assert client.post(f"/buckets/{bid}/add", data={"csrf": t, "symbol": "INFY.NS"}).status_code == 400


def test_search_finds_stocks_and_hides_ones_already_in_the_bucket(client):
    sign_in(client)
    bid = new_bucket(client)
    db.add_bucket_item(bid, "RELIANCE.NS", 100.0)
    html = client.get(f"/buckets/{bid}?q=apple").get_data(as_text=True)
    assert "AAPL" in html
    page = client.get(f"/buckets/{bid}?q=reliance").get_data(as_text=True)
    assert 'data-have="RELIANCE.NS"' in page            # tells the browser not to offer it again
    assert "RELIANCE.NS" not in page.split("Add a stock")[1].replace('data-have="RELIANCE.NS"', "")


def test_rename_remove_and_delete(client):
    sign_in(client)
    bid = new_bucket(client)
    db.add_bucket_item(bid, "TCS.NS", 100.0)
    t = csrf(client, f"/buckets/{bid}")
    client.post(f"/buckets/{bid}/rename", data={"csrf": t, "name": "Renamed"})
    assert "Renamed" in client.get(f"/buckets/{bid}").get_data(as_text=True)
    client.post(f"/buckets/{bid}/remove", data={"csrf": t, "symbol": "TCS.NS"})
    assert db.bucket_items_for([bid])[bid] == []
    assert client.post(f"/buckets/{bid}/delete", data={"csrf": t}).status_code == 302
    assert client.get(f"/buckets/{bid}").status_code == 404


def test_buckets_are_private_to_their_owner(client):
    sign_in(client, "a@x.com")
    bid = new_bucket(client, "Mine")
    t = csrf(client, "/buckets")
    sign_in(client, "b@x.com")
    assert client.get(f"/buckets/{bid}").status_code == 404
    t2 = csrf(client, "/buckets")
    for path, data in [("add", {"symbol": "TCS.NS"}), ("remove", {"symbol": "TCS.NS"}), ("rename", {"name": "x"}), ("delete", {})]:
        assert client.post(f"/buckets/{bid}/{path}", data={"csrf": t2, **data}).status_code == 404, path
    assert "Mine" not in client.get("/buckets").get_data(as_text=True)
    assert db.get_bucket(db.get_or_create_user("a@x.com")["id"], bid) is not None  # untouched


def test_bucket_pages_need_sign_in_and_deleting_the_account_removes_buckets(client):
    assert client.get("/buckets").status_code == 302 and client.get("/buckets/1").status_code == 302
    sign_in(client)
    bid = new_bucket(client)
    db.add_bucket_item(bid, "TCS.NS", 100.0)
    uid = db.get_or_create_user("a@x.com")["id"]
    client.post("/delete-account", data={"csrf": csrf(client, "/settings")})
    assert db.list_buckets(uid) == [] and db.bucket_items_for([bid])[bid] == []


# ---------- sharing ----------

def share_on(client, bid, action="on", **extra):
    return client.post(f"/buckets/{bid}/share", data={"csrf": csrf(client, f"/buckets/{bid}"), "action": action, **extra})


def test_shared_bucket_is_private_until_shared_and_shows_no_prices_by_default(client):
    sign_in(client)
    bid = new_bucket(client, "Secret picks")
    db.add_bucket_item(bid, "TCS.NS", 3500.0)
    db.save_quotes([Quote("TCS.NS", 3150.0, 1, 0, "d")])
    assert "Create share link" in client.get(f"/buckets/{bid}").get_data(as_text=True)
    assert share_on(client, bid).status_code == 302
    token = db.get_bucket(db.get_or_create_user("a@x.com")["id"], bid)["share_token"]
    assert token and len(token) >= 12
    anon = client.application.test_client()
    page_html = anon.get(f"/b/{token}").get_data(as_text=True)
    assert "Secret picks" in page_html and "Tata Consultancy Services" in page_html
    for leak in ("3500", "3150", "10.00%", "a@x.com", "Average since added"):
        assert leak not in page_html
    assert f"/b/{token}/link.png" in page_html and "noindex" in page_html
    assert anon.get(f"/b/{token}/story.png").data[:4] == b"\x89PNG"
    assert anon.get(f"/b/{token}/link.png").data[:4] == b"\x89PNG"
    assert anon.get(f"/b/{token}/other.png").status_code == 404
    assert anon.get("/b/not-a-real-token").status_code == 404


def test_owner_can_include_average_and_stop_sharing(client):
    sign_in(client)
    bid = new_bucket(client)
    db.add_bucket_item(bid, "TCS.NS", 100.0)
    db.save_quotes([Quote("TCS.NS", 110.0, 1, 0, "d")])
    share_on(client, bid)
    token = db.get_bucket(db.get_or_create_user("a@x.com")["id"], bid)["share_token"]
    share_on(client, bid, "perf", perf="1")
    anon = client.application.test_client()
    html = anon.get(f"/b/{token}").get_data(as_text=True)
    assert "+10.00%" in html and "100" not in html.replace("10.00", "")
    share_on(client, bid, "perf")  # unchecked box sends nothing
    assert "+10.00%" not in anon.get(f"/b/{token}").get_data(as_text=True)
    share_on(client, bid, "off")
    assert anon.get(f"/b/{token}").status_code == 404 and anon.get(f"/b/{token}/story.png").status_code == 404
    share_on(client, bid)  # turning it on again makes a fresh link
    assert db.get_bucket(db.get_or_create_user("a@x.com")["id"], bid)["share_token"] != token


def test_only_the_owner_can_change_sharing_and_deleting_ends_it(client):
    sign_in(client, "a@x.com")
    bid = new_bucket(client)
    db.add_bucket_item(bid, "TCS.NS", 100.0)
    share_on(client, bid)
    token = db.get_bucket(db.get_or_create_user("a@x.com")["id"], bid)["share_token"]
    sign_in(client, "b@x.com")
    assert client.post(f"/buckets/{bid}/share", data={"csrf": csrf(client, "/buckets"), "action": "off"}).status_code == 404
    assert client.application.test_client().get(f"/b/{token}").status_code == 200
    sign_in(client, "a@x.com")
    assert client.post(f"/buckets/{bid}/share", data={"action": "off"}).status_code == 400  # no csrf
    assert share_on(client, bid, "bogus").status_code == 400
    client.post(f"/buckets/{bid}/delete", data={"csrf": csrf(client, f"/buckets/{bid}")})
    assert client.application.test_client().get(f"/b/{token}").status_code == 404


def test_cards_cope_with_long_names_and_many_stocks():
    from buydips import card
    stocks = [{"name": "Tata Consultancy Services " * 3, "market": "IN"}] * 30
    for kind in ("story", "link"):
        png = card.render({"name": "x" * 40, "n": 30, "stocks": stocks, "avg_text": "−11.40%", "tone": "down"}, kind)
        assert png[:4] == b"\x89PNG"


def test_wording_matches_how_sign_in_and_performance_now_work(client):
    html = client.get("/").get_data(as_text=True)
    assert "6-digit code" in html and "How do I sign in?" in html and "What is the daily AI brief?" in html
    priv = client.get("/privacy").get_data(as_text=True)
    assert "sign-in codes" in priv and "How your list has done" in priv


def test_watchlist_header_has_an_ai_icon_and_import_and_no_clutter(client):
    sign_in(client)
    html = client.get("/watchlist?start=india").get_data(as_text=True)
    assert 'class="pill pill-ai" href="/performance"' in html and "Your daily AI brief" in html
    assert 'class="pill" href="/import"' in html
    assert "Import from your broker" not in html and "Indian indexes" not in html and "US indexes" not in html
    assert 'id="clearall"' in html and html.count("Clear all") == 1
