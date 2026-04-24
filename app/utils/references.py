"""Transaction reference generator.

Format: ``{YYYYMMDDHHMI}{PREFIX}{USERSHORT}{ULID}``

Why this shape:
  * VTPass requires their ``request_id`` to start with 12 numeric
    characters representing the current date-time in Africa/Lagos as
    ``YYYYMMDDHHMI`` (see
    https://vtpass.com/documentation/how-to-generate-request-id/). We use
    ``tx.reference`` directly as the VTPass ``request_id`` for bill
    purchases, so the reference must satisfy that spec or VTPass parks
    the transaction in pending forever.
  * Positions 13+ may be any alphanumeric. We embed a short prefix
    (``TMP`` for regular, ``TMPR`` for refunds) right after the date so
    the transaction class is still greppable from the reference string.
  * We keep 6 hex chars of the user id + a 10-char Crockford base-32
    ULID suffix for collision safety and operator-side triage.

Example references:
  * purchase : ``202604241530TMPfb14b10D50G291W0``  (30 chars)
  * refund   : ``202604241530TMPRfb14b10D50G291W0`` (31 chars)
"""
import secrets
from datetime import datetime
from zoneinfo import ZoneInfo

_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_LAGOS = ZoneInfo("Africa/Lagos")


def _ulid() -> str:
    """10-char Crockford-base32 random suffix — collision-safe for TX refs."""
    return "".join(secrets.choice(_ULID_ALPHABET) for _ in range(10))


def new_transaction_reference(*, user_id: str, prefix: str = 'TMP') -> str:
    """Return a VTPass-compliant, sortable, unique transaction reference.

    ``user_id`` may be a UUID string; the first 6 hex chars are embedded
    as a short id so operators can tie a reference back to a user without
    querying the db.

    ``prefix`` is placed after the 12-digit timestamp so the reference
    still starts with a numeric date that satisfies VTPass's
    ``request_id`` spec. Use ``prefix='TMPR'`` for refund transactions
    to visually distinguish them from regular purchases.
    """
    stamp = datetime.now(_LAGOS).strftime("%Y%m%d%H%M")
    short = user_id.lower().replace("-", "")[:6]
    return f"{stamp}{prefix}{short}{_ulid()}"
