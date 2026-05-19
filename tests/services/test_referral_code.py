"""Unit tests for the referral-code generator.

The helper is intentionally db-free — callers pass in a `code_exists`
callable so we can exercise collision-retry behaviour without spinning up
a session.
"""
import re

import pytest

from app.services.referral_code import (
    _AMBIGUOUS_PATTERN,
    _PRIMARY_LENGTH,
    _SAFE_ALPHABET,
    _strip_ambiguous,
    generate_referral_code,
)


def test_generated_code_is_six_chars_when_first_draw_is_unique():
    code = generate_referral_code(code_exists=lambda _c: False)

    assert len(code) == _PRIMARY_LENGTH


def test_generated_code_uses_only_safe_alphabet():
    code = generate_referral_code(code_exists=lambda _c: False)

    assert all(ch in _SAFE_ALPHABET for ch in code), code


def test_generated_code_never_contains_ambiguous_chars():
    # 200 trials — the alphabet excludes 0/O/1/I/L so this is just defence
    # in depth against a future regression that re-introduces them.
    for _ in range(200):
        code = generate_referral_code(code_exists=lambda _c: False)
        assert not _AMBIGUOUS_PATTERN.search(code), code


def test_collision_retry_eventually_returns_unique_code():
    """When the first N attempts collide, the helper must keep trying
    within the 5-attempt primary budget and still return a 6-char code."""
    seen = []

    def fake_exists(candidate: str) -> bool:
        seen.append(candidate)
        return len(seen) < 3  # first 2 collide, 3rd is unique

    code = generate_referral_code(code_exists=fake_exists)

    assert len(code) == _PRIMARY_LENGTH
    assert len(seen) == 3


def test_fallback_to_seven_chars_when_all_primary_attempts_collide():
    """Pathological case: every 6-char draw collides. The helper bumps
    to 7 chars on the fallback — verifies the safety net fires."""
    attempts = []

    def always_exists(candidate: str) -> bool:
        attempts.append(candidate)
        return True

    code = generate_referral_code(code_exists=always_exists)

    # 5 collided primary attempts + 1 fallback draw (which is NOT
    # checked for collisions — pure safety net).
    assert len(attempts) == 5
    assert all(len(a) == _PRIMARY_LENGTH for a in attempts)
    assert len(code) == 7


def test_strip_ambiguous_replaces_each_ambiguous_char():
    """The brainstorm spec lists `_strip_ambiguous` as a public-ish
    helper for cases where a code is sourced from outside the generator
    (e.g. a test seeding a particular collision). Verify it only touches
    the ambiguous five."""
    out = _strip_ambiguous("0OIL1ABC")

    assert len(out) == 8
    # The original alphanumerics in the safe alphabet must be untouched.
    assert out.endswith("ABC"), out
    # The first 5 positions must have been redrawn from the safe alphabet.
    assert all(ch in _SAFE_ALPHABET for ch in out[:5]), out


def test_code_exists_is_called_with_the_candidate_string():
    captured = []

    def capture(candidate: str) -> bool:
        captured.append(candidate)
        return False

    code = generate_referral_code(code_exists=capture)

    assert captured == [code]


def test_codes_are_well_distributed_over_safe_alphabet():
    """Sanity check: across 500 draws each character of the alphabet
    should appear at least once. This catches regressions where the
    alphabet accidentally shrinks (e.g. a typo removes half the letters)."""
    chars_seen: set[str] = set()
    for _ in range(500):
        code = generate_referral_code(code_exists=lambda _c: False)
        chars_seen.update(code)

    missing = set(_SAFE_ALPHABET) - chars_seen
    assert not missing, f"alphabet chars never appeared in 500 draws: {missing}"
