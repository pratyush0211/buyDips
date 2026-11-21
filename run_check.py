"""Run one alert check for all subscribers. Exits non-zero if too many prices failed to load,
so a scheduled run turns red (and GitHub emails you) instead of failing silently."""

import logging
import sys

from buydips import db
from buydips.checker import DataFetchError, run_check

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

if __name__ == "__main__":
    db.init_db()
    try:
        print(run_check())
    except DataFetchError as exc:
        logging.error("Data fetch problem: %s", exc)
        sys.exit(1)
