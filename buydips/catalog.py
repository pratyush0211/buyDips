"""The curated list of things users can subscribe to (see catalog.json)."""

import json
from pathlib import Path
from typing import Dict, List

_PATH = Path(__file__).parent / "catalog.json"

ITEMS: List[dict] = json.loads(_PATH.read_text(encoding="utf-8"))
BY_SYMBOL: Dict[str, dict] = {i["symbol"]: i for i in ITEMS}
# Everyday names people search for ("google" finds Alphabet). Kept apart from catalog.json so that
# tools/refresh_catalog.py can rebuild the list without losing them.
ALIASES: Dict[str, str] = json.loads((Path(__file__).parent / "aliases.json").read_text(encoding="utf-8"))

CURRENCY = {"India": "\u20b9", "US": "$"}  # stocks only: index levels are points, not money

DEFAULT_THRESHOLD = {"index": 1.0, "stock": 3.0}
MIN_THRESHOLD, MAX_THRESHOLD = 0.5, 20.0


def name_of(symbol: str) -> str:
    return BY_SYMBOL.get(symbol, {}).get("name", symbol)


def currency_symbol(symbol: str) -> str:
    """'₹' for Indian stocks, '$' for US stocks, '' for indexes and anything unknown."""
    item = BY_SYMBOL.get(symbol)
    return CURRENCY.get(item["group"], "") if item and item["type"] == "stock" else ""


def search_text(item: dict) -> str:
    """Lower-case text the watchlist search box matches against."""
    return " ".join([item["name"], item["symbol"], ALIASES.get(item["symbol"], "")]).strip().lower()


def default_threshold(symbol: str) -> float:
    return DEFAULT_THRESHOLD.get(BY_SYMBOL.get(symbol, {}).get("type", "stock"), 1.0)


_GROUP_ORDER = ["India", "US", "Europe", "Asia"]


def grouped() -> List[dict]:
    """Sections for the watchlist page: indexes by region (India first), then Indian stocks, then US stocks."""
    sections: Dict[str, List[dict]] = {}
    for item in ITEMS:
        if item["type"] == "index":
            title = f"Indexes: {item['group']}"
        else:
            title = "Indian stocks (NSE)" if item["group"] == "India" else "US stocks (Nasdaq-100)"
        sections.setdefault(title, []).append(item)

    def rank(title: str) -> int:
        if title.startswith("Indian stocks"):
            return len(_GROUP_ORDER) + 1
        if title.startswith("US stocks"):
            return len(_GROUP_ORDER) + 2
        region = title.split(": ", 1)[1]
        return _GROUP_ORDER.index(region) if region in _GROUP_ORDER else len(_GROUP_ORDER)

    def tab(title: str) -> str:
        return "india" if title.startswith("Indian stocks") else "us" if title.startswith("US stocks") else "indexes"

    return [
        {"title": t, "tab": tab(t), "items": [{**i, "search": search_text(i)} for i in items]}
        for t, items in sorted(sections.items(), key=lambda kv: rank(kv[0]))
    ]


def tab_totals() -> Dict[str, int]:
    """How many items each tab of the watchlist page holds."""
    totals = {"indexes": 0, "india": 0, "us": 0}
    for sec in grouped():
        totals[sec["tab"]] += len(sec["items"])
    return totals
