"""One alert run: fetch each symbol whose market is open once, then email every user whose threshold
was crossed today. Each (user, symbol, trading session) is alerted at most once."""

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable, Optional

from . import buckets as bucketmath, catalog, config, db, emails, links, markets
from .data import Provider, YahooProvider
from .mailer import send_email

log = logging.getLogger("buyDips.check")


class DataFetchError(RuntimeError):
    """Too many symbols failed to load; the run should be marked as failed."""


def run_check(
    provider: Optional[Provider] = None,
    send: Callable[..., bool] = send_email,
    now: Optional[datetime] = None,
) -> dict:
    provider = provider or YahooProvider()
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
    bucket_rows = db.all_bucket_items()
    live_bucket = {r["symbol"] for r in bucket_rows if markets.is_live(r["symbol"], now)}
    unpriced = {r["symbol"] for r in bucket_rows if r["added_price"] is None}
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

    _flag_bucket_stocks(bucket_rows, quotes, now, send, summary)

    failed = len(summary["failed_symbols"])
    log.info("Checked %d symbols (%d failed) for %d users; sent %d emails",
             len(symbols), failed, summary["users"], summary["emails"])
    if failed / len(symbols) > config.MAX_FETCH_FAILURE_RATIO:
        raise DataFetchError(
            f"{failed}/{len(symbols)} symbols failed to load: {', '.join(summary['failed_symbols'][:10])}"
        )
    return summary


def _flag_bucket_stocks(rows, quotes, now, send, summary) -> None:
    """Fill in missing starting prices, then send the one-time red-flag email for any bucket stock that is
    down BUCKET_FLAG_PCT or more since it was added. Only fresh prices from an open market can flag a stock."""
    for r in rows:
        q = quotes.get(r["symbol"])
        if r["added_price"] is None and q is not None:
            db.set_added_price(r["bucket_id"], r["symbol"], q.price)
            r["added_price"] = q.price

    pending = defaultdict(list)
    for r in rows:
        q = quotes.get(r["symbol"])
        if (not r["active"] or r["flagged_at"] or r["added_price"] is None or q is None
                or not markets.is_live(r["symbol"], now) or not markets.is_fresh(r["symbol"], q.session, now)):
            continue
        pct = bucketmath.since_pct(q.price, r["added_price"])
        if pct is None or pct > -config.BUCKET_FLAG_PCT:
            continue
        cur = catalog.currency_symbol(r["symbol"])
        pending[(r["user_id"], r["email"])].append({
            "bucket_id": r["bucket_id"], "symbol": r["symbol"], "bucket": r["bucket_name"], "name": catalog.name_of(r["symbol"]),
            "pct": pct, "added_price": f"{cur}{r['added_price']:,.2f}", "price": f"{cur}{q.price:,.2f}",
        })
    for (user_id, email), items in pending.items():
        unsub = links.unsubscribe_url(user_id)
        subject, body, html = emails.flagged(items, unsub)
        if send(email, subject, body, unsubscribe_url=unsub, html=html):
            db.mark_flagged([(i["bucket_id"], i["symbol"]) for i in items])
            summary["flag_emails"] += 1
        else:
            log.error("Red-flag email to user %s failed; will retry on the next run", user_id)
