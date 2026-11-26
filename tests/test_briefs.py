"""The daily brief: signals found by arithmetic, research gated and checked, cost kept low, shown and emailed only when notable."""
import json
import math
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from buydips import briefs, config, db, emails, signals
from buydips.checker import run_check
from tests.test_checker import FakeProvider, IN_OPEN, Outbox, q, user
from tests.test_web import client, csrf, sign_in  # noqa: F401  (client is a fixture)

DAY = IN_OPEN.date().isoformat()          # 2026-11-20, 05:00 UTC: after the morning run time
END = date(2026, 11, 20)


def rows(closes, vols=None):
    n = len(closes)
    return [((END - timedelta(days=n - 1 - i)).isoformat(), c, (vols[i] if vols else None)) for i, c in enumerate(closes)]


def flat(n=250, v=100.0):
    return [v + 0.01 * (i % 3) for i in range(n)]


# ---------------- signals ----------------

def kinds(rs):
    return {s["k"] for s in signals.compute(rs)}


def test_52_week_high_and_low():
    assert "high52" in kinds(rows(flat(249) + [150.0]))
    assert "low52" in kinds(rows(flat(249) + [60.0]))
    assert not kinds(rows(flat()))                                   # nothing unusual: no signals, so no research
    assert kinds(rows(flat(60))) == set()                            # under a year of data: no 52-week claim


def test_a_big_one_day_move_is_reported_with_its_size():
    s = signals.compute(rows(flat(249) + [112.0]))
    move = [x for x in s if x["k"] == "move"][0]
    assert move["text"] == "Up 12.0% in one day" and move["s"] == 3
    assert [x["text"] for x in signals.compute(rows(flat(249) + [93.0])) if x["k"] == "move"] == ["Down 7.0% in one day"]
    assert not [x for x in signals.compute(rows(flat(249) + [103.0])) if x["k"] == "move"]      # 3% is not notable


def test_average_crossover_only_within_the_last_few_sessions():
    down_then_up = [200 - i * 0.5 for i in range(200)] + [100 + i * 2 for i in range(50)]
    assert "cross_up" in kinds(rows(down_then_up))
    up_then_down = [100 + i * 0.5 for i in range(200)] + [200 - i * 2 for i in range(50)]
    assert "cross_down" in kinds(rows(up_then_down))
    assert "cross_up" not in kinds(rows([100 + i * 0.5 for i in range(250)]))     # a steady climb, no cross


def test_rsi_extremes_and_volume_spikes():
    assert "rsi" in kinds(rows([100 + i for i in range(60)]))                    # relentless climb
    assert "rsi" in kinds(rows([200 - i for i in range(60)]))
    vols = [1000.0] * 249 + [3500.0]
    s = signals.compute(rows(flat(250), vols))
    assert [x["text"] for x in s if x["k"] == "volume"] == ["Volume 3.5x its 20-day average"]
    assert "volume" not in kinds(rows(flat(250), [1000.0] * 250))


def test_signals_are_sorted_strongest_first_and_carry_only_numbers_we_computed():
    s = signals.compute(rows([100 + i for i in range(249)] + [400.0]))
    assert [x["s"] for x in s] == sorted((x["s"] for x in s), reverse=True) and s[0]["s"] == 3


# ---------------- choosing what to research ----------------

def perf_row(symbol, sigs=(), r1m=0.0):
    return {"symbol": symbol, "r1m": r1m, "r3m": 1.0, "r1y": 10.0, "signals": json.dumps([{"k": "x", "text": t, "s": s} for t, s in sigs])}


def test_only_stocks_with_a_signal_or_a_dip_are_researched_and_never_indexes():
    followed = {"RELIANCE.NS": perf_row("RELIANCE.NS", [("At a 52-week high", 3)]), "TCS.NS": perf_row("TCS.NS"),
                "^NSEI": perf_row("^NSEI", [("At a 52-week high", 3)]), "AAPL": perf_row("AAPL", [("Down 6.4% in one day", 2)]),
                "INFY.NS": perf_row("INFY.NS", [("RSI at 80 (above 75)", 1)])}
    assert briefs.candidates(followed) == ["RELIANCE.NS", "AAPL"]    # strongest first; TCS quiet; INFY's lone minor signal is not worth reading the web for; index skipped
    assert briefs.candidates(followed, dipped={"TCS.NS"}) == ["RELIANCE.NS", "AAPL", "TCS.NS"]   # a dip counts even without a signal (ties sort by symbol)
    assert briefs.candidates({"TCS.NS": perf_row("TCS.NS")}) == []                # nothing: zero requests


def test_the_daily_cap_is_a_hard_ceiling(monkeypatch):
    monkeypatch.setattr(config, "BRIEF_MAX_PER_DAY", 2)
    syms = ["RELIANCE.NS", "TCS.NS", "INFY.NS", "AAPL"]
    assert len(briefs.candidates({s: perf_row(s, [("At a 52-week high", 3)]) for s in syms})) == 2


