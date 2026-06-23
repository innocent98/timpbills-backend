"""Email factory env-allowlist guard — mirrors the Termii / VTPass / Paystack
factory tests so the OTP-logging fake can NEVER be silently selected in
staging/production (where a missing key would otherwise mean "don't send"
or the fake would land OTP codes in logs).

The selection rule is identical in spirit to termii's _is_fake_env():
  - eligible env + (FORCE_FAKE or no key) → fake
  - eligible env + key + no force-fake    → real ResendClient
  - non-eligible env + FORCE_FAKE          → raise
  - non-eligible env + no key              → raise
  - non-eligible env + key                 → real ResendClient
"""
import pytest

from app.core.config import settings
from app.integrations.email import factory
from app.integrations.email.fake import FakeEmailClient, SentEmail
from app.integrations.email.resend import ResendClient


def test_dev_with_force_fake_true_returns_fake(monkeypatch):
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_real")
    assert isinstance(factory.select_email_client(), FakeEmailClient)


def test_dev_without_api_key_falls_back_to_fake(monkeypatch):
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(settings, "RESEND_API_KEY", None)
    assert isinstance(factory.select_email_client(), FakeEmailClient)


def test_dev_with_key_and_force_fake_false_returns_real(monkeypatch):
    """The headline behaviour change: real email in dev when a key is set."""
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_real")
    assert isinstance(factory.select_email_client(), ResendClient)


def test_dev_with_blank_key_falls_back_to_fake(monkeypatch):
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(settings, "RESEND_API_KEY", "")
    assert isinstance(factory.select_email_client(), FakeEmailClient)


def test_production_with_force_fake_true_raises(monkeypatch):
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_real")
    with pytest.raises(factory.FakeEmailInEligibleEnvError):
        factory.select_email_client()


def test_staging_with_force_fake_true_raises(monkeypatch):
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "staging")
    with pytest.raises(factory.FakeEmailInEligibleEnvError):
        factory.select_email_client()


def test_production_without_api_key_raises(monkeypatch):
    """Prod missing the key must refuse rather than silently not-send."""
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "RESEND_API_KEY", None)
    with pytest.raises(factory.FakeEmailInEligibleEnvError):
        factory.select_email_client()


def test_unknown_env_with_force_fake_true_raises(monkeypatch):
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "prooduction")
    with pytest.raises(factory.FakeEmailInEligibleEnvError):
        factory.select_email_client()


def test_staging_with_key_uses_real(monkeypatch):
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "staging")
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_real")
    assert isinstance(factory.select_email_client(), ResendClient)


def test_fake_singleton_shared_across_calls(monkeypatch):
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    a = factory.select_email_client()
    b = factory.select_email_client()
    assert a is b


def test_reset_clears_singleton_in_place(monkeypatch):
    # Reset must clear captured emails WITHOUT rebinding — api/e2e tests and
    # the Celery worker hold an import-time reference to the singleton.
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    before = factory.select_email_client()
    before.sent.append(SentEmail(to="a@a.co", subject="s", code_or_body="123456"))
    factory.reset_fake_singleton()
    after = factory.select_email_client()
    assert after is before
    assert after.sent == []
