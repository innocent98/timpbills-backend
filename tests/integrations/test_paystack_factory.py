"""Regression: select_paystack_client() must honor FORCE_FAKE_PROVIDERS
only in explicitly-eligible environments; any leak into staging / preview
/ production / unknown-env values must refuse loudly."""
import pytest

from app.core.config import settings
from app.integrations.paystack import factory
from app.integrations.paystack.client import PaystackClient
from app.integrations.paystack.fake import FakePaystackClient


def test_factory_returns_fake_when_force_fake_true(monkeypatch):
    """FORCE_FAKE_PROVIDERS=True in an eligible env returns the fake."""
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    assert isinstance(factory.select_paystack_client(), FakePaystackClient)


def test_factory_returns_real_when_force_fake_false_and_prod(monkeypatch):
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    assert isinstance(factory.select_paystack_client(), PaystackClient)


def test_factory_force_fake_false_uses_real_client_in_dev(monkeypatch):
    """When developers explicitly opt into real Paystack (FORCE_FAKE_PROVIDERS=False
    in dev), the factory honors that. Previous behavior silently returned the fake
    regardless, which caused real Paystack to reject the fake authorization URL."""
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    assert isinstance(factory.select_paystack_client(), PaystackClient)


def test_factory_production_with_force_fake_true_raises(monkeypatch):
    """Production safety net (S2C-6): if FORCE_FAKE_PROVIDERS=True leaks into
    a production deploy, refuse loudly rather than silently using the real
    client — the operator clearly meant to fake but is in the wrong place."""
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    with pytest.raises(factory.FakeProviderInEligibleEnvError):
        factory.select_paystack_client()


def test_factory_staging_with_force_fake_true_raises(monkeypatch):
    """Staging is where the worst leak scenarios land — a user could fund their
    wallet with FAKE_SIG and mint real-looking balance. Block it."""
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "staging")
    with pytest.raises(factory.FakeProviderInEligibleEnvError):
        factory.select_paystack_client()


def test_factory_unknown_env_with_force_fake_true_raises(monkeypatch):
    """Typos in ENVIRONMENT (e.g. 'devlopment') must not silently downgrade
    to the fake or to the real client — either answer could be wrong. Refuse."""
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "devlopment")
    with pytest.raises(factory.FakeProviderInEligibleEnvError):
        factory.select_paystack_client()


def test_factory_staging_with_force_fake_false_uses_real(monkeypatch):
    """Staging with the fake flag off is the normal shape — use the real client."""
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "staging")
    assert isinstance(factory.select_paystack_client(), PaystackClient)


def test_factory_fake_singleton_shared_across_calls(monkeypatch):
    """Two select_paystack_client() calls in fake mode must return the SAME
    instance — otherwise API state (e.g. will_succeed marks) won't be visible
    to the reconcile worker."""
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    a = factory.select_paystack_client()
    b = factory.select_paystack_client()
    assert a is b


def test_factory_reset_replaces_singleton(monkeypatch):
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    before = factory.select_paystack_client()
    factory.reset_fake_singleton()
    after = factory.select_paystack_client()
    assert before is not after
