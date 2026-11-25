"""One alert run: fetch each symbol whose market is open once, then email every user whose threshold
was crossed today. Each (user, symbol, trading session) is alerted at most once."""

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from . import briefs, buckets as bucketmath, catalog, config, db, emails, links, markets, performance
from .data import Provider, YahooProvider
from .mailer import send_email

log = logging.getLogger("buyDips.check")


class DataFetchError(RuntimeError):
    """Too many symbols failed to load; the run should be marked as failed."""


PERF_MAX_PER_RUN = 120   # symbols whose price history is fetched in one run (the rest follow on the next run)


def _refresh_performance(provider) -> int:
    """Once a day, store 1-week/1-month/3-month/1-year figures for every followed symbol and the two index yardsticks.
    Skipped for a provider with no history() (the tests' fake one)."""
    fetch_history = getattr(provider, "history", None)
    if fetch_history is None:
        return 0
    followed = {s["symbol"] for s in db.active_subscriptions() if s["symbol"] in catalog.BY_SYMBOL}
    stale = db.perf_stale(sorted(followed | set(performance.BENCH.values())))[:PERF_MAX_PER_RUN]
    if not stale:
        return 0
    got = fetch_history(stale)
    db.save_perf({s: performance.snapshot(got[s]) for s in stale if s in got})
    return len(got)


def run_check(
    provider: Optional[Provider] = None,
    send: Callable[..., bool] = send_email,
    now: Optional[datetime] = None,
) -> dict:
    provider = provider or YahooProvider()
    summary = _run_check(provider, send, now)
    try:                       # a failure here must never affect alerts, which have already been sent
        summary["performance_refreshed"] = _refresh_performance(provider)
    except Exception as exc:  # noqa: BLE001
        log.error("performance refresh failed: %s", exc)
        summary["performance_refreshed"] = 0
    try:                       # once a day: research what is notable, then email those who asked for the brief
        summary["briefs"] = briefs.run_daily(provider, now)
        summary["brief_emails"] = _send_brief_emails(send, now)
    except Exception as exc:  # noqa: BLE001
        log.error("daily brief failed: %s", exc)
        summary.setdefault("briefs", {"researched": 0, "worthy": 0, "skipped": "error"})
        summary.setdefault("brief_emails", 0)
    return summary


def _attach_briefs(hits: list, now: Optional[datetime]) -> None:
    """Give a dip alert the researched reason behind the fall, when today's brief for that stock is notable. Only
    today's: yesterday's news does not explain today's move."""
    today = (now or datetime.now(timezone.utc)).date().isoformat()
    found = db.worthy_briefs([h["symbol"] for h in hits], today)
    for h in hits:
        b = found.get(h["symbol"])
        if b:
            h["brief"] = {"headline": b["headline"], "points": b["body"].get("points", [])[:2], "url": briefs.url(h["symbol"], b["day"])}


def _send_brief_emails(send, now: Optional[datetime]) -> int:
    """One email per opted-in person on days a stock they follow has a notable brief. Nothing on quiet days."""
    now = now or datetime.now(timezone.utc)
    day = now.date().isoformat()
    if not db.brief_run_done(day):
        return 0
    people = {u["user_id"]: u for u in db.users_for_brief_email(day)}
    if not people:
        return 0
    held = defaultdict(set)
    for s in db.active_subscriptions():
        if s["user_id"] in people:
            held[s["user_id"]].add(s["symbol"])
    today = db.worthy_briefs(set().union(*held.values()) if held else set(), day)
    perf = db.perf_for(list(today))
    sent = 0
    for uid, symbols in held.items():
        items = sorted((briefs.view(s, today[s], perf.get(s)) for s in symbols if s in today and today[s]["day"] == day),
                       key=lambda v: -v["material"])
        if not items:
            continue
        unsub = links.unsubscribe_url(uid)
        subject, body, html = emails.brief(items, unsub)
        if send(people[uid]["email"], subject, body, unsubscribe_url=unsub, html=html):
            db.mark_brief_sent(uid, day)
            sent += 1
    return sent