def test_the_facts_sent_to_the_model_are_short_and_contain_no_personal_data():
    p = perf_row("RELIANCE.NS", [("At a 52-week high", 3), ("Up 5.2% in one day", 2)], r1m=4.0)
    f = briefs.facts_for("RELIANCE.NS", p, {"mean": 1650.0})
    assert f.splitlines()[0] == "Reliance Industries (RELIANCE.NS)" and "Yahoo analyst mean target ₹1650" in f
    assert "Signals: At a 52-week high; Up 5.2% in one day" in f and len(f) < 400


# ---------------- checking what comes back ----------------

URLS = {"https://economictimes.com/a", "https://www.moneycontrol.com/b"}
CITED = "Reliance reported net profit of 18,540 crore. Motilal Oswal set a target price of 1,650."
FACTS = "Reliance Industries (RELIANCE.NS)\nSignals: At a 52-week high\nMoves: 1M +4.0%"


def good(**over):
    raw = {"material": 8, "headline": "Reliance posts quarterly profit of 18540 crore",
           "points": [{"t": "Net profit came in at 18540 crore", "u": "https://economictimes.com/a"}],
           "targets": [{"by": "Motilal Oswal", "p": 1650, "u": "https://www.moneycontrol.com/b"}]}
    raw.update(over)
    return raw


def test_a_well_sourced_brief_is_kept_with_domains():
    b = briefs.validate(good(), URLS, CITED, FACTS)
    assert b["material"] == 8 and b["points"][0]["d"] == "economictimes.com"
    assert b["targets"] == [{"by": "Motilal Oswal", "p": 1650.0, "u": "https://www.moneycontrol.com/b", "d": "moneycontrol.com"}]


def test_points_without_a_real_source_are_dropped_and_no_points_means_no_brief():
    raw = good(points=[{"t": "Net profit came in at 18540 crore", "u": "https://invented.example/x"}])
    assert briefs.validate(raw, URLS, CITED, FACTS) is None
    two = good(points=[{"t": "Net profit came in at 18540 crore", "u": "https://economictimes.com/a"},
                       {"t": "Something unsupported", "u": "https://invented.example/x"}])
    assert len(briefs.validate(two, URLS, CITED, FACTS)["points"]) == 1


@pytest.mark.parametrize("text", [
    "Analysts say it is a good time to buy Reliance", "The stock looks undervalued", "Investors should accumulate on dips",
    "A promising pick for the long run", "See https://x.example for more", "Mail me@x.com", "x" * 300, "", None,
])
def test_recommendations_valuation_calls_links_and_junk_are_dropped(text):
    raw = good(points=[{"t": text, "u": "https://economictimes.com/a"}])
    assert briefs.validate(raw, URLS, CITED, FACTS) is None


def test_forecast_wording_is_fine_in_a_sourced_point_but_never_in_the_headline():
    soft = good(headline="Reliance shares will rally after results",
                points=[{"t": "Brokerages expect further gains and the company may raise guidance", "u": "https://economictimes.com/a"}])
    report = {}
    b = briefs.validate(soft, URLS, CITED, FACTS, report, "Reliance Industries")
    assert b["points"][0]["t"].startswith("Brokerages expect") and report["why"] == {"headline_advice_words:will": 1, "headline_replaced": 1}
    assert b["headline"] == "Reliance Industries is in the news"             # the point is not promoted: it has forecast wording too
    calm = good(headline="Reliance shares will rally",
                points=[{"t": "Net profit came in at 18540 crore", "u": "https://economictimes.com/a"}])
    assert briefs.validate(calm, URLS, CITED, FACTS, {}, "Reliance Industries")["headline"] == "Net profit came in at 18540 crore"


def test_figures_are_trusted_with_their_source_by_default_and_checked_when_strict(monkeypatch):
    raw = good(points=[{"t": "Net profit rose 77 percent", "u": "https://economictimes.com/a"}], headline="Reliance reports quarterly results")
    assert briefs.validate(raw, URLS, CITED, FACTS)["points"][0]["t"] == "Net profit rose 77 percent"
    monkeypatch.setattr(config, "BRIEF_STRICT_NUMBERS", True)
    assert briefs.validate(raw, URLS, CITED, FACTS) is None
    assert briefs.validate(good(targets=[{"by": "Motilal Oswal", "p": 2100, "u": "https://www.moneycontrol.com/b"}]), URLS, CITED, FACTS)["targets"] == []


def test_broker_targets_must_appear_in_a_cited_passage_and_be_sourced():
    assert briefs.validate(good(targets=[{"by": "Motilal Oswal", "p": 2100, "u": "https://www.moneycontrol.com/b"}]), URLS, CITED, FACTS)["targets"] == []
    assert briefs.validate(good(targets=[{"by": "Motilal Oswal", "p": 1650, "u": "https://invented.example"}]), URLS, CITED, FACTS)["targets"] == []
    assert briefs.validate(good(targets=[{"by": "Buy now <b>", "p": 1650, "u": "https://www.moneycontrol.com/b"}]), URLS, CITED, FACTS)["targets"] == []
    assert briefs.validate(good(targets="junk"), URLS, CITED, FACTS)["targets"] == []


