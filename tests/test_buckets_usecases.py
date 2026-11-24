"""Buckets and sharing, end to end: limits, races, empty states, hostile input, query budgets, whole user journeys
and the alert job's behaviour across several users. Written to break things, not to confirm the happy path."""

import contextlib
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import OperationalError

from buydips import card, catalog, config, db, web
from buydips.checker import run_check
from buydips.data import Quote
from tests.test_checker import FakeProvider, IN_OPEN, Outbox, US_OPEN, bq
from tests.test_web import client, csrf, new_bucket, share_on, sign_in  # noqa: F401  (client is a fixture)

STOCKS = [i["symbol"] for i in catalog.ITEMS if i["type"] == "stock"]
IN_STOCKS = [s for s in STOCKS if s.endswith(".NS")]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Adding a stock normally takes one live price; tests answer it locally."""
    monkeypatch.setattr(web, "_fetch_quote", lambda sym: Quote(sym, 100.0, 99.0, 1.0, "2026-11-20"))


def add(client, bid, symbol):
    return client.post(f"/buckets/{bid}/add", data={"csrf": csrf(client, f"/buckets/{bid}"), "symbol": symbol})


@contextlib.contextmanager
def count_queries():
    seen = []
    def hook(conn, cursor, statement, params, context, executemany):
        seen.append(statement)
    event.listen(db.engine(), "before_cursor_execute", hook)
    try:
        yield seen
    finally:
        event.remove(db.engine(), "before_cursor_execute", hook)


# ---------------- the limits ----------------

def test_the_documented_limits():
    assert config.MAX_BUCKET_ITEMS == 25 and config.MAX_BUCKETS == 10


def test_a_bucket_holds_exactly_25_and_the_26th_is_refused(client):
    sign_in(client)
    bid = new_bucket(client)
    for sym in IN_STOCKS[:25]:
        assert add(client, bid, sym).status_code == 302
    assert add(client, bid, IN_STOCKS[25]).status_code == 400
    assert len(db.bucket_items_for([bid])[bid]) == 25
    html = client.get(f"/buckets/{bid}").get_data(as_text=True)
    assert "25 of 25" in html and "is full" in html
    client.post(f"/buckets/{bid}/remove", data={"csrf": csrf(client, f"/buckets/{bid}"), "symbol": IN_STOCKS[0]})
    assert add(client, bid, IN_STOCKS[25]).status_code == 302  # room again
    assert len(db.bucket_items_for([bid])[bid]) == 25


def test_adding_a_stock_already_in_the_bucket_when_full_is_harmless(client):
    sign_in(client)
    bid = new_bucket(client)
    for sym in IN_STOCKS[:25]:
        add(client, bid, sym)
    assert add(client, bid, IN_STOCKS[0]).status_code == 302  # already there: not an error


def test_db_level_outcomes():
    u = db.get_or_create_user("a@x.com")
    bid = db.create_bucket(u["id"], "B")
    assert db.add_bucket_item(bid, "TCS.NS", 1.0, limit=2) == "added"
    assert db.add_bucket_item(bid, "TCS.NS", 1.0, limit=2) == "exists"
    assert db.add_bucket_item(bid, "INFY.NS", 1.0, limit=2) == "added"
    assert db.add_bucket_item(bid, "WIPRO.NS", 1.0, limit=2) == "full"
    assert db.add_bucket_item(bid, "TCS.NS", 1.0, limit=2) == "exists"  # "exists" wins over "full"


def _retrying(fn, tries=60):
    """SQLite (used in tests) refuses overlapping writers outright; Postgres, in production, queues them on a row lock.
    Either way the limit must hold, so retry on a busy database and let the limit logic decide."""
    for _ in range(tries):
        try:
            return fn()
        except OperationalError:
            continue
    raise AssertionError("database stayed busy")


def test_simultaneous_adds_never_exceed_the_limit():
    u = db.get_or_create_user("a@x.com")
    bid = db.create_bucket(u["id"], "B")
    outcomes, lock = [], threading.Lock()
    def worker(sym):
        r = _retrying(lambda: db.add_bucket_item(bid, sym, 1.0, limit=25))
        with lock:
            outcomes.append(r)
    threads = [threading.Thread(target=worker, args=(s,)) for s in IN_STOCKS[:40]]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert len(db.bucket_items_for([bid])[bid]) == 25
    assert outcomes.count("added") == 25 and outcomes.count("full") == 15


def test_simultaneous_adds_of_the_same_stock_add_it_once():
    u = db.get_or_create_user("a@x.com")
    bid = db.create_bucket(u["id"], "B")
    outcomes, lock = [], threading.Lock()
    def worker():
        r = _retrying(lambda: db.add_bucket_item(bid, "TCS.NS", 1.0, limit=25))
        with lock:
            outcomes.append(r)
    threads = [threading.Thread(target=worker) for _ in range(12)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert outcomes.count("added") == 1 and outcomes.count("exists") == 11


def test_bucket_count_limit_holds_under_races_and_per_user():
    a, b = db.get_or_create_user("a@x.com"), db.get_or_create_user("b@x.com")
    results, lock = [], threading.Lock()
    def worker(uid, i):
        r = _retrying(lambda: db.create_bucket(uid, f"B{i}", limit=10))
        with lock:
            results.append((uid, r))
    threads = [threading.Thread(target=worker, args=(a["id"], i)) for i in range(16)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert db.count_buckets(a["id"]) == 10 and db.count_buckets(b["id"]) == 0
    assert db.create_bucket(b["id"], "mine", limit=10) is not None  # one user's limit doesn't touch another's


# ---------------- empty bucket: the call to action ----------------

def test_empty_bucket_leads_with_one_clear_next_step(client):
    sign_in(client)
    bid = new_bucket(client, "Banks")
    html = client.get(f"/buckets/{bid}").get_data(as_text=True)
    assert "Add your first stock" in html and f'action="/buckets/{bid}/add"' in html
    assert "Some large, widely held stocks" in html and "Reliance" in html and "Apple" in html
    assert 'name="q"' in html                                # search is right there
    for absent in ("Average since added", "Create share link", "Share</h2>", "Waiting for the first prices"):
        assert absent not in html
    assert "Rename" in html and "Delete bucket" in html       # managing it still works
    assert "Empty. Tap to add your first stock" in client.get("/buckets").get_data(as_text=True)


def test_empty_bucket_search_and_one_tap_add(client):
    sign_in(client)
    bid = new_bucket(client)
    html = client.get(f"/buckets/{bid}?q=apple").get_data(as_text=True)
    assert "AAPL" in html and "Some large, widely held stocks" not in html     # results replace the suggestions
    assert add(client, bid, "AAPL").status_code == 302
    page = client.get(f"/buckets/{bid}").get_data(as_text=True)
    assert "Add your first stock" not in page and "Average since added" in page and "1 of 25" in page


def test_cannot_share_an_empty_bucket_and_emptying_a_shared_one_hides_it(client):
    sign_in(client)
    bid = new_bucket(client)
    assert share_on(client, bid).status_code == 400
    assert db.get_bucket(db.get_or_create_user("a@x.com")["id"], bid)["share_token"] is None
    add(client, bid, "TCS.NS"); share_on(client, bid)
    token = db.get_bucket(db.get_or_create_user("a@x.com")["id"], bid)["share_token"]
    anon = client.application.test_client()
    assert anon.get(f"/b/{token}").status_code == 200
    client.post(f"/buckets/{bid}/remove", data={"csrf": csrf(client, f"/buckets/{bid}"), "symbol": "TCS.NS"})
    assert anon.get(f"/b/{token}").status_code == 404 and anon.get(f"/b/{token}/story.png").status_code == 404
    add(client, bid, "INFY.NS")
    assert anon.get(f"/b/{token}").status_code == 200          # the same link works again


# ---------------- hostile and odd input ----------------

EVIL = '<script>alert(1)</script>"><img src=x onerror=alert(2)>'


def test_names_are_escaped_everywhere(client):
    sign_in(client)
    bid = new_bucket(client, EVIL)
    add(client, bid, "TCS.NS"); share_on(client, bid)
    token = db.get_bucket(db.get_or_create_user("a@x.com")["id"], bid)["share_token"]
    pages = [client.get("/buckets").get_data(as_text=True), client.get(f"/buckets/{bid}").get_data(as_text=True),
             client.application.test_client().get(f"/b/{token}").get_data(as_text=True)]
    for html in pages:
        assert "<script>alert" not in html and "<img src=x" not in html
    assert client.application.test_client().get(f"/b/{token}/story.png").data[:4] == b"\x89PNG"


@pytest.mark.parametrize("symbol", ["", "tcs.ns", " TCS.NS", "TCS.NS ", "^NSEI", "NOPE", "A" * 5000, "TCS.NS\x00", "'; DROP TABLE buckets;--"])
def test_bad_symbols_are_refused_cleanly(client, symbol):
    sign_in(client)
    bid = new_bucket(client)
    assert add(client, bid, symbol).status_code == 400
    assert db.bucket_items_for([bid])[bid] == []


def test_missing_fields_and_wrong_methods(client):
    sign_in(client)
    bid = new_bucket(client)
    t = csrf(client, f"/buckets/{bid}")
    assert client.post(f"/buckets/{bid}/add", data={"csrf": t}).status_code == 400
    assert client.post(f"/buckets/{bid}/rename", data={"csrf": t}).status_code == 400
    assert client.post(f"/buckets/{bid}/remove", data={"csrf": t}).status_code == 302   # removing nothing is harmless
    for verb in ("add", "remove", "rename", "delete", "share"):
        assert client.get(f"/buckets/{bid}/{verb}").status_code == 405


def test_every_changing_route_needs_a_csrf_token_and_a_session(client):
    sign_in(client)
    bid = new_bucket(client)
    add(client, bid, "TCS.NS")
    for verb, data in [("add", {"symbol": "INFY.NS"}), ("remove", {"symbol": "TCS.NS"}), ("rename", {"name": "x"}),
                       ("share", {"action": "on"}), ("delete", {})]:
        assert client.post(f"/buckets/{bid}/{verb}", data=data).status_code == 400, verb
    assert len(db.bucket_items_for([bid])[bid]) == 1
    anon = client.application.test_client()
    for verb in ("add", "remove", "rename", "share", "delete"):
        assert anon.post(f"/buckets/{bid}/{verb}", data={"symbol": "TCS.NS"}).status_code in (302, 400), verb
    assert db.get_bucket(db.get_or_create_user("a@x.com")["id"], bid) is not None


@pytest.mark.parametrize("path", ["/buckets/0", "/buckets/-1", "/buckets/99999999999999999999999", "/buckets/2147483648",
                                  "/b/" + "x" * 500, "/b/%E2%9C%93", "/b/x/story.png", "/b/x/link.png"])
def test_made_up_ids_and_tokens_are_404_not_500(client, path):
    sign_in(client)
    assert client.get(path).status_code in (404,)


def test_names_are_trimmed_collapsed_capped_and_unicode_safe(client):
    sign_in(client)
    a = new_bucket(client, "  A   lot\tof   space  ")
    b = new_bucket(client, "x" * 100)
    c = new_bucket(client, "Tech 🚀 picks ₹ मेरा")
    me = db.get_or_create_user("a@x.com")["id"]
    assert db.get_bucket(me, a)["name"] == "A lot of space"
    assert len(db.get_bucket(me, b)["name"]) == 40
    assert "🚀" in client.get(f"/buckets/{c}").get_data(as_text=True)
    add(client, c, "TCS.NS"); share_on(client, c)
    token = db.get_bucket(me, c)["share_token"]
    assert client.application.test_client().get(f"/b/{token}/story.png").data[:4] == b"\x89PNG"  # emoji dropped from the image, no crash


def test_search_input_is_safe(client):
    sign_in(client)
    bid = new_bucket(client)
    for q in ["%", "_", "'", '"><script>', "a" * 5000, "  ", "\x00", "résumé", "रिलायंस"]:
        r = client.get(f"/buckets/{bid}", query_string={"q": q})
        assert r.status_code == 200 and "<script>" not in r.get_data(as_text=True)


# ---------------- sharing privacy ----------------

def test_a_shared_page_never_contains_prices_emails_or_other_buckets(client):
    sign_in(client, "owner@secret.com")
    bid = new_bucket(client, "Public one")
    other = new_bucket(client, "Private one")
    add(client, bid, "TCS.NS"); add(client, other, "INFY.NS")
    db.save_quotes([Quote("TCS.NS", 3333.33, 1, 0, "d")])
    share_on(client, bid, "on"); share_on(client, bid, "perf", perf="1")
    token = db.get_bucket(db.get_or_create_user("owner@secret.com")["id"], bid)["share_token"]
    html = client.application.test_client().get(f"/b/{token}").get_data(as_text=True)
    for leak in ("owner@secret.com", "secret.com", "Private one", "Infosys", "3333", "3,333", "100.00", "added_price", "flagged"):
        assert leak not in html, leak
    for png in ("story", "link"):
        data = client.application.test_client().get(f"/b/{token}/{png}.png").data
        assert b"secret.com" not in data


def test_cards_are_the_documented_size_and_cached_not_recomputed(client):
    from io import BytesIO
    from PIL import Image
    stocks = [{"name": f"Stock {i}", "market": "IN" if i % 2 else "US"} for i in range(25)]
    m = {"name": "N" * 40, "n": 25, "stocks": stocks, "avg_text": "−33.33%", "tone": "down"}
    assert Image.open(BytesIO(card.render(m, "story"))).size == (1080, 1350)
    assert Image.open(BytesIO(card.render(m, "link"))).size == (1200, 630)
    assert card.render(m, "story") is card.render(m, "story")


def test_the_card_font_filter():
    assert card.printable("Banks & IT") == "Banks & IT"
    assert card.printable("Tech 🚀🔥 picks") == "Tech picks"
    assert card.printable("🚀") == "My bucket" and card.printable("   ") == "My bucket"


# ---------------- whole journeys ----------------

def test_a_full_user_journey(client):
    sign_in(client)
    me = db.get_or_create_user("a@x.com")["id"]
    bid = new_bucket(client, "Banks")
    assert "Add your first stock" in client.get(f"/buckets/{bid}").get_data(as_text=True)
    for sym in ("HDFCBANK.NS", "ICICIBANK.NS", "AAPL"):
        assert add(client, bid, sym).status_code == 302
    assert all(i["added_price"] == 100.0 for i in db.bucket_items_for([bid])[bid])
    db.save_quotes([Quote("HDFCBANK.NS", 110.0, 1, 0, "d"), Quote("ICICIBANK.NS", 85.0, 1, 0, "d"), Quote("AAPL", 100.0, 1, 0, "d")])
    page = client.get(f"/buckets/{bid}").get_data(as_text=True)
    assert "-1.67%" in page.replace("−", "-") and "Down 10%+" in page and "1 stock is down" in page   # (10 - 15 + 0) / 3
    share_on(client, bid)
    token = db.get_bucket(me, bid)["share_token"]
    anon = client.application.test_client()
    assert "Banks" in anon.get(f"/b/{token}").get_data(as_text=True) and "Average since added" not in anon.get(f"/b/{token}").get_data(as_text=True)
    share_on(client, bid, "perf", perf="1")
    assert "Average since added" in anon.get(f"/b/{token}").get_data(as_text=True)
    client.post(f"/buckets/{bid}/rename", data={"csrf": csrf(client, f"/buckets/{bid}"), "name": "Big banks"})
    assert "Big banks" in anon.get(f"/b/{token}").get_data(as_text=True)
    client.post(f"/buckets/{bid}/remove", data={"csrf": csrf(client, f"/buckets/{bid}"), "symbol": "ICICIBANK.NS"})
    assert "Down 10%+" not in client.get(f"/buckets/{bid}").get_data(as_text=True)
    client.post(f"/buckets/{bid}/delete", data={"csrf": csrf(client, f"/buckets/{bid}")})
    assert anon.get(f"/b/{token}").status_code == 404 and client.get(f"/buckets/{bid}").status_code == 404
    assert db.bucket_items_for([bid])[bid] == []


def test_re_adding_a_removed_stock_starts_a_fresh_baseline(client, monkeypatch):
    sign_in(client)
    bid = new_bucket(client)
    add(client, bid, "TCS.NS")                                    # measured from 100
    client.post(f"/buckets/{bid}/remove", data={"csrf": csrf(client, f"/buckets/{bid}"), "symbol": "TCS.NS"})
    with db.engine().begin() as c:
        c.execute(text("delete from quotes_cache"))               # so the next add takes a fresh live price
    monkeypatch.setattr(web, "_fetch_quote", lambda sym: Quote(sym, 150.0, 1, 0, "d"))
    add(client, bid, "TCS.NS")
    assert db.bucket_items_for([bid])[bid][0]["added_price"] == 150.0


def test_the_same_stock_can_sit_in_two_buckets_with_separate_baselines(client):
    u = db.get_or_create_user("a@x.com")
    one, two = db.create_bucket(u["id"], "One"), db.create_bucket(u["id"], "Two")
    db.add_bucket_item(one, "TCS.NS", 100.0); db.add_bucket_item(two, "TCS.NS", 200.0)
    db.save_quotes([Quote("TCS.NS", 150.0, 1, 0, "d")])
    sign_in(client)
    assert "+50.00%" in client.get(f"/buckets/{one}").get_data(as_text=True)
    assert "−25.00%" in client.get(f"/buckets/{two}").get_data(as_text=True)


def test_unpriced_stocks_wait_without_breaking_the_average_or_the_share(client):
    sign_in(client)
    bid = new_bucket(client)
    me = db.get_or_create_user("a@x.com")["id"]
    db.add_bucket_item(bid, "TCS.NS", None)
    page = client.get(f"/buckets/{bid}").get_data(as_text=True)
    assert "Waiting for the first prices" in page and "waiting for a price" in page
    share_on(client, bid); share_on(client, bid, "perf", perf="1")
    anon = client.application.test_client().get(f"/b/{db.get_bucket(me, bid)['share_token']}")
    assert anon.status_code == 200 and "Average since added" not in anon.get_data(as_text=True)  # nothing honest to show yet


def test_deleting_the_account_removes_buckets_items_and_share_links(client):
    sign_in(client)
    bid = new_bucket(client)
    add(client, bid, "TCS.NS"); share_on(client, bid)
    token = db.get_bucket(db.get_or_create_user("a@x.com")["id"], bid)["share_token"]
    client.post("/delete-account", data={"csrf": csrf(client, "/settings")})
    assert client.application.test_client().get(f"/b/{token}").status_code == 404
    with db.engine().connect() as c:
        assert c.execute(text("select count(*) from buckets")).scalar() == 0
        assert c.execute(text("select count(*) from bucket_items")).scalar() == 0


# ---------------- database efficiency ----------------

def _seed_heavy(client, buckets=10, per=25):
    sign_in(client)
    me = db.get_or_create_user("a@x.com")["id"]
    ids = []
    for i in range(buckets):
        bid = db.create_bucket(me, f"Bucket {i}")
        for sym in IN_STOCKS[:per]:
            db.add_bucket_item(bid, sym, 100.0)
        ids.append(bid)
    db.save_quotes([Quote(s, 95.0, 1, 0, "d") for s in IN_STOCKS[:per]])
    return me, ids


def test_pages_use_a_fixed_small_number_of_queries_however_big_the_buckets_are(client):
    me, ids = _seed_heavy(client, buckets=10, per=25)
    small = db.create_bucket(me, "Small"); db.add_bucket_item(small, "TCS.NS", 100.0)
    db.set_share(ids[0], "tok", True)
    share_small = db.create_bucket(me, "Share small"); db.add_bucket_item(share_small, "TCS.NS", 100.0); db.set_share(share_small, "toksmall", True)
    cases = {"list": ("/buckets", 10), "big": (f"/buckets/{ids[0]}", 10), "small": (f"/buckets/{small}", 10),
             "search": (f"/buckets/{ids[0]}?q=bank", 10), "shared": ("/b/tok", 8), "card": ("/b/tok/story.png", 8)}
    used = {}
    for name, (path, cap) in cases.items():
        with count_queries() as q:
            assert client.get(path).status_code == 200, name
        used[name] = len(q)
        assert len(q) <= cap, f"{name}: {len(q)} queries\n" + "\n".join(q)
    with count_queries() as q_small:
        client.get("/b/toksmall")
    assert used["shared"] == len(q_small)          # same cost for 25 stocks or 1: no per-stock queries


def test_adding_removing_and_creating_are_a_handful_of_statements(client):
    sign_in(client)
    bid = new_bucket(client)
    with count_queries() as q:
        add(client, bid, "TCS.NS")
    assert len(q) <= 14, q            # includes the csrf page load, the session user lookup, price cache check and write
    with count_queries() as q:
        client.post(f"/buckets/{bid}/remove", data={"csrf": csrf(client, f"/buckets/{bid}"), "symbol": "TCS.NS"})
    assert len(q) <= 14


def test_saving_500_prices_is_a_few_statements_not_a_thousand():
    qs = [Quote(f"S{i}", 10.0 + i, 9.0, 1.0, "2026-11-20") for i in range(500)]
    with count_queries() as q:
        db.save_quotes(qs)
    assert len(q) <= 4, len(q)
    assert len(db.quotes_for([f"S{i}" for i in range(500)])) == 500
    qs2 = [Quote(f"S{i}", 20.0 + i, 9.0, 1.0, "2026-11-23") for i in range(500)] + [Quote("NEW", 1.0, 1.0, 0.0, "d")]
    with count_queries() as q:
        db.save_quotes(qs2)           # mostly updates, one insert
    assert len(q) <= 5, len(q)
    got = db.quotes_for(["S0", "S499", "NEW"])
    assert got["S0"]["price"] == 20.0 and got["S499"]["price"] == 519.0 and got["NEW"]["price"] == 1.0
    db.save_quotes([])                # nothing to save is fine
    db.save_quotes([Quote("DUP", 1.0, 1.0, 0, "d"), Quote("DUP", 2.0, 1.0, 0, "d")])   # same symbol twice in one batch
    assert db.quotes_for(["DUP"])["DUP"]["price"] == 2.0


def test_old_prices_are_pruned_and_fresh_ones_kept():
    db.save_quotes([Quote("OLD", 1.0, 1.0, 0, "d"), Quote("NEW", 1.0, 1.0, 0, "d")])
    with db.engine().begin() as c:
        c.execute(text("update quotes_cache set updated_at = :t where symbol = 'OLD'"), {"t": datetime.utcnow() - timedelta(days=45)})
    assert db.prune_quotes(30) == 1
    assert set(db.quotes_for(["OLD", "NEW"])) == {"NEW"}


def test_the_job_does_a_bounded_number_of_statements_for_a_lot_of_buckets():
    quotes = {}
    for u in range(20):
        me = db.get_or_create_user(f"u{u}@x.com")["id"]
        for b in range(3):
            bid = db.create_bucket(me, f"B{b}")
            for sym in IN_STOCKS[:25]:
                db.add_bucket_item(bid, sym, 100.0)
    quotes = {s: bq(s, 95.0) for s in IN_STOCKS[:25]}
    with count_queries() as q:
        run_check(FakeProvider(quotes), Outbox(), now=IN_OPEN)
    assert len(q) <= 40, f"{len(q)} statements for 1,500 bucket rows"


def test_indexes_exist_on_new_databases():
    idx = {i["name"]: i for i in inspect(db.engine()).get_indexes("buckets")}
    assert "ix_buckets_user" in idx and idx["ix_buckets_user"]["column_names"] == ["user_id"]
    assert any(i["unique"] and i["column_names"] == ["share_token"] for i in inspect(db.engine()).get_indexes("buckets")) \
        or any(u["column_names"] == ["share_token"] for u in inspect(db.engine()).get_unique_constraints("buckets"))
    assert inspect(db.engine()).get_pk_constraint("bucket_items")["constrained_columns"] == ["bucket_id", "symbol"]


def test_an_existing_buckets_table_gets_the_index_and_share_columns(tmp_path):
    url = f"sqlite:///{tmp_path}/old.db"
    old = create_engine(url)
    with old.begin() as c:   # buckets exactly as the first release created it: no index, no share columns
        c.execute(text("create table users (id integer primary key, email varchar(320) unique not null, active boolean not null,"
                       " created_at timestamp not null, last_login_sent_at timestamp, first_signin_at timestamp)"))
        c.execute(text("create table buckets (id integer primary key, user_id integer not null, name varchar(60) not null, created_at timestamp not null)"))
        c.execute(text("insert into users values (1, 'a@x.com', 1, '2026-11-20 00:00:00', null, '2026-11-20 00:00:00')"))
        c.execute(text("insert into buckets values (1, 1, 'Old bucket', '2026-11-24 00:00:00')"))
    db.reset_engine(url)
    db.init_db(); db.init_db()
    cols = {c["name"] for c in inspect(db.engine()).get_columns("buckets")}
    assert {"share_token", "share_perf"} <= cols
    assert "ix_buckets_user" in {i["name"] for i in inspect(db.engine()).get_indexes("buckets")}
    b = db.get_bucket(1, 1)
    assert b["name"] == "Old bucket" and b["share_token"] is None and not b["share_perf"]


# ---------------- the alert job, several users at once ----------------

def _bucket_for(email, symbol, price, name="B"):
    u = db.get_or_create_user(email)
    bid = db.create_bucket(u["id"], name)
    db.add_bucket_item(bid, symbol, price)
    return u, bid


def test_two_people_following_the_same_stock_each_get_their_own_single_email():
    _bucket_for("a@x.com", "TCS.NS", 100.0); _bucket_for("b@x.com", "TCS.NS", 100.0)
    out = Outbox()
    p = FakeProvider({"TCS.NS": bq("TCS.NS", 80.0)})
    run_check(p, out, now=IN_OPEN); run_check(p, out, now=IN_OPEN)
    assert sorted(m[0] for m in out.sent) == ["a@x.com", "b@x.com"]
    assert p.requested[0] == ["TCS.NS"]      # fetched once for both


def test_a_mixed_bucket_flags_only_stocks_whose_market_is_open_now():
    u, bid = _bucket_for("a@x.com", "TCS.NS", 100.0)
    db.add_bucket_item(bid, "AAPL", 100.0)
    out = Outbox()
    # 21:30 IST / 11:00 New York: India has closed, the US is open.
    run_check(FakeProvider({"TCS.NS": bq("TCS.NS", 50.0), "AAPL": bq("AAPL", 80.0)}), out, now=US_OPEN)
    assert len(out.sent) == 1 and "Apple" in out.sent[0][2] and "Tata" not in out.sent[0][2]


def test_removed_stocks_and_deleted_buckets_are_never_flagged():
    u, bid = _bucket_for("a@x.com", "TCS.NS", 100.0)
    db.remove_bucket_item(bid, "TCS.NS")
    _, bid2 = _bucket_for("b@x.com", "INFY.NS", 100.0)
    db.delete_bucket(bid2)
    out = Outbox()
    run_check(FakeProvider({"TCS.NS": bq("TCS.NS", 50.0), "INFY.NS": bq("INFY.NS", 50.0)}), out, now=IN_OPEN)
    assert out.sent == []


def test_a_missing_quote_for_one_stock_does_not_hurt_the_others():
    u, bid = _bucket_for("a@x.com", "TCS.NS", 100.0)
    db.add_bucket_item(bid, "INFY.NS", 100.0)
    for sym in IN_STOCKS[:20]:
        db.add_bucket_item(bid, sym, 100.0)
    quotes = {s: bq(s, 100.0) for s in IN_STOCKS[:20]}
    quotes["TCS.NS"] = bq("TCS.NS", 80.0)           # INFY.NS has no quote at all
    out = Outbox()
    run_check(FakeProvider(quotes), out, now=IN_OPEN)
    assert len(out.sent) == 1 and "Tata" in out.sent[0][2]


def test_a_watchlist_dip_and_a_bucket_flag_in_the_same_run_are_two_separate_emails():
    u, _ = _bucket_for("a@x.com", "TCS.NS", 100.0)
    db.set_subscriptions(u["id"], {"^NSEI": 1.0})
    out = Outbox()
    run_check(FakeProvider({"TCS.NS": bq("TCS.NS", 80.0), "^NSEI": Quote("^NSEI", 98.0, 100.0, -2.0, "2026-11-20")}), out, now=IN_OPEN)
    assert len(out.sent) == 2
    assert {("down" in m[1]) for m in out.sent} == {True}


def test_re_adding_a_flagged_stock_after_removing_it_can_flag_again_once():
    u, bid = _bucket_for("a@x.com", "TCS.NS", 100.0)
    out = Outbox()
    p = FakeProvider({"TCS.NS": bq("TCS.NS", 80.0)})
    run_check(p, out, now=IN_OPEN)
    assert len(out.sent) == 1
    db.remove_bucket_item(bid, "TCS.NS"); db.add_bucket_item(bid, "TCS.NS", 100.0)   # a new baseline, a new row
    run_check(p, out, now=IN_OPEN); run_check(p, out, now=IN_OPEN)
    assert len(out.sent) == 2


def test_the_job_streams_only_stocks_that_still_need_looking_at():
    u, bid = _bucket_for("a@x.com", "TCS.NS", 100.0)
    db.add_bucket_item(bid, "INFY.NS", 100.0); db.add_bucket_item(bid, "WIPRO.NS", None)
    db.mark_flagged([(bid, "TCS.NS")])
    seen = {r["symbol"] for r in db.iter_open_bucket_items(batch=1)}
    assert seen == {"INFY.NS", "WIPRO.NS"}
    everything, unpriced = db.bucket_symbol_sets()
    assert everything == {"TCS.NS", "INFY.NS", "WIPRO.NS"} and unpriced == {"WIPRO.NS"}
    assert db.bucket_symbol_sets.__name__ and db.iter_open_bucket_items.__name__


def test_a_big_run_streams_in_batches_and_still_flags_correctly(monkeypatch):
    u = db.get_or_create_user("a@x.com")
    bid = db.create_bucket(u["id"], "Big")
    for sym in IN_STOCKS[:25]:
        db.add_bucket_item(bid, sym, 100.0)
    real = db.iter_open_bucket_items
    monkeypatch.setattr(db, "iter_open_bucket_items", lambda batch=5000: real(batch=3))   # force many small batches
    quotes = {s: bq(s, 100.0) for s in IN_STOCKS[:25]}
    quotes[IN_STOCKS[7]] = bq(IN_STOCKS[7], 70.0)
    out = Outbox()
    run_check(FakeProvider(quotes), out, now=IN_OPEN)
    assert len(out.sent) == 1


# ---------------- wording (SEBI-sensitive) ----------------

def test_user_facing_copy_is_informational_not_advice(client):
    import re
    sign_in(client)
    bid = new_bucket(client)
    pages = {"home": client.application.test_client().get("/").get_data(as_text=True),
             "bucket": client.get(f"/buckets/{bid}").get_data(as_text=True),
             "alerts": client.get("/alerts").get_data(as_text=True),
             "settings": client.get("/settings").get_data(as_text=True)}
    for name, html in pages.items():
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"buydips|buyDips", "", text)
        assert not re.search(r"\bdips?\b|\bpicks\b|\bbargain|\bopportunit|\bundervalued|\bguarantee", text, re.I), name
    assert "not a recommendation" in pages["bucket"] and "SEBI-registered" in pages["home"]
    add(client, bid, "TCS.NS")
    share = client.get(f"/buckets/{bid}").get_data(as_text=True)
    assert "not as a tip" in share and "SEBI registration" in share
    from buydips import emails
    for subject, text, html in (emails.alert([{"name": "N", "symbol": "TCS.NS", "change_pct": -3.0, "price": 1.0, "prev_close": 2.0,
                                               "session": "2026-11-20", "threshold": 1.0}], "u"),
                                emails.flagged([{"bucket": "B", "name": "N", "symbol": "TCS.NS", "pct": -12.0, "added_price": "1", "price": "2"}], "u")):
        assert "SEBI-registered" in text and "SEBI-registered" in html
        assert not re.search(r"\b(buy now|opportunity|bargain|good time)\b", subject + text, re.I)
