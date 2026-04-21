"""/bills/* endpoints — airtime + data (Sprint 3). Electricity + cable
come in Sprint 4 and will be appended to this router."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import (
    get_bill_service,
    get_current_user,
    get_db,
    get_idempotency_service,
    require_idempotency_key,
    require_pin_token,
)
from app.core.limiter import limiter, per_user_or_ip
from app.db.models.user import User
from app.schemas.bills import (
    AirtimePurchaseRequest,
    AirtimePurchaseResponse,
    DataPlanListResponse,
    DataPlanView,
    DataPurchaseRequest,
    DataPurchaseResponse,
    NetworkListResponse,
    NetworkView,
)
from app.services.bill_service import BillService, DataPlanNotFound
from app.services.idempotency_service import IdempotencyConflict, IdempotencyService
from app.services.wallet_service import InsufficientBalance
from app.utils.responses import success


router = APIRouter(prefix="/bills", tags=["bills"])


# Static catalog — VTPass serviceIDs are stable. Prefix table drives the
# client-side network autodetect.
_NETWORK_CATALOG = [
    NetworkView(
        id="mtn", name="MTN",
        prefixes=["0803", "0806", "0810", "0813", "0814", "0816", "0703",
                  "0706", "0903", "0906"],
    ),
    NetworkView(
        id="airtel", name="Airtel",
        prefixes=["0802", "0808", "0812", "0701", "0708", "0902", "0907", "0901"],
    ),
    NetworkView(
        id="glo", name="Glo",
        prefixes=["0805", "0807", "0811", "0815", "0705", "0905"],
    ),
    NetworkView(
        id="etisalat", name="9mobile",
        prefixes=["0809", "0817", "0818", "0909", "0908"],
    ),
]


# ── Networks ─────────────────────────────────────────────────────────────


@router.get("/airtime/networks", response_model=None)
async def list_airtime_networks(request: Request, user: User = Depends(get_current_user)):
    body = NetworkListResponse(networks=_NETWORK_CATALOG)
    return success(
        body.model_dump(mode="json"),
        request_id=getattr(request.state, "request_id", None),
    )


# ── Airtime ──────────────────────────────────────────────────────────────


@router.post("/airtime", response_model=None, status_code=200)
@limiter.limit("30/minute", key_func=per_user_or_ip)
async def purchase_airtime(
    request: Request,
    body: AirtimePurchaseRequest,
    idem_key: str = Depends(require_idempotency_key),
    user: User = Depends(get_current_user),
    _pin_token: str = Depends(require_pin_token),
    bill_svc: BillService = Depends(get_bill_service),
    idem: IdempotencyService = Depends(get_idempotency_service),
):
    req_hash = idem.hash_body(
        user_id=str(user.id),
        endpoint="/bills/airtime",
        body=body.model_dump(mode="json"),
    )
    try:
        cached = await idem.lookup(
            user_id=str(user.id), key=idem_key, request_hash=req_hash,
        )
    except IdempotencyConflict:
        raise HTTPException(
            status_code=409,
            detail={"code": "IDEMPOTENCY_CONFLICT",
                    "message": "Idempotency key reused with different request"},
        )
    if cached is not None:
        return cached[1]

    try:
        result = await bill_svc.purchase_airtime(
            user_id=UUID(str(user.id)),
            network=body.network,
            phone=body.phone,
            amount_ngn=body.amount,
        )
    except InsufficientBalance:
        raise HTTPException(
            status_code=402,
            detail={"code": "INSUFFICIENT_BALANCE",
                    "message": "Wallet balance is not enough for this purchase"},
        )

    tx = result.tx
    resp = result.response
    partial = (
        resp.status.value == "delivered"
        and resp.delivered_amount_ngn < resp.requested_amount_ngn
    )
    body_out = success(
        AirtimePurchaseResponse(
            reference=tx.reference,
            status=tx.status.value,
            delivered_amount=resp.delivered_amount_ngn,
            requested_amount=resp.requested_amount_ngn,
            partial=partial,
        ).model_dump(mode="json"),
        request_id=getattr(request.state, "request_id", None),
    )

    await idem.store(
        user_id=str(user.id), key=idem_key, request_hash=req_hash,
        response_status=200, response_body=body_out,
    )
    return body_out


# ── Data ─────────────────────────────────────────────────────────────────


@router.get("/data/plans", response_model=None)
async def list_data_plans(
    request: Request,
    network: str,
    user: User = Depends(get_current_user),
    bill_svc: BillService = Depends(get_bill_service),
):
    plans = await bill_svc.list_data_plans(network=network)
    body = DataPlanListResponse(
        service_id=plans.service_id,
        plans=[
            DataPlanView(
                variation_code=v.variation_code,
                name=v.name,
                price=v.price_ngn,
                validity=v.validity,
            )
            for v in plans.variations
        ],
    )
    return success(
        body.model_dump(mode="json"),
        request_id=getattr(request.state, "request_id", None),
    )


@router.post("/data", response_model=None, status_code=200)
@limiter.limit("30/minute", key_func=per_user_or_ip)
async def purchase_data(
    request: Request,
    body: DataPurchaseRequest,
    idem_key: str = Depends(require_idempotency_key),
    user: User = Depends(get_current_user),
    _pin_token: str = Depends(require_pin_token),
    bill_svc: BillService = Depends(get_bill_service),
    idem: IdempotencyService = Depends(get_idempotency_service),
):
    req_hash = idem.hash_body(
        user_id=str(user.id),
        endpoint="/bills/data",
        body=body.model_dump(mode="json"),
    )
    try:
        cached = await idem.lookup(
            user_id=str(user.id), key=idem_key, request_hash=req_hash,
        )
    except IdempotencyConflict:
        raise HTTPException(
            status_code=409,
            detail={"code": "IDEMPOTENCY_CONFLICT",
                    "message": "Idempotency key reused with different request"},
        )
    if cached is not None:
        return cached[1]

    try:
        result = await bill_svc.purchase_data(
            user_id=UUID(str(user.id)),
            network=body.network,
            phone=body.phone,
            variation_code=body.variation_code,
        )
    except DataPlanNotFound as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "UNKNOWN_DATA_PLAN", "message": str(exc)},
        )
    except InsufficientBalance:
        raise HTTPException(
            status_code=402,
            detail={"code": "INSUFFICIENT_BALANCE",
                    "message": "Wallet balance is not enough for this purchase"},
        )

    tx = result.tx
    body_out = success(
        DataPurchaseResponse(
            reference=tx.reference,
            status=tx.status.value,
            plan_name=(tx.meta or {}).get("plan_name", ""),
            price=tx.amount,
        ).model_dump(mode="json"),
        request_id=getattr(request.state, "request_id", None),
    )

    await idem.store(
        user_id=str(user.id), key=idem_key, request_hash=req_hash,
        response_status=200, response_body=body_out,
    )
    return body_out
