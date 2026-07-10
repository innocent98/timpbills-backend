"""KycProvider Protocol — the single interface the real Dojah client (A3)
and KycService (A6) depend on. Never store raw PII: implementations return
only `masked_id` (last 2 digits) on the wire."""
from typing import Protocol, runtime_checkable

from app.integrations.dojah.schemas import KycVerificationResult


@runtime_checkable
class KycProvider(Protocol):
    def fetch_verification(self, *, reference_id: str) -> KycVerificationResult: ...
