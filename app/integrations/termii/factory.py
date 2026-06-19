"""DI-free Termii SMS client selection — same env-allowlist pattern as
VTPass / Paystack.

Guarantees the OTP-logging fake (``FakeTermiiClient``) can NEVER be
selected in staging/production: ``FORCE_FAKE_PROVIDERS=true`` leaking
into a non-dev env raises loudly rather than silently delivering OTP
codes to stdout where they'd be readable in logs.
"""
from app.core.config import settings
from app.integrations.base import SmsProvider
from app.integrations.termii.client import TermiiClient
from app.integrations.termii.fake import FakeTermiiClient

# Singleton fake so tests can inspect ``.sent`` across calls.
_fake_singleton: FakeTermiiClient = FakeTermiiClient()


# Environments where FORCE_FAKE_PROVIDERS=true (or default-to-fake) is
# honored. Any other env (staging / preview / prod / typo) refuses the
# fake loudly — the operator meant to fake but is in the wrong place.
# Identical rule to VTPass's _FAKE_ELIGIBLE_ENVS.
_FAKE_ELIGIBLE_ENVS = frozenset({"dev", "development", "test", "testing", "local"})


class FakeTermiiInEligibleEnvError(RuntimeError):
    """Raised when the SMS fake would be selected in a non-dev env —
    either via FORCE_FAKE_PROVIDERS=true or via a missing API key.

    In staging/prod the OTP must be a real SMS; the fake logs the code
    to stdout, so selecting it there is a security failure, not a
    convenience fallback."""


def _is_fake_env() -> bool:
    env = getattr(settings, "ENVIRONMENT", "dev").lower()
    force_fake = bool(settings.FORCE_FAKE_PROVIDERS)
    if env not in _FAKE_ELIGIBLE_ENVS:
        if force_fake:
            raise FakeTermiiInEligibleEnvError(
                f"FORCE_FAKE_PROVIDERS=true is not allowed in "
                f"ENVIRONMENT={env!r}. The SMS fake logs OTP codes to "
                f"stdout and is only usable in {sorted(_FAKE_ELIGIBLE_ENVS)}."
            )
        if not settings.TERMII_API_KEY:
            # Non-dev env with no API key: the real client can't send.
            # Refuse loudly rather than fall back to the OTP-logging fake.
            raise FakeTermiiInEligibleEnvError(
                f"TERMII_API_KEY is required in ENVIRONMENT={env!r}. "
                f"Refusing to fall back to the OTP-logging SMS fake."
            )
        return False
    # In an eligible env, FORCE_FAKE_PROVIDERS is authoritative. If a real
    # API key is missing we still fall back to the fake so dev boots
    # without needing a Termii account.
    if force_fake:
        return True
    return not settings.TERMII_API_KEY


def select_sms_client() -> SmsProvider:
    if _is_fake_env():
        return _fake_singleton
    return TermiiClient()


def get_fake_singleton() -> FakeTermiiClient:
    return _fake_singleton


def reset_fake_singleton() -> None:
    """Clear the fake's captured messages IN PLACE (does not rebind).

    api/e2e tests bind ``_fake_sms_singleton`` at import time via
    ``deps.__getattr__``, capturing the current object. Rebinding the
    singleton here would leave those references pointing at a stale, empty
    object — producing order-dependent failures where a test's ``.sent``
    assertions read the old object while ``select_sms_client()`` returns the
    new one. Clearing in place keeps object identity stable and mirrors
    ``deps.reset_fake_sms``.
    """
    _fake_singleton.sent.clear()
