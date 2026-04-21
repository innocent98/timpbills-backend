from fastapi import Request
from jose import JWTError, jwt
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import settings


def per_user_or_ip(request: Request) -> str:
    """Rate-limit key: the JWT subject if the request is authenticated,
    otherwise the client IP.

    PRD §9.3 requires money endpoints to be limited per-user, not per-IP,
    so that a shared NAT (carrier network, office) doesn't share the quota.
    We decode the Authorization header to extract the user id and fall back
    to the IP for unauthenticated hits (which shouldn't normally reach the
    money endpoints — they're gated by get_current_user — but we still
    need a key to return a 401 quickly under flood).
    """
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:]
        try:
            payload = jwt.decode(
                token,
                settings.SECRET_KEY,
                algorithms=[settings.ALGORITHM],
                options={"verify_exp": False},  # key derivation only
            )
            sub = payload.get("sub")
            if sub:
                return f"user:{sub}"
        except JWTError:
            pass
    return get_remote_address(request)


limiter = Limiter(key_func=get_remote_address)
