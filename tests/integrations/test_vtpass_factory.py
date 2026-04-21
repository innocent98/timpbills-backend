"""VTPass factory env-allowlist guard — mirrors the Paystack factory
tests (S2C-6) so the two providers have identical safety properties."""
import pytest

from app.core.config import settings
from app.integrations.vtpass import factory
from app.integrations.vtpass.client import VTPassClient
from app.integrations.vtpass.fake import FakeVTPassClient


def test_dev_with_force_fake_true_returns_fake(monkeypatch):
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    assert isinstance(factory.select_vtpass_client(), FakeVTPassClient)


def test_dev_without_real_keys_falls_back_to_fake(monkeypatch):
    """Dev boot without VTPass credentials should not crash — it should
    silently use the fake so the backend can run for UI testing."""
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(settings, "VTPASS_SECRET_KEY", None)
    monkeypatch.setattr(settings, "VTPASS_API_KEY", None)
    assert isinstance(factory.select_vtpass_client(), FakeVTPassClient)


def test_dev_with_real_keys_and_force_fake_false_returns_real(monkeypatch):
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(settings, "VTPASS_SECRET_KEY", "sk_real")
    monkeypatch.setattr(settings, "VTPASS_API_KEY", "api_real")
    assert isinstance(factory.select_vtpass_client(), VTPassClient)


def test_production_with_force_fake_true_raises(monkeypatch):
    """The fintech-safety case: FORCE_FAKE_PROVIDERS=true leaking into
    prod must refuse loudly, not silently fall through."""
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    with pytest.raises(factory.FakeVTPassInEligibleEnvError):
        factory.select_vtpass_client()


def test_staging_with_force_fake_true_raises(monkeypatch):
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "staging")
    with pytest.raises(factory.FakeVTPassInEligibleEnvError):
        factory.select_vtpass_client()


def test_unknown_env_with_force_fake_true_raises(monkeypatch):
    """Typos in ENVIRONMENT must not silently pick either path."""
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "devlopment")
    with pytest.raises(factory.FakeVTPassInEligibleEnvError):
        factory.select_vtpass_client()


def test_staging_with_real_keys_uses_real(monkeypatch):
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "staging")
    monkeypatch.setattr(settings, "VTPASS_SECRET_KEY", "sk_real")
    monkeypatch.setattr(settings, "VTPASS_API_KEY", "api_real")
    assert isinstance(factory.select_vtpass_client(), VTPassClient)


def test_fake_singleton_shared_across_calls(monkeypatch):
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    a = factory.select_vtpass_client()
    b = factory.select_vtpass_client()
    assert a is b


def test_reset_replaces_singleton(monkeypatch):
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    before = factory.select_vtpass_client()
    factory.reset_fake_singleton()
    after = factory.select_vtpass_client()
    assert before is not after