def _run_check(provider, send, now) -> dict:
    now = now or datetime.now(timezone.utc)
    stale_year = markets.missing_holiday_year(now)
    if stale_year:
        log.warning("holidays.json has no dates for %s: market holidays are not being skipped", stale_year)
    all_subs = db.active_subscriptions()
    # Only symbols whose own market is open right now (or just closed) are looked at.
    subs = [s for s in all_subs if markets.is_live(s["symbol"], now)]
    sub_symbols = {s["symbol"] for s in subs}
    # Bucket stocks are tracked too: the ones whose market is open, plus any that have no price yet (so a
    # newly added stock gets its starting price even after hours).
    bucket_symbols, unpriced = db.bucket_symbol_sets()
    live_bucket = {sym for sym in bucket_symbols if markets.is_live(sym, now)}
    symbols = sorted(sub_symbols | live_bucket | unpriced)
    unknown = sorted({s["symbol"] for s in all_subs if markets.market_of(s["symbol"]) is None})
    if unknown:
        log.warning("Ignoring subscriptions to symbols that are not in the catalog: %s", ", ".join(unknown))
    summary = {"users": len({s["user_id"] for s in subs}), "symbols": len(symbols),
               "fetched": 0, "emails": 0, "failed_symbols": [], "unknown_symbols": unknown,
               "skipped_closed": len({s["symbol"] for s in all_subs}) - len(sub_symbols) - len(unknown),
               "flag_emails": 0}
    if not symbols:
        log.info("No watched market is open right now; nothing to check.")
        return summary

    quotes = provider.fetch(symbols)
    summary["fetched"] = len(quotes)
    db.save_quotes(quotes.values())
    db.prune_quotes()
    summary["failed_symbols"] = [s for s in symbols if s not in quotes]

    by_user = defaultdict(list)
    for s in subs:
        by_user[(s["user_id"], s["email"])].append(s)

    for (user_id, email), user_subs in by_user.items():
        already = db.sent_sessions(user_id)
        hits = []
        for s in user_subs:
            q = quotes.get(s["symbol"])
            if q is None or q.change_pct > -s["threshold_pct"]:
                continue
            if not markets.is_fresh(q.symbol, q.session, now):
                continue  # not this market's current trading day: stale data, weekend or holiday
            if (q.symbol, q.session) in already:
                continue
            hits.append({
                "symbol": q.symbol, "name": catalog.name_of(q.symbol), "price": q.price,
                "prev_close": q.prev_close, "change_pct": q.change_pct, "session": q.session,
                "threshold": s["threshold_pct"],
            })
        if not hits:
            continue
        _attach_briefs(hits, now)
        unsub = links.unsubscribe_url(user_id)
        subject, body, html = emails.alert(hits, unsub)
        if send(email, subject, body, unsubscribe_url=unsub, html=html):
            db.record_alerts(user_id, [
                (h["symbol"], h["session"], {"change_pct": h["change_pct"], "price": h["price"],
                                             "prev_close": h["prev_close"], "threshold_pct": h["threshold"]})
                for h in hits])
            summary["emails"] += 1
        else:
            log.error("Alert email to user %s failed; will retry on the next run", user_id)

    _flag_bucket_stocks(provider, quotes, now, send, summary)

    failed = len(summary["failed_symbols"])
    log.info("Checked %d symbols (%d failed) for %d users; sent %d emails",
             len(symbols), failed, summary["users"], summary["emails"])
    if failed / len(symbols) > config.MAX_FETCH_FAILURE_RATIO:
        raise DataFetchError(
            f"{failed}/{len(symbols)} symbols failed to load: {', '.join(summary['failed_symbols'][:10])}"
        )
    return summary


SPLIT_RECHECK = timedelta(hours=6)   # how long a "no split found" answer is trusted for one stock
SPLIT_LOOKUPS_PER_RUN = 40           # a safety cap on provider calls; the rest wait for the next run
SPLIT_LOOK_AT_PCT = 5.0              # a stock that has moved this far (either way) since it was added might have split


