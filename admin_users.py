"""List everyone who has entered an email, and what they follow. Run on your own Mac:

    cd ~/Documents/buyDips && .venv/bin/python admin_users.py

Reads DATABASE_URL from your local .env. The output is private: don't share or export it.
"""

import warnings

warnings.filterwarnings("ignore")

from sqlalchemy import text  # noqa: E402

from buydips import db  # noqa: E402

SQL = """
select u.email, u.created_at, u.active, count(s.symbol) as items
from users u left join subscriptions s on s.user_id = u.id
group by u.id order by u.created_at desc
"""

if __name__ == "__main__":
    with db.engine().connect() as conn:
        rows = conn.execute(text(SQL)).all()
    print(f"{len(rows)} people have entered an email\n")
    print(f"{'email':38} {'first entered (UTC)':20} {'alerts on':10} items")
    for email, created, active, items in rows:
        print(f"{email:38} {created:%d %b %Y %H:%M}      {str(active):10} {items}")
