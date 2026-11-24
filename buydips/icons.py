"""Tiny stock badges with no image files: a coloured circle with the company's initials, gold and silver coins for the
metal ETFs, and a chart glyph for indexes. The colour comes from the name, so a stock looks the same on every page,
including the public shared page (which carries names, not symbols)."""

import re
import zlib
from typing import Optional, Tuple

from markupsafe import Markup, escape

from . import catalog

PALETTE = 8                      # ico-0 .. ico-7 in style.css
_SKIP = {"ltd", "limited", "inc", "corp", "corporation", "co", "company", "plc", "the", "of", "and", "ag"}
_INDEX_GLYPH = ('<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 17l5-6 4 3 8-9"/>'
                '<path d="M15 5h5v5"/></svg>')


def initials(name: str) -> str:
    """Two capital letters: first letters of the first two words, or the first two letters of a one-word name."""
    words = [w for w in re.findall(r"[A-Za-z0-9]+", name) if w.lower() not in _SKIP] or re.findall(r"[A-Za-z0-9]+", name)
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def spec(symbol: Optional[str] = None, name: Optional[str] = None) -> Tuple[str, str]:
    """(css class suffix, text): suffix is "0".."7", "gold", "silver" or "idx"; text is the letters ("" for indexes)."""
    item = catalog.BY_SYMBOL.get(symbol or "")
    if item:
        name = item["name"]
        if item["type"] == "index":
            return "idx", ""
    name = name or symbol or "?"
    low = name.lower()
    if (item and item["type"] == "etf") or (not item and ("gold" in low or "silver" in low) and "etf" in low):
        if "gold" in low:
            return "gold", "Au"
        if "silver" in low:
            return "silver", "Ag"
    return str(zlib.crc32(name.encode("utf-8")) % PALETTE), initials(name)


def badge(symbol: Optional[str] = None, name: Optional[str] = None) -> Markup:
    cls, text = spec(symbol, name)
    inner = Markup(_INDEX_GLYPH) if cls == "idx" else escape(text)
    return Markup('<span class="ico ico-{}" aria-hidden="true">{}</span>').format(cls, inner)
