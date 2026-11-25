"""Market data. Everything else talks to a provider through `fetch()`, so swapping Yahoo
for a licensed provider later means writing one new class here and nothing else."""

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Protocol

log = logging.getLogger("buyDips.data")


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    prev_close: float
    change_pct: float
    session: str  # trading day (YYYY-MM-DD) this price belongs to


class Provider(Protocol):
    def fetch(self, symbols: Iterable[str]) -> Dict[str, Quote]:
        """Return quotes for the symbols it could load. Missing symbols are simply absent."""


class YahooProvider:
    """Unofficial Yahoo Finance data via the `yfinance` library (personal / beta use)."""

    def __init__(self, workers: int = 4):
        self.workers = workers

    def _one(self, symbol: str) -> Optional[Quote]:
        try:
            import yfinance as yf

            closes = yf.Ticker(symbol).history(period="7d", interval="1d")["Close"].dropna()
            if len(closes) < 2:
                raise ValueError("not enough price history")
            price, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
            return Quote(
                symbol=symbol,
                price=price,
                prev_close=prev,
                change_pct=(price - prev) / prev * 100,
                session=closes.index[-1].date().isoformat(),
            )
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to fetch %s: %s", symbol, exc)
            return None

    def splits(self, symbol: str):
        """[(ex-date, ratio)] for stock splits and bonus issues (a 1:1 bonus is 2.0, a 10-for-1 split is 10.0, a
        reverse split is below 1). Raises if Yahoo can't be reached, so callers never mistake "unknown" for "none"."""
        import yfinance as yf

        series = yf.Ticker(symbol).splits
        return [(d.date(), float(v)) for d, v in series.items() if v and v == v and v > 0]

    def _history_one(self, symbol: str):
        try:
            import yfinance as yf

            # auto_adjust (the default) restates old prices for splits and dividends, so returns are like for like
            hist = yf.Ticker(symbol).history(period="1y", interval="1d")
            vol = hist["Volume"] if "Volume" in hist else None
            rows = []
            for i, c in hist["Close"].dropna().items():
                v = float(vol.get(i, 0) or 0) if vol is not None else 0.0
                rows.append((i.date().isoformat(), float(c), v if v > 0 else None))
            return symbol, rows
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to fetch history for %s: %s", symbol, exc)
            return symbol, None

    def analyst(self, symbols: Iterable[str]) -> Dict[str, dict]:
        """{symbol: {mean, low, high}} analyst price-target consensus where Yahoo has one (patchy for Indian stocks)."""
        out = {}
        for s in dict.fromkeys(symbols):
            try:
                import yfinance as yf

                t = yf.Ticker(s).analyst_price_targets or {}
                mean = t.get("mean")
                if mean and mean == mean and mean > 0:
                    out[s] = {k: round(float(t[k]), 2) for k in ("mean", "low", "high") if t.get(k) and t[k] == t[k]}
            except Exception as exc:  # noqa: BLE001
                log.info("No analyst targets for %s: %s", s, exc)
        return out

    def history(self, symbols: Iterable[str]) -> Dict[str, list]:
        """{symbol: [(YYYY-MM-DD, adjusted close, volume or None), ...] oldest first} for roughly the last year. Failed symbols are absent."""
        symbols = list(dict.fromkeys(symbols))
        with ThreadPoolExecutor(self.workers) as pool:
            results = list(pool.map(self._history_one, symbols))
        return {s: rows for s, rows in results if rows}

    def fetch(self, symbols: Iterable[str]) -> Dict[str, Quote]:
        symbols = list(dict.fromkeys(symbols))
        with ThreadPoolExecutor(self.workers) as pool:
            results = list(pool.map(self._one, symbols))
        return {q.symbol: q for q in results if q}