def _split_factor(splits, since) -> tuple:
    """(product of split ratios with an ex-date after `since`, the latest such ex-date)."""
    factor, latest = 1.0, None
    for day, ratio in splits:
        if day > since:
            factor *= ratio
            latest = day if latest is None else max(latest, day)
    return factor, latest


def _flag_bucket_stocks(provider, quotes, now, send, summary) -> None:
    """Fill in missing starting prices, then send the one-time red-flag email for any bucket stock that is
    down BUCKET_FLAG_PCT or more since it was added. Only fresh prices from an open market can flag a stock.

    A split or bonus issue makes the price fall without anyone losing money, so before a stock is judged its starting
    price is restated for any split since it was added (once), and a stock whose splits can't be checked is not flagged
    this run. One streamed pass over the not-yet-flagged rows; only the few that need action are kept in memory."""
    fills, candidates = [], []
    for r in db.iter_open_bucket_items():
        q = quotes.get(r["symbol"])
        if q is None:
            continue
        if r["added_price"] is None:
            fills.append((r["bucket_id"], r["symbol"], q.price))
            r["added_price"] = q.price
        if not r["active"] or not markets.is_live(r["symbol"], now) or not markets.is_fresh(r["symbol"], q.session, now):
            continue
        pct = bucketmath.since_pct(q.price, r["added_price"])
        if pct is None or abs(pct) < SPLIT_LOOK_AT_PCT:
            continue
        r["quote"] = q
        candidates.append(r)
    for bucket_id, symbol, price in fills:  # written after the read finishes, so the two never contend
        db.set_added_price(bucket_id, symbol, price)

    lookup = getattr(provider, "splits", None)
    cache, lookups, unchanged, pending = {}, 0, [], defaultdict(list)
    for r in candidates:
        q = r["quote"]
        recently = r["split_checked_at"] and now.replace(tzinfo=None) - r["split_checked_at"] < SPLIT_RECHECK
        if lookup and not recently:
            if r["symbol"] not in cache:
                if lookups >= SPLIT_LOOKUPS_PER_RUN:
                    cache[r["symbol"]] = None
                else:
                    lookups += 1
                    try:
                        cache[r["symbol"]] = lookup(r["symbol"])
                    except Exception as exc:  # noqa: BLE001
                        log.warning("Could not check splits for %s: %s", r["symbol"], exc)
                        cache[r["symbol"]] = None
            splits = cache[r["symbol"]]
            if splits is None:
                continue  # unverified: do not judge this stock on a possibly split-distorted price; try again next run
            since = r["split_through"] or r["added_at"].date()
            factor, latest = _split_factor(splits, since)
            if factor != 1.0:
                r["added_price"] = r["added_price"] / factor
                db.update_baseline(r["bucket_id"], r["symbol"], r["added_price"], latest)
            else:
                unchanged.append((r["bucket_id"], r["symbol"]))
        pct = bucketmath.since_pct(q.price, r["added_price"])
        if pct is None or pct > -config.BUCKET_FLAG_PCT:
            continue
        cur = catalog.currency_symbol(r["symbol"])
        pending[(r["user_id"], r["email"])].append({
            "bucket_id": r["bucket_id"], "symbol": r["symbol"], "bucket": r["bucket_name"], "name": catalog.name_of(r["symbol"]),
            "pct": pct, "added_price": f"{cur}{r['added_price']:,.2f}", "price": f"{cur}{q.price:,.2f}",
        })
    if unchanged:
        db.mark_split_checked(unchanged)
    for (user_id, email), items in pending.items():
        unsub = links.unsubscribe_url(user_id)
        subject, body, html = emails.flagged(items, unsub)
        if send(email, subject, body, unsubscribe_url=unsub, html=html):
            db.mark_flagged([(i["bucket_id"], i["symbol"]) for i in items])
            summary["flag_emails"] += 1
        else:
            log.error("Red-flag email to user %s failed; will retry on the next run", user_id)
