import os

import pytest

from buydips import config, db


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """A new empty SQLite file per test. Set TEST_DATABASE_URL to a throwaway Postgres to run the same tests there
    (production runs Postgres); every table is dropped and rebuilt for each test."""
    url = os.getenv("TEST_DATABASE_URL") or f"sqlite:///{tmp_path}/test.db"
    monkeypatch.setattr(config, "DATABASE_URL", url)
    db.reset_engine(url)
    if url.startswith("postgres"):
        db.metadata.drop_all(db.engine())
    db.init_db()
    yield