def test_material_is_clamped_and_garbage_is_rejected():
    assert briefs.validate(good(material=99), URLS, CITED, FACTS)["material"] == 10
    assert briefs.validate(good(material=-4), URLS, CITED, FACTS)["material"] == 0
    assert briefs.validate(good(material="high"), URLS, CITED, FACTS) is None
    b = briefs.validate(good(headline=""), URLS, CITED, FACTS, {}, "Reliance Industries")      # a sourced point survives a bad headline
    assert b["headline"] == "Net profit came in at 18540 crore"
    assert briefs.validate(good(headline="", points=[]), URLS, CITED, FACTS) is None             # but no sourced point, no brief


def test_parse_handles_code_fences_chatter_and_junk():
    assert briefs.parse('{"material": 3}') == {"material": 3}
    assert briefs.parse('```json\n{"material": 3}\n```') == {"material": 3}
    assert briefs.parse('Here you go: {"material": 3} done') == {"material": 3}
    assert briefs.parse("no json here") is None and briefs.parse("{broken") is None and briefs.parse("[1, 2]") is None


# ---------------- the request itself: tokens kept low ----------------

def fake_response(text='{"material": 7}', stop="end_turn", urls=("https://economictimes.com/a",), cited="profit 18,540"):
    dump = {"content": [{"type": "server_tool_use", "name": "web_search"},
                        {"type": "web_search_tool_result", "content": [{"type": "web_search_result", "url": u, "title": "t"} for u in urls]},
                        {"type": "text", "text": text, "citations": [{"url": u, "cited_text": cited} for u in urls]}]}
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)], model_dump=lambda: dump,
                           usage=SimpleNamespace(input_tokens=4200, output_tokens=210))


