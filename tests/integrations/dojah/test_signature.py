import hashlib
import hmac

from app.core.config import settings
from app.integrations.dojah.signature import verify_dojah_signature


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_valid_and_invalid_signature(monkeypatch):
    monkeypatch.setattr(settings, "DOJAH_WEBHOOK_SECRET", "shh")
    body = b'{"reference_id":"PASS-BVN-1"}'
    sig = _sign("shh", body)
    assert verify_dojah_signature(body, sig) is True
    assert verify_dojah_signature(body, "deadbeef") is False


def test_rejects_missing_signature(monkeypatch):
    monkeypatch.setattr(settings, "DOJAH_WEBHOOK_SECRET", "shh")
    assert verify_dojah_signature(b"{}", "") is False
    assert verify_dojah_signature(b"{}", None) is False


def test_rejects_when_secret_unset(monkeypatch):
    monkeypatch.setattr(settings, "DOJAH_WEBHOOK_SECRET", None)
    body = b'{"reference_id":"PASS-BVN-1"}'
    sig = _sign("shh", body)
    assert verify_dojah_signature(body, sig) is False
