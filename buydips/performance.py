"""How the stocks someone follows have done over the last week, month, three months and year.

The figures come from daily closing prices (adjusted for splits and dividends) that the alert job stores once a day.
Everything here is plain arithmetic on those numbers: descriptive, never a view on what happens next."""

import hashlib
import json
from datetime import date, timedelta
from typing import Dict, List, Optional

from . import catalog, markets

WINDOWS = {"r1w": 7, "r1m": 30, "r3m": 91, "r1y": 365}      # calendar days looked back
BENCH = {"India": "^NSEI", "US": "^NDX"}                     # what each market is compared with
LABELS = {"r1w": "1 week", "r1m": "1 month", "r3m": "3 months", "r1y": "1 year"}


def metrics(closes: List[tuple]) -> Optional[dict]:
    """Figures from [(YYYY-MM-DD, close)] oldest first, or None if there isn't enough history to say anything."""
    pts = [(date.fromisoformat(d), float(c)) for d, c in closes if c and c == c and c > 0]
    if len(pts) < 5:
        return None
    pts.sort()
    last_day, last = pts[-1]
    out = {"last_date": last_day.isoformat(), "last_price": last}
    for key, days in WINDOWS.items():
        target = last_day - timedelta(days=days)
        base = next((c for d, c in reversed(pts) if d <= target), None)
        # a window only counts if the history really reaches back that far (within a few days of slack)
        out[key] = (last / base - 1) * 100 if base and (target - pts[0][0]).days >= -4 else None
    year = [c for d, c in pts if d >= last_day - timedelta(days=365)]
    out["from_high"] = (last / max(year) - 1) * 100 if year else None
    return out


def pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:+.1f}%"


def _market(symbol: str) -> Optional[str]:
    return markets.market_of(symbol)


def build(subs: Dict[str, float], perf: Dict[str, dict]) -> dict:
    """The page model for the symbols someone follows: rows (best month first), index comparisons, counts, data date."""
    rows = []
    for sym in subs:
        item = catalog.BY_SYMBOL.get(sym)
        p = perf.get(sym)
        if not item:
            continue
        rows.append({"symbol": sym, "name": item["name"], "type": item["type"], "market": _market(sym),
                     **{k: (p or {}).get(k) for k in ("r1w", "r1m", "r3m", "r1y", "from_high", "last_date")}})
    rows.sort(key=lambda r: (r["r1m"] is None, -(r["r1m"] or 0), r["name"]))
    bench = {}
    for market, sym in BENCH.items():
        p = perf.get(sym)
        if p and p.get("r1m") is not None:
            bench[market] = {"name": catalog.name_of(sym), **{k: p.get(k) for k in ("r1w", "r1m", "r3m", "r1y")}}
    have = [r for r in rows if r["r1m"] is not None]
    dates = [r["last_date"] for r in rows if r["last_date"]]
    return {"rows": rows, "n": len(rows), "n_have": len(have), "bench": bench,
            "as_of": max(dates) if dates else None, "up": sum(r["r1m"] > 0 for r in have), "down": sum(r["r1m"] < 0 for r in have)}


def _market_vs_index(model: dict) -> List[dict]:
    out = []
    for market, b in model["bench"].items():
        mine = [r["r1m"] for r in model["rows"] if r["market"] == market and r["type"] != "index" and r["r1m"] is not None]
        if mine:
            out.append({"market": market, "avg": sum(mine) / len(mine), "index": b["name"], "index_r1m": b["r1m"], "n": len(mine)})
    return out


def facts(model: dict) -> str:
    """The numbers as plain text lines. This is ALL a written summary is allowed to use."""
    have = [r for r in model["rows"] if r["r1m"] is not None]
    if not have:
        return ""
    lines = [f"Period: the past month, up to {model['as_of']}.",
             f"Followed with a figure: {len(have)}. Up: {model['up']}. Down: {model['down']}."]
    best, worst = have[0], have[-1]
    lines.append(f"Strongest: {best['name']} {pct(best['r1m'])}.")
    if worst is not best:
        lines.append(f"Weakest: {worst['name']} {pct(worst['r1m'])}.")
    for m in _market_vs_index(model):
        where = "Indian" if m["market"] == "India" else "US"
        lines.append(f"{where} items followed averaged {pct(m['avg'])}; {m['index']} {pct(m['index_r1m'])}.")
    return "\n".join(lines)


def template_summary(model: dict) -> str:
    """A plain, deterministic summary built from the same facts."""
    have = [r for r in model["rows"] if r["r1m"] is not None]
    if not have:
        return ""
    n = len(have)
    if n == 1:
        r = have[0]
        move = "unchanged" if r["r1m"] == 0 else f"{'up' if r['r1m'] > 0 else 'down'} {abs(r['r1m']):.1f}%"
        parts = [f"Over the past month, {r['name']} is {move}."]
    else:
        up, down = model["up"], model["down"]
        parts = [f"Over the past month, {up} of the {n} items you follow {'is' if up == 1 else 'are'} up and {down} "
                 f"{'is' if down == 1 else 'are'} down."]
        parts.append(f"The strongest was {have[0]['name']} ({pct(have[0]['r1m'])}) and the weakest was {have[-1]['name']} ({pct(have[-1]['r1m'])}).")
    for m in _market_vs_index(model):
        where = "Indian" if m["market"] == "India" else "US"
        parts.append(f"{where} items averaged {pct(m['avg'])}, against {pct(m['index_r1m'])} for the {m['index']}.")
    return " ".join(parts)


def share_model(model: dict) -> Optional[dict]:
    """Exactly what a shared performance link reveals: the names of the stocks followed (no thresholds, no email) and
    their equal-weighted average move over the past month, set beside the index for each market present. None when
    there is no figure to show."""
    items = [r for r in model["rows"] if r["type"] != "index"]
    have = [r for r in items if r["r1m"] is not None]
    if not have:
        return None
    avg = sum(r["r1m"] for r in have) / len(have)
    vs = []
    for market in ("India", "US"):
        b = model["bench"].get(market)
        if b and any(r["market"] == market for r in have):
            vs.append({"name": b["name"], "text": pct(b["r1m"])})
    return {"name": "How my list has done", "n": len(items), "n_priced": len(have), "avg_text": pct(avg),
            "tone": "up" if avg > 0 else "down" if avg < 0 else "none", "vs": vs, "as_of": model["as_of"],
            "stocks": [{"name": r["name"], "market": "IN" if r["market"] == "India" else "US"} for r in items],
            "kicker": f"MY LIST OF {len(items)} {'STOCK' if len(items) == 1 else 'STOCKS'}",
            "avg_label": "average over the past month", "cta": "Follow your own stocks at buydips.in"}


def digest(model: dict) -> str:
    """Identifies the exact facts, so the same facts reuse the same written summary."""
    return hashlib.sha1(facts(model).encode("utf-8")).hexdigest()
