"""DI-free Paystack client selection.

Used by both FastAPI DI (`get_paystack_provider`) and Celery workers
(`reconcile_tasks`). One fake-singleton lives here so API and worker share
state in dev.
"""
from app.core.config import settings
from app.integrations.paystack.base import PaymentProvider
from app.integrations.paystack.client import PaystackClient
from app.integrations.paystack.fake import FakePaystackClient


_fake_singleton: FakePaystackClient = FakePaystackClient()


def _is_fake_env() -> bool:
    """Decide whether to return the fake Paystack client.

    Rules:
      * production never uses the fake — safety net against shipping a
        misconfigured deploy that silently mints fake authorization URLs
      * otherwise, `FORCE_FAKE_PROVIDERS` is authoritative: True → fake,
        False → real. Set it to False in dev when testing real Paystack.
    """
    env = getattr(settings, "ENVIRONMENT", "dev").lower()
    if env == "production":
        return False
    return bool(settings.FORCE_FAKE_PROVIDERS)


def select_paystack_client() -> PaymentProvider:
    if _is_fake_env():
        return _fake_singleton
    return PaystackClient()


def get_fake_singleton() -> FakePaystackClient:
    return _fake_singleton


def reset_fake_singleton() -> None:
    global _fake_singleton
    _fake_singleton = FakePaystackClient()
