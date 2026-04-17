"""Paystack HMAC-SHA512 signature verifier.

Per Paystack docs: sign the raw request body with your secret key, hex-encode
with sha512, and compare (constant-time) against the x-paystack-signature header.
"""
import hashlib
import hmac


def verify_paystack_signature(
    *, raw_body: bytes, signature: str | None, secret: str
) -> bool:
    if not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature)
