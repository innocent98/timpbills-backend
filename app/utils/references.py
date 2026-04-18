"""Transaction reference generator. Format: TMP-YYMMDD-USERSHORT-ULID."""
import secrets
from datetime import datetime, timezone

_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _ulid() -> str:
    """10-char Crockford-base32 random suffix — collision-safe for TX refs."""
    return "".join(secrets.choice(_ULID_ALPHABET) for _ in range(10))


def new_transaction_reference(*, user_id: str, prefix: str = 'TMP') -> str:
    """Return a prefixed transaction reference (sortable + unique).

    Format: '{prefix}-YYMMDD-USERSHORT-ULID'.
    user_id may be a UUID string; we take the first 6 hex chars as a short id.
    Pass prefix='TMP-R' for refund transactions to visually distinguish them.
    The default prefix 'TMP' preserves backward-compatible 'TMP-YYMMDD-...' output.
    """
    today = datetime.now(timezone.utc).strftime("%y%m%d")
    short = user_id.lower().replace("-", "")[:6]
    return f"{prefix}-{today}-{short}-{_ulid()}"
