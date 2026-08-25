"""Dojah HMAC-SHA256 webhook signature verifier.

Per Dojah docs: the `x-dojah-signature` header is HMAC-SHA256 of the raw
request body, hex-encoded, using our webhook secret. Mirrors
`app/integrations/paystack/signature.py` (which uses sha512 + an
explicitly-passed secret); this one reads `settings.DOJAH_WEBHOOK_SECRET`
directly since the webhook route has no other natural place to source it.
"""
import hashlib
import hmac

from app.core.config import settings


def verify_dojah_signature(raw_body: bytes, signature: str | None) -> bool:
    secret = settings.DOJAH_WEBHOOK_SECRET
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
