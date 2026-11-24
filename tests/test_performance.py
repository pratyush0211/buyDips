"""How the followed stocks have done, and the optional written summary."""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from buydips import config, db, performance, summarize
from buydips.checker import run_check
from tests.test_checker import FakeProvider, IN_OPEN, Outbox, q, user
from tests.test_web import client, csrf, sign_in  # noqa: F401  (client is a fixture)

DAY = date(2026, 11, 20)


def series(start_price, end_price, days=400, end=DAY):
    """A steady climb (or fall) from start_price to end_price, one close per calendar day."""
    out = []
    for i in range(days, -1, -1):
        out.append(((end - timedelta(days=i)).isoformat(), start_price + (end_price - start_price) * (days - i) / days))
    return out


def seed(symbol, **figs):
    db.save_perf({symbol: {"last_date": "2026-11-20", "last_price": 100.0, "r1w": 0.5, "r1m": 2.0, "r3m": 5.0, "r1y": 20.0,
                           "from_high": -3.0, **figs}})


# ---------------- the arithmetic ----------------

def test_returns_use_calendar_windows_and_the_year_high():
    m = performance.metrics(series(100, 140))
    assert m["last_date"] == "2026-11-20" and m["last_price"] == pytest.approx(140)
    assert m["r1w"] == pytest.approx((140 / (140 - 40 * 7 / 400) - 1) * 100, abs=0.01)
    assert m["r1m"] > m["r1w"] > 0 and m["r1y"] > m["r3m"] > m["r1m"]
    assert m["from_high"] == pytest.approx(0.0)                       # at its high


def test_a_window_the_history_does_not_reach_is_missing_not_guessed():
    short = performance.metrics(series(100, 110, days=40))
    assert short["r1w"] is not None and short["r1m"] is not None and short["r3m"] is None and short["r1y"] is None
    assert performance.metrics(series(100, 110, days=2)) is None       # too little to say anything
    assert performance.metrics([("2026-11-20", 0.0)] * 10) is None     # zeros are not prices


def test_distance_from_the_high_after_a_fall():
    rising = series(100, 200, days=300)
    fall = rising + [((DAY + timedelta(days=i)).isoformat(), 200 - i * 5) for i in range(1, 11)]      # down 25% from 200 to 150
    m = performance.metrics(fall)
    assert m["from_high"] == pytest.approx(-25.0, abs=0.1) and m["r1w"] < 0


def test_bad_points_are_ignored():
    pts = series(100, 120)
    pts[10] = (pts[10][0], float("nan"))
    assert performance.metrics(pts)["r1m"] is not None


def test_the_page_model_orders_rows_names_the_yardsticks_and_counts_up_and_down():
    subs = {"RELIANCE.NS": 3.0, "TCS.NS": 3.0, "AAPL": 3.0, "OLDGONE.NS": 3.0}
    perf = {"RELIANCE.NS": {"r1m": 4.0, "last_date": "2026-11-20"}, "TCS.NS": {"r1m": -2.0, "last_date": "2026-11-19"},
            "AAPL": {"r1m": 9.0, "last_date": "2026-11-20"}, "^NSEI": {"r1m": 1.0, "r1w": 0.1, "r3m": 2.0, "r1y": 9.0},
            "^NDX": {"r1m": 3.0, "r1w": 0.2, "r3m": 4.0, "r1y": 19.0}}
    m = performance.build(subs, perf)
    assert [r["symbol"] for r in m["rows"]] == ["AAPL", "RELIANCE.NS", "TCS.NS"]            # unknown symbol dropped, best first
    assert m["up"] == 2 and m["down"] == 1 and m["n_have"] == 3 and m["as_of"] == "2026-11-20"
    assert set(m["bench"]) == {"India", "US"} and m["bench"]["India"]["name"] == "Nifty 50"


