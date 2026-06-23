"""DI-free Resend email client selection — same env-allowlist pattern as
Termii / VTPass / Paystack.

Guarantees the OTP-logging fake (``FakeEmailClient``) can NEVER be
selected in staging/production: ``FORCE_FAKE_PROVIDERS=true`` leaking
into a non-dev env raises loudly rather than silently delivering OTP
codes to stdout, and a *missing* RESEND_API_KEY in a non-dev env raises
rather than silently not-sending real email.

The selection rule is identical in spirit to termii's ``_is_fake_env``:
fake is chosen by KEY presence + FORCE_FAKE flag, never by env NAME.
"""
from app.core.config import settings
from app.integrations.email.base import EmailProvider
from app.integrations.email.fake import FakeEmailClient
from app.integrations.email.resend import ResendClient

# Singleton fake so tests + the Celery worker can inspect ``.sent`` across
# calls and across the DI boundary (the worker reaches this object directly).
_fake_singleton: FakeEmailClient = FakeEmailClient()


# Environments where FORCE_FAKE_PROVIDERS=true (or default-to-fake) is
# honored. Any other env (staging / preview / prod / typo) refuses the
# fake loudly. Identical set to termii's _FAKE_ELIGIBLE_ENVS.
_FAKE_ELIGIBLE_ENVS = frozenset({"dev", "development", "test", "testing", "local"})


class FakeEmailInEligibleEnvError(RuntimeError):
    """Raised when the email fake would be selected in a non-dev env —
    either via FORCE_FAKE_PROVIDERS=true or via a missing API key.

    In staging/prod the OTP must be a real email; the fake logs the code
    to stdout, so selecting it there is a security failure, and a missing
    key would silently drop delivery — both are refused, not fallen back
    to."""


def _is_fake_env() -> bool:
    env = getattr(settings, "ENVIRONMENT", "dev").lower()
    force_fake = bool(settings.FORCE_FAKE_PROVIDERS)
    if env not in _FAKE_ELIGIBLE_ENVS:
        if force_fake:
            raise FakeEmailInEligibleEnvError(
                f"FORCE_FAKE_PROVIDERS=true is not allowed in "
                f"ENVIRONMENT={env!r}. The email fake logs OTP codes to "
                f"stdout and is only usable in {sorted(_FAKE_ELIGIBLE_ENVS)}."
            )
        if not settings.RESEND_API_KEY:
            # Non-dev env with no API key: the real client can't send.
            # Refuse loudly rather than silently not-send.
            raise FakeEmailInEligibleEnvError(
                f"RESEND_API_KEY is required in ENVIRONMENT={env!r}. "
                f"Refusing to fall back to the OTP-logging email fake."
            )
        return False
    # In an eligible env, FORCE_FAKE_PROVIDERS is authoritative. If a real
    # API key is missing we still fall back to the fake so dev boots
    # without needing a Resend account.
    if force_fake:
        return True
    return not settings.RESEND_API_KEY


def select_email_client() -> EmailProvider:
    if _is_fake_env():
        return _fake_singleton
    return ResendClient()


def get_fake_singleton() -> FakeEmailClient:
    return _fake_singleton


def reset_fake_singleton() -> None:
    """Clear the fake's captured emails IN PLACE (does not rebind).

    api/e2e tests and the Celery worker bind this singleton at import time
    (via ``deps._fake_email_singleton`` / ``deps.reset_fake_email``).
    Rebinding here would leave those references pointing at a stale, empty
    object — producing order-dependent failures. Clearing in place keeps
    object identity stable and mirrors ``termii.factory.reset_fake_singleton``.
    """
    _fake_singleton.sent.clear()
