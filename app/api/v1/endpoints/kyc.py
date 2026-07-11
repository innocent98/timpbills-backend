"""KYC endpoints (A7) — the frozen HTTP contract mobile integrates
against. Wraps KycService (A6) + the Dojah webhook signature verifier
(A3). JSON keys and status codes here are a contract; don't change them
without coordinating with mobile.

All endpoints require an authenticated user EXCEPT `/kyc/webhook`, which
is unauthenticated and instead HMAC-signature-verified against the raw
request body — Dojah is the source of truth for verification outcomes,
same pattern as the Paystack/VTPass webhooks in
app/api/v1/endpoints/webhooks.py.
"""
import asyncio
import json
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db, get_kyc_service
from app.core.config import settings
from app.core.limiter import limiter
from app.core.logger import log
from app.db.models.kyc_record import KycRecord
from app.db.models.user import User
from app.integrations.dojah.signature import verify_dojah_signature
from app.schemas.kyc import (
    KycConfigResponse,
    KycConfirmRequest,
    KycStartRequest,
    KycStartResponse,
    KycStatusRecord,
    KycStatusResponse,
    KycVerifyResponse,
)
from app.services.kyc_service import (
    DobRequired,
    KycProviderError,
    KycService,
    KycTierPrecondition,
    UnknownReference,
)
from app.utils.responses import success

router = APIRouter(prefix="/kyc", tags=["kyc"])

# GET /kyc/status re-confirms only a recent pending record, and bounds the
# Dojah round-trip, so a stale/slow verification can't make the poll time out.
_STATUS_RECONFIRM_WINDOW_MIN = 15
_STATUS_RECONFIRM_TIMEOUT_S = 8


def _verify_response_from_record(record: KycRecord) -> KycVerifyResponse:
    tier = record.tier_after if record.status == "success" else record.tier_before
    return KycVerifyResponse(
        status=record.status,
        tier=tier,
        verification_type=record.verification_type,
        reference=record.provider_reference,
        liveness_passed=bool(record.liveness_passed),
        face_match=bool(record.face_match),
        failure_reason=record.failure_reason,
    )


@router.get("/config")
def get_kyc_config(
    request: Request,
    user: User = Depends(get_current_user),
):
    body = KycConfigResponse(
        app_id=settings.DOJAH_APP_ID,
        public_key=settings.DOJAH_PUBLIC_KEY,
        bvn_widget_id=settings.DOJAH_BVN_WIDGET_ID,
        nin_widget_id=settings.DOJAH_NIN_WIDGET_ID,
        environment=settings.DOJAH_ENVIRONMENT,
    )
    return success(
        body.model_dump(mode="json"),
        request_id=getattr(request.state, "request_id", None),
    )


@router.post("/verify/start")
def start_verify(
    request: Request,
    body: KycStartRequest,
    user: User = Depends(get_current_user),
    svc: KycService = Depends(get_kyc_service),
):
    try:
        reference_id = svc.start_verification(
            user=user,
            verification_type=body.verification_type,
            date_of_birth=body.date_of_birth,
        )
    except KycTierPrecondition as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "KYC_TIER_PRECONDITION", "message": str(exc)},
        )
    except DobRequired:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "DATE_OF_BIRTH_REQUIRED",
                "message": "date_of_birth is required",
            },
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_VERIFICATION_TYPE", "message": str(exc)},
        )
    return success(
        KycStartResponse(reference_id=reference_id).model_dump(mode="json"),
        request_id=getattr(request.state, "request_id", None),
    )


@router.post("/verify/confirm")
async def confirm_verify(
    request: Request,
    body: KycConfirmRequest,
    user: User = Depends(get_current_user),
    svc: KycService = Depends(get_kyc_service),
):
    try:
        record = await svc.confirm_verification(
            reference_id=body.reference_id,
            source="api",
            expected_user_id=user.id,
        )
    except UnknownReference:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "UNKNOWN_REFERENCE",
                "message": "No verification found for that reference",
            },
        )
    except KycProviderError as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": "KYC_PROVIDER_ERROR", "message": str(exc)},
        )
    return success(
        _verify_response_from_record(record).model_dump(mode="json"),
        request_id=getattr(request.state, "request_id", None),
    )


