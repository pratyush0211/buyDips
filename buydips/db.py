"""Database layer (SQLAlchemy Core). SQLite locally, Postgres in production, same code."""

import base64
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import (
    Boolean, Column, DateTime, false, Float, ForeignKey, Integer, MetaData, PrimaryKeyConstraint,
    String, Table, create_engine, delete, func, insert, inspect, select, text, update,
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
)

bucket_items = Table(
    "bucket_items", metadata,
    Column("bucket_id", Integer, ForeignKey("buckets.id", ondelete="CASCADE"), nullable=False),
    Column("symbol", String(40), nullable=False),
    Column("added_price", Float),   # price when the stock was added; filled by the next sync if it wasn't known yet
    Column("added_at", DateTime, nullable=False),
    Column("flagged_at", DateTime),  # set once, when the red-flag email for this stock has been sent
    PrimaryKeyConstraint("bucket_id", "symbol"),
)

# The latest price per symbol, refreshed by the alert job, so pages never call the data provider.
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

_engine = None


def engine():
    global _engine
    if _engine is None:
        _engine = create_engine(config.DATABASE_URL, pool_pre_ping=True)
    return _engine


def reset_engine(url: Optional[str] = None) -> None:
    """Point at a different database (used by tests)."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = create_engine(url or config.DATABASE_URL, pool_pre_ping=True)


def init_db() -> None:
    metadata.create_all(engine())
    _migrate()


def _migrate() -> None:
    """Add columns introduced after the first deploy (create_all never alters existing tables)."""
    wanted = [("users", "first_signin_at", "TIMESTAMP"), ("alerts_sent", "change_pct", "DOUBLE PRECISION"),
              ("alerts_sent", "price", "DOUBLE PRECISION"), ("alerts_sent", "prev_close", "DOUBLE PRECISION"),
              ("alerts_sent", "threshold_pct", "DOUBLE PRECISION"),
              ("buckets", "share_token", "VARCHAR(32)"), ("buckets", "share_perf", "BOOLEAN DEFAULT FALSE")]
    for table, column, ddl in wanted:
        if column in {c["name"] for c in inspect(engine()).get_columns(table)}:
            continue
        try:
            with engine().begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                if column == "first_signin_at":
                    # Everyone who already exists counts as already welcomed, so they don't trigger notices.
                    conn.execute(text("UPDATE users SET first_signin_at = created_at WHERE first_signin_at IS NULL"))
        except Exception:  # noqa: BLE001  (another worker may have added it at the same moment)
            if column not in {c["name"] for c in inspect(engine()).get_columns(table)}:
                raise


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
    with engine().connect() as conn:
        return conn.execute(select(func.count()).select_from(users)).scalar()


def get_user(user_id: int) -> Optional[dict]:
    with engine().connect() as conn:
        row = conn.execute(select(users).where(users.c.id == user_id)).mappings().first()
        return dict(row) if row else None


def seconds_since_login_link(email: str) -> Optional[float]:
    """Seconds since we last emailed this address a sign-in link (None if never)."""
    with engine().connect() as conn:
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
    with engine().connect() as conn:
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
    with engine().connect() as conn:
        rows = conn.execute(
            select(users.c.id, users.c.email, subscriptions.c.symbol, subscriptions.c.threshold_pct)
            .join(subscriptions, subscriptions.c.user_id == users.c.id)
            .where(users.c.active.is_(True))
        ).all()
    return [{"user_id": r[0], "email": r[1], "symbol": r[2], "threshold_pct": r[3]} for r in rows]


# ---------- alert dedupe ----------

def sent_sessions(user_id: int) -> set:
    """(symbol, session) pairs this user has already been alerted about."""
    with engine().connect() as conn:
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
    with engine().connect() as conn:
        rows = conn.execute(
            select(alerts_sent).where(alerts_sent.c.user_id == user_id)
            .order_by(alerts_sent.c.sent_at.desc()).limit(limit)
        ).mappings().all()
    return [dict(r) for r in rows]


def set_active(user_id: int, active: bool) -> None:
    with engine().begin() as conn:
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


def email_for_login_code(code: str) -> Optional[str]:
    """The email a valid, unexpired sign-in code belongs to, else None."""
    with engine().connect() as conn:
        row = conn.execute(
            select(login_codes.c.email, login_codes.c.expires_at).where(login_codes.c.code_hash == _hash(code))
        ).first()
    if not row or row[1] < _now():
        return None
    return row[0]


# ---------- buckets ----------

def count_buckets(user_id: int) -> int:
    with engine().connect() as conn:
        return conn.execute(select(func.count()).select_from(buckets).where(buckets.c.user_id == user_id)).scalar()


def create_bucket(user_id: int, name: str) -> int:
    with engine().begin() as conn:
        res = conn.execute(insert(buckets).values(user_id=user_id, name=name, created_at=_now()))
        return int(res.inserted_primary_key[0])


def list_buckets(user_id: int) -> List[dict]:
    with engine().connect() as conn:
        rows = conn.execute(select(buckets).where(buckets.c.user_id == user_id).order_by(buckets.c.created_at, buckets.c.id)).mappings().all()
    return [dict(r) for r in rows]


def get_bucket(user_id: int, bucket_id: int) -> Optional[dict]:
    """The bucket, but only if it belongs to this user (anyone else's looks like it doesn't exist)."""
    with engine().connect() as conn:
        row = conn.execute(select(buckets).where((buckets.c.id == bucket_id) & (buckets.c.user_id == user_id))).mappings().first()
    return dict(row) if row else None


def set_share(bucket_id: int, token: Optional[str], perf: bool) -> None:
    with engine().begin() as conn:
        conn.execute(update(buckets).where(buckets.c.id == bucket_id).values(share_token=token, share_perf=perf))


def get_shared_bucket(token: str) -> Optional[dict]:
    """A bucket by its share link. Only buckets whose owner is currently sharing have a token."""
    if not token:
        return None
    with engine().connect() as conn:
        row = conn.execute(select(buckets).where(buckets.c.share_token == token)).mappings().first()
    return dict(row) if row else None


def rename_bucket(bucket_id: int, name: str) -> None:
    with engine().begin() as conn:
        conn.execute(update(buckets).where(buckets.c.id == bucket_id).values(name=name))


def delete_bucket(bucket_id: int) -> None:
    with engine().begin() as conn:
        conn.execute(delete(bucket_items).where(bucket_items.c.bucket_id == bucket_id))
        conn.execute(delete(buckets).where(buckets.c.id == bucket_id))


def bucket_items_for(bucket_ids: List[int]) -> Dict[int, List[dict]]:
    out: Dict[int, List[dict]] = {i: [] for i in bucket_ids}
    if not bucket_ids:
        return out
    with engine().connect() as conn:
        rows = conn.execute(select(bucket_items).where(bucket_items.c.bucket_id.in_(bucket_ids))
                            .order_by(bucket_items.c.added_at, bucket_items.c.symbol)).mappings().all()
    for r in rows:
        out[r["bucket_id"]].append(dict(r))
    return out


def add_bucket_item(bucket_id: int, symbol: str, added_price: Optional[float]) -> bool:
    """False if the stock is already in the bucket."""
    with engine().begin() as conn:
        if conn.execute(select(bucket_items.c.symbol).where(
                (bucket_items.c.bucket_id == bucket_id) & (bucket_items.c.symbol == symbol))).first():
            return False
        conn.execute(insert(bucket_items).values(bucket_id=bucket_id, symbol=symbol, added_price=added_price, added_at=_now()))
    return True


def remove_bucket_item(bucket_id: int, symbol: str) -> None:
    with engine().begin() as conn:
        conn.execute(delete(bucket_items).where((bucket_items.c.bucket_id == bucket_id) & (bucket_items.c.symbol == symbol)))


def quotes_for(symbols) -> Dict[str, dict]:
    symbols = list(symbols)
    if not symbols:
        return {}
    with engine().connect() as conn:
        rows = conn.execute(select(quotes_cache).where(quotes_cache.c.symbol.in_(symbols))).mappings().all()
    return {r["symbol"]: dict(r) for r in rows}


def save_quotes(quotes) -> None:
    """Remember the latest price per symbol. `quotes` are objects with symbol, price, prev_close, session."""
    with engine().begin() as conn:
        for q in quotes:
            vals = dict(price=q.price, prev_close=q.prev_close, session=q.session, updated_at=_now())
            if conn.execute(select(quotes_cache.c.symbol).where(quotes_cache.c.symbol == q.symbol)).first():
                conn.execute(update(quotes_cache).where(quotes_cache.c.symbol == q.symbol).values(**vals))
            else:
                conn.execute(insert(quotes_cache).values(symbol=q.symbol, **vals))


def all_bucket_items() -> List[dict]:
    """Every bucket stock across all users, with who owns it. Used by the alert job."""
    with engine().connect() as conn:
        rows = conn.execute(
            select(bucket_items.c.bucket_id, bucket_items.c.symbol, bucket_items.c.added_price, bucket_items.c.flagged_at,
                   buckets.c.name.label("bucket_name"), buckets.c.user_id, users.c.email, users.c.active)
            .select_from(bucket_items.join(buckets, buckets.c.id == bucket_items.c.bucket_id)
                         .join(users, users.c.id == buckets.c.user_id))
        ).mappings().all()
    return [dict(r) for r in rows]


def set_added_price(bucket_id: int, symbol: str, price: float) -> None:
    with engine().begin() as conn:
        conn.execute(update(bucket_items).where((bucket_items.c.bucket_id == bucket_id) & (bucket_items.c.symbol == symbol)
                                                & (bucket_items.c.added_price.is_(None))).values(added_price=price))


def mark_flagged(pairs: List[tuple]) -> None:
    with engine().begin() as conn:
        for bucket_id, symbol in pairs:
            conn.execute(update(bucket_items).where((bucket_items.c.bucket_id == bucket_id) & (bucket_items.c.symbol == symbol)
                                                    & (bucket_items.c.flagged_at.is_(None))).values(flagged_at=_now()))
