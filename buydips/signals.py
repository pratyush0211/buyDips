"""Technical signals found by plain arithmetic on daily prices. No AI is involved: these decide which stocks are worth
researching each day, and they are shown as-is next to a stock. Each is a descriptive fact, never a view on what happens
next. rows are (YYYY-MM-DD, close[, volume]) oldest first."""

from typing import List, Optional


def _sma(vals: List[float], n: int, end: int) -> Optional[float]:
    """Average of the n values ending just before index `end` (exclusive), or None if there are not enough."""
    return sum(vals[end - n:end]) / n if end >= n else None


def _rsi(closes: List[float], n: int = 14) -> Optional[float]:
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / n, losses / n
    for i in range(n + 1, len(closes)):          # Wilder's smoothing
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0)) / n
    return 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)


def compute(rows) -> List[dict]:
    """[{k, text, s}] strongest first. s is 1 (minor) to 3 (major)."""
    closes = [float(r[1]) for r in rows if r[1] and r[1] > 0]
    if len(closes) < 30:
        return []
    vols = [r[2] if len(r) > 2 else None for r in rows if r[1] and r[1] > 0]
    out = []
    last, prev = closes[-1], closes[-2]

    prior = closes[-251:-1]
    if len(closes) >= 120:
        if last > max(prior):
            out.append({"k": "high52", "text": "At a 52-week high", "s": 3})
        elif last < min(prior):
            out.append({"k": "low52", "text": "At a 52-week low", "s": 3})

    day = (last / prev - 1) * 100
    if abs(day) >= 5:
        out.append({"k": "move", "text": f"{'Up' if day > 0 else 'Down'} {abs(day):.1f}% in one day", "s": 3 if abs(day) >= 8 else 2})

    n = len(closes)
    if n >= 205:
        now50, now200 = _sma(closes, 50, n), _sma(closes, 200, n)
        then50, then200 = _sma(closes, 50, n - 5), _sma(closes, 200, n - 5)
        if None not in (now50, now200, then50, then200):
            if then50 <= then200 and now50 > now200:
                out.append({"k": "cross_up", "text": "50-day average crossed above the 200-day", "s": 2})
            elif then50 >= then200 and now50 < now200:
                out.append({"k": "cross_down", "text": "50-day average crossed below the 200-day", "s": 2})

    rsi = _rsi(closes)
    if rsi is not None and (rsi >= 75 or rsi <= 25):
        out.append({"k": "rsi", "text": f"RSI at {rsi:.0f} ({'above 75' if rsi >= 75 else 'below 25'})", "s": 1})

    if vols and vols[-1] and len([v for v in vols[-21:-1] if v]) >= 15:
        avg = sum(v for v in vols[-21:-1] if v) / len([v for v in vols[-21:-1] if v])
        if avg > 0 and vols[-1] / avg >= 2.5:
            out.append({"k": "volume", "text": f"Volume {vols[-1] / avg:.1f}x its 20-day average", "s": 2})

    return sorted(out, key=lambda x: -x["s"])
