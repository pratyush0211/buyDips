"""Database layer (SQLAlchemy Core). SQLite locally, Postgres in production, same code."""

from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Integer, MetaData, PrimaryKeyConstraint,
    String, Table, create_engine, delete, insert, select, update,
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
    PrimaryKeyConstraint("user_id", "symbol", "session"),
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


def record_alerts(user_id: int, pairs: List[tuple]) -> None:
    with engine().begin() as conn:
        for symbol, session in pairs:
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
                        user_id=user_id, symbol=symbol, session=session, sent_at=_now()
                    )
                )

