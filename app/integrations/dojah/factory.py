"""DI-free Dojah KYC provider selection.

Unlike Paystack/VTPass, KYC has no env-eligibility gate on
FORCE_FAKE_PROVIDERS: real Dojah is the working path in every environment
(spec decision #2), and FakeKycProvider is stateless (no shared singleton
needed — see app/integrations/dojah/fake.py).
"""
from app.core.config import settings
from app.integrations.dojah.base import KycProvider
from app.integrations.dojah.client import DojahClient
from app.integrations.dojah.fake import FakeKycProvider


def get_kyc_provider() -> KycProvider:
    if settings.FORCE_FAKE_PROVIDERS or settings.DOJAH_API_KEY is None:
        return FakeKycProvider()
    return DojahClient()
