import pytest

from app.utils.email import normalize_email


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Example@Mail.com", "example@mail.com"),
        ("example@mail.com", "example@mail.com"),
        ("USER@EXAMPLE.CO", "user@example.co"),
        ("  Spaced@X.com  ", "spaced@x.com"),
        ("\tTab@X.com\n", "tab@x.com"),
        ("MixedCASE.Local+tag@Sub.Domain.COM", "mixedcase.local+tag@sub.domain.com"),
    ],
)
def test_normalize_lowercases_and_trims(raw, expected):
    assert normalize_email(raw) == expected


def test_normalize_is_idempotent():
    once = normalize_email("Example@X.com")
    assert normalize_email(once) == once
