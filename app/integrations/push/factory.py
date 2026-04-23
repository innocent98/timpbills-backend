"""Factory for selecting the push client.

Mirrors the Paystack / VTPass env-allowlist guards (S2C-6, Sprint 3 B4):
FakePushClient is acceptable in dev/test only; any other env must go
through the real FCMPushClient (or explicitly opt in via
FORCE_FAKE_PROVIDERS in a dev/test env).
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


def _fcm_config_present() -> bool:
    """True when enough FCM_* settings exist to build an FCMPushClient."""
    has_creds = bool(settings.FCM_CREDENTIALS_PATH) or bool(settings.FCM_CREDENTIALS_JSON)
    return bool(settings.FCM_PROJECT_ID) and has_creds


def _real() -> BasePushClient:
    """Build the production FCM HTTP v1 client. Imported lazily so
    dev/test runs that stick with the fake don't pull google-auth into
    their startup path."""
    from app.integrations.push.fcm import FCMPushClient  # noqa: PLC0415
    return FCMPushClient()


def select_push_client() -> BasePushClient:
    """Return the push client, honoring the S3C-M1 double-gate: the
    fake is only allowed when ``ENVIRONMENT`` is in ``_FAKE_OK_ENVS``
    AND ``FORCE_FAKE_PROVIDERS`` is explicitly true. Pre-M1 behavior
    had an env-only fallback that silently swallowed prod misconfigs
    (e.g. ``ENVIRONMENT=dev`` deployed to staging). Same policy the
    Paystack factory uses.

    Real-env resolution: when FCM_PROJECT_ID + (FCM_CREDENTIALS_PATH
    or FCM_CREDENTIALS_JSON) are set, we return FCMPushClient.
    Otherwise we fail loudly rather than silently dropping pushes."""
    env = (settings.ENVIRONMENT or "").lower()
    force_fake = bool(settings.FORCE_FAKE_PROVIDERS)

    if env not in _FAKE_OK_ENVS:
        if force_fake:
            raise FakePushInEligibleEnvError(
                f"FORCE_FAKE_PROVIDERS=True is not allowed in env={env!r}. "
                f"Unset it or move to one of {sorted(_FAKE_OK_ENVS)}."
            )
        if not _fcm_config_present():
            raise RuntimeError(
                "FCMPushClient is not configured: set FCM_PROJECT_ID and "
                "one of FCM_CREDENTIALS_PATH / FCM_CREDENTIALS_JSON."
            )
        return _real()

    # Eligible env — honor the opt-in flag, otherwise fall through to
    # the real client if FCM is actually configured. This lets
    # integration runs against a real FCM sandbox work under env=dev.
    if force_fake:
        return _fake()
    if _fcm_config_present():
        return _real()
    raise RuntimeError(
        "Push client is not configured: set FORCE_FAKE_PROVIDERS=True "
        f"to use FakePushClient in env={env!r}, or configure FCM_*."
    )
