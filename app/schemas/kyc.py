"""KYC request/response schemas (A7) — the frozen HTTP contract mobile
integrates against. Field names and shapes here must match the spec
exactly; see app/api/v1/endpoints/kyc.py for the mapping from KycRecord.
"""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel


class KycConfigResponse(BaseModel):
    app_id: str | None
    public_key: str | None
    bvn_widget_id: str | None
    nin_widget_id: str | None
    environment: str


class KycStartRequest(BaseModel):
    # Not a Literal: an unrecognized value must reach KycService (which
    # raises ValueError) so the endpoint can map it to the spec's
    # `INVALID_VERIFICATION_TYPE` error code, rather than surfacing
    # FastAPI's generic VALIDATION_ERROR envelope.
    verification_type: str
    date_of_birth: date | None = None


class KycStartResponse(BaseModel):
    reference_id: str


class KycConfirmRequest(BaseModel):
    reference_id: str


class KycVerifyResponse(BaseModel):
    status: str
    tier: int
    verification_type: str
    reference: str
    liveness_passed: bool
    face_match: bool
    failure_reason: str | None = None


class KycStatusRecord(BaseModel):
    verification_type: str
    status: str
    reference: str
    liveness_passed: bool
    face_match: bool
    created_at: datetime
    failure_reason: str | None = None


class KycStatusResponse(BaseModel):
    tier: int
    records: list[KycStatusRecord]
