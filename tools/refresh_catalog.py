"""Rebuild the stock parts of buydips/catalog.json: the Nifty 500 (from NSE's official list) and the
Nasdaq-100 (from Nasdaq's). Only symbols that currently return prices on Yahoo are kept.
Indexes are never touched.

    .venv/bin/python tools/refresh_catalog.py            # preview what would change
    .venv/bin/python tools/refresh_catalog.py --write    # update catalog.json

Run it about once a month: companies get renamed, merged and delisted.
"""

import csv
import io
import json
import logging
import re
import sys
import warnings
from pathlib import Path

import requests
import yfinance as yf

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

LIST_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
NASDAQ_URL = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"
CATALOG = Path(__file__).resolve().parent.parent / "buydips" / "catalog.json"
SUFFIX = re.compile(r"\s+(Ltd\.?|Limited)$", re.I)
# Share-type wording on Nasdaq's names ("Apple Inc. Common Stock", "ASML ... New York Registry Shares").
US_NOISE = re.compile(
    r"\s*[-,]?\s*(Common (Stock|Shares)|Ordinary Shares?|New York Registry Shares|"
    r"American Depositary (Shares|Receipts?)|Capital Stock)\b.*$", re.I
)
US_SUFFIX = re.compile(
    r"[\s,]+(Inc\.?|Incorporated|Corporation|Corp\.?|Company|plc|N\.V\.|Ltd\.?|Limited|\(DE\))$", re.I
)


def nifty500() -> dict:
    resp = requests.get(LIST_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    resp.raise_for_status()
    rows = csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig")))
    out = {}
    for r in rows:
        sym = r["Symbol"].strip()
        if r.get("Series", "EQ").strip() != "EQ" or sym.upper().startswith("DUMMY"):
            continue
        out[f"{sym}.NS"] = SUFFIX.sub("", r["Company Name"].strip())
    return out


def clean_us_name(raw: str) -> tuple:
    """(plain company name, share class or None)."""
    name = raw.strip()
    cls = re.search(r"\b(Class [A-C])\b", name)
    name = US_NOISE.sub("", name).strip(" ,-")
    name = re.sub(r"\s*[-,]?\s*Class [A-C]\b.*$", "", name).strip(" ,-")
    while US_SUFFIX.search(name):
        name = US_SUFFIX.sub("", name).strip(" ,")
    return name, (cls.group(1) if cls else None)


def nasdaq100() -> dict:
    resp = requests.get(NASDAQ_URL, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}, timeout=30)
    resp.raise_for_status()
    parsed = {r["symbol"].strip(): clean_us_name(r["companyName"])
              for r in resp.json()["data"]["data"]["rows"] if r["symbol"].strip()}
    bases = [base for base, _ in parsed.values()]
    # Show the share class only where two listings share a company name (Alphabet's A and C).
    return {sym: f"{base} ({cls})" if cls and bases.count(base) > 1 else base
            for sym, (base, cls) in parsed.items()}


def has_prices(symbols: list) -> set:
    ok = set()
    for k in range(0, len(symbols), 50):
        chunk = symbols[k:k + 50]
        df = yf.download(chunk, period="1mo", group_by="ticker", threads=True, progress=False, auto_adjust=False)
        for s in chunk:
            try:
                if len(df[s]["Close"].dropna()) >= 2:
                    ok.add(s)
            except Exception:  # noqa: BLE001
                pass
    return ok


def build(current: list, wanted: dict, group: str) -> tuple:
    """Stocks for one market: what the official list says, plus anything we already offer that still trades."""
    old = {i["symbol"]: i["name"] for i in current if i["type"] == "stock" and i["group"] == group}
    wanted = dict(wanted)
    for sym, name in old.items():
        wanted.setdefault(sym, name)
    live = has_prices(sorted(wanted))
    if len(live) < 0.8 * len(wanted):
        sys.exit(f"{group}: only {len(live)} of {len(wanted)} symbols returned prices; Yahoo looks unhealthy. "
                 "Not changing anything.")
    stocks = [{"symbol": s, "name": old.get(s, wanted[s]), "type": "stock", "group": group}
              for s in sorted(live, key=lambda s: old.get(s, wanted[s]).lower())]
    added = sorted(set(live) - set(old))
    gone = sorted(set(old) - set(live))
    print(f"{group}: {len(stocks)} stocks | added {len(added)} | removed {len(gone)} {gone} | "
          f"no prices: {sorted(set(wanted) - live)}")
    return stocks


def main(write: bool) -> None:
    current = json.loads(CATALOG.read_text(encoding="utf-8"))
    indexes = [i for i in current if i["type"] == "index"]
    etfs = [i for i in current if i["type"] == "etf"]  # hand-picked (gold and silver), not part of any index list
    india = build(current, nifty500(), "India")
    us = build(current, nasdaq100(), "US")
    print(f"{len(indexes)} indexes + {len(etfs)} ETFs + {len(india)} Indian + {len(us)} US stocks")
    if write:
        CATALOG.write_text(json.dumps(indexes + etfs + india + us, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print("catalog.json updated")
    else:
        print("(preview only; use --write to save)")
        print("US names:", ", ".join(f"{s['symbol']}={s['name']}" for s in us))


if __name__ == "__main__":
    main("--write" in sys.argv)
