"""How a bucket is doing: each stock's move since it was added, the bucket's average, and the red flags.

Equal weight: every stock counts the same, whatever its price. Pure functions, so they are easy to test."""

from typing import Dict, List, Optional

from . import catalog, config


def since_pct(price: Optional[float], added: Optional[float]) -> Optional[float]:
    if price is None or not added:
        return None
    return (price - added) / added * 100


def fmt_pct(p: Optional[float]) -> str:
    if p is None:
        return "\u2013"
    return ("+" if p > 0.005 else "\u2212" if p < -0.005 else "") + f"{abs(p):.2f}%"


def tone(p: Optional[float]) -> str:
    return "none" if p is None else "up" if p > 0.005 else "down" if p < -0.005 else "flat"


def view(items: List[dict], quotes: Dict[str, dict]) -> dict:
    """Per-stock rows plus the bucket's average and flag count. `items` come from the bucket_items table."""
    rows, moves = [], []
    for it in items:
        q = quotes.get(it["symbol"])
        price = q["price"] if q else None
        pct = since_pct(price, it.get("added_price"))
        cur = catalog.currency_symbol(it["symbol"])
        flagged = pct is not None and pct <= -config.BUCKET_FLAG_PCT
        if pct is not None:
            moves.append(pct)
        rows.append({
            "symbol": it["symbol"], "name": catalog.name_of(it["symbol"]), "pct": pct, "pct_text": fmt_pct(pct), "tone": tone(pct),
            "flagged": flagged, "added": None if it.get("added_price") is None else f"{cur}{it['added_price']:,.2f}",
            "now": None if price is None else f"{cur}{price:,.2f}", "waiting": pct is None,
        })
    avg = sum(moves) / len(moves) if moves else None
    return {"rows": rows, "average": avg, "average_text": fmt_pct(avg), "tone": tone(avg), "n": len(rows),
            "n_priced": len(moves), "n_flagged": sum(r["flagged"] for r in rows)}
