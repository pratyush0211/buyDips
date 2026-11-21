"""One alert run: fetch each distinct symbol once, then email every user whose threshold was
crossed. Each (user, symbol, trading session) is alerted at most once."""

import logging
from collections import defaultdict
from typing import Callable, Optional

from . import catalog, config, db, emails, links
from .data import Provider, YahooProvider
from .mailer import send_email

log = logging.getLogger("buyDips.check")


class DataFetchError(RuntimeError):
    """Too many symbols failed to load; the run should be marked as failed."""


def run_check(
    provider: Optional[Provider] = None,
    send: Callable[..., bool] = send_email,
) -> dict:
    provider = provider or YahooProvider()
    subs = db.active_subscriptions()
    symbols = sorted({s["symbol"] for s in subs})
    summary = {"users": len({s["user_id"] for s in subs}), "symbols": len(symbols),
               "fetched": 0, "emails": 0, "failed_symbols": []}
    if not symbols:
        log.info("No active subscriptions; nothing to check.")
        return summary

    quotes = provider.fetch(symbols)
    summary["fetched"] = len(quotes)
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
            db.record_alerts(user_id, [(h["symbol"], h["session"]) for h in hits])
            summary["emails"] += 1
        else:
            log.error("Alert email to user %s failed; will retry on the next run", user_id)

    failed = len(summary["failed_symbols"])
    log.info("Checked %d symbols (%d failed) for %d users; sent %d emails",
             len(symbols), failed, summary["users"], summary["emails"])
    if failed / len(symbols) > config.MAX_FETCH_FAILURE_RATIO:
        raise DataFetchError(
            f"{failed}/{len(symbols)} symbols failed to load: {', '.join(summary['failed_symbols'][:10])}"
        )
    return summary
