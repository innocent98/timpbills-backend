"""Factory for selecting the push client.

Mirrors the Paystack / VTPass env-allowlist guards (S2C-6, Sprint 3 B4):
FakePushClient is acceptable in dev/test; any other env must go through
the real client (or explicitly opt in via FORCE_FAKE_PROVIDERS).

Since real FCM is still a follow-up, the "real" branch currently raises
RuntimeError. Production deployments either (a) set FORCE_FAKE_PROVIDERS
in a non-prod env, or (b) wait for the FCM HTTP v1 client to land.
"""
from app.core.config import settings
from app.integrations.push.base import BasePushClient
from app.integrations.push.fake import FakePushClient


class FakePushInEligibleEnvError(RuntimeError):
    """Raised when FORCE_FAKE_PROVIDERS=True leaks into staging/prod."""


# Environments where the fake is acceptable. Kept in sync with the
# Paystack + VTPass factories.
_FAKE_OK_ENVS = {"dev", "development", "test", "testing"}

_fake_singleton: FakePushClient | None = None


def _fake() -> FakePushClient:
    global _fake_singleton
    if _fake_singleton is None:
        _fake_singleton = FakePushClient()
    return _fake_singleton


def reset_fake_singleton() -> None:
    """Test isolation — drop the in-memory sent list between tests."""
    global _fake_singleton
    if _fake_singleton is not None:
        _fake_singleton.sent.clear()


def get_fake_singleton() -> FakePushClient:
    """Expose the test singleton so tests can inspect `.sent`."""
    return _fake()


def select_push_client() -> BasePushClient:
    env = (settings.ENVIRONMENT or "").lower()
    if settings.FORCE_FAKE_PROVIDERS:
        if env not in _FAKE_OK_ENVS:
            raise FakePushInEligibleEnvError(
                f"FORCE_FAKE_PROVIDERS=True is not allowed in env={env!r}. "
                f"Unset it or move to one of {sorted(_FAKE_OK_ENVS)}."
            )
        return _fake()
    if env in _FAKE_OK_ENVS:
        # No real creds required to decide — default to fake in dev/test.
        return _fake()
    # TODO(sprint-4): wire FCM HTTP v1 client here. For now, fail loudly
    # so nobody ships a real deploy expecting push to work silently.
    raise RuntimeError(
        "No real push client is configured yet (FCM HTTP v1 is a Sprint 4 "
        "follow-up). For now, set FORCE_FAKE_PROVIDERS=True in non-prod envs "
        "or wait for the real client to land."
    )
