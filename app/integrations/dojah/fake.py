"""In-memory fake Dojah KYC provider — deterministic by `reference_id`
prefix. TEST-ONLY: never wired into the real DI/factory. The real Dojah
client (calling api.dojah.io) ships in A3."""
from typing import Literal

from app.integrations.dojah.schemas import KycVerificationResult


def _verification_type(reference_id: str) -> Literal["bvn", "nin"]:
    if "-BVN-" in reference_id:
        return "bvn"
    if "-NIN-" in reference_id:
        return "nin"
    return "bvn"


def _masked_id(reference_id: str) -> str:
    """Last-2-digits style, e.g. '•••••••••17' — never the raw ID."""
    digits = "".join(ch for ch in reference_id if ch.isdigit())
    last_two = digits[-2:].rjust(2, "0")
    return f"{'•' * 9}{last_two}"


class FakeKycProvider:
    def fetch_verification(self, *, reference_id: str) -> KycVerificationResult:
        verification_type = _verification_type(reference_id)
        masked_id = _masked_id(reference_id)

        if reference_id.startswith("PASS"):
            return KycVerificationResult(
                verification_type=verification_type,
                status="success",
                id_verified=True,
                liveness_passed=True,
                face_match=True,
                face_match_confidence=95,
                masked_id=masked_id,
                provider_reference=reference_id,
                failure_reason=None,
            )
        if reference_id.startswith("FAILFACE"):
            return KycVerificationResult(
                verification_type=verification_type,
                status="failed",
                id_verified=True,
                liveness_passed=True,
                face_match=False,
                face_match_confidence=40,
                masked_id=masked_id,
                provider_reference=reference_id,
                failure_reason="face_mismatch",
            )
        if reference_id.startswith("FAILLIVE"):
            return KycVerificationResult(
                verification_type=verification_type,
                status="failed",
                id_verified=True,
                liveness_passed=False,
                face_match=True,
                face_match_confidence=95,
                masked_id=masked_id,
                provider_reference=reference_id,
                failure_reason="liveness_failed",
            )
        if reference_id.startswith("PENDING"):
            return KycVerificationResult(
                verification_type=verification_type,
                status="pending",
                id_verified=False,
                liveness_passed=False,
                face_match=False,
                face_match_confidence=0,
                masked_id=masked_id,
                provider_reference=reference_id,
                failure_reason=None,
            )
        raise ValueError(
            f"FakeKycProvider: unrecognized reference_id prefix: {reference_id!r}"
        )
