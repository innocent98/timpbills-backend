"""Webhook authentication for /webhooks/vtpass.

VTPass does NOT sign webhook bodies (unlike Paystack's HMAC-SHA512). We
instead check a shared secret header `X-VTPass-Secret` that we configure
on both sides. Weaker than HMAC but still catches the common threat:
adversary POSTing a forged webhook to our endpoint without knowing the
secret.

Use `hmac.compare_digest` for the comparison so the check is timing-safe
— a naive `==` leaks the correct prefix via response-time analysis.
"""
import hmac

from app.core.config import settings


class WebhookSecretNotConfigured(RuntimeError):
    """Raised when VTPASS_WEBHOOK_SECRET is unset — the endpoint must
    refuse rather than accept the forged request."""


def verify_vtpass_secret(*, header_value: str | None) -> bool:
    """Return True iff the caller's `X-VTPass-Secret` header matches our
    configured secret. Returns False on missing header; raises on missing
    server-side config so we never silently accept."""
    expected = settings.VTPASS_WEBHOOK_SECRET
    if not expected:
        raise WebhookSecretNotConfigured(
            "VTPASS_WEBHOOK_SECRET must be set before /webhooks/vtpass "
            "can accept requests. Refusing to proceed."
        )
    if not header_value:
        return False
    return hmac.compare_digest(header_value, expected)
