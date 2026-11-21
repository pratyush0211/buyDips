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


def signin(url: str, minutes: int) -> Tuple[str, str, str]:
    """(subject, plain text, html) for the sign-in email."""
    text = (
        f"Click to sign in to buyDips (valid for {minutes} minutes):\n\n{url}\n\n"
        "If you didn't ask for this, ignore this email."
    )
    html = _env.get_template("signin.html").render(url=url, minutes=minutes)
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
