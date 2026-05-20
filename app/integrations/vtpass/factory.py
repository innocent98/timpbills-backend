"""DI-free VTPass client selection — same env-allowlist pattern as Paystack.

Shared by FastAPI DI (`get_vtpass_provider`) and the Celery reconcile
worker so API and worker agree on which client is active.
"""
from app.core.config import settings
from app.integrations.vtpass.base import BillProvider
from app.integrations.vtpass.client import VTPassClient
from app.integrations.vtpass.fake import FakeVTPassClient

_fake_singleton: FakeVTPassClient = FakeVTPassClient()


# Environments where FORCE_FAKE_PROVIDERS=true is honored. Any other env
# (staging / preview / prod / typo) refuses the fake loudly — the operator
# meant to fake but is in the wrong place. Same rule as Paystack's S2C-6.
_FAKE_ELIGIBLE_ENVS = frozenset({"dev", "development", "test", "testing", "local"})


class FakeVTPassInEligibleEnvError(RuntimeError):
    """Raised when FORCE_FAKE_PROVIDERS=true is set in a non-dev env."""


def _is_fake_env() -> bool:
    env = getattr(settings, "ENVIRONMENT", "dev").lower()
    force_fake = bool(settings.FORCE_FAKE_PROVIDERS)
    if env not in _FAKE_ELIGIBLE_ENVS:
        if force_fake:
            raise FakeVTPassInEligibleEnvError(
                f"FORCE_FAKE_PROVIDERS=true is not allowed in ENVIRONMENT={env!r}. "
                f"VTPass fake is only usable in {sorted(_FAKE_ELIGIBLE_ENVS)}."
            )
        return False
    # In an eligible env, FORCE_FAKE_PROVIDERS is authoritative. If real
    # credentials are missing we still fall back to the fake so dev boots
    # without needing a VTPass account.
    if force_fake:
        return True
    return not (settings.VTPASS_SECRET_KEY and settings.VTPASS_API_KEY)


def select_vtpass_client() -> BillProvider:
    if _is_fake_env():
        return _fake_singleton
    return VTPassClient()


def get_fake_singleton() -> FakeVTPassClient:
    return _fake_singleton


def reset_fake_singleton() -> None:
    global _fake_singleton
    _fake_singleton = FakeVTPassClient()
