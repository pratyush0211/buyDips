import pytest

from buydips import db
from buydips.checker import DataFetchError, run_check
from buydips.data import Quote


class FakeProvider:
    def __init__(self, quotes):
        self.quotes = quotes
        self.requested = []

    def fetch(self, symbols):
        self.requested.append(sorted(symbols))
        return {s: q for s, q in self.quotes.items() if s in symbols}


def q(symbol, pct, session="2026-11-20"):
    return Quote(symbol, 100 + pct, 100.0, pct, session)


class Outbox:
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    def __call__(self, to, subject, body, unsubscribe_url=None, html=None):
        self.sent.append((to, subject, body, unsubscribe_url))
        self.html = html
        return self.ok


def user(email, subs):
    u = db.get_or_create_user(email)
    db.set_subscriptions(u["id"], subs)
    return u


def test_alerts_only_past_each_users_own_threshold():
    user("a@x.com", {"^NSEI": 1.0})
    user("b@x.com", {"^NSEI": 3.0})
    out = Outbox()
    run_check(FakeProvider({"^NSEI": q("^NSEI", -1.5)}), out)
    assert [m[0] for m in out.sent] == ["a@x.com"]


def test_fetches_each_symbol_once_for_all_users():
    for i in range(5):
        user(f"u{i}@x.com", {"^NSEI": 1.0, "^GSPC": 1.0})
    p = FakeProvider({"^NSEI": q("^NSEI", 0.1), "^GSPC": q("^GSPC", 0.1)})
    run_check(p, Outbox())
    assert p.requested == [["^GSPC", "^NSEI"]]


def test_one_alert_per_session_then_again_next_session():
    user("a@x.com", {"^NSEI": 1.0})
    out = Outbox()
    run_check(FakeProvider({"^NSEI": q("^NSEI", -2)}), out)
    run_check(FakeProvider({"^NSEI": q("^NSEI", -2)}), out)
    assert len(out.sent) == 1
    run_check(FakeProvider({"^NSEI": q("^NSEI", -2, session="2026-11-23")}), out)
    assert len(out.sent) == 2


def test_multiple_dips_are_one_email_per_user():
    user("a@x.com", {"^NSEI": 1.0, "^GSPC": 1.0, "^DJI": 1.0})
    out = Outbox()
    run_check(FakeProvider({"^NSEI": q("^NSEI", -2), "^GSPC": q("^GSPC", -3), "^DJI": q("^DJI", 1)}), out)
    assert len(out.sent) == 1
    assert "2 of your watchlist" in out.sent[0][1]
    assert out.sent[0][3].startswith("http")  # unsubscribe link present


def test_failed_send_is_retried_next_run():
    user("a@x.com", {"^NSEI": 1.0})
    p = FakeProvider({"^NSEI": q("^NSEI", -2)})
    run_check(p, Outbox(ok=False))
    out = Outbox()
    run_check(p, out)
    assert len(out.sent) == 1


def test_unsubscribed_users_get_nothing():
    u = user("a@x.com", {"^NSEI": 1.0})
    db.unsubscribe_user(u["id"])
    out = Outbox()
    run_check(FakeProvider({"^NSEI": q("^NSEI", -5)}), out)
    assert out.sent == []


def test_run_fails_loudly_when_most_prices_missing():
    user("a@x.com", {"^NSEI": 1.0, "^GSPC": 1.0, "^DJI": 1.0})
    with pytest.raises(DataFetchError):
        run_check(FakeProvider({}), Outbox())


def test_no_advice_wording_in_alert():
    user("a@x.com", {"^NSEI": 1.0})
    out = Outbox()
    run_check(FakeProvider({"^NSEI": q("^NSEI", -2)}), out)
    assert "buy the dip" not in out.sent[0][2].lower()
    assert "not investment advice" in out.sent[0][2].lower()
