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
