"""Referral code generation.

Sprint 5b. Pulled into its own module so it's testable without touching
the AuthService or DB session machinery — callers pass a `code_exists`
callback (typically a closure over `db.query(User).filter_by(...)`).

Design choices (locked in spec §2-Q5 + §5.1):

* 6 characters, upper-case alphanumeric, ambiguous chars stripped
  (0/O, 1/I/L collapse to a curated alphabet). This trades ~10 bits of
  entropy against support-call friction when users dictate codes verbally.
* System-generated only. No vanity codes in v1.
* Collision-retry up to 5 times at 6 chars; one fallback attempt at 7
  chars if all 5 collide. With ~3.4×10⁸ usable 6-char codes the
  probability of even one collision in a 10-million-user base is
  negligible — the fallback is for paranoia, not realism.
"""
import re
import secrets

# Curated alphabet — drops 0, O, 1, I, L so a code dictated over the
# phone won't get garbled. We keep digits 2-9 and letters A-Z minus the
# ambiguous five.
_SAFE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_AMBIGUOUS_PATTERN = re.compile(r"[0OIL1]")

_PRIMARY_LENGTH = 6
_FALLBACK_LENGTH = 7
_MAX_PRIMARY_ATTEMPTS = 5


def _random_code(length: int) -> str:
    """Generate one candidate code of the given length using the safe
    alphabet. We don't use ``secrets.token_urlsafe`` directly because its
    output includes characters outside the safe alphabet; doing the
    substitution after the fact is fine, but easier and cheaper to draw
    from the safe alphabet up front.
    """
    return "".join(secrets.choice(_SAFE_ALPHABET) for _ in range(length))


def _strip_ambiguous(code: str) -> str:
    """Replace any ambiguous character with a fresh draw from the safe
    alphabet. Public for testability — callers that source codes from
    elsewhere (e.g. tests pre-seeding particular collisions) can pre-clean
    them with this helper.
    """
    return _AMBIGUOUS_PATTERN.sub(
        lambda _m: secrets.choice(_SAFE_ALPHABET), code
    )


def generate_referral_code(code_exists) -> str:
    """Generate a unique referral code.

    ``code_exists`` is a callable that takes a candidate string and
    returns True iff the code is already in use. The caller owns the DB
    binding; this helper only cares about uniqueness.

    Returns a 6-character code unless the (vanishingly unlikely) 5
    consecutive collisions happen, in which case it bumps to 7 chars
    once. The fallback is a safety net — never expected to fire.
    """
    for _ in range(_MAX_PRIMARY_ATTEMPTS):
        candidate = _random_code(_PRIMARY_LENGTH)
        if not code_exists(candidate):
            return candidate
    return _random_code(_FALLBACK_LENGTH)
