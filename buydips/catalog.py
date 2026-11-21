"""The curated list of things users can subscribe to (see catalog.json)."""

import json
from pathlib import Path
from typing import Dict, List

_PATH = Path(__file__).parent / "catalog.json"

ITEMS: List[dict] = json.loads(_PATH.read_text(encoding="utf-8"))
BY_SYMBOL: Dict[str, dict] = {i["symbol"]: i for i in ITEMS}

DEFAULT_THRESHOLD = {"index": 1.0, "stock": 3.0}
MIN_THRESHOLD, MAX_THRESHOLD = 0.5, 20.0


def name_of(symbol: str) -> str:
    return BY_SYMBOL.get(symbol, {}).get("name", symbol)


def default_threshold(symbol: str) -> float:
    return DEFAULT_THRESHOLD.get(BY_SYMBOL.get(symbol, {}).get("type", "stock"), 1.0)


def grouped() -> List[dict]:
    """Sections for the watchlist page: indexes by region first, then stocks."""
    sections: Dict[str, List[dict]] = {}
    for item in ITEMS:
        title = f"Indexes: {item['group']}" if item["type"] == "index" else "Indian stocks (NSE)"
        sections.setdefault(title, []).append(item)
    ordered = sorted(sections.items(), key=lambda kv: (kv[0].startswith("Indian stocks"), kv[0]))
    return [{"title": t, "items": items} for t, items in ordered]
