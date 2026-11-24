from datetime import datetime, timezone

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


# Fri 20 Nov 2026: 10:30 IST (India open) and 11:00 EST (US open). Mon 23 Nov: 10:30 IST.
IN_OPEN = datetime(2026, 11, 20, 5, 0, tzinfo=timezone.utc)
US_OPEN = datetime(2026, 11, 20, 16, 0, tzinfo=timezone.utc)
IN_MON = datetime(2026, 11, 23, 5, 0, tzinfo=timezone.utc)


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
    run_check(FakeProvider({"^NSEI": q("^NSEI", -1.5)}), out, now=IN_OPEN)
    assert [m[0] for m in out.sent] == ["a@x.com"]


def test_fetches_each_symbol_once_for_all_users():
    for i in range(5):
        user(f"u{i}@x.com", {"^DJI": 1.0, "^GSPC": 1.0})
    p = FakeProvider({"^DJI": q("^DJI", 0.1), "^GSPC": q("^GSPC", 0.1)})
    run_check(p, Outbox(), now=US_OPEN)
    assert p.requested == [["^DJI", "^GSPC"]]


def test_one_alert_per_session_then_again_next_session():
    user("a@x.com", {"^NSEI": 1.0})
    out = Outbox()
    run_check(FakeProvider({"^NSEI": q("^NSEI", -2)}), out, now=IN_OPEN)
    run_check(FakeProvider({"^NSEI": q("^NSEI", -2)}), out, now=IN_OPEN)
    assert len(out.sent) == 1
    run_check(FakeProvider({"^NSEI": q("^NSEI", -2, session="2026-11-23")}), out, now=IN_MON)
    assert len(out.sent) == 2


def test_multiple_dips_are_one_email_per_user():
    user("a@x.com", {"^IXIC": 1.0, "^GSPC": 1.0, "^DJI": 1.0})
    out = Outbox()
    run_check(FakeProvider({"^IXIC": q("^IXIC", -2), "^GSPC": q("^GSPC", -3), "^DJI": q("^DJI", 1)}), out, now=US_OPEN)
    assert len(out.sent) == 1
    assert "2 of your watchlist" in out.sent[0][1]
    assert out.sent[0][3].startswith("http")  # unsubscribe link present


def test_failed_send_is_retried_next_run():
    user("a@x.com", {"^NSEI": 1.0})
    p = FakeProvider({"^NSEI": q("^NSEI", -2)})
    run_check(p, Outbox(ok=False), now=IN_OPEN)
    out = Outbox()
    run_check(p, out, now=IN_OPEN)
    assert len(out.sent) == 1


def test_unsubscribed_users_get_nothing():
    u = user("a@x.com", {"^NSEI": 1.0})
    db.unsubscribe_user(u["id"])
    out = Outbox()
    run_check(FakeProvider({"^NSEI": q("^NSEI", -5)}), out, now=IN_OPEN)
    assert out.sent == []


def test_run_fails_loudly_when_most_prices_missing():
    user("a@x.com", {"^IXIC": 1.0, "^GSPC": 1.0, "^DJI": 1.0})
    with pytest.raises(DataFetchError):
        run_check(FakeProvider({}), Outbox(), now=US_OPEN)


def test_no_advice_wording_in_alert():
    user("a@x.com", {"^NSEI": 1.0})
    out = Outbox()
    run_check(FakeProvider({"^NSEI": q("^NSEI", -2)}), out, now=IN_OPEN)
    assert "buy the dip" not in out.sent[0][2].lower()
    assert "not investment advice" in out.sent[0][2].lower()


# ---- market hours and freshness (the "alert after midnight IST" bug) ----

def utc(day, hh, mm=0):
    return datetime(2026, 11, day, hh, mm, tzinfo=timezone.utc)


def test_nothing_is_fetched_or_sent_while_the_market_is_closed():
    user("a@x.com", {"^NSEI": 1.0})
    p, out = FakeProvider({"^NSEI": q("^NSEI", -5, session="2026-11-23")}), Outbox()
    # Mon 23 Nov 19:01 UTC = Tue 00:31 IST: India has been closed for hours.
    summary = run_check(p, out, now=utc(23, 19, 1))
    assert p.requested == [] and out.sent == [] and summary["skipped_closed"] == 1


def test_stale_session_is_never_alerted_even_in_hours():
    user("a@x.com", {"^NSEI": 1.0})
    out = Outbox()
    # Monday 10:30 IST, but Yahoo still returns Friday's bar: must not alert.
    run_check(FakeProvider({"^NSEI": q("^NSEI", -5, session="2026-11-20")}), out, now=IN_MON)
    assert out.sent == []


