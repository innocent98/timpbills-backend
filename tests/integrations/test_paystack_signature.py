import hashlib
import hmac

from app.integrations.paystack.signature import verify_paystack_signature


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()


def test_accepts_correct_signature():
    body = b'{"event":"charge.success"}'
    secret = "sk_test_xxx"
    sig = _sign(secret, body)
    assert verify_paystack_signature(raw_body=body, signature=sig, secret=secret)


def test_rejects_tampered_signature():
    body = b'{"event":"charge.success"}'
    assert not verify_paystack_signature(
        raw_body=body, signature="deadbeef", secret="sk_test_xxx"
    )


def test_rejects_empty_signature():
    assert not verify_paystack_signature(
        raw_body=b"{}", signature="", secret="x"
    )
