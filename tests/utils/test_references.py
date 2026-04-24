"""Tests for the VTPass-compliant transaction reference generator.

See `app/utils/references.py` module docstring for the format spec.
VTPass rejects request_ids that don't start with 12 numeric chars in
YYYYMMDDHHMI (Africa/Lagos); missing the compliance silently parks
every bill purchase in `processing`.
"""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from app.utils.references import new_transaction_reference


# {12-digit date-time}{TMP|TMPR}{6 hex}{10 Crockford b32}
# Regular purchase: 28 chars total
# Refund         : 29 chars total
_SHAPE_RE = re.compile(r"^(\d{12})(TMPR?)([a-z0-9]{6})([0-9A-HJKMNP-TV-Z]{10,})$")


def test_reference_has_expected_shape():
    ref = new_transaction_reference(user_id="abc123def456")
    assert _SHAPE_RE.match(ref), ref


def test_refund_reference_has_R_marker():
    ref = new_transaction_reference(user_id="abc123def456", prefix="TMPR")
    match = _SHAPE_RE.match(ref)
    assert match, ref
    assert match.group(2) == "TMPR"


def test_reference_starts_with_valid_YYYYMMDDHHMI():
    """The first 12 chars must be a real date-time in Africa/Lagos so
    VTPass's `request_id` parser accepts the reference. See
    https://vtpass.com/documentation/how-to-generate-request-id/.
    """
    ref = new_transaction_reference(user_id="abc123def456")
    stamp = ref[:12]
    # Must parse; must be within a minute of now in Africa/Lagos.
    parsed = datetime.strptime(stamp, "%Y%m%d%H%M").replace(
        tzinfo=ZoneInfo("Africa/Lagos"),
    )
    now = datetime.now(ZoneInfo("Africa/Lagos"))
    assert abs((now - parsed).total_seconds()) < 120, (
        f"reference timestamp {stamp} diverges from now {now}"
    )


def test_references_are_unique():
    refs = {new_transaction_reference(user_id="u1") for _ in range(1000)}
    assert len(refs) == 1000
