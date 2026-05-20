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


# Environments where FORCE_FAKE_PROVIDERS=true is even considered. Staging,
# preview, production, and any typo'd / unrecognised env string all refuse
# the fake — a safety net against a leaked FORCE_FAKE_PROVIDERS flag minting
# wallet credit in a live-ish environment.
_FAKE_ELIGIBLE_ENVS = frozenset({"dev", "development", "test", "testing", "local"})


class FakeProviderInEligibleEnvError(RuntimeError):
    """Raised at startup when FORCE_FAKE_PROVIDERS=true is set in a non-dev env."""


def _is_fake_env() -> bool:
    """Decide whether to return the fake Paystack client.

    Rules:
      * fake is only allowed when ENVIRONMENT is in the dev/test allowlist.
        Staging/preview/production all ignore the flag and use the real
        client, no matter what FORCE_FAKE_PROVIDERS says.
      * within an eligible env, FORCE_FAKE_PROVIDERS is authoritative.
      * if FORCE_FAKE_PROVIDERS=true is set in a non-eligible env we raise
        on the first call rather than silently using the real client — the
        operator meant to fake but didn't; refuse to do the wrong thing.
    """
    env = getattr(settings, "ENVIRONMENT", "dev").lower()
    force_fake = bool(settings.FORCE_FAKE_PROVIDERS)
    if env not in _FAKE_ELIGIBLE_ENVS:
        if force_fake:
            raise FakeProviderInEligibleEnvError(
                f"FORCE_FAKE_PROVIDERS=true is not allowed in ENVIRONMENT={env!r}. "
                f"Fake providers are only usable in {sorted(_FAKE_ELIGIBLE_ENVS)}."
            )
        return False
    return force_fake


def select_paystack_client() -> PaymentProvider:
    if _is_fake_env():
        return _fake_singleton
    return PaystackClient()


def get_fake_singleton() -> FakePaystackClient:
    return _fake_singleton


def reset_fake_singleton() -> None:
    global _fake_singleton
    _fake_singleton = FakePaystackClient()
