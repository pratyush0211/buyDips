from sqlalchemy import create_engine, text

from buydips import db


def test_existing_database_gets_the_new_column_and_old_users_are_not_treated_as_new(tmp_path):
    url = f"sqlite:///{tmp_path}/old.db"
    old = create_engine(url)
    with old.begin() as c:  # the users table exactly as it was before first_signin_at existed
        c.execute(text("create table users (id integer primary key, email varchar(320) unique not null, "
                       "active boolean not null, created_at timestamp not null, last_login_sent_at timestamp)"))
        c.execute(text("insert into users (email, active, created_at) values ('old@x.com', 1, '2026-11-21 10:00:00')"))
    db.reset_engine(url)
    db.init_db()
    db.init_db()  # running it again must be harmless
    user = db.get_or_create_user("old@x.com")
    assert db.mark_first_signin(user["id"]) is False  # long-time user: no "new user" notice
    fresh = db.get_or_create_user("fresh@x.com")
    assert db.mark_first_signin(fresh["id"]) is True
    assert db.mark_first_signin(fresh["id"]) is False


def test_alerts_sent_gains_detail_columns_on_an_old_database(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from buydips import db

    url = f"sqlite:///{tmp_path}/old.db"
    old = create_engine(url)
    with old.begin() as conn:
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR(320) UNIQUE NOT NULL, active BOOLEAN NOT NULL,"
                          " created_at DATETIME NOT NULL, last_login_sent_at DATETIME, first_signin_at DATETIME)"))
        conn.execute(text("CREATE TABLE alerts_sent (user_id INTEGER NOT NULL, symbol VARCHAR(40) NOT NULL, session VARCHAR(10) NOT NULL,"
                          " sent_at DATETIME NOT NULL, PRIMARY KEY (user_id, symbol, session))"))
        conn.execute(text("INSERT INTO users VALUES (1, 'a@x.com', 1, '2026-11-20 00:00:00', NULL, '2026-11-20 00:00:00')"))
        conn.execute(text("INSERT INTO alerts_sent VALUES (1, '^NSEI', '2026-11-20', '2026-11-20 05:00:00')"))
    db.reset_engine(url)
    db.init_db()
    db.init_db()  # running it twice must be harmless
    cols = {c["name"] for c in inspect(db.engine()).get_columns("alerts_sent")}
    assert {"change_pct", "price", "prev_close", "threshold_pct"} <= cols
    rows = db.alerts_for_user(1)
    assert len(rows) == 1 and rows[0]["change_pct"] is None  # the old alert survives, with no details
