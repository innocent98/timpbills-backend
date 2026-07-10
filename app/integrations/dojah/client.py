# app/integrations/dojah/client.py
"""Real Dojah client — HTTPX async + tenacity retry on 5xx.

CONFIRMED-ON-INTEGRATION OPEN ITEM (see
docs/superpowers/specs/2026-07-10-kyc-bvn-nin-dojah-design.md §1): the exact
verification-status endpoint path and the Dojah response field names used in
`_parse_result` below are a best-effort default per public Dojah docs, not
yet verified against a live sandbox account. When credentials/docs are
confirmed, revise ONLY `_ENDPOINT_PATH` and `_parse_result` — nothing else
in this module or its callers (KycService, the confirm endpoint, the
webhook) should need to change.
"""
from typing import Any, Literal

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.integrations.dojah.schemas import KycVerificationResult

_KycStatus = Literal["success", "pending", "failed"]


def _is_retryable_dojah_error(exc: BaseException) -> bool:
    """5xx and genuine transport failures (timeouts, connection errors) are
    worth retrying — the request may simply need to land again. A 4xx
    (bad reference, malformed params, auth failure) will fail identically
    on every attempt, so retrying it only wastes the 3-attempt budget and
    delays surfacing the real error."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)

# Dojah's documented verification statuses, mapped to our tri-state status.
# Unknown/unrecognized values default to "pending" rather than raising —
# a forward-compatible new Dojah status should not crash the reconcile path.
_STATUS_MAP: dict[str, _KycStatus] = {
    "Completed": "success",
    "Pending": "pending",
    "Ongoing": "pending",
    "Failed": "failed",
    "Abandoned": "failed",
}


class DojahError(Exception):
    pass


class DojahClient:
    # --- CONFIRMED-ON-INTEGRATION OPEN ITEM ---------------------------------
    # Reasonable default per Dojah docs; confirm the exact path once a live
    # sandbox account is available.
    _ENDPOINT_PATH = "/api/v1/kyc/verification/status"
    # -------------------------------------------------------------------------

    def __init__(self) -> None:
        if not settings.DOJAH_API_KEY:
            raise RuntimeError("DOJAH_API_KEY must be set for real client")
        self._base = settings.DOJAH_BASE_URL
        self._headers = {
            "Authorization": settings.DOJAH_API_KEY,
            "AppId": settings.DOJAH_APP_ID or "",
        }

    @retry(
        reraise=True,
        retry=retry_if_exception(_is_retryable_dojah_error),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
    )
    async def fetch_verification(self, *, reference_id: str) -> KycVerificationResult:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"{self._base}{self._ENDPOINT_PATH}",
                params={"reference_id": reference_id},
                headers=self._headers,
            )
            r.raise_for_status()
        return self._parse_result(r.json())

    # --- CONFIRMED-ON-INTEGRATION OPEN ITEM ---------------------------------
    # The exact Dojah response field names (id_verification.verified,
    # liveness.passed, face_match.confidence, identity.name/dob, masked_id)
    # are a best-effort reconstruction from public docs. This is the ONLY
    # method to revise once the live sandbox account confirms the real
    # response schema. Deriving the pass/fail *decision* (and the
    # human-facing failure_reason enum) is KycService's job downstream —
    # this method only maps raw Dojah fields to our component booleans.
    def _parse_result(self, payload: dict[str, Any]) -> KycVerificationResult:
        dojah_status = str(payload.get("status", ""))
        status: _KycStatus = _STATUS_MAP.get(dojah_status, "pending")

        id_verification = payload.get("id_verification") or {}
        liveness = payload.get("liveness") or {}
        face_match_data = payload.get("face_match") or {}
        identity = payload.get("identity") or {}

        id_verified = bool(id_verification.get("verified", False))
        liveness_passed = bool(liveness.get("passed", False))
        face_match_confidence = int(face_match_data.get("confidence", 0))
        face_match = face_match_confidence >= settings.DOJAH_FACE_MATCH_THRESHOLD

        return KycVerificationResult(
            verification_type=payload.get("verification_type", "bvn"),
            status=status,
            id_verified=id_verified,
            liveness_passed=liveness_passed,
            face_match=face_match,
            face_match_confidence=face_match_confidence,
            masked_id=payload.get("masked_id") or "••••••••••",
            provider_reference=payload.get("reference_id", ""),
            identity_name=identity.get("name"),
            identity_dob=identity.get("dob"),
            failure_reason=payload.get("failure_reason"),
        )
    # -------------------------------------------------------------------------
