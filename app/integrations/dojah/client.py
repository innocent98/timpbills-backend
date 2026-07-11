# app/integrations/dojah/client.py
"""Real Dojah client — HTTPX async + tenacity retry on 5xx.

Endpoint path and response shape are confirmed against Dojah's official
docs (https://docs.dojah.io/docs/technical-reference/get-verification-details),
2026-07-10. See `_ENDPOINT_PATH` and `_parse_result` below.
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

# Dojah's documented `verification_status` values, mapped to our tri-state
# status. Unknown/unrecognized values default to "pending" rather than
# raising — a forward-compatible new Dojah status should not crash the
# reconcile path.
_STATUS_MAP: dict[str, _KycStatus] = {
    "Completed": "success",
    "Pending": "pending",
    "Ongoing": "pending",
    "Failed": "failed",
    "Abandoned": "failed",
}


class DojahError(Exception):
    pass


_DEFAULT_PROD_HOST = "https://api.dojah.io"
_SANDBOX_HOST = "https://sandbox.dojah.io"


def _resolve_base_url() -> str:
    """Pick the Dojah host from DOJAH_ENVIRONMENT.

    Sandbox Secret Keys (``test_sk_…``) authenticate ONLY against
    ``sandbox.dojah.io``; production keys against ``api.dojah.io``. Pointing
    sandbox creds at the prod host returns a misleading
    ``401 "Your Secret Key could not be Authorized"`` — a real footgun we hit
    during go-live. So the environment drives the host. An operator who sets
    ``DOJAH_BASE_URL`` to something other than the ``api.dojah.io`` default
    (a proxy, a pinned test host) still overrides.
    """
    configured = (settings.DOJAH_BASE_URL or "").rstrip("/")
    if configured and configured != _DEFAULT_PROD_HOST:
        return configured
    env = (settings.DOJAH_ENVIRONMENT or "sandbox").lower()
    return _SANDBOX_HOST if env == "sandbox" else _DEFAULT_PROD_HOST


class DojahClient:
    # Confirmed real path (singular "verification") per Dojah's
    # get-verification-details docs.
    _ENDPOINT_PATH = "/api/v1/kyc/verification"

    def __init__(self) -> None:
        if not settings.DOJAH_API_KEY:
            raise RuntimeError("DOJAH_API_KEY must be set for real client")
        self._base = _resolve_base_url()
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

    # REAL Dojah response shape (verified against a live sandbox verification,
    # 2026-07-11) — the whole verification is nested under `entity`:
    #   {"entity": {
    #     "verification_status": "Completed"|"Ongoing"|"Pending"|"Failed"|"Abandoned",
    #     "verification_type": "BVN" | "NIN",
    #     "reference_id": "KYC-BVN-...",
    #     "data": {
    #       "government_data": {"status": bool,
    #                           "data": {"bvn": {"entity": {"bvn": "...",
    #                                    "first_name": "...", "date_of_birth": "01-Jun-1982"}}}},
    #       "selfie": {"status": bool, "message": "Successfully validated your liveness"},
    #     },
    #   }}
    # (An earlier version read these at the TOP level — so verification_status
    # was never found and every result defaulted to "pending". That was the
    # "always in progress" bug.) Every access is a defensive nested .get(...).
    # liveness_score/match_score come back null on the widget path, so there is
    # no numeric confidence to threshold — selfie.status IS the combined
    # liveness+face pass/fail; the confidence we report is synthetic (100/0).
    # We do NOT surface identity_dob: Dojah's format is non-ISO and Dojah
    # already validated the ID against a live selfie, so KycService does not
    # re-gate on DOB (that check stays dormant with identity_dob=None).
    def _parse_result(self, payload: dict[str, Any]) -> KycVerificationResult:
        entity = payload.get("entity")
        if not isinstance(entity, dict):
            entity = payload  # tolerate an already-unwrapped shape (defensive)

        dojah_status = str(entity.get("verification_status", ""))
        status: _KycStatus = _STATUS_MAP.get(dojah_status, "pending")

        data = entity.get("data") or {}
        government_data = data.get("government_data") or {}
        selfie = data.get("selfie") or {}

        id_verified = bool(government_data.get("status", False))

        selfie_passed = bool(selfie.get("status", False))
        liveness_passed = selfie_passed
        face_match = selfie_passed
        face_match_confidence = 100 if selfie_passed else 0

        gov_records = government_data.get("data") or {}
        vt = str(entity.get("verification_type", "")).lower()
        if vt not in ("bvn", "nin"):
            vt = "nin" if "nin" in gov_records else "bvn"
        verification_type: Literal["bvn", "nin"] = "nin" if vt == "nin" else "bvn"

        # The government record sits one level deeper, under ["entity"].
        gov_record = (gov_records.get(verification_type) or {}).get("entity") or {}
        id_number = gov_record.get(verification_type)
        id_str = str(id_number) if id_number else ""
        masked_id = f"{'•' * max(len(id_str) - 2, 0)}{id_str[-2:]}" if id_str else "••"

        first_name = gov_record.get("first_name")
        last_name = gov_record.get("last_name")
        identity_name = " ".join(p for p in (first_name, last_name) if p) or None

        return KycVerificationResult(
            verification_type=verification_type,
            status=status,
            id_verified=id_verified,
            liveness_passed=liveness_passed,
            face_match=face_match,
            face_match_confidence=face_match_confidence,
            masked_id=masked_id,
            provider_reference=str(entity.get("reference_id", "")),
            identity_name=identity_name,
            identity_dob=None,
            failure_reason=None,
        )
