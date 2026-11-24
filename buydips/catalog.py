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

CURRENCY = {"India": "\u20b9", "US": "$"}  # stocks and ETFs only: index levels are points, not money

DEFAULT_THRESHOLD = {"index": 1.0, "stock": 3.0, "etf": 2.0}  # a gold or silver ETF moves less than a typical stock
MIN_THRESHOLD, MAX_THRESHOLD = 0.5, 20.0


def name_of(symbol: str) -> str:
    return BY_SYMBOL.get(symbol, {}).get("name", symbol)


def currency_symbol(symbol: str) -> str:
    """'₹' for Indian stocks and ETFs, '$' for US stocks, '' for indexes and anything unknown."""
    item = BY_SYMBOL.get(symbol)
    return CURRENCY.get(item["group"], "") if item and item["type"] in ("stock", "etf") else ""

# What a bucket can hold: anything that trades like a share.
TRADED = ("stock", "etf")


def search_text(item: dict) -> str:
    """Lower-case text the watchlist search box matches against."""
    return " ".join([item["name"], item["symbol"], ALIASES.get(item["symbol"], "")]).strip().lower()


def default_threshold(symbol: str) -> float:
    return DEFAULT_THRESHOLD.get(BY_SYMBOL.get(symbol, {}).get("type", "stock"), 1.0)


_GROUP_ORDER = ["India", "US"]


def offered(item: dict) -> bool:
    """False for items we no longer offer. They stay in BY_SYMBOL so people already following one keep their alerts."""
    return not item.get("retired")


def grouped(keep=()) -> List[dict]:
    """Sections for the watchlist page: indexes by region (India first), then Indian stocks, US stocks, gold & silver ETFs.
    Retired items appear only if their symbol is in `keep` (the ones this user already follows)."""
    sections: Dict[str, List[dict]] = {}
    for item in ITEMS:
        if not offered(item) and item["symbol"] not in keep:
            continue
        if item["type"] == "index":
            title = f"Indexes: {item['group']}"
        elif item["type"] == "etf":
            title = "Gold & silver ETFs (NSE)"
        else:
            title = "Indian stocks (NSE)" if item["group"] == "India" else "US stocks (Nasdaq-100)"
        sections.setdefault(title, []).append(item)

    def rank(title: str) -> int:
        if title.startswith("Indian stocks"):
            return len(_GROUP_ORDER) + 1
        if title.startswith("US stocks"):
            return len(_GROUP_ORDER) + 2
        if title.startswith("Gold"):
            return len(_GROUP_ORDER) + 3
        region = title.split(": ", 1)[1]
        return _GROUP_ORDER.index(region) if region in _GROUP_ORDER else len(_GROUP_ORDER)

    def tab(title: str) -> str:
        return ("india" if title.startswith("Indian stocks") else "us" if title.startswith("US stocks")
                else "metals" if title.startswith("Gold") else "indexes")

    return [
        {"title": t, "tab": tab(t), "items": [{**i, "search": search_text(i)} for i in items]}
        for t, items in sorted(sections.items(), key=lambda kv: rank(kv[0]))
    ]


def tab_totals() -> Dict[str, int]:
    """How many items each tab of the watchlist page offers (retired ones are not counted)."""
    totals = {"indexes": 0, "india": 0, "us": 0, "metals": 0}
    for sec in grouped():
        totals[sec["tab"]] += len(sec["items"])
    return totals