@router.get("/status")
async def kyc_status(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    svc: KycService = Depends(get_kyc_service),
):
    # Re-confirm AT MOST the single most-recent, still-fresh pending record so a
    # status poll can advance a just-completed verification. Deliberately
    # bounded: re-confirming every pending record — including stale/abandoned
    # ones Dojah is slow to look up — blocked this endpoint past the client's
    # timeout. Stale pending records (older than the freshness window) are left
    # for the webhook backstop. The 8s cap keeps one slow Dojah call from
    # blowing the read; a hiccup/timeout must never break the read.
    fresh_cutoff = datetime.now(UTC) - timedelta(minutes=_STATUS_RECONFIRM_WINDOW_MIN)
    latest_pending = (
        db.query(KycRecord)
        .filter(
            KycRecord.user_id == user.id,
            KycRecord.status == "pending",
            KycRecord.created_at >= fresh_cutoff,
        )
        .order_by(KycRecord.created_at.desc())
        .first()
    )
    if latest_pending is not None:
        try:
            await asyncio.wait_for(
                svc.confirm_verification(
                    reference_id=latest_pending.provider_reference,
                    source="status",
                    expected_user_id=user.id,
                ),
                timeout=_STATUS_RECONFIRM_TIMEOUT_S,
            )
        except (KycProviderError, UnknownReference, TimeoutError) as exc:
            log.info(
                "kyc_status: pending re-confirm skipped ref=%s: %s",
                latest_pending.provider_reference, exc,
            )
    db.refresh(user)

    records = (
        db.query(KycRecord)
        .filter(KycRecord.user_id == user.id)
        .order_by(KycRecord.created_at.desc())
        .all()
    )
    body = KycStatusResponse(
        tier=user.kyc_level.numeric,
        records=[
            KycStatusRecord(
                verification_type=r.verification_type,
                status=r.status,
                reference=r.provider_reference,
                liveness_passed=bool(r.liveness_passed),
                face_match=bool(r.face_match),
                created_at=r.created_at,
                failure_reason=r.failure_reason,
            )
            for r in records
        ],
    )
    return success(
        body.model_dump(mode="json"),
        request_id=getattr(request.state, "request_id", None),
    )


@router.post("/webhook")
@limiter.limit("60/minute")
async def kyc_webhook(
    request: Request,
    svc: KycService = Depends(get_kyc_service),
):
    raw_body = await request.body()
    signature = request.headers.get("x-dojah-signature")
    if not verify_dojah_signature(raw_body, signature):
        raise HTTPException(
            status_code=401,
            detail={
                "code": "INVALID_SIGNATURE",
                "message": "Bad or missing x-dojah-signature",
            },
        )

    try:
        payload = json.loads(raw_body or b"{}")
    except ValueError:
        # Malformed JSON on a validly-signed body — nothing to reconcile.
        return success({"status": "ok"})

    reference_id = payload.get("reference_id") or payload.get("referenceId")
    if not reference_id:
        return success({"status": "ok"})

    try:
        # No expected_user_id — the webhook is the payment-grade source of
        # truth for any user's verification (see
        # feedback_webhook_source_of_truth.md).
        await svc.confirm_verification(reference_id=reference_id, source="webhook")
    except UnknownReference:
        # Nothing to reconcile — don't make Dojah retry forever.
        return success({"status": "ok"})
    except KycProviderError as exc:
        log.error("kyc webhook: provider error ref=%s: %s", reference_id, exc)
        raise HTTPException(
            status_code=502,
            detail={"code": "KYC_PROVIDER_ERROR", "message": str(exc)},
        )
    return success({"status": "ok"})
