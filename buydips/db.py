"""Database layer (SQLAlchemy Core). SQLite locally, Postgres in production, same code."""

import base64
import time
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import event, exc
from sqlalchemy.exc import IntegrityError
from sqlalchemy import (
    Boolean, Column, Date, DateTime, false, Float, ForeignKey, Index, Integer, MetaData, PrimaryKeyConstraint,
    String, Table, bindparam, create_engine, delete, exists, func, insert, inspect, literal, select, text, update,
)

from . import config

metadata = MetaData()

users = Table(
    "users", metadata,
    Column("id", Integer, primary_key=True),
    Column("email", String(320), unique=True, nullable=False),
    Column("active", Boolean, nullable=False, default=True),
    Column("created_at", DateTime, nullable=False),
    Column("last_login_sent_at", DateTime),
    Column("first_signin_at", DateTime),  # set once, when the person first clicks a sign-in link
    Column("alerts_seen_at", DateTime),   # when they last opened Alerts: newer alerts show as a badge on the bell
    Column("buckets_seen_at", DateTime),  # when they last opened Buckets: newer red flags show as a badge on the tab
    Column("perf_share_token", String(32), unique=True),  # set only while they share "how my list has done"; NULL = private
)

subscriptions = Table(
    "subscriptions", metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("symbol", String(40), nullable=False),
    Column("threshold_pct", Float, nullable=False),
    PrimaryKeyConstraint("user_id", "symbol"),
)

alerts_sent = Table(
    "alerts_sent", metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("symbol", String(40), nullable=False),
    Column("session", String(10), nullable=False),
    Column("sent_at", DateTime, nullable=False),
    # What the alert said, kept for the Alerts tab. Empty on rows sent before 24 Nov 2026.
    Column("change_pct", Float), Column("price", Float), Column("prev_close", Float), Column("threshold_pct", Float),
    PrimaryKeyConstraint("user_id", "symbol", "session"),
)

