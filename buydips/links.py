"""Links in emails. Sign-in links use short database-backed codes; unsubscribe links are signed tokens
that never expire and need no database lookup."""

from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import config


def _ser(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.SECRET_KEY, salt=salt)


def login_url(code: str) -> str:
    return f"{config.BASE_URL}/signin/{code}"


def unsubscribe_url(user_id: int) -> str:
    return f"{config.BASE_URL}/unsubscribe/{_ser('unsub').dumps(user_id)}"


def read_unsubscribe_token(token: str):
    try:
        return _ser("unsub").loads(token)  # unsubscribe links never expire
    except BadSignature:
        return None