def test_the_template_sentence_and_the_facts_use_the_same_numbers():
    perf = {"RELIANCE.NS": {"r1m": 4.0, "last_date": "2026-11-20"}, "TCS.NS": {"r1m": -2.0, "last_date": "2026-11-20"},
            "^NSEI": {"r1m": 1.0, "r1w": 0, "r3m": 0, "r1y": 0}}
    m = performance.build({"RELIANCE.NS": 3.0, "TCS.NS": 3.0}, perf)
    text, facts = performance.template_summary(m), performance.facts(m)
    assert "1 of the 2 items you follow is up and 1 is down" in text
    assert "Reliance Industries (+4.0%)" in text and "Tata Consultancy Services (-2.0%)" in text
    assert "averaged +1.0%, against +1.0% for the Nifty 50" in text
    assert "Strongest: Reliance Industries +4.0%." in facts and "Weakest: Tata Consultancy Services -2.0%." in facts
    assert performance.template_summary(performance.build({}, {})) == "" and performance.facts(performance.build({}, {})) == ""


# ---------------- the written summary and its safety checks ----------------

FACTS = "Period: the past month, up to 2026-11-20.\nFollowed with a figure: 3. Up: 2. Down: 1.\nStrongest: Apple +9.1%.\nWeakest: HDFC Bank -4.2%."


@pytest.mark.parametrize("text", [
    "Over the past month, 2 of the 3 items you follow are up and 1 is down.",
    "Apple was the strongest at +9.1% and HDFC Bank the weakest at -4.2%.",
])
def test_plain_descriptions_pass(text):
    assert summarize.ok(text, FACTS)


@pytest.mark.parametrize("text", [
    "", "   ", "x" * 500,
    "Apple is up +9.1%, a good time to buy more.",              # advice
    "Apple at +9.1% looks undervalued.",                         # a view on value
    "HDFC Bank at -4.2% should recover next month.",             # a forecast
    "HDFC Bank will likely bounce from -4.2%.",
    "Consider holding Apple, up +9.1%.",
    "Apple is up +12.0% this month.",                            # a number that is not in the facts
    "Apple is up +9.1% and 7 others are flat.",                  # an invented count
    "Apple is up +9.1%.\nHDFC Bank is down -4.2%.",              # more than one paragraph
    "See https://example.com for more: Apple +9.1%.",            # a link
    "Email me@x.com: Apple +9.1%.",
    "The best performer was Apple at +9.1%, a risk-free gain.",
])
def test_anything_that_sounds_like_advice_or_invents_a_figure_is_rejected(text):
    assert not summarize.ok(text, FACTS)


def _model(n=3):
    perf = {"RELIANCE.NS": {"r1m": 4.0, "last_date": "2026-11-20"}, "TCS.NS": {"r1m": -2.0, "last_date": "2026-11-20"},
            "AAPL": {"r1m": 9.0, "last_date": "2026-11-20"}}
    return performance.build({k: 3.0 for k in list(perf)[:n]}, perf)