def test_the_request_is_small_capped_and_has_thinking_off(monkeypatch):
    sent = {}
    def create(**kw):
        sent.update(kw)
        return fake_response()
    monkeypatch.setattr(briefs, "_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=create)))
    text, urls, cited = briefs.ask("Reliance Industries (RELIANCE.NS)")
    assert sent["model"] == config.BRIEF_MODEL == "claude-haiku-5-5" and sent["max_tokens"] <= 900
    assert sent["thinking"] == {"type": "disabled"} and sent["output_config"] == {"effort": "low"}
    assert sent["tools"] == [{"type": "web_search_20250305", "name": "web_search", "max_uses": config.BRIEF_MAX_SEARCHES}]
    assert config.BRIEF_MAX_SEARCHES == 1
    assert len(sent["system"].split()) < 160 and sent["messages"] == [{"role": "user", "content": "Reliance Industries (RELIANCE.NS)"}]
    assert urls == {"https://economictimes.com/a"} and "18,540" in cited and text == '{"material": 7}'


@pytest.mark.parametrize("stop", ["pause_turn", "refusal", "max_tokens"])
def test_an_unfinished_reply_is_never_used(monkeypatch, stop):
    monkeypatch.setattr(briefs, "_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: fake_response(stop=stop))))
    assert briefs.ask("x") is None


def test_service_errors_mean_no_brief_not_a_crash(monkeypatch):
    def boom(**kw):
        raise TimeoutError("slow")
    monkeypatch.setattr(briefs, "_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=boom)))
    assert briefs.ask("x") is None
    monkeypatch.setattr(briefs, "_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: fake_response(text=""))))
    assert briefs.ask("x") is None


# ---------------- the daily run ----------------

class P(FakeProvider):
    def __init__(self):
        super().__init__({})
        self.analyst_calls = []

    def analyst(self, symbols):
        self.analyst_calls.append(list(symbols))
        return {s: {"mean": 1650.0, "low": 1400.0, "high": 1900.0} for s in symbols}


def seed_perf(symbol, sigs=(("At a 52-week high", 3),), series=True, **fig):
    closes = [100 + 20 * math.sin(i / 25) + i * 0.1 for i in range(250)]
    db.save_perf({symbol: {"last_date": DAY, "last_price": closes[-1], "r1w": 1.0, "r1m": 4.0, "r3m": 6.0, "r1y": 14.2, "from_high": 0.0,
                           "series": json.dumps([[(END - timedelta(days=249 - i)).isoformat(), round(c, 2)] for i, c in enumerate(closes)]) if series else None,
                           "signals": json.dumps([{"k": "k", "text": t, "s": s} for t, s in sigs]), **fig}})


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key-not-real")
    asked = []
    def ask(facts):
        asked.append(facts)
        text = json.dumps(good(headline="Reliance posts quarterly profit of 18540 crore"))
        return text, URLS, CITED
    monkeypatch.setattr(briefs, "ask", ask)
    return asked


def test_one_request_per_stock_shared_by_everyone_who_follows_it(keyed):
    user("a@x.com", {"RELIANCE.NS": 3.0, "TCS.NS": 3.0})
    user("b@x.com", {"RELIANCE.NS": 5.0})
    user("c@x.com", {"RELIANCE.NS": 2.0})
    seed_perf("RELIANCE.NS"); seed_perf("TCS.NS", sigs=())
    out = briefs.run_daily(P(), IN_OPEN)
    assert (out["researched"], out["worthy"], out["skipped"]) == (1, 1, None) and len(keyed) == 1      # three followers, one request; quiet TCS none
    assert "a@x.com" not in keyed[0] and "@" not in keyed[0]


def test_it_runs_once_a_day_and_not_before_morning_and_not_without_a_key(keyed, monkeypatch):
    user("a@x.com", {"RELIANCE.NS": 3.0}); seed_perf("RELIANCE.NS")
    assert briefs.run_daily(P(), datetime(2026, 11, 20, 1, 0, tzinfo=timezone.utc))["skipped"] == "too early" and not keyed
    briefs.run_daily(P(), IN_OPEN)
    assert briefs.run_daily(P(), IN_OPEN + timedelta(hours=1))["skipped"] == "done" and len(keyed) == 1
    assert db.brief_run_done(DAY) and not db.brief_run_done("2026-11-21")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    assert briefs.run_daily(P(), IN_OPEN + timedelta(days=1))["skipped"] == "no key"


def test_the_next_day_researches_again(keyed):
    user("a@x.com", {"RELIANCE.NS": 3.0}); seed_perf("RELIANCE.NS")
    briefs.run_daily(P(), IN_OPEN); briefs.run_daily(P(), IN_OPEN + timedelta(days=1))
    assert len(keyed) == 2


def test_a_brief_below_the_bar_is_recorded_but_never_worthy(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setattr(briefs, "ask", lambda f: (json.dumps(good(material=config.BRIEF_MIN_MATERIAL - 1)), URLS, CITED))
    user("a@x.com", {"RELIANCE.NS": 3.0}); seed_perf("RELIANCE.NS")
    assert briefs.run_daily(P(), IN_OPEN)["worthy"] == 0
    b = db.get_brief("RELIANCE.NS", DAY)
    assert b["worthy"] is False and b["headline"] is None and b["body"] == {}
    assert db.worthy_briefs(["RELIANCE.NS"], DAY) == {}


def test_a_reply_that_fails_the_checks_is_not_worthy(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key-not-real")
    bad = good(headline="A great time to buy Reliance", points=[{"t": "Analysts say you should buy Reliance now", "u": "https://economictimes.com/a"}])
    monkeypatch.setattr(briefs, "ask", lambda f: (json.dumps(bad), URLS, CITED))
    user("a@x.com", {"RELIANCE.NS": 3.0}); seed_perf("RELIANCE.NS")
    assert briefs.run_daily(P(), IN_OPEN)["worthy"] == 0 and db.worthy_briefs(["RELIANCE.NS"], DAY) == {}


def test_a_bad_headline_alone_does_not_cost_a_sourced_brief_and_never_reaches_the_page(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setattr(briefs, "ask", lambda f: (json.dumps(good(headline="A great time to buy Reliance")), URLS, CITED))
    user("a@x.com", {"RELIANCE.NS": 3.0}); seed_perf("RELIANCE.NS")
    assert briefs.run_daily(P(), IN_OPEN)["worthy"] == 1
    saved = db.worthy_briefs(["RELIANCE.NS"], DAY)["RELIANCE.NS"]
    assert "buy" not in saved["headline"].lower() and saved["headline"] == "Net profit came in at 18540 crore"


def test_repeated_failures_stop_the_run_and_it_resumes_next_hour(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key-not-real")
    calls = []
    monkeypatch.setattr(briefs, "ask", lambda f: calls.append(f))            # every request fails
    syms = ["RELIANCE.NS", "TCS.NS", "INFY.NS", "AAPL", "MSFT"]
    user("a@x.com", {s: 3.0 for s in syms})
    for s in syms:
        seed_perf(s)
    briefs.run_daily(P(), IN_OPEN)
    assert len(calls) == config.BRIEF_MAX_FAILURES and not db.brief_run_done(DAY)       # stopped early, not marked done
    monkeypatch.setattr(briefs, "ask", lambda f: (json.dumps(good()), URLS, CITED))
    briefs.run_daily(P(), IN_OPEN + timedelta(hours=1))
    assert db.brief_run_done(DAY)


def test_a_long_run_pauses_at_its_time_budget_and_resumes(keyed, monkeypatch):
    monkeypatch.setattr(config, "BRIEF_TIME_BUDGET_SECONDS", -1)             # budget already used up
    user("a@x.com", {"RELIANCE.NS": 3.0, "AAPL": 3.0}); seed_perf("RELIANCE.NS"); seed_perf("AAPL")
    out = briefs.run_daily(P(), IN_OPEN)
    assert out["researched"] == 0 and not keyed and not db.brief_run_done(DAY)
    monkeypatch.setattr(config, "BRIEF_TIME_BUDGET_SECONDS", 480)
    assert briefs.run_daily(P(), IN_OPEN + timedelta(minutes=20))["researched"] == 2 and db.brief_run_done(DAY)


def test_the_workflow_passes_the_key_and_documents_it():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    assert "ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}" in (root / ".github/workflows/check.yml").read_text()
    assert "ANTHROPIC_API_KEY" in (root / "README.md").read_text()


def test_consensus_is_fetched_only_for_stocks_being_researched(keyed):
    user("a@x.com", {"RELIANCE.NS": 3.0, "TCS.NS": 3.0}); seed_perf("RELIANCE.NS"); seed_perf("TCS.NS", sigs=())
    p = P()
    briefs.run_daily(p, IN_OPEN)
    assert p.analyst_calls == [["RELIANCE.NS"]]
    assert db.get_brief("RELIANCE.NS", DAY)["body"]["consensus"]["mean"] == 1650.0


def test_run_check_does_the_research_after_alerts_and_survives_failure(keyed, monkeypatch):
    user("a@x.com", {"RELIANCE.NS": 1.0}); seed_perf("RELIANCE.NS")
    out = Outbox()
    s = run_check(P_with_quote(), out, now=IN_OPEN)
    assert s["briefs"]["worthy"] == 1 and len(out.sent) == 1                  # the dip alert still went out first
    monkeypatch.setattr(briefs, "run_daily", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    s2 = run_check(P_with_quote(), Outbox(), now=IN_OPEN + timedelta(hours=1))
    assert s2["briefs"]["skipped"] == "error" and s2["brief_emails"] == 0


def P_with_quote():
    p = P()
    p.quotes = {"RELIANCE.NS": q("RELIANCE.NS", -3)}
    return p


# ---------------- email ----------------

def make_brief(sym="RELIANCE.NS", worthy=True, day=DAY):
    db.save_brief(sym, day, worthy, 8 if worthy else 3, "Reliance posts quarterly profit of 18540 crore" if worthy else None,
                  {"points": [{"t": "Net profit came in at 18540 crore", "u": "https://economictimes.com/a", "d": "economictimes.com"}],
                   "targets": [{"by": "Motilal Oswal", "p": 1650.0, "u": "https://www.moneycontrol.com/b", "d": "moneycontrol.com"}],
                   "consensus": {"mean": 1650.0, "low": 1400.0, "high": 1900.0},
                   "signals": [{"k": "high52", "text": "At a 52-week high", "s": 3}]} if worthy else None)


def opted_in(email="a@x.com", subs=None):
    u = user(email, subs or {"RELIANCE.NS": 3.0})
    db.set_brief_email(u["id"], True)
    return u


def mark_run():
    db.save_brief(db.RUN_MARK, DAY, False, 0, None, None)


def test_the_brief_email_goes_only_to_people_who_opted_in_once_a_day_when_something_is_notable():
    opted_in("yes@x.com"); user("no@x.com", {"RELIANCE.NS": 3.0})
    seed_perf("RELIANCE.NS"); make_brief(); mark_run()
    from buydips.checker import _send_brief_emails
    out = Outbox()
    assert _send_brief_emails(out, IN_OPEN) == 1 and [m[0] for m in out.sent] == ["yes@x.com"]
    assert _send_brief_emails(out, IN_OPEN + timedelta(hours=1)) == 0 and len(out.sent) == 1        # not twice in a day


def test_a_quiet_day_sends_nothing_and_a_failed_send_is_retried():
    opted_in(); seed_perf("RELIANCE.NS"); make_brief(worthy=False); mark_run()
    from buydips.checker import _send_brief_emails
    out = Outbox()
    assert _send_brief_emails(out, IN_OPEN) == 0 and not out.sent
    make_brief()
    bad = Outbox(ok=False)
    assert _send_brief_emails(bad, IN_OPEN) == 0
    good_out = Outbox()
    assert _send_brief_emails(good_out, IN_OPEN + timedelta(hours=1)) == 1                          # tried again next run


def test_nothing_is_sent_before_the_days_research_is_complete_or_for_unfollowed_stocks():
    opted_in(); seed_perf("RELIANCE.NS"); make_brief()
    from buydips.checker import _send_brief_emails
    assert _send_brief_emails(Outbox(), IN_OPEN) == 0                                               # run not marked done yet
    mark_run(); make_brief("TCS.NS")
    out = Outbox()
    assert _send_brief_emails(out, IN_OPEN) == 1 and "Tata" not in out.sent[0][2]                   # only what they follow


def test_the_brief_email_content():
    seed_perf("RELIANCE.NS"); make_brief()
    v = briefs.view("RELIANCE.NS", db.get_brief("RELIANCE.NS", DAY), db.perf_for(["RELIANCE.NS"])["RELIANCE.NS"])
    subject, text, html = emails.brief([v], "https://buydips.in/unsubscribe/x")
    assert subject == "buyDips brief: Reliance posts quarterly profit of 18540 crore"
    assert "https://economictimes.com/a" in text and "Reported target: Motilal Oswal ₹1,650" in text and "Written by AI" in text
    assert f'href="{v["url"]}"' in html and f'src="{v["url"]}/chart.png"' in html and 'href="https://economictimes.com/a"' in html
    assert "in the news" in html and "not investment advice" in html.lower() and "Stop brief emails" in html
    assert "automated price alert" not in html and "news come from third-party sources" in html
    assert "buy" not in subject.lower().replace("buydips", "")
    two = emails.brief([v, {**v, "name": "Apple", "symbol": "AAPL", "headline": "Apple supplier update"}], "https://x/u")
    assert two[0] == "buyDips brief: 2 of your stocks have news"
    evil = emails.brief([{**v, "headline": "<script>alert(1)</script>"}], "https://x/u")[2]
    assert "<script>" not in evil


def test_a_dip_alert_carries_the_reason_when_a_notable_brief_exists():
    user("a@x.com", {"RELIANCE.NS": 1.0}); seed_perf("RELIANCE.NS"); make_brief()
    seen = []
    def send(to, subject, body, **kw):
        seen.append((subject, body, kw.get("html", "")))
        return True
    run_check(P_with_quote(), send, now=IN_OPEN)
    subject, body, html = seen[0]
    assert "What's behind it: Reliance posts quarterly profit" in body and "What&rsquo;s behind it" in html
    assert "economictimes.com" in html and "AI-written from the sources linked" in html


def test_a_dip_alert_without_a_brief_is_unchanged():
    user("a@x.com", {"RELIANCE.NS": 1.0})
    seen = []
    run_check(P_with_quote(), lambda to, s, b, **kw: seen.append((b, kw.get("html", ""))) or True, now=IN_OPEN)
    assert "What's behind it" not in seen[0][0] and "behind it" not in seen[0][1]


# ---------------- pages ----------------

def test_the_ai_page_shows_todays_brief_or_an_honest_empty_state(client):
    sign_in(client)
    uid = db.get_or_create_user("a@x.com")["id"]
    db.set_subscriptions(uid, {"RELIANCE.NS": 3.0}); seed_perf("RELIANCE.NS")
    html = client.get("/performance").get_data(as_text=True)
    assert "Your brief" in html and "Nothing notable today" in html and "bcard" not in html
    make_brief(day=datetime.now(timezone.utc).date().isoformat())
    html = client.get("/performance").get_data(as_text=True)
    assert "Reliance posts quarterly profit" in html and "economictimes.com" in html and "Motilal Oswal" in html
    assert "Nothing notable today" not in html and 'class="chart-svg' in html and "data-share" in html


def test_a_brief_for_a_stock_you_do_not_follow_is_not_on_your_page(client):
    sign_in(client)
    db.set_subscriptions(db.get_or_create_user("a@x.com")["id"], {"TCS.NS": 3.0}); seed_perf("TCS.NS")
    make_brief(day=datetime.now(timezone.utc).date().isoformat())
    assert "Reliance posts" not in client.get("/performance").get_data(as_text=True)


def test_the_public_brief_page_exists_only_for_notable_briefs_and_is_not_indexed(client):
    seed_perf("RELIANCE.NS"); make_brief(); make_brief("TCS.NS", worthy=False); seed_perf("TCS.NS")
    anon = client.application.test_client()
    r = anon.get(f"/brief/{DAY}/RELIANCE.NS")
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and r.headers["X-Robots-Tag"] == "noindex" and "Reliance posts quarterly profit" in body
    assert f"/brief/{DAY}/RELIANCE.NS/link.png" in body and "not investment advice" in body.lower()
    for path in (f"/brief/{DAY}/TCS.NS", f"/brief/{DAY}/NOPE", f"/brief/2026-01-01/RELIANCE.NS", "/brief/not-a-date/RELIANCE.NS"):
        assert anon.get(path).status_code == 404
    assert anon.get(f"/brief/{DAY}/TCS.NS/story.png").status_code == 404                # never an image of a non-notable brief


def test_the_brief_images_render_at_the_right_sizes(client):
    seed_perf("RELIANCE.NS"); make_brief()
    anon = client.application.test_client()
    for kind, size in (("story", (1080, 1350)), ("link", (1200, 630)), ("chart", (1000, 300))):
        r = anon.get(f"/brief/{DAY}/RELIANCE.NS/{kind}.png")
        assert r.status_code == 200 and r.mimetype == "image/png" and r.headers["X-Robots-Tag"] == "noindex"
        assert Image.open(BytesIO(r.data)).size == size
    assert anon.get(f"/brief/{DAY}/RELIANCE.NS/other.png").status_code == 404
    assert "attachment" in anon.get(f"/brief/{DAY}/RELIANCE.NS/story.png?download=1").headers["Content-Disposition"]


def test_an_ampersand_symbol_works_in_links(client):
    seed_perf("M&M.NS"); make_brief("M&M.NS")
    anon = client.application.test_client()
    assert anon.get(f"/brief/{DAY}/M%26M.NS").status_code == 200
    assert anon.get(f"/brief/{DAY}/M%26M.NS/story.png").status_code == 200


def test_a_brief_without_price_history_still_has_a_page_but_no_chart(client):
    seed_perf("RELIANCE.NS", series=False); make_brief()
    anon = client.application.test_client()
    r = anon.get(f"/brief/{DAY}/RELIANCE.NS")
    assert r.status_code == 200 and "chart-svg" not in r.get_data(as_text=True)
    assert anon.get(f"/brief/{DAY}/RELIANCE.NS/story.png").status_code == 404


def test_the_settings_toggle_turns_the_brief_email_on_and_off(client):
    sign_in(client)
    uid = db.get_or_create_user("a@x.com")["id"]
    assert "Daily brief by email" in client.get("/settings").get_data(as_text=True)
    t = csrf(client, "/settings")
    client.post("/settings/brief", data={"csrf": t, "on": "1"})
    assert db.users_for_brief_email("2000-01-01") == [{"user_id": uid, "email": "a@x.com"}]
    assert "Daily brief emails are on" in client.get("/settings?changed=brief_on").get_data(as_text=True)
    client.post("/settings/brief", data={"csrf": t})
    assert db.users_for_brief_email("2000-01-01") == []
    assert client.post("/settings/brief", data={"on": "1"}).status_code == 400                # needs CSRF


def test_the_ai_icon_page_title_and_privacy_describe_the_new_flow(client):
    sign_in(client)
    assert "Your brief" in client.get("/performance").get_data(as_text=True)
    priv = client.application.test_client().get("/privacy").get_data(as_text=True)
    assert "doesn't know who follows what" in priv and "30 days" in priv


def test_figures_saved_before_charts_existed_are_refreshed_at_once_but_empty_ones_are_not_retried():
    db.save_perf({"RELIANCE.NS": {"last_date": "2026-11-20", "last_price": 100.0, "r1m": 2.0}})      # old row: a price, no series
    db.save_perf({"TCS.NS": None})                                                                    # looked, no usable history
    seed_perf("INFY.NS")                                                                              # complete row
    assert db.perf_stale(["RELIANCE.NS", "TCS.NS", "INFY.NS"]) == ["RELIANCE.NS"]


def test_a_dip_alert_never_carries_yesterdays_brief():
    user("a@x.com", {"RELIANCE.NS": 1.0}); seed_perf("RELIANCE.NS")
    make_brief(day=(IN_OPEN - timedelta(days=1)).date().isoformat())
    seen = []
    run_check(P_with_quote(), lambda to, s, b, **kw: seen.append((b, kw.get("html", ""))) or True, now=IN_OPEN)
    assert "What's behind it" not in seen[0][0] and "behind it" not in seen[0][1]


def _page_with_one_followed(client):
    sign_in(client)
    uid = db.get_or_create_user("a@x.com")["id"]
    db.set_subscriptions(uid, {"RELIANCE.NS": 3.0}); seed_perf("RELIANCE.NS")
    return datetime.now(timezone.utc).date().isoformat()


def test_the_empty_ai_page_says_which_state_it_is_in(client):
    today = _page_with_one_followed(client)
    html = client.get("/performance").get_data(as_text=True)
    assert "hasn't run yet" in html and "after about 8:30 AM IST" in html                       # nothing has run
    db.save_brief("RELIANCE.NS", today, False, 3, None, None)
    assert "still running" in client.get("/performance").get_data(as_text=True)                  # a stock done, run not complete
    db.save_brief(db.RUN_MARK, today, False, 0, None, None)
    html = client.get("/performance").get_data(as_text=True)
    assert "1 of your 1 stock stood out and was researched, but none had news worth showing" in html
    db.save_perf({"RELIANCE.NS": None})
    db.set_subscriptions(db.get_or_create_user("a@x.com")["id"], {"RELIANCE.NS": 3.0, "TCS.NS": 3.0}); seed_perf("RELIANCE.NS"); seed_perf("TCS.NS")
    with db.engine().begin() as conn:
        from sqlalchemy import delete
        conn.execute(delete(db.briefs).where(db.briefs.c.symbol == "RELIANCE.NS"))
    html = client.get("/performance").get_data(as_text=True)
    assert "None of the 2 stocks you follow had an unusual move" in html                         # ran, nothing stood out


def test_the_page_never_claims_a_run_that_did_not_happen_for_yesterday(client):
    _page_with_one_followed(client)
    db.save_brief(db.RUN_MARK, (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat(), False, 0, None, None)
    assert "hasn't run yet" in client.get("/performance").get_data(as_text=True)


def test_thinking_blocks_between_searches_are_ignored_and_only_the_answer_is_used(monkeypatch):
    reply = SimpleNamespace(stop_reason="end_turn",
                            content=[SimpleNamespace(type="thinking", text="Let me search for that."),
                                     SimpleNamespace(type="text", text='{"material": 7}')],
                            model_dump=lambda: {"content": []}, usage=SimpleNamespace(input_tokens=1, output_tokens=1))
    monkeypatch.setattr(briefs, "_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: reply)))
    assert briefs.ask("x")[0] == '{"material": 7}'


def test_funds_get_no_analyst_lookup_but_are_still_researched(keyed):
    user("a@x.com", {"GOLDBEES.NS": 2.0, "RELIANCE.NS": 3.0}); seed_perf("GOLDBEES.NS"); seed_perf("RELIANCE.NS")
    p = P()
    out = briefs.run_daily(p, IN_OPEN)
    assert out["researched"] == 2 and p.analyst_calls == [["RELIANCE.NS"]]


def test_the_larger_models_get_the_newer_search_tool_and_their_own_thinking_setting(monkeypatch):
    sent = {}
    monkeypatch.setattr(config, "BRIEF_MODEL", "claude-sonnet-5-5")
    monkeypatch.setattr(briefs, "_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: sent.update(kw) or fake_response())))
    briefs.ask("x")
    assert sent["thinking"] == {"type": "between_tools"} and sent["tools"][0]["type"] == "web_search_20260209"


def test_a_thinking_setting_the_model_refuses_is_retried_once_with_the_other(monkeypatch):
    class BadRequestError(Exception):
        pass
    seen = []
    def create(**kw):
        seen.append(kw["thinking"]["type"])
        if len(seen) == 1:
            raise BadRequestError("To turn thinking off on this model, send between_tools instead")
        return fake_response()
    monkeypatch.setattr(briefs, "_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=create)))
    assert briefs.ask("x") is not None and seen == ["disabled", "between_tools"]
    seen.clear()
    def always_bad(**kw):
        seen.append(1)
        raise BadRequestError("something else is wrong")
    monkeypatch.setattr(briefs, "_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=always_bad)))
    assert briefs.ask("x") is None and len(seen) == 1                       # an unrelated 400 is not retried


def test_the_run_reports_what_it_spent(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key-not-real")
    def create(**kw):
        r = fake_response(text=json.dumps(good()), urls=tuple(URLS), cited=CITED)
        r.usage = SimpleNamespace(input_tokens=9000, output_tokens=300, server_tool_use=SimpleNamespace(web_search_requests=1))
        return r
    monkeypatch.setattr(briefs, "_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=create)))
    user("a@x.com", {"RELIANCE.NS": 3.0, "AAPL": 3.0}); seed_perf("RELIANCE.NS"); seed_perf("AAPL")
    out = briefs.run_daily(P(), IN_OPEN)
    assert (out["tokens_in"], out["tokens_out"], out["searches"]) == (18000, 600, 2)


def test_validate_reports_why_things_were_dropped(monkeypatch):
    raw = good(points=[{"t": "Net profit came in at 18540 crore", "u": "https://economictimes.com/a"},
                       {"t": "Investors should buy more", "u": "https://economictimes.com/a"},
                       {"t": "Revenue was 99999 crore", "u": "https://economictimes.com/a"},
                       {"t": "Invented", "u": "https://invented.example"}])
    report = {}
    monkeypatch.setattr(config, "BRIEF_STRICT_NUMBERS", True)
    b = briefs.validate(raw, URLS, CITED, FACTS, report)
    assert len(b["points"]) == 1 and report["kept"] == 1 and report["seen"] == 3        # only the first three are read
    assert report["why"] == {"advice_words:should": 1, "unsupported_number": 1} and report["material"] == 8
    report2 = {}
    briefs.validate(good(points=[{"t": "x", "u": "https://invented.example"}]), URLS, CITED, FACTS, report2)
    assert report2["why"] == {"not_in_search_results": 1}


def test_a_search_result_headline_counts_as_quoted_text_for_checking_numbers():
    urls, cited = set(), []
    briefs._walk({"content": [{"type": "web_search_result", "url": "https://economictimes.com/a",
                               "title": "Reliance Q2 profit rises 12% to Rs 18,540 crore"}]}, urls, cited)
    assert urls == {"https://economictimes.com/a"} and "18,540" in " ".join(cited)
    raw = good(points=[{"t": "Quarterly profit rose 12 percent", "u": "https://economictimes.com/a"}], targets=[])
    assert briefs.validate(raw, urls, " ".join(cited), FACTS)["points"][0]["t"] == "Quarterly profit rose 12 percent"


def test_the_log_says_what_happened_to_each_brief_without_secrets(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="buyDips.briefs")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key-not-real")
    replies = iter([(json.dumps(good(points=[{"t": "Investors should buy now", "u": "https://economictimes.com/a"}])), URLS, CITED),
                    ("sorry, I could not find anything", URLS, CITED)])
    monkeypatch.setattr(briefs, "ask", lambda f: next(replies))
    user("a@x.com", {"RELIANCE.NS": 3.0, "AAPL": 3.0}); seed_perf("RELIANCE.NS"); seed_perf("AAPL")
    briefs.run_daily(P(), IN_OPEN)
    assert "points kept 0 of 1" in caplog.text and "advice_words:should" in caplog.text and "not valid JSON" in caplog.text
    assert "test-key-not-real" not in caplog.text and "a@x.com" not in caplog.text


def test_a_manual_forced_run_researches_again_even_when_the_day_is_done(keyed, monkeypatch):
    user("a@x.com", {"RELIANCE.NS": 3.0}); seed_perf("RELIANCE.NS")
    briefs.run_daily(P(), IN_OPEN)
    assert briefs.run_daily(P(), IN_OPEN + timedelta(hours=1))["skipped"] == "done" and len(keyed) == 1
    monkeypatch.setattr(config, "BRIEF_FORCE", True)
    assert briefs.run_daily(P(), IN_OPEN + timedelta(hours=2))["researched"] == 1 and len(keyed) == 2
    assert config.BRIEF_FORCE is True


def test_forcing_is_off_by_default_and_only_the_manual_workflow_input_turns_it_on():
    import pathlib
    wf = (pathlib.Path(__file__).resolve().parent.parent / ".github/workflows/check.yml").read_text()
    assert "BRIEF_FORCE: ${{ inputs.force_brief }}" in wf and "default: false" in wf
    import importlib
    import os
    os.environ.pop("BRIEF_FORCE", None)
    importlib.reload(config)
    assert config.BRIEF_FORCE is False
