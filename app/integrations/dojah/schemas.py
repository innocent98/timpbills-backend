from datetime import date
from typing import Literal

from pydantic import BaseModel


class KycVerificationResult(BaseModel):
    verification_type: Literal["bvn", "nin"]
    status: Literal["success", "pending", "failed"]
    id_verified: bool
    liveness_passed: bool
    face_match: bool
    face_match_confidence: int
    masked_id: str
    provider_reference: str
    identity_name: str | None = None
    identity_dob: date | None = None
    failure_reason: str | None = None
