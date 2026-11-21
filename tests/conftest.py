import pytest

from buydips import config, db


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/test.db"
    monkeypatch.setattr(config, "DATABASE_URL", url)
    db.reset_engine(url)
    db.init_db()
    yield
