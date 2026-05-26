"""Test helper for the B9-era /auth/email/verify contract.

B9 changed the response shape: a fresh registration verifying email
returns ``next_action=phone_verification_required`` with NO tokens.
Tokens are only issued on the migration branch (email last gate +
phone already verified + PIN already set).

Sibling test files registered + verified email + grabbed tokens to
build their auth headers. To keep that contract working without
waiting for B10 (/phone/verify unauth) + B11 (/pin/set scoped-token),
this helper pre-stamps the row right after register so /email/verify
takes the ``tokens_issued`` branch.

This is a TEST helper only — production code must not rely on it.
"""
from __future__ import annotations

from app.api.deps import get_db
from app.core.security import hash_pin
from app.db.models.user import User
from app.main import app


def stamp_for_email_verify_tokens(*, email: str, pin: str = "8527") -> None:
    """Force the user row identified by ``email`` into the migration
    state so the *next* /auth/email/verify call returns full tokens.

    Sets ``is_phone_verified=True`` + ``pin_hash`` so the migration
    branch fires. Deliberately does NOT promote ``kyc_level`` —
    downstream tests that pre-date B9 assume the seed leaves the user
    at tier_0 (₦50,000 cap). The KYC promotion happens in the real
    flow inside ``verify_phone_otp``; we are bypassing that step here
    for token issuance only.

    Resolves the DB session through the active ``get_db`` override
    installed by the test's ``client`` fixture; no extra fixture wiring
    needed in caller files.
    """
    override = app.dependency_overrides.get(get_db)
    if override is None:  # pragma: no cover — guards against fixture drift
        raise RuntimeError("stamp_for_email_verify_tokens requires the client fixture")
    gen = override()
    try:
        db = next(gen)
    except StopIteration as exc:  # pragma: no cover
        raise RuntimeError("get_db override yielded nothing") from exc

    user = db.query(User).filter(User.email == email).first()
    if user is None:
        raise AssertionError(f"no user with email={email!r} — call after /auth/register")
    user.is_phone_verified = True
    user.pin_hash = hash_pin(pin)
    db.commit()
