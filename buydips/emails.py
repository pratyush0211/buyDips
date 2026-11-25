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
    # The code leads the subject and the first line, because phone notifications show only those: no need to open the email.
    text = f"Your buyDips sign-in code is {pin}. It works for {minutes} minutes.\n\n" if pin else ""
    text += f"Click to sign in to buyDips (valid for {minutes} minutes):\n\n{url}\n\n"
    if pin:
        text += f"Or, on another device, type this code on the sign-in page: {shown}\n\n"
    text += "If you didn't ask for this, ignore this email."
    html = _env.get_template("signin.html").render(url=url, minutes=minutes, pin=shown, pin_plain=pin)
    return (f"{pin} is your buyDips sign-in code" if pin else "Your buyDips sign-in link"), text, html


BRIEF_DISCLAIMER = ("For information only. This is not investment advice or a recommendation to buy or sell anything, and buyDips is "
                    "not a SEBI-registered investment adviser or research analyst. Prices and news come from third-party sources "
                    "and may be delayed or inaccurate.")


def brief(items: List[dict], unsubscribe_url: str) -> Tuple[str, str, str]:
    """(subject, plain text, html) for the daily brief. `items` are briefs.view() dicts, most notable first."""
    n = len(items)
    head = items[0]["headline"]
    subject = f"buyDips brief: {head if len(head) <= 70 else head[:67].rstrip() + '...'}" if n == 1 \
        else f"buyDips brief: {n} of your stocks have news"
    day = items[0]["day_label"]
    lines = [f"Your buyDips brief for {day}", ""]
    for i in items:
        lines += [f"{i['name']}" + (f" ({i['move']} over the past year)" if i["move"] else ""), i["headline"]]
        if i["signals"]:
            lines.append("Signals: " + "; ".join(i["signals"]))
        lines += [f"  * {p['t']} ({p['d']}: {p['u']})" for p in i["points"]]
        lines += [f"  Reported target: {t['by']} {t['price']} ({t['d']}: {t['u']})" for t in i["targets"]]
        if i["consensus"]:
            lines.append(f"  {i['consensus']}")
        lines += [f"Read it: {i['url']}", ""]
    lines += ["Written by AI from the sources linked above.", BRIEF_DISCLAIMER, "",
              f"Stop brief emails: {config.BASE_URL}/settings", f"Unsubscribe from everything: {unsubscribe_url}"]
    for i in items:
        i["chart_url"] = i["url"] + "/chart.png"
    html = _env.get_template("brief.html").render(items=items, day=day, n=n, unsubscribe_url=unsubscribe_url,
                                                  settings_url=f"{config.BASE_URL}/settings", disclaimer=BRIEF_DISCLAIMER)
    return subject, "\n".join(lines), html


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
            "brief": i.get("brief"),
        })
    lines = ["These items on your watchlist have fallen past your alert level:", ""]
    for r in rows:
        lines.append(f"- {r['name']}: down {r['drop']} on {r['day']} "
                     f"({r['price']} vs previous close {r['prev']}; your alert level: {r['level']})")
        if r["brief"]:
            lines.append(f"  What's behind it: {r['brief']['headline']}")
            lines += [f"    * {p['t']} ({p['d']}: {p['u']})" for p in r["brief"]["points"]]
            lines.append(f"  Full brief: {r['brief']['url']}")
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