def test_holiday_or_missing_todays_bar_sends_nothing():
    user("a@x.com", {"^NSEI": 1.0})
    out = Outbox()
    run_check(FakeProvider({"^NSEI": q("^NSEI", -5, session="2026-11-19")}), out, now=IN_OPEN)
    assert out.sent == []


def test_only_open_markets_are_fetched_for_a_mixed_watchlist():
    user("a@x.com", {"^NSEI": 1.0, "^GSPC": 1.0})
    p, out = FakeProvider({"^NSEI": q("^NSEI", -2), "^GSPC": q("^GSPC", -2)}), Outbox()
    run_check(p, out, now=IN_OPEN)  # India open, US closed
    assert p.requested == [["^NSEI"]]
    assert "Nifty" in out.sent[0][2] and "S&P" not in out.sent[0][2]


def test_window_is_open_to_one_hour_after_the_close_and_never_on_weekends():
    from buydips import markets

    # India closes 15:30 IST = 10:00 UTC. 16:25 IST is inside the grace hour, 16:35 IST is not.
    assert markets.is_live("^NSEI", utc(20, 10, 55)) and not markets.is_live("^NSEI", utc(20, 11, 5))
    # Opens 09:15 IST = 03:45 UTC.
    assert not markets.is_live("^NSEI", utc(20, 3, 40)) and markets.is_live("^NSEI", utc(20, 3, 50))
    # Saturday and Sunday, mid-morning IST.
    assert not markets.is_live("^NSEI", utc(21, 5, 0)) and not markets.is_live("^NSEI", utc(22, 5, 0))
    # Unknown symbols are never treated as live.
    assert not markets.is_live("NOPE", utc(20, 5, 0))


def test_us_market_follows_new_york_time():
    user("a@x.com", {"^GSPC": 1.0})
    p, out = FakeProvider({"^GSPC": q("^GSPC", -2)}), Outbox()
    run_check(p, out, now=utc(20, 4, 0))  # 04:00 UTC Fri = 23:00 ET Thu: closed
    assert p.requested == []
    run_check(p, out, now=utc(20, 15, 0))  # 10:00 ET Fri: open
    assert len(out.sent) == 1


def test_market_holidays_are_skipped():
    from buydips import markets

    # Tue 24 Nov 2026 is an NSE holiday: India is closed all day, the US is not.
    assert not markets.is_live("^NSEI", utc(24, 5, 0))
    assert markets.is_live("^NSEI", utc(25, 5, 0))
    # Thanksgiving, Thu 26 Nov: NYSE closed, NSE open.
    assert not markets.is_live("^GSPC", utc(26, 16, 0)) and markets.is_live("^NSEI", utc(26, 5, 0))
    user("a@x.com", {"^NSEI": 1.0})
    p, out = FakeProvider({"^NSEI": q("^NSEI", -5, session="2026-11-24")}), Outbox()
    run_check(p, out, now=utc(24, 5, 0))
    assert p.requested == [] and out.sent == []


def test_every_holiday_is_a_real_weekday_date():
    from datetime import date

    from buydips import markets

    for market, days in markets.HOLIDAYS.items():
        for d in days:
            assert date.fromisoformat(d).weekday() < 5, (market, d)
    assert markets.missing_holiday_year(utc(24, 5, 0)) is None
    assert markets.missing_holiday_year(datetime(2027, 1, 4, tzinfo=timezone.utc)) == 2027


def test_unsupported_symbols_are_reported_not_silently_dropped():
    user("a@x.com", {"^N225": 1.0, "^NSEI": 1.0})
    p, out = FakeProvider({"^NSEI": q("^NSEI", -2)}), Outbox()
    s = run_check(p, out, now=IN_OPEN)
    assert s["unknown_symbols"] == ["^N225"] and s["skipped_closed"] == 0
    assert p.requested == [["^NSEI"]] and len(out.sent) == 1


# ---- buckets: since-added performance and the one-time red flag ----

def bucket(email, symbol, added_price, name="Tech"):
    u = db.get_or_create_user(email)
    bid = db.create_bucket(u["id"], name)
    db.add_bucket_item(bid, symbol, added_price)
    return u, bid


def bq(symbol, price, session="2026-11-20"):
    return Quote(symbol, price, price, 0.0, session)


