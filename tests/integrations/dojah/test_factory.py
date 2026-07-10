from app.core.config import settings
from app.integrations.dojah.client import DojahClient
from app.integrations.dojah.factory import get_kyc_provider
from app.integrations.dojah.fake import FakeKycProvider


def test_factory_uses_fake_when_forced(monkeypatch):
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    monkeypatch.setattr(settings, "DOJAH_API_KEY", "some-key")
    assert isinstance(get_kyc_provider(), FakeKycProvider)


def test_factory_uses_fake_when_key_unset(monkeypatch):
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", False)
    monkeypatch.setattr(settings, "DOJAH_API_KEY", None)
    assert isinstance(get_kyc_provider(), FakeKycProvider)


def test_factory_uses_real_client_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", False)
    monkeypatch.setattr(settings, "DOJAH_API_KEY", "some-key")
    monkeypatch.setattr(settings, "DOJAH_APP_ID", "some-app-id")
    assert isinstance(get_kyc_provider(), DojahClient)