buckets = Table(
    "buckets", metadata,
    Column("id", Integer, primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("name", String(60), nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("share_token", String(32), unique=True),   # set only while the owner is sharing; NULL means private
    Column("share_perf", Boolean, nullable=False, default=False, server_default=false()),  # also show the average since added
    Index("ix_buckets_user", "user_id"),   # every page lists or counts a user's buckets
)

bucket_items = Table(
    "bucket_items", metadata,
    Column("bucket_id", Integer, ForeignKey("buckets.id", ondelete="CASCADE"), nullable=False),
    Column("symbol", String(40), nullable=False),
    Column("added_price", Float),   # price when the stock was added; filled by the next sync if it wasn't known yet
    Column("added_at", DateTime, nullable=False),
    Column("flagged_at", DateTime),  # set once, when the red-flag email for this stock has been sent
    Column("split_through", Date),       # splits/bonuses up to this ex-date are already folded into added_price
    Column("split_checked_at", DateTime),  # last time we looked for splits, so a stock is not re-checked every run
    PrimaryKeyConstraint("bucket_id", "symbol"),
)

# The latest price per symbol, refreshed by the alert job, so pages never call the data provider.
# Daily price-history figures per symbol (1 week / 1 month / 3 months / 1 year returns and distance from the 1-year
# high), refreshed once a day by the alert job so pages never wait on Yahoo.
perf_cache = Table(
    "perf_cache", metadata,
    Column("symbol", String(40), primary_key=True),
    Column("last_date", String(10)),
    Column("last_price", Float),
    Column("r1w", Float), Column("r1m", Float), Column("r3m", Float), Column("r1y", Float),
    Column("from_high", Float),
    Column("updated_at", DateTime, nullable=False),
)

# Written summaries already produced, keyed by a digest of the exact facts they describe.
summaries = Table(
    "summaries", metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("digest", String(40), primary_key=True),
    Column("text", String(1000), nullable=False),
    Column("created_at", DateTime, nullable=False, index=True),
)

quotes_cache = Table(
    "quotes_cache", metadata,
    Column("symbol", String(40), primary_key=True),
    Column("price", Float, nullable=False),
    Column("prev_close", Float, nullable=False),
    Column("session", String(10), nullable=False),
    Column("updated_at", DateTime, nullable=False),
)

login_codes = Table(
    "login_codes", metadata,
    Column("code_hash", String(64), primary_key=True),  # SHA-256 of the code; the code itself is never stored
    Column("email", String(320), nullable=False),
    Column("expires_at", DateTime, nullable=False),
)

# The 6-digit code that comes with a sign-in email, as an alternative to tapping the link. One per email.
login_pins = Table(
    "login_pins", metadata,
    Column("email", String(320), primary_key=True),
    Column("pin_hash", String(64), nullable=False),     # SHA-256 of email + code; the code itself is never stored
    Column("tries", Integer, nullable=False, default=0),
    Column("expires_at", DateTime, nullable=False),
)

_engine = None


def _make_engine(url: str):
    """The database is far from the web server, so every network round trip counts (about 0.1 to 0.25 s each).
    No ping on every checkout (that doubled the cost of each statement); instead a connection is only tested when it
    has sat idle, and old connections are replaced before the pooler can drop them."""
    eng = create_engine(url, pool_recycle=240)
    if eng.dialect.name == "postgresql":
        @event.listens_for(eng, "checkout")
        def _ping_if_idle(dbapi_con, record, proxy):
            now = time.time()
            idle = now - record.info.get("last_used", 0)
            record.info["last_used"] = now
            if idle > 30:
                try:
                    cur = dbapi_con.cursor()
                    cur.execute("SELECT 1")
                    cur.close()
                    dbapi_con.rollback()
                except Exception as exc_:  # noqa: BLE001
                    raise exc.DisconnectionError() from exc_
    return eng


def engine():
    global _engine
    if _engine is None:
        _engine = _make_engine(config.DATABASE_URL)
    return _engine


def _auto():
    """A connection for ONE statement: autocommit, so there is no BEGIN, COMMIT or ROLLBACK round trip around it."""
    return engine().connect().execution_options(isolation_level="AUTOCOMMIT")


def reset_engine(url: Optional[str] = None) -> None:
    """Point at a different database (used by tests)."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = _make_engine(url or config.DATABASE_URL)


def init_db() -> None:
    metadata.create_all(engine())
    _migrate()


def _migrate() -> None:
    """Add columns introduced after the first deploy (create_all never alters existing tables)."""
    wanted = [("users", "first_signin_at", "TIMESTAMP"), ("alerts_sent", "change_pct", "DOUBLE PRECISION"),
              ("alerts_sent", "price", "DOUBLE PRECISION"), ("alerts_sent", "prev_close", "DOUBLE PRECISION"),
              ("alerts_sent", "threshold_pct", "DOUBLE PRECISION"),
              ("bucket_items", "split_through", "DATE"), ("bucket_items", "split_checked_at", "TIMESTAMP"),
              ("users", "alerts_seen_at", "TIMESTAMP"), ("users", "buckets_seen_at", "TIMESTAMP"),
              ("buckets", "share_token", "VARCHAR(32)"), ("buckets", "share_perf", "BOOLEAN DEFAULT FALSE"),
              ("users", "perf_share_token", "VARCHAR(32)")]
    for table, column, ddl in wanted:
        if column in {c["name"] for c in inspect(engine()).get_columns(table)}:
            continue
        try:
            with engine().begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                if column in ("alerts_seen_at", "buckets_seen_at"):
                    # Everything that already happened counts as seen, so nobody opens to a pile of badges.
                    conn.execute(text(f"UPDATE users SET {column} = :t"), {"t": _now()})
                if column == "first_signin_at":
                    # Everyone who already exists counts as already welcomed, so they don't trigger notices.
                    conn.execute(text("UPDATE users SET first_signin_at = created_at WHERE first_signin_at IS NULL"))
        except Exception:  # noqa: BLE001  (another worker may have added it at the same moment)
            if column not in {c["name"] for c in inspect(engine()).get_columns(table)}:
                raise
    # create_all never touches a table that already exists, so the buckets index has to be added by hand.
    if "ix_buckets_user" not in {i["name"] for i in inspect(engine()).get_indexes("buckets")}:
        with engine().begin() as conn:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_buckets_user ON buckets (user_id)"))


def _now() -> datetime:
    return datetime.utcnow()


def normalize_email(email: str) -> str:
    return email.strip().lower()


# ---------- users ----------

def get_or_create_user(email: str) -> dict:
    email = normalize_email(email)
    with engine().begin() as conn:
        row = conn.execute(select(users).where(users.c.email == email)).mappings().first()
        if row is None:
            conn.execute(insert(users).values(email=email, active=True, created_at=_now()))
            row = conn.execute(select(users).where(users.c.email == email)).mappings().first()
        elif not row["active"]:
            conn.execute(update(users).where(users.c.id == row["id"]).values(active=True))
            row = conn.execute(select(users).where(users.c.id == row["id"])).mappings().first()
        return dict(row)


def mark_first_signin(user_id: int) -> bool:
    """True exactly once per user: the first time they sign in (safe if two workers race)."""
    with engine().begin() as conn:
        result = conn.execute(
            update(users).where((users.c.id == user_id) & (users.c.first_signin_at.is_(None)))
            .values(first_signin_at=_now())
        )
    return result.rowcount == 1


def user_count() -> int:
    with _auto() as conn:
        return conn.execute(select(func.count()).select_from(users)).scalar()


def get_user(user_id: int) -> Optional[dict]:
    with _auto() as conn:
        row = conn.execute(select(users).where(users.c.id == user_id)).mappings().first()
        return dict(row) if row else None


_EPOCH = datetime(1970, 1, 1)


def mark_seen(user_id: int, what: str) -> datetime:
    """Record that the person has just looked at "alerts" or "buckets"; returns the time stored."""
    col = {"alerts": users.c.alerts_seen_at, "buckets": users.c.buckets_seen_at}[what]
    now = _now()
    with _auto() as conn:
        conn.execute(update(users).where(users.c.id == user_id).values({col.key: now}))
    return now


def get_user_with_badges(user_id: int) -> Optional[dict]:
    """The user row plus bell_n (alerts since they last looked) and flag_n (red flags since they last looked): one
    statement instead of two, because it runs on every page."""
    a = (select(func.count()).select_from(alerts_sent)
         .where((alerts_sent.c.user_id == users.c.id) & (alerts_sent.c.sent_at > func.coalesce(users.c.alerts_seen_at, _EPOCH)))
         .scalar_subquery())
    f = (select(func.count()).select_from(bucket_items.join(buckets, buckets.c.id == bucket_items.c.bucket_id))
         .where((buckets.c.user_id == users.c.id) & (bucket_items.c.flagged_at > func.coalesce(users.c.buckets_seen_at, _EPOCH)))
         .scalar_subquery())
    with _auto() as conn:
        row = conn.execute(select(users, a.label("bell_n"), f.label("flag_n")).where(users.c.id == user_id)).mappings().first()
    return dict(row) if row else None


def seconds_since_login_link(email: str) -> Optional[float]:
    """Seconds since we last emailed this address a sign-in link (None if never)."""
    with _auto() as conn:
        row = conn.execute(
            select(users.c.last_login_sent_at).where(users.c.email == normalize_email(email))
        ).first()
    if not row or not row[0]:
        return None
    return (_now() - row[0]).total_seconds()


def mark_login_link_sent(email: str) -> None:
    with engine().begin() as conn:
        conn.execute(
            update(users).where(users.c.email == normalize_email(email)).values(last_login_sent_at=_now())
        )


def unsubscribe_user(user_id: int) -> None:
    """Stop all emails but keep the account (they can sign in again later)."""
    with engine().begin() as conn:
        conn.execute(update(users).where(users.c.id == user_id).values(active=False))


def delete_user(user_id: int) -> None:
    with engine().begin() as conn:
        ids = [r[0] for r in conn.execute(select(buckets.c.id).where(buckets.c.user_id == user_id))]
        if ids:
            conn.execute(delete(bucket_items).where(bucket_items.c.bucket_id.in_(ids)))
        conn.execute(delete(buckets).where(buckets.c.user_id == user_id))
        conn.execute(delete(alerts_sent).where(alerts_sent.c.user_id == user_id))
        conn.execute(delete(subscriptions).where(subscriptions.c.user_id == user_id))
        conn.execute(delete(users).where(users.c.id == user_id))


# ---------- subscriptions ----------

def get_subscriptions(user_id: int) -> Dict[str, float]:
    with _auto() as conn:
        rows = conn.execute(
            select(subscriptions.c.symbol, subscriptions.c.threshold_pct).where(
                subscriptions.c.user_id == user_id
            )
        ).all()
    return {symbol: pct for symbol, pct in rows}


def set_subscriptions(user_id: int, items: Dict[str, float]) -> None:
    with engine().begin() as conn:
        conn.execute(delete(subscriptions).where(subscriptions.c.user_id == user_id))
        if items:
            conn.execute(
                insert(subscriptions),
                [{"user_id": user_id, "symbol": s, "threshold_pct": t} for s, t in items.items()],
            )
        conn.execute(update(users).where(users.c.id == user_id).values(active=True))


def active_subscriptions() -> List[dict]:
    """Every (user, symbol, threshold) that should be checked, for active users only."""
    with _auto() as conn:
        rows = conn.execute(
            select(users.c.id, users.c.email, subscriptions.c.symbol, subscriptions.c.threshold_pct)
            .join(subscriptions, subscriptions.c.user_id == users.c.id)
            .where(users.c.active.is_(True))
        ).all()
    return [{"user_id": r[0], "email": r[1], "symbol": r[2], "threshold_pct": r[3]} for r in rows]


# ---------- alert dedupe ----------

def sent_sessions(user_id: int) -> set:
    """(symbol, session) pairs this user has already been alerted about."""
    with _auto() as conn:
        rows = conn.execute(
            select(alerts_sent.c.symbol, alerts_sent.c.session).where(alerts_sent.c.user_id == user_id)
        ).all()
    return {(s, sess) for s, sess in rows}


def record_alerts(user_id: int, items: List[tuple]) -> None:
    """Remember what was sent. Each item is (symbol, session) or (symbol, session, details), where
    details may hold change_pct, price, prev_close and threshold_pct."""
    with engine().begin() as conn:
        for symbol, session, *rest in items:
            extra = {k: v for k, v in (rest[0] if rest else {}).items()
                     if k in ("change_pct", "price", "prev_close", "threshold_pct")}
            exists = conn.execute(
                select(alerts_sent.c.user_id).where(
                    (alerts_sent.c.user_id == user_id)
                    & (alerts_sent.c.symbol == symbol)
                    & (alerts_sent.c.session == session)
                )
            ).first()
            if not exists:
                conn.execute(
                    insert(alerts_sent).values(
                        user_id=user_id, symbol=symbol, session=session, sent_at=_now(), **extra
                    )
                )


def alerts_for_user(user_id: int, limit: int = 100) -> List[dict]:
    """This user's alert history, newest first."""
    with _auto() as conn:
        rows = conn.execute(
            select(alerts_sent).where(alerts_sent.c.user_id == user_id)
            .order_by(alerts_sent.c.sent_at.desc()).limit(limit)
        ).mappings().all()
    return [dict(r) for r in rows]


def set_active(user_id: int, active: bool) -> None:
    with _auto() as conn:
        conn.execute(update(users).where(users.c.id == user_id).values(active=active))


# ---------- sign-in codes ----------

def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def create_login_code(email: str, minutes: int) -> str:
    """A short random code for a sign-in link, valid for `minutes`."""
    # 16 lowercase letters/digits (a-z, 2-7): easy to read, 80 bits of randomness.
    code = base64.b32encode(secrets.token_bytes(10)).decode().lower()
    with engine().begin() as conn:
        conn.execute(delete(login_codes).where(login_codes.c.expires_at < _now()))  # tidy up
        conn.execute(insert(login_codes).values(
            code_hash=_hash(code), email=normalize_email(email), expires_at=_now() + timedelta(minutes=minutes)
        ))
    return code


def create_login_pin(email: str, minutes: int) -> str:
    """A fresh 6-digit code for this email (replacing any earlier one), valid for `minutes`."""
    email = normalize_email(email)
    pin = f"{secrets.randbelow(10 ** 6):06d}"
    with engine().begin() as conn:
        conn.execute(delete(login_pins).where(login_pins.c.expires_at < _now()))  # tidy up
        conn.execute(delete(login_pins).where(login_pins.c.email == email))
        conn.execute(insert(login_pins).values(email=email, pin_hash=_hash(f"{email}:{pin}"), tries=0,
                                               expires_at=_now() + timedelta(minutes=minutes)))
    return pin


def check_login_pin(email: str, pin: str, max_tries: int) -> bool:
    """True once, for the right unexpired code. Every guess uses up one of `max_tries` before it is compared, so
    parallel guesses cannot get extra attempts; at the limit the code is deleted and a new link must be requested."""
    email = normalize_email(email)
    with engine().begin() as conn:
        used = conn.execute(update(login_pins).where(
            (login_pins.c.email == email) & (login_pins.c.expires_at >= _now()) & (login_pins.c.tries < max_tries)
        ).values(tries=login_pins.c.tries + 1)).rowcount
        if used != 1:
            conn.execute(delete(login_pins).where((login_pins.c.email == email) & (login_pins.c.tries >= max_tries)))
            return False
        right = conn.execute(delete(login_pins).where(
            (login_pins.c.email == email) & (login_pins.c.pin_hash == _hash(f"{email}:{pin}")))).rowcount
        return right == 1


def consume_login_code(code: str) -> Optional[str]:
    """Use up a sign-in link: the email it belongs to the first time, None after that or once expired."""
    email = email_for_login_code(code)
    if not email:
        return None
    with engine().begin() as conn:
        gone = conn.execute(delete(login_codes).where(login_codes.c.code_hash == _hash(code))).rowcount
    return email if gone == 1 else None


def clear_login_credentials(email: str) -> None:
    """After someone signs in, neither the other way in (link or code) for that email works any more."""
    email = normalize_email(email)
    with engine().begin() as conn:
        conn.execute(delete(login_pins).where(login_pins.c.email == email))
        conn.execute(delete(login_codes).where(login_codes.c.email == email))


def email_for_login_code(code: str) -> Optional[str]:
    """The email a valid, unexpired sign-in code belongs to, else None."""
    with _auto() as conn:
        row = conn.execute(
            select(login_codes.c.email, login_codes.c.expires_at).where(login_codes.c.code_hash == _hash(code))
        ).first()
    if not row or row[1] < _now():
        return None
    return row[0]


# ---------- buckets ----------

def count_buckets(user_id: int) -> int:
    with _auto() as conn:
        return conn.execute(select(func.count()).select_from(buckets).where(buckets.c.user_id == user_id)).scalar()


def create_bucket(user_id: int, name: str, limit: Optional[int] = None) -> Optional[int]:
    """The new bucket's id, or None if the user is already at `limit` buckets. The limit check is part of the INSERT
    itself (one atomic statement), and the user's row is locked first on Postgres, so simultaneous creates can't overshoot."""
    cap = limit if limit is not None else 10 ** 9
    with engine().begin() as conn:
        conn.execute(select(users.c.id).where(users.c.id == user_id).with_for_update())
        count = select(func.count()).select_from(buckets).where(buckets.c.user_id == user_id).scalar_subquery()
        stmt = (insert(buckets).from_select(["user_id", "name", "created_at"],
                                            select(literal(user_id, Integer), literal(name, String), literal(_now(), DateTime)).where(count < cap))
                .returning(buckets.c.id))
        row = conn.execute(stmt).first()
    return row[0] if row else None


def list_buckets(user_id: int) -> List[dict]:
    with _auto() as conn:
        rows = conn.execute(select(buckets).where(buckets.c.user_id == user_id).order_by(buckets.c.created_at, buckets.c.id)).mappings().all()
    return [dict(r) for r in rows]


def get_bucket(user_id: int, bucket_id: int) -> Optional[dict]:
    """The bucket, but only if it belongs to this user (anyone else's looks like it doesn't exist)."""
    if not 0 < bucket_id < 2**31:  # a made-up huge id in the URL must read as "not found", not crash the database driver
        return None
    with _auto() as conn:
        row = conn.execute(select(buckets).where((buckets.c.id == bucket_id) & (buckets.c.user_id == user_id))).mappings().first()
    return dict(row) if row else None


def set_share(bucket_id: int, token: Optional[str], perf: bool) -> None:
    with _auto() as conn:
        conn.execute(update(buckets).where(buckets.c.id == bucket_id).values(share_token=token, share_perf=perf))


def set_perf_share(user_id: int, token: Optional[str]) -> None:
    with _auto() as conn:
        conn.execute(update(users).where(users.c.id == user_id).values(perf_share_token=token))


def get_perf_share_user(token: str) -> Optional[dict]:
    """The person sharing their performance under this link. Only people currently sharing have a token."""
    if not token or len(token) > 32:
        return None
    with _auto() as conn:
        row = conn.execute(select(users).where((users.c.perf_share_token == token) & users.c.active)).mappings().first()
    return dict(row) if row else None


def get_shared_bucket(token: str) -> Optional[dict]:
    """A bucket by its share link. Only buckets whose owner is currently sharing have a token."""
    if not token or len(token) > 32:
        return None
    with _auto() as conn:
        row = conn.execute(select(buckets).where(buckets.c.share_token == token)).mappings().first()
    return dict(row) if row else None


def rename_bucket(bucket_id: int, name: str) -> None:
    with _auto() as conn:
        conn.execute(update(buckets).where(buckets.c.id == bucket_id).values(name=name))


def delete_bucket(bucket_id: int) -> None:
    with engine().begin() as conn:
        conn.execute(delete(bucket_items).where(bucket_items.c.bucket_id == bucket_id))
        conn.execute(delete(buckets).where(buckets.c.id == bucket_id))


def bucket_items_for(bucket_ids: List[int]) -> Dict[int, List[dict]]:
    out: Dict[int, List[dict]] = {i: [] for i in bucket_ids}
    if not bucket_ids:
        return out
    with _auto() as conn:
        rows = conn.execute(select(bucket_items).where(bucket_items.c.bucket_id.in_(bucket_ids))
                            .order_by(bucket_items.c.added_at, bucket_items.c.symbol)).mappings().all()
    for r in rows:
        out[r["bucket_id"]].append(dict(r))
    return out


def add_bucket_items(bucket_id: int, items, limit: Optional[int] = None) -> Dict[str, str]:
    """Add several stocks in ONE transaction. `items` is [(symbol, added_price)]. Returns {symbol: "added" | "exists" |
    "full"}. Each limit and duplicate check is part of its INSERT statement, and the bucket row is locked first on
    Postgres, so simultaneous requests can't overshoot the limit or double up."""
    cap = limit if limit is not None else 10 ** 9
    mine = bucket_items.c.bucket_id == bucket_id
    out: Dict[str, str] = {}
    with engine().begin() as conn:
        conn.execute(select(buckets.c.id).where(buckets.c.id == bucket_id).with_for_update())
        for symbol, added_price in items:
            if symbol in out:
                continue
            count = select(func.count()).select_from(bucket_items).where(mine).scalar_subquery()
            dup = exists().where(mine & (bucket_items.c.symbol == symbol))
            stmt = insert(bucket_items).from_select(
                ["bucket_id", "symbol", "added_price", "added_at"],
                select(literal(bucket_id, Integer), literal(symbol, String), literal(added_price, Float), literal(_now(), DateTime))
                .where((count < cap) & ~dup))
            try:
                done = conn.execute(stmt).rowcount == 1
            except IntegrityError:  # lost a race on the primary key: someone else added it a moment ago
                out[symbol] = "exists"
                continue
            if done:
                out[symbol] = "added"
            else:
                out[symbol] = "exists" if conn.execute(select(bucket_items.c.symbol).where(mine & (bucket_items.c.symbol == symbol))).first() else "full"
    return out


def add_bucket_item(bucket_id: int, symbol: str, added_price: Optional[float], limit: Optional[int] = None) -> str:
    """One stock: "added", "exists" or "full" (see add_bucket_items)."""
    return add_bucket_items(bucket_id, [(symbol, added_price)], limit)[symbol]


def remove_bucket_item(bucket_id: int, symbol: str) -> None:
    with _auto() as conn:
        conn.execute(delete(bucket_items).where((bucket_items.c.bucket_id == bucket_id) & (bucket_items.c.symbol == symbol)))


def quotes_for(symbols) -> Dict[str, dict]:
    symbols = list(symbols)
    if not symbols:
        return {}
    with _auto() as conn:
        rows = conn.execute(select(quotes_cache).where(quotes_cache.c.symbol.in_(symbols))).mappings().all()
    return {r["symbol"]: dict(r) for r in rows}


def save_quotes(quotes) -> None:
    """Remember the latest price per symbol: one read, then one bulk insert and one bulk update, however many
    symbols there are. `quotes` are objects with symbol, price, prev_close, session."""
    rows = {q.symbol: dict(symbol=q.symbol, price=q.price, prev_close=q.prev_close, session=q.session, updated_at=_now())
            for q in quotes}
    if not rows:
        return
    with engine().begin() as conn:
        have = set()
        names = list(rows)
        for i in range(0, len(names), 500):  # keep each IN list modest
            have.update(r[0] for r in conn.execute(select(quotes_cache.c.symbol).where(quotes_cache.c.symbol.in_(names[i:i + 500]))))
        new = [r for k, r in rows.items() if k not in have]
        old = [{"b_symbol": k, **{c: v for c, v in r.items() if c != "symbol"}} for k, r in rows.items() if k in have]
        if new:
            conn.execute(insert(quotes_cache), new)
        if old:
            conn.execute(update(quotes_cache).where(quotes_cache.c.symbol == bindparam("b_symbol")), old)


def perf_for(symbols) -> Dict[str, dict]:
    symbols = list(symbols)
    if not symbols:
        return {}
    with _auto() as conn:
        rows = conn.execute(select(perf_cache).where(perf_cache.c.symbol.in_(symbols))).mappings().all()
    return {r["symbol"]: dict(r) for r in rows}


def perf_stale(symbols, max_age_hours: int = 20) -> list:
    """Which of `symbols` have no figures yet, or figures older than `max_age_hours`."""
    symbols = list(symbols)
    if not symbols:
        return []
    fresh_after = _now() - timedelta(hours=max_age_hours)
    with _auto() as conn:
        fresh = {r[0] for r in conn.execute(select(perf_cache.c.symbol).where(
            perf_cache.c.symbol.in_(symbols) & (perf_cache.c.updated_at >= fresh_after)))}
    return [s for s in symbols if s not in fresh]


def save_perf(rows: Dict[str, Optional[dict]]) -> None:
    """Replace the figures for each symbol. A symbol with no usable history is stored empty, so it isn't retried every
    hour. One delete and one bulk insert."""
    if not rows:
        return
    keys = ("last_date", "last_price", "r1w", "r1m", "r3m", "r1y", "from_high")
    now = _now()
    batch = [{"symbol": s, "updated_at": now, **{k: (m or {}).get(k) for k in keys}} for s, m in rows.items()]
    with engine().begin() as conn:
        conn.execute(delete(perf_cache).where(perf_cache.c.symbol.in_(list(rows))))
        conn.execute(insert(perf_cache), batch)


def get_summary(user_id: int, digest: str) -> Optional[str]:
    with _auto() as conn:
        row = conn.execute(select(summaries.c.text).where((summaries.c.user_id == user_id) & (summaries.c.digest == digest))).first()
    return row[0] if row else None


def save_summary(user_id: int, digest: str, text: str) -> None:
    with engine().begin() as conn:
        conn.execute(delete(summaries).where(summaries.c.created_at < _now() - timedelta(days=14)))   # tidy up
        conn.execute(delete(summaries).where((summaries.c.user_id == user_id) & (summaries.c.digest == digest)))
        conn.execute(insert(summaries).values(user_id=user_id, digest=digest, text=text, created_at=_now()))


def summaries_today(user_id: Optional[int] = None) -> int:
    """How many summaries were written in the last 24 hours (for one person, or for everyone)."""
    cond = summaries.c.created_at >= _now() - timedelta(hours=24)
    if user_id is not None:
        cond = cond & (summaries.c.user_id == user_id)
    with _auto() as conn:
        return conn.execute(select(func.count()).select_from(summaries).where(cond)).scalar() or 0


def prune_quotes(days: int = 30) -> int:
    """Forget prices for symbols nothing has refreshed in `days` days (stocks nobody follows any more)."""
    with _auto() as conn:
        return conn.execute(delete(quotes_cache).where(quotes_cache.c.updated_at < _now() - timedelta(days=days))).rowcount


def bucket_symbol_sets() -> tuple:
    """(every symbol in any bucket, the symbols that still lack a starting price): two small DISTINCT queries, so the
    alert job can decide what to fetch without loading every bucket row."""
    with _auto() as conn:
        everything = {r[0] for r in conn.execute(select(bucket_items.c.symbol).distinct())}
        unpriced = {r[0] for r in conn.execute(select(bucket_items.c.symbol).where(bucket_items.c.added_price.is_(None)).distinct())}
    return everything, unpriced


def iter_open_bucket_items(batch: int = 5000):
    """Bucket stocks that haven't been red-flagged yet, with their owner, streamed in batches so memory stays flat
    however many there are. (Already-flagged stocks never need looking at again.)"""
    stmt = (select(bucket_items.c.bucket_id, bucket_items.c.symbol, bucket_items.c.added_price, bucket_items.c.flagged_at,
                   bucket_items.c.added_at, bucket_items.c.split_through, bucket_items.c.split_checked_at,
                   buckets.c.name.label("bucket_name"), buckets.c.user_id, users.c.email, users.c.active)
            .select_from(bucket_items.join(buckets, buckets.c.id == bucket_items.c.bucket_id)
                         .join(users, users.c.id == buckets.c.user_id))
            .where(bucket_items.c.flagged_at.is_(None)))
    with engine().connect() as conn:
        for row in conn.execution_options(stream_results=True, yield_per=batch).execute(stmt).mappings():
            yield dict(row)


def update_baseline(bucket_id: int, symbol: str, added_price: float, split_through) -> None:
    """A split or bonus happened after the stock was added: restate its starting price in today's terms, once."""
    with _auto() as conn:
        conn.execute(update(bucket_items).where((bucket_items.c.bucket_id == bucket_id) & (bucket_items.c.symbol == symbol))
                     .values(added_price=added_price, split_through=split_through, split_checked_at=_now()))


def mark_split_checked(pairs: List[tuple]) -> None:
    """Nothing to adjust: remember we looked, so this stock isn't re-checked on every run."""
    with engine().begin() as conn:
        for bucket_id, symbol in pairs:
            conn.execute(update(bucket_items).where((bucket_items.c.bucket_id == bucket_id) & (bucket_items.c.symbol == symbol))
                         .values(split_checked_at=_now()))


def set_added_price(bucket_id: int, symbol: str, price: float) -> None:
    with _auto() as conn:
        conn.execute(update(bucket_items).where((bucket_items.c.bucket_id == bucket_id) & (bucket_items.c.symbol == symbol)
                                                & (bucket_items.c.added_price.is_(None))).values(added_price=price))


def mark_flagged(pairs: List[tuple]) -> None:
    with engine().begin() as conn:
        for bucket_id, symbol in pairs:
            conn.execute(update(bucket_items).where((bucket_items.c.bucket_id == bucket_id) & (bucket_items.c.symbol == symbol)
                                                    & (bucket_items.c.flagged_at.is_(None))).values(flagged_at=_now()))
