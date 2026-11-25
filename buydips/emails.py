"""Email content: a plain-text version (always sent) and an HTML version with a button."""

from datetime import datetime
from pathlib import Path
from typing import List, Tuple

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import catalog, config

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates" / "email"),
    autoescape=select_autoescape(["html"]),
)


def signin(url: str, minutes: int, pin: str = "") -> Tuple[str, str, str]:
    """(subject, plain text, html) for the sign-in email. `pin` is the 6-digit alternative to the link."""
    shown = f"{pin[:3]} {pin[3:]}" if pin else ""
    text = f"Click to sign in to buyDips (valid for {minutes} minutes):\n\n{url}\n\n"
    if pin:
        text += f"Or, on another device, type this code on the sign-in page: {shown}\n\n"
    text += "If you didn't ask for this, ignore this email."
    html = _env.get_template("signin.html").render(url=url, minutes=minutes, pin=shown)
    return "Your buyDips sign-in link", text, html


def alert(items: List[dict], unsubscribe_url: str) -> Tuple[str, str, str]:
    """(subject, plain text, html) for a dip alert. `items` need name, change_pct, price, prev_close,
    session (YYYY-MM-DD) and threshold."""
    items = sorted(items, key=lambda i: i["change_pct"])
    n = len(items)
    subject = (
        f"buyDips: {items[0]['name']} is down {abs(items[0]['change_pct']):.2f}%"
        if n == 1
        else f"buyDips: {n} of your watchlist are down"
    )
    rows = []
    for i in items:
        cur = catalog.currency_symbol(i.get("symbol", ""))
        day = datetime.fromisoformat(i["session"]).strftime("%a %d %b")
        rows.append({
            "name": i["name"], "drop": f"{abs(i['change_pct']):.2f}%", "day": day,
            "price": f"{cur}{i['price']:,.2f}", "prev": f"{cur}{i['prev_close']:,.2f}", "level": f"{i['threshold']:g}%",
        })
    lines = ["These items on your watchlist have fallen past your alert level:", ""]
    for r in rows:
        lines.append(f"- {r['name']}: down {r['drop']} on {r['day']} "
                     f"({r['price']} vs previous close {r['prev']}; your alert level: {r['level']})")
    lines += ["", f"Change your watchlist: {config.BASE_URL}/watchlist", f"Unsubscribe: {unsubscribe_url}",
              "", config.DISCLAIMER]
    html = _env.get_template("alert.html").render(
        rows=rows, watchlist_url=f"{config.BASE_URL}/watchlist", unsubscribe_url=unsubscribe_url,
        disclaimer=config.DISCLAIMER,
    )
    return subject, "\n".join(lines), html


def flagged(items: List[dict], unsubscribe_url: str) -> Tuple[str, str, str]:
    """(subject, plain text, html) for the one-time red-flag email. `items` need bucket, name, symbol, pct
    (negative), added_price and price (already formatted with their currency)."""
    items = sorted(items, key=lambda i: i["pct"])
    n = len(items)
    limit = f"{config.BUCKET_FLAG_PCT:g}%"
    subject = (f"buyDips: {items[0]['name']} is down {abs(items[0]['pct']):.1f}% since you added it" if n == 1
               else f"buyDips: {n} stocks in your buckets are down {limit} or more")
    rows = [{"bucket": i["bucket"], "name": i["name"], "drop": f"{abs(i['pct']):.1f}%",
             "added": i["added_price"], "price": i["price"]} for i in items]
    lines = [f"These stocks are down {limit} or more since you added them to a bucket:", ""]
    for r in rows:
        lines.append(f"- {r['name']} (bucket: {r['bucket']}): down {r['drop']} since added ({r['added']} then, {r['price']} now)")
    lines += ["", "You'll only get this email once for each stock.", f"Your buckets: {config.BASE_URL}/buckets",
              f"Unsubscribe: {unsubscribe_url}", "", config.DISCLAIMER]
    html = _env.get_template("flag.html").render(rows=rows, limit=limit, buckets_url=f"{config.BASE_URL}/buckets",
                                                 unsubscribe_url=unsubscribe_url, disclaimer=config.DISCLAIMER)
    return subject, "\n".join(lines), html
