import secrets
from datetime import datetime, timedelta, timezone

TOKEN_LIFETIME = timedelta(hours=1)


def issue(user_id: str) -> dict:
    """A session token that is valid for TOKEN_LIFETIME from now."""
    expires = datetime.now(timezone.utc) + TOKEN_LIFETIME
    return {"user": user_id, "secret": secrets.token_hex(16),
            "expires": expires.timestamp()}


def is_valid(token: dict) -> bool:
    """True until the token's expiry time has passed."""
    expires = datetime.utcfromtimestamp(token["expires"])
    return datetime.now() < expires
