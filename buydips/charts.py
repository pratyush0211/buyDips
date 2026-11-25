"""Price-chart geometry for the pages: a smooth one-year line as SVG path strings (drawn by the template and coloured by
the stylesheet). The email and share images draw the same series with Pillow in card.py."""

import json
from typing import List, Optional


def closes(series_json: Optional[str]) -> List[float]:
    try:
        return [float(p[1]) for p in json.loads(series_json or "[]") if p and p[1] and p[1] > 0]
    except (ValueError, TypeError, IndexError):
        return []


def geometry(values: List[float], w: int = 320, h: int = 120, pad: int = 8) -> Optional[dict]:
    """{line, area, dot_x, dot_y, tone} for a series of closes, or None if there is too little to draw."""
    if len(values) < 10:
        return None
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    n = len(values)
    pts = [(pad + (w - 2 * pad) * i / (n - 1), pad + (h - 2 * pad) * (1 - (v - lo) / span)) for i, v in enumerate(values)]
    line = "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts)
    area = f"{line} L{pts[-1][0]:.1f} {h} L{pts[0][0]:.1f} {h} Z"
    return {"line": line, "area": area, "dot_x": round(pts[-1][0], 1), "dot_y": round(pts[-1][1], 1),
            "tone": "up" if values[-1] >= values[0] else "down", "w": w, "h": h}
