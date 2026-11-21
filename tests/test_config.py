import pytest

from buydips import config


@pytest.mark.parametrize("bad", [
    "postgresql://u:[YOUR-PASSWORD]@host:5432/postgres",
    "mysql://u:p@host/db",
    "postgresql://u:pw@host:notaport/postgres",
])
def test_bad_database_urls_get_a_clear_error(bad, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", bad)
    with pytest.raises(RuntimeError) as e:
        config._db_url()
    assert "DATABASE_URL is invalid" in str(e.value)
    assert "pw@host" not in str(e.value)  # never echo the URL


def test_quotes_and_whitespace_are_tolerated(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", '  "postgres://u:pw@host:5432/postgres"\n')
    assert config._db_url() == "postgresql+psycopg2://u:pw@host:5432/postgres"


@pytest.mark.parametrize("given", [
    "postgresql://u:pw@h:5432/postgres",
    "postgres://u:pw@h:5432/postgres",
    "postgresql+psycopg://u:pw@h:5432/postgres",
    "postgresql+psycopg2://u:pw@h:5432/postgres",
])
def test_every_postgres_style_uses_the_installed_driver(given, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", given)
    assert config._db_url() == "postgresql+psycopg2://u:pw@h:5432/postgres"
