"""Termii SMS factory env-allowlist guard — mirrors the VTPass / Paystack
factory tests so the OTP-logging fake can NEVER be selected in
staging/production (where the code would land in logs)."""
import pytest

from app.core.config import settings
from app.integrations.termii import factory
from app.integrations.termii.client import TermiiClient
from app.integrations.termii.fake import FakeTermiiClient, SentMessage


def test_dev_with_force_fake_true_returns_fake(monkeypatch):
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    assert isinstance(factory.select_sms_client(), FakeTermiiClient)


def test_dev_without_api_key_falls_back_to_fake(monkeypatch):
    """Dev boot without a Termii key should not crash — it uses the fake
    so the backend can run for UI testing."""
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(settings, "TERMII_API_KEY", None)
    assert isinstance(factory.select_sms_client(), FakeTermiiClient)


def test_dev_with_key_and_force_fake_false_returns_real(monkeypatch):
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(settings, "TERMII_API_KEY", "tk_real")
    assert isinstance(factory.select_sms_client(), TermiiClient)


def test_production_with_force_fake_true_raises(monkeypatch):
    """The go-live safety case: FORCE_FAKE_PROVIDERS=true leaking into
    prod must refuse loudly so OTP codes never hit stdout."""
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    with pytest.raises(factory.FakeTermiiInEligibleEnvError):
        factory.select_sms_client()


def test_staging_with_force_fake_true_raises(monkeypatch):
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "staging")
    with pytest.raises(factory.FakeTermiiInEligibleEnvError):
        factory.select_sms_client()


def test_production_without_api_key_raises(monkeypatch):
    """Even with FORCE_FAKE_PROVIDERS=false, a prod env missing the key
    must refuse rather than silently fall back to the OTP-logging fake."""
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "TERMII_API_KEY", None)
    with pytest.raises(factory.FakeTermiiInEligibleEnvError):
        factory.select_sms_client()


def test_unknown_env_with_force_fake_true_raises(monkeypatch):
    """A non-canonical / typo'd ENVIRONMENT must not silently pick the fake —
    use a value that is neither an eligible env nor a known prod/staging name."""
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "prooduction")
    with pytest.raises(factory.FakeTermiiInEligibleEnvError):
        factory.select_sms_client()


def test_staging_with_key_uses_real(monkeypatch):
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "staging")
    monkeypatch.setattr(settings, "TERMII_API_KEY", "tk_real")
    assert isinstance(factory.select_sms_client(), TermiiClient)


def test_fake_singleton_shared_across_calls(monkeypatch):
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    a = factory.select_sms_client()
    b = factory.select_sms_client()
    assert a is b


def test_reset_clears_singleton_in_place(monkeypatch):
    # Reset must clear the captured messages WITHOUT rebinding the object —
    # api/e2e tests hold an import-time reference to it, so identity must hold.
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    before = factory.select_sms_client()
    before.sent.append(SentMessage(phone="+2348011111111", code_or_message="123456"))
    factory.reset_fake_singleton()
    after = factory.select_sms_client()
    assert after is before          # identity stable for import-bound refs
    assert after.sent == []         # captured messages cleared
