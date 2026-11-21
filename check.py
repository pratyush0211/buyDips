"""Run one price check and send alerts. Used by the GitHub Actions schedule."""
import logging
import sys

import app

if __name__ == "__main__":
    if "--test" in sys.argv:
        ok = app.notify("✅ buyDips test message from GitHub Actions: cloud alerts are working.")
        sys.exit(0 if ok else 1)
    app.check_indexes()
    for symbol, q in app.quotes.items():
        logging.info(
            "%-8s %-18s %10.2f %+6.2f%% %s%s",
            symbol, q["name"], q["price"], q["change_pct"], q["session"],
            "  DIP" + (" (alerted)" if q["alerted_today"] else "") if q["dip"] else "",
        )
