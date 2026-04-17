"""Regression: select_paystack_client() must honor FORCE_FAKE_PROVIDERS."""
from app.core.config import settings
from app.integrations.paystack import factory
from app.integrations.paystack.client import PaystackClient
from app.integrations.paystack.fake import FakePaystackClient


def test_factory_returns_fake_when_force_fake_true(monkeypatch):
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    assert isinstance(factory.select_paystack_client(), FakePaystackClient)


def test_factory_returns_real_when_force_fake_false_and_prod(monkeypatch):
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    assert isinstance(factory.select_paystack_client(), PaystackClient)


def test_factory_returns_fake_in_dev_env_even_when_force_fake_false(monkeypatch):
    """Dev environments get the fake regardless — protects against accidental
    outbound calls to Paystack from developer machines."""
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    assert isinstance(factory.select_paystack_client(), FakePaystackClient)


def test_factory_fake_singleton_shared_across_calls(monkeypatch):
    """Two select_paystack_client() calls in fake mode must return the SAME
    instance — otherwise API state (e.g. will_succeed marks) won't be visible
    to the reconcile worker."""
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    a = factory.select_paystack_client()
    b = factory.select_paystack_client()
    assert a is b


def test_factory_reset_replaces_singleton(monkeypatch):
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    before = factory.select_paystack_client()
    factory.reset_fake_singleton()
    after = factory.select_paystack_client()
    assert before is not after
