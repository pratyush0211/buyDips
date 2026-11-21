"""Rebuild the NSE stock part of buydips/catalog.json from NSE's official Nifty 500 list,
keeping only symbols that currently return prices on Yahoo. Indexes are never touched.

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
CATALOG = Path(__file__).resolve().parent.parent / "buydips" / "catalog.json"
SUFFIX = re.compile(r"\s+(Ltd\.?|Limited)$", re.I)


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


def main(write: bool) -> None:
    current = json.loads(CATALOG.read_text(encoding="utf-8"))
    indexes = [i for i in current if i["type"] == "index"]
    old_names = {i["symbol"]: i["name"] for i in current if i["type"] == "stock"}

    wanted = nifty500()
    for sym, name in old_names.items():          # keep anything we already offer, if it still has prices
        wanted.setdefault(sym, name)
    live = has_prices(sorted(wanted))
    if len(live) < 0.8 * len(wanted):
        sys.exit(f"Only {len(live)} of {len(wanted)} symbols returned prices; Yahoo looks unhealthy. Not changing anything.")

    stocks = [
        {"symbol": s, "name": old_names.get(s, wanted[s]), "type": "stock", "group": "India"}
        for s in sorted(live, key=lambda s: old_names.get(s, wanted[s]).lower())
    ]
    dropped = sorted(set(wanted) - live)
    added = sorted(set(s["symbol"] for s in stocks) - set(old_names))
    gone = sorted(set(old_names) - set(s["symbol"] for s in stocks))
    print(f"{len(indexes)} indexes + {len(stocks)} stocks | added {len(added)} | removed {len(gone)} {gone} | no prices: {dropped}")
    if write:
        CATALOG.write_text(json.dumps(indexes + stocks, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print("catalog.json updated")
    else:
        print("(preview only; use --write to save)")


if __name__ == "__main__":
    main("--write" in sys.argv)
