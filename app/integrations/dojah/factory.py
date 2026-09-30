"""DI-free Dojah KYC provider selection — same env-allowlist pattern as
Termii / Paystack / VTPass.

``FakeKycProvider`` approves every verification that lacks a failure
marker, so selecting it outside dev/test would upgrade every user's KYC
tier (tier_1→tier_2 via BVN, tier_2→tier_3 via NIN, lifting wallet caps)
without a real check — a KYC/AML bypass.

Reversal of the original "spec decision #2": this factory used to return
the fake whenever FORCE_FAKE_PROVIDERS was true or DOJAH_API_KEY was unset,
in ANY environment, on the reasoning that real Dojah is the working path
everywhere. That left a missing / unrendered production secret silently
approving every KYC submission. The fake is now allowed only in
``FAKE_ELIGIBLE_ENVS``; anywhere else, either condition raises. Settings
validation (``Settings._refuse_fake_kyc_outside_dev``) enforces the same
rule at boot; this check is the runtime backstop.

The fake is stateless, so no shared singleton is needed (see
app/integrations/dojah/fake.py).
"""

from app.core.config import FAKE_ELIGIBLE_ENVS, settings
from app.integrations.dojah.base import KycProvider
from app.integrations.dojah.client import DojahClient
from app.integrations.dojah.fake import FakeKycProvider


class FakeKycInEligibleEnvError(RuntimeError):
    """Raised when the KYC fake would be selected in a non-dev env —
    either via FORCE_FAKE_PROVIDERS=true or via a missing DOJAH_API_KEY.

    The fake approves every verification, so selecting it there is a
    KYC bypass, not a convenience fallback."""


def _is_fake_env() -> bool:
    env = getattr(settings, "ENVIRONMENT", "dev").strip().lower()
    force_fake = bool(settings.FORCE_FAKE_PROVIDERS)
    if env not in FAKE_ELIGIBLE_ENVS:
        if force_fake:
            raise FakeKycInEligibleEnvError(
                f"FORCE_FAKE_PROVIDERS=true is not allowed in "
                f"ENVIRONMENT={env!r}. The KYC fake approves every "
                f"verification and is only usable in {sorted(FAKE_ELIGIBLE_ENVS)}."
            )
        if not settings.DOJAH_API_KEY:
            raise FakeKycInEligibleEnvError(
                f"DOJAH_API_KEY is required in ENVIRONMENT={env!r}. "
                f"Refusing to fall back to the approve-everything KYC fake."
            )
        return False
    # In an eligible env, FORCE_FAKE_PROVIDERS is authoritative; a missing
    # key still falls back to the fake so dev boots without a Dojah account.
    if force_fake:
        return True
    return not settings.DOJAH_API_KEY


def get_kyc_provider() -> KycProvider:
    if _is_fake_env():
        return FakeKycProvider()
    return DojahClient()