@pytest.fixture
def ai_on(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key-not-real")
    calls = []
    def fake(facts):
        calls.append(facts)                       # a reply built only from numbers present in the facts
        n = facts.split("Followed with a figure: ")[1].split(".")[0]
        return f"Over the past month, {n} items you follow had a figure."
    monkeypatch.setattr(summarize, "_ask", fake)
    return calls


def test_without_a_key_the_template_is_used_and_nothing_is_called(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(summarize, "_ask", lambda f: pytest.fail("must not call the API without a key"))
    text, by_ai = summarize.get(db.get_or_create_user("a@x.com")["id"], _model())
    assert by_ai is False and "are up" in text


def test_a_valid_reply_is_used_cached_and_not_asked_for_again(ai_on):
    uid = db.get_or_create_user("a@x.com")["id"]
    first = summarize.get(uid, _model())
    second = summarize.get(uid, _model())
    assert first == second == ("Over the past month, 3 items you follow had a figure.", True)
    assert len(ai_on) == 1                                                         # the second view cost nothing
    assert "@" not in ai_on[0] and "a@x.com" not in ai_on[0]                       # no email ever goes to the service
    assert set(ai_on[0].split("\n")[0:1]) == {"Period: the past month, up to 2026-11-20."}


def test_a_changed_watchlist_makes_a_new_summary(ai_on):
    uid = db.get_or_create_user("a@x.com")["id"]
    summarize.get(uid, _model(3))
    summarize.get(uid, _model(2))
    assert len(ai_on) == 2


@pytest.mark.parametrize("reply", [None, "Apple is a great buy at +9.1%.", "Apple is up +55%.", ""])
def test_a_bad_or_missing_reply_falls_back_to_the_template(monkeypatch, reply):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setattr(summarize, "_ask", lambda f: reply)
    uid = db.get_or_create_user("a@x.com")["id"]
    text, by_ai = summarize.get(uid, _model())
    assert by_ai is False and text == performance.template_summary(_model())
    assert db.summaries_today(uid) == 0                                           # a rejected reply is never stored


def test_a_cached_summary_is_rechecked_before_it_is_shown(ai_on):
    uid = db.get_or_create_user("a@x.com")["id"]
    m = _model()
    db.save_summary(uid, performance.digest(m), "Apple is a good buy at +9.1%.")      # something bad somehow stored
    text, by_ai = summarize.get(uid, m)
    assert "buy" not in text.lower() or by_ai is True and "buy" not in text.lower()
    assert "good buy" not in text


def test_spending_limits_stop_new_calls(ai_on, monkeypatch):
    uid = db.get_or_create_user("a@x.com")["id"]
    monkeypatch.setattr(config, "SUMMARY_PER_USER_PER_DAY", 2)
    for n in (3, 2):
        assert summarize.get(uid, _model(n))[1] is True
    assert summarize.get(uid, _model(1)) [1] is False and len(ai_on) == 2           # the third distinct watchlist is not sent
    other = db.get_or_create_user("b@x.com")["id"]
    monkeypatch.setattr(config, "SUMMARY_PER_DAY_TOTAL", 2)
    assert summarize.get(other, _model(3))[1] is False and len(ai_on) == 2           # and a global ceiling holds for everyone


def test_the_request_to_claude_carries_only_the_facts(monkeypatch):
    sent = {}
    def create(**kw):
        sent.update(kw)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=" Two items are up. ")])
    monkeypatch.setattr(summarize, "_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=create)))
    assert summarize._ask(FACTS) == "Two items are up."
    assert sent["model"] == config.SUMMARY_MODEL == "claude-haiku-5-5" and sent["messages"] == [{"role": "user", "content": FACTS}]
    assert sent["thinking"] == {"type": "disabled"} and sent["output_config"] == {"effort": "low"} and sent["max_tokens"] <= 400
    assert "buy" in sent["system"] and "predict" in sent["system"]


@pytest.mark.parametrize("stop", ["refusal", "max_tokens", "tool_use"])
def test_a_reply_that_did_not_finish_normally_is_never_used(monkeypatch, stop):
    reply = SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text="Two items are up.")])
    monkeypatch.setattr(summarize, "_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: reply)))
    assert summarize._ask(FACTS) is None


def test_service_errors_and_missing_text_mean_none_not_a_crash(monkeypatch):
    def boom(**kw):
        raise TimeoutError("slow")
    monkeypatch.setattr(summarize, "_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=boom)))
    assert summarize._ask(FACTS) is None
    empty = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="thinking", text="")])
    monkeypatch.setattr(summarize, "_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: empty)))
    assert summarize._ask(FACTS) is None


# ---------------- the daily refresh in the alert job ----------------

class HistoryProvider(FakeProvider):
    def __init__(self, quotes, histories):
        super().__init__(quotes)
        self.histories, self.history_calls = histories, []

    def history(self, symbols):
        self.history_calls.append(sorted(symbols))
        return {s: self.histories[s] for s in symbols if s in self.histories}


def test_the_job_stores_figures_once_a_day_for_followed_symbols_and_the_yardsticks():
    user("a@x.com", {"RELIANCE.NS": 3.0, "AAPL": 3.0})
    hist = {s: series(100, 120) for s in ("RELIANCE.NS", "AAPL", "^NSEI", "^NDX")}
    p, out = HistoryProvider({s: q(s, -1) for s in ("RELIANCE.NS", "AAPL")}, hist), Outbox()
    summary = run_check(p, out, now=IN_OPEN)
    assert summary["performance_refreshed"] == 4 and p.history_calls == [["AAPL", "RELIANCE.NS", "^NDX", "^NSEI"]]
    got = db.perf_for(["RELIANCE.NS", "^NSEI"])
    assert got["RELIANCE.NS"]["r1m"] > 0 and got["^NSEI"]["last_date"] == "2026-11-20"
    again = run_check(p, out, now=IN_OPEN)                                          # the next hourly run: nothing stale
    assert again["performance_refreshed"] == 0 and len(p.history_calls) == 1


def test_a_symbol_with_no_usable_history_is_not_retried_every_hour():
    user("a@x.com", {"RELIANCE.NS": 3.0})
    p = HistoryProvider({"RELIANCE.NS": q("RELIANCE.NS", -1)}, {"RELIANCE.NS": series(100, 101, days=2), "^NSEI": series(100, 120), "^NDX": series(100, 120)})
    run_check(p, Outbox(), now=IN_OPEN)
    assert db.perf_for(["RELIANCE.NS"])["RELIANCE.NS"]["r1m"] is None
    assert db.perf_stale(["RELIANCE.NS"]) == []


def test_a_history_failure_never_stops_alerts_from_being_sent():
    user("a@x.com", {"RELIANCE.NS": 1.0})
    class Broken(FakeProvider):
        def history(self, symbols):
            raise RuntimeError("yahoo is down")
    out = Outbox()
    summary = run_check(Broken({"RELIANCE.NS": q("RELIANCE.NS", -3)}), out, now=IN_OPEN)
    assert len(out.sent) == 1 and summary["performance_refreshed"] == 0


def test_stale_figures_are_refreshed_and_the_cap_is_respected(monkeypatch):
    from buydips import checker
    db.save_perf({"RELIANCE.NS": {"last_date": "2026-11-01", "r1m": 1.0}})
    with db.engine().begin() as conn:
        from sqlalchemy import text
        conn.execute(text("update perf_cache set updated_at = :t"), {"t": db._now() - timedelta(hours=30)})
    assert db.perf_stale(["RELIANCE.NS", "TCS.NS"]) == ["RELIANCE.NS", "TCS.NS"]
    monkeypatch.setattr(checker, "PERF_MAX_PER_RUN", 1)
    user("a@x.com", {"RELIANCE.NS": 3.0, "TCS.NS": 3.0})
    p = HistoryProvider({s: q(s, -1) for s in ("RELIANCE.NS", "TCS.NS")}, {s: series(100, 120) for s in ("RELIANCE.NS", "TCS.NS", "^NSEI", "^NDX")})
    run_check(p, Outbox(), now=IN_OPEN)
    assert len(p.history_calls[0]) == 1                                              # one batch per run at most this big


# ---------------- the page ----------------

def test_the_page_needs_sign_in_and_is_linked_from_the_watchlist(client):
    assert client.application.test_client().get("/performance").status_code == 302
    sign_in(client)
    assert 'href="/performance"' in client.get("/watchlist?start=india").get_data(as_text=True)


def test_empty_and_waiting_states_say_what_is_going_on(client):
    sign_in(client)
    html = client.get("/performance").get_data(as_text=True)
    assert "aren't following anything yet" in html
    db.set_subscriptions(db.get_or_create_user("a@x.com")["id"], {"RELIANCE.NS": 3.0})
    html = client.get("/performance").get_data(as_text=True)
    assert "gathering the price history" in html and "<ul class=\"lrows perf\">" not in html


def test_the_page_shows_figures_the_summary_and_honest_labels(client, ai_on):
    sign_in(client)
    uid = db.get_or_create_user("a@x.com")["id"]
    db.set_subscriptions(uid, {"RELIANCE.NS": 3.0, "TCS.NS": 3.0, "AAPL": 3.0})
    seed("RELIANCE.NS", r1m=4.0); seed("TCS.NS", r1m=-2.0, from_high=-12.0); seed("AAPL", r1m=9.0)
    seed("^NSEI", r1m=1.0); seed("^NDX", r1m=3.0)
    html = client.get("/performance").get_data(as_text=True)
    assert "+9.0%" in html and "-2.0%" in html and "12% below its 1-year high" in html
    assert html.index("Apple") < html.index("Reliance Industries") < html.index("Tata Consultancy Services")   # best month first
    assert "For comparison" in html and "Nifty 50" in html and "Nasdaq-100" in html
    assert "Written by AI from the figures below" in html and "not investment advice" in html and "SEBI-registered" in html
    assert 'class="ico' in html and "adjusted for splits and dividends" in html


def test_without_a_key_the_page_works_and_does_not_claim_ai(client, monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    sign_in(client)
    uid = db.get_or_create_user("a@x.com")["id"]
    db.set_subscriptions(uid, {"RELIANCE.NS": 3.0})
    seed("RELIANCE.NS")
    html = client.get("/performance").get_data(as_text=True)
    assert "Reliance Industries is up 2.0%" in html and "Written by AI" not in html


def test_privacy_page_discloses_the_service_and_what_it_gets(client):
    html = client.application.test_client().get("/privacy").get_data(as_text=True)
    assert "Anthropic" in html and "never your email" in html


# ---------------- sharing how the list has done ----------------

def _owner(client):
    sign_in(client)
    uid = db.get_or_create_user("a@x.com")["id"]
    db.set_subscriptions(uid, {"RELIANCE.NS": 3.0, "TCS.NS": 5.0, "AAPL": 3.0, "^NSEI": 2.0})
    seed("RELIANCE.NS", r1m=4.0); seed("TCS.NS", r1m=-2.0); seed("AAPL", r1m=9.0)
    seed("^NSEI", r1m=1.0); seed("^NDX", r1m=3.0)
    return uid


def _start_sharing(client):
    client.post("/performance/share", data={"csrf": csrf(client, "/performance"), "action": "on"})
    return db.get_or_create_user("a@x.com")["perf_share_token"]


def test_share_model_averages_stocks_not_indexes_and_names_the_yardsticks():
    subs = {"RELIANCE.NS": 3.0, "TCS.NS": 3.0, "AAPL": 3.0, "^NSEI": 2.0}
    perf = {"RELIANCE.NS": {"r1m": 4.0, "last_date": "2026-11-20"}, "TCS.NS": {"r1m": -2.0, "last_date": "2026-11-20"},
            "AAPL": {"r1m": 9.0, "last_date": "2026-11-20"}, "^NSEI": {"r1m": 50.0, "last_date": "2026-11-20"},
            "^NDX": {"r1m": 3.0, "r1w": 0, "r3m": 0, "r1y": 0}}
    perf["^NSEI"].update(r1w=0, r3m=0, r1y=0)
    s = performance.share_model(performance.build(subs, perf))
    assert s["avg_text"] == "+3.7%" and s["n"] == 3 and s["n_priced"] == 3 and s["tone"] == "up"
    assert [v["name"] for v in s["vs"]] == ["Nifty 50", "Nasdaq-100"]
    assert {st["name"] for st in s["stocks"]} == {"Reliance Industries", "Tata Consultancy Services", "Apple"}
    assert performance.share_model(performance.build({"^NSEI": 2.0}, perf)) is None      # only an index: nothing to share
    assert performance.share_model(performance.build({"TCS.NS": 3.0}, {})) is None       # no history yet


def test_sharing_is_off_until_chosen_and_the_link_shows_only_what_was_promised(client):
    _owner(client)
    html = client.get("/performance").get_data(as_text=True)
    assert "Create share link" in html and "/p/" not in html
    token = _start_sharing(client)
    assert token
    other = client.application.test_client()                 # a stranger with no sign-in
    page = other.get(f"/p/{token}")
    body = page.get_data(as_text=True)
    assert page.status_code == 200 and page.headers["X-Robots-Tag"] == "noindex"
    assert "+3.7%" in body and "Reliance Industries" in body and "Nifty 50 +1.0%" in body
    assert "a@x.com" not in body and "threshold" not in body.lower()        # nothing about the owner beyond the list
    assert "not investment advice" in body and "Past performance says nothing about the future" in body
    assert f"/p/{token}/link.png" in body


def test_the_share_pictures_render_and_are_not_indexed(client):
    _owner(client)
    token = _start_sharing(client)
    other = client.application.test_client()
    for kind, size in (("story", (1080, 1350)), ("link", (1200, 630))):
        r = other.get(f"/p/{token}/{kind}.png")
        assert r.status_code == 200 and r.mimetype == "image/png" and r.headers["X-Robots-Tag"] == "noindex"
        from io import BytesIO
        from PIL import Image
        assert Image.open(BytesIO(r.data)).size == size
    assert other.get(f"/p/{token}/other.png").status_code == 404
    assert "attachment" in other.get(f"/p/{token}/story.png?download=1").headers["Content-Disposition"]


def test_stopping_sharing_kills_the_link_and_a_guessed_token_finds_nothing(client):
    _owner(client)
    token = _start_sharing(client)
    other = client.application.test_client()
    assert other.get(f"/p/{token}").status_code == 200
    client.post("/performance/share", data={"csrf": csrf(client, "/performance"), "action": "off"})
    assert other.get(f"/p/{token}").status_code == 404 and other.get(f"/p/{token}/story.png").status_code == 404
    assert other.get("/p/" + "x" * 12).status_code == 404 and other.get("/p/" + "x" * 80).status_code == 404
    html = client.get("/performance?changed=unshared").get_data(as_text=True)
    assert "the old link no longer works" in html.lower()


def test_sharing_again_after_stopping_gives_a_fresh_link(client):
    _owner(client)
    first = _start_sharing(client)
    client.post("/performance/share", data={"csrf": csrf(client, "/performance"), "action": "off"})
    assert _start_sharing(client) != first


def test_sharing_needs_sign_in_csrf_and_something_to_show(client):
    anon = client.application.test_client()
    assert anon.post("/performance/share", data={"action": "on"}).status_code == 302
    sign_in(client)
    assert client.post("/performance/share", data={"action": "on"}).status_code in (400, 403)      # no CSRF token
    token = csrf(client, "/watchlist?start=india")
    assert client.post("/performance/share", data={"csrf": token, "action": "on"}).status_code == 400  # nothing followed
    assert db.get_or_create_user("a@x.com")["perf_share_token"] is None
    assert client.post("/performance/share", data={"csrf": token, "action": "bogus"}).status_code == 400


def test_a_link_goes_dark_if_the_owner_unfollows_everything(client):
    uid = _owner(client)
    token = _start_sharing(client)
    db.set_subscriptions(uid, {})
    assert client.application.test_client().get(f"/p/{token}").status_code == 404


def test_the_link_follows_the_list_as_it_changes(client):
    uid = _owner(client)
    token = _start_sharing(client)
    other = client.application.test_client()
    db.set_subscriptions(uid, {"AAPL": 3.0})
    body = other.get(f"/p/{token}").get_data(as_text=True)
    assert "+9.0%" in body and "Reliance Industries" not in body and "Nasdaq-100 +3.0%" in body and "Nifty 50" not in body
