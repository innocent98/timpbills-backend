import re

from app.utils.references import new_transaction_reference


def test_reference_has_expected_shape():
    ref = new_transaction_reference(user_id="abc123def456")
    assert re.match(r"^TMP-\d{6}-[a-z0-9]{6}-[0-9A-HJKMNP-TV-Z]{10,}$", ref), ref


def test_references_are_unique():
    refs = {new_transaction_reference(user_id="u1") for _ in range(1000)}
    assert len(refs) == 1000
