"""Signed, tamper-proof tokens for sign-in links and unsubscribe links (no extra DB tables)."""

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import config


def _ser(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.SECRET_KEY, salt=salt)


def login_token(email: str) -> str:
    return _ser("login").dumps(email)


def read_login_token(token: str):
    """The email in a valid, unexpired sign-in token, else None."""
    try:
        return _ser("login").loads(token, max_age=config.LOGIN_LINK_MINUTES * 60)
    except (BadSignature, SignatureExpired):
        return None


def login_url(email: str) -> str:
    return f"{config.BASE_URL}/auth/{login_token(email)}"


def unsubscribe_url(user_id: int) -> str:
    return f"{config.BASE_URL}/unsubscribe/{_ser('unsub').dumps(user_id)}"


def read_unsubscribe_token(token: str):
    try:
        return _ser("unsub").loads(token)  # unsubscribe links never expire
    except BadSignature:
        return None
