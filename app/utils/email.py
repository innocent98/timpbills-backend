"""Email address normalisation (case-insensitive).

The domain part of an email address is case-insensitive by definition
(DNS is case-insensitive), and while RFC 5321 permits the local-part to
be case-sensitive, virtually every real-world provider (Gmail, Outlook,
Yahoo, corporate M365, etc.) treats it case-insensitively. So we
lowercase the *whole* address to guarantee ``Example@Mail.com`` and
``example@mail.com`` resolve to the same account.

This mirrors ``app.utils.phone.normalize_to_e164`` (the primary login
identifier is normalised at the boundary) and the admin auth paths
(``admin_auth`` login + ``create_admin``) which already ``.lower()``
the email before lookup/store. Unlike phone normalisation this never
raises: shape validation is Pydantic's ``EmailStr`` job — this util
only canonicalises case + surrounding whitespace.
"""


def normalize_email(email: str) -> str:
    """Return the canonical (lowercased, stripped) form of ``email``."""
    return email.strip().lower()