def test_red_flag_email_goes_out_once_for_a_stock_down_ten_percent():
    bucket("a@x.com", "RELIANCE.NS", 100.0)
    out = Outbox()
    s = run_check(FakeProvider({"RELIANCE.NS": bq("RELIANCE.NS", 85.0)}), out, now=IN_OPEN)
    assert s["flag_emails"] == 1 and len(out.sent) == 1
    assert "Reliance" in out.sent[0][1] and "down 15.0% since you added it" in out.sent[0][1]
    run_check(FakeProvider({"RELIANCE.NS": bq("RELIANCE.NS", 80.0)}), out, now=IN_OPEN)   # worse: still no second email
    run_check(FakeProvider({"RELIANCE.NS": bq("RELIANCE.NS", 99.0)}), out, now=IN_OPEN)   # recovered...
    run_check(FakeProvider({"RELIANCE.NS": bq("RELIANCE.NS", 70.0)}), out, now=IN_OPEN)   # ...and fell again: still only once
    assert len(out.sent) == 1


def test_flag_line_is_exactly_ten_percent():
    bucket("a@x.com", "TCS.NS", 100.0)
    out = Outbox()
    run_check(FakeProvider({"TCS.NS": bq("TCS.NS", 90.01)}), out, now=IN_OPEN)  # down 9.99%
    assert out.sent == []
    run_check(FakeProvider({"TCS.NS": bq("TCS.NS", 90.0)}), out, now=IN_OPEN)   # down 10.00%
    assert len(out.sent) == 1


def test_no_flag_from_stale_data_or_a_closed_market():
    bucket("a@x.com", "RELIANCE.NS", 100.0)
    out = Outbox()
    run_check(FakeProvider({"RELIANCE.NS": bq("RELIANCE.NS", 50.0, session="2026-11-19")}), out, now=IN_OPEN)  # yesterday's bar
    run_check(FakeProvider({"RELIANCE.NS": bq("RELIANCE.NS", 50.0, session="2026-11-23")}), out, now=utc(23, 19, 1))  # 00:31 IST, closed
    assert out.sent == []


def test_flags_are_per_stock_and_grouped_into_one_email_per_user():
    u, bid = bucket("a@x.com", "RELIANCE.NS", 100.0)
    db.add_bucket_item(bid, "TCS.NS", 100.0)
    db.add_bucket_item(bid, "INFY.NS", 100.0)
    out = Outbox()
    run_check(FakeProvider({"RELIANCE.NS": bq("RELIANCE.NS", 80.0), "TCS.NS": bq("TCS.NS", 95.0), "INFY.NS": bq("INFY.NS", 60.0)}),
              out, now=IN_OPEN)
    assert len(out.sent) == 1 and "2 stocks in your buckets" in out.sent[0][1]
    assert "TCS" not in out.sent[0][2]  # down only 5%


def test_paused_users_get_no_flag_email_and_failed_sends_are_retried():
    u, _ = bucket("a@x.com", "RELIANCE.NS", 100.0)
    p = FakeProvider({"RELIANCE.NS": bq("RELIANCE.NS", 80.0)})
    run_check(p, Outbox(ok=False), now=IN_OPEN)           # send fails: not marked as flagged
    out = Outbox()
    run_check(p, out, now=IN_OPEN)
    assert len(out.sent) == 1
    u2, _ = bucket("b@x.com", "RELIANCE.NS", 100.0)
    db.set_active(u2["id"], False)
    out2 = Outbox()
    run_check(p, out2, now=IN_OPEN)
    assert [m[0] for m in out2.sent] == []                # a@ already flagged; b@ is paused


def test_a_new_stock_gets_its_starting_price_from_the_next_sync_even_after_hours():
    u, bid = bucket("a@x.com", "RELIANCE.NS", None)       # added when no price was known
    out = Outbox()
    run_check(FakeProvider({"RELIANCE.NS": bq("RELIANCE.NS", 120.0, session="2026-11-23")}), out, now=utc(23, 19, 1))
    item = db.bucket_items_for([bid])[bid][0]
    assert item["added_price"] == 120.0 and out.sent == []
    assert db.quotes_for(["RELIANCE.NS"])["RELIANCE.NS"]["price"] == 120.0   # and the price is cached for the pages


def test_bucket_stocks_in_a_closed_market_are_not_fetched_once_priced():
    bucket("a@x.com", "RELIANCE.NS", 100.0)
    p = FakeProvider({"RELIANCE.NS": bq("RELIANCE.NS", 50.0)})
    run_check(p, Outbox(), now=utc(23, 19, 1))
    assert p.requested == []
