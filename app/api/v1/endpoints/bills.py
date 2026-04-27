"""/bills/* endpoints — airtime, data, and electricity. Cable endpoints
are appended alongside electricity as Sprint 4 lands."""
import re
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import (
    get_bill_service,
    get_current_user,
    get_db,
    get_idempotency_service,
    get_wallet_service,
    require_idempotency_key,
    require_pin_token,
)
from app.core.config import settings
from app.core.limiter import limiter, per_user_or_ip
from app.core.logger import log
from app.db.models.user import User
from app.integrations.vtpass.base import (
    ProviderPermanentFailure,
    ProviderTemporaryFailure,
)
from app.schemas.bills import (
    AirtimePurchaseRequest,
    AirtimePurchaseResponse,
    CablePlanListResponse,
    CablePlanView,
    CableProviderListResponse,
    CableProviderView,
    CablePurchaseRequest,
    CablePurchaseResponse,
    DataPlanListResponse,
    DataPlanView,
    DataPurchaseRequest,
    DataPurchaseResponse,
    DiscoListResponse,
    DiscoView,
    ElectricityPurchaseRequest,
    ElectricityPurchaseResponse,
    MeterValidationRequest,
    MeterValidationResponse,
    NetworkListResponse,
    NetworkView,
    SmartcardValidationRequest,
    SmartcardValidationResponse,
)
from app.services.bill_service import (
    BillService,
    CablePlanNotFound,
    CableRenewalUnavailable,
    DataPlanNotFound,
)
from app.services.idempotency_service import IdempotencyConflict, IdempotencyService
from app.services.wallet_service import InsufficientBalance, WalletService
from app.utils.responses import success


router = APIRouter(prefix="/bills", tags=["bills"])


# Sprint 4 B28 (B-I1 follow-up): VTPass's ProviderPermanentFailure /
# ProviderTemporaryFailure messages are constructed as:
#   "vtpass {op} {service_id}/{identifier}: code=... desc=..."
# Logging the raw exception at WARNING routes the meter / smartcard
# numbers into Sentry + Datadog — the earlier B21 fix moved them out
# of the API response but left them in the log stream. Scrub before
# logging so ops retains service_id + error code/desc without the PII.
_VTPASS_EXC_ID_RE = re.compile(
    r"^(vtpass \S+ )([^/:]+)/(\S+)(:.*)$", re.DOTALL,
)


def _scrub_vtpass_error(exc: Exception) -> str:
    """Return the exception message with the user-supplied identifier
    (meter number / smartcard number) masked. Keeps service_id and the
    trailing code/desc so ops can still triage. Non-matching messages
    pass through untouched — the exception still flows up unchanged,
    only the string representation used for logging is modified."""
    raw = str(exc)
    m = _VTPASS_EXC_ID_RE.match(raw)
    if not m:
        return raw
    prefix, service_id, identifier, tail = m.groups()
    # Keep only the last 4 digits of the identifier so logs remain
    # correlatable across retries without leaking the full number.
    masked = (
        f"•••• {identifier[-4:]}" if len(identifier) >= 4 else "••••"
    )
    return f"{prefix}{service_id}/{masked}{tail}"


# Prefix table for client-side network autodetect — VTPass doesn't own
# this, so it stays server-side. The catalog names/logos come from VTPass
# at runtime; this table just tells the mobile UI "these prefixes route
# to this serviceID."
_NETWORK_PREFIXES: dict[str, list[str]] = {
    "mtn":      ["0803", "0806", "0810", "0813", "0814", "0816", "0703",
                 "0706", "0903", "0906"],
    "airtel":   ["0802", "0808", "0812", "0701", "0708", "0902", "0907", "0901"],
    "glo":      ["0805", "0807", "0811", "0815", "0705", "0905"],
    "etisalat": ["0809", "0817", "0818", "0909", "0908"],
}


def _cap_for(service_id: str) -> Decimal:
    """Per-DisCo purchase cap with the default-cap fallback.

    Kept at module scope (not a BillService method) so tests can
    monkeypatch ``settings.ELECTRICITY_DISCO_CAPS`` without wiring a
    service, and so the resolution stays pure — no DB, no I/O.
    """
    return settings.ELECTRICITY_DISCO_CAPS.get(
        service_id, settings.ELECTRICITY_DEFAULT_CAP
    )


# Cable bouquet providers — fetched live from VTPass
# /api/services?identifier=tv-subscription via BillService.list_service_catalog.
# Filter set: we only surface the bouquets our mobile app knows how to
# transact (streaming-only providers like ShowMax don't fit the
# validate-smartcard + variation-code flow yet).
_CABLE_SUPPORTED_IDS = {"dstv", "gotv", "startimes"}


# ── Networks ─────────────────────────────────────────────────────────────


@router.get("/airtime/networks", response_model=None)
async def list_airtime_networks(
    request: Request,
    user: User = Depends(get_current_user),
    bill_svc: BillService = Depends(get_bill_service),
):
    """Dynamic network list — names + logos + min/max amounts come from
    VTPass; prefix table (for client-side autodetect) comes from our
    `_NETWORK_PREFIXES`. Only the four NG GSM networks are surfaced
    (VTPass also returns `foreign-airtime` under this identifier; we
    filter it out because our airtime flow is NG-only)."""
    catalog = await bill_svc.list_service_catalog(identifier="airtime")
    networks = [
        NetworkView(
            id=s.service_id,
            name=s.name,
            prefixes=_NETWORK_PREFIXES.get(s.service_id, []),
            image=s.image,
            minimum_amount=s.minimum_amount,
            maximum_amount=s.maximum_amount,
        )
        for s in catalog.services
        if s.service_id in _NETWORK_PREFIXES
    ]
    body = NetworkListResponse(networks=networks)
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
        state, cached = await idem.lookup_or_acquire(
            user_id=str(user.id), key=idem_key, request_hash=req_hash,
        )
    except IdempotencyConflict:
        raise HTTPException(
            status_code=409,
            detail={"code": "IDEMPOTENCY_CONFLICT",
                    "message": "Idempotency key reused with different request"},
        )
    if state == "hit":
        assert cached is not None
        return cached[1]
    if state == "in_flight":
        raise HTTPException(
            status_code=409,
            detail={"code": "TX_IN_FLIGHT",
                    "message": "Transaction is still being processed; retry shortly"},
        )

    try:
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
    except HTTPException:
        # Known/expected error — release the sentinel so the user can retry
        # with the same Idempotency-Key after fixing the underlying issue
        # (e.g., funding the wallet). store() never ran, so the slot still
        # holds the in-flight sentinel.
        await idem.release_in_flight(user_id=str(user.id), key=idem_key)
        raise
    except Exception:
        # Unexpected failure — release sentinel too. Sentinel TTL (60s)
        # would catch it eventually, but explicit cleanup avoids a stuck
        # 60-second window for the user.
        await idem.release_in_flight(user_id=str(user.id), key=idem_key)
        raise


# ── Electricity ──────────────────────────────────────────────────────────


@router.post("/electricity/validate-meter", response_model=None, status_code=200)
@limiter.limit("30/minute", key_func=per_user_or_ip)
async def validate_meter(
    request: Request,
    body: MeterValidationRequest,
    user: User = Depends(get_current_user),
    bill_svc: BillService = Depends(get_bill_service),
):
    """Meter-number lookup against a DisCo. Not money-moving — no
    pin_token, no Idempotency-Key. Caching (5 min per-user) lives in
    BillService so repeated keystroke-edits don't burn VTPass credits."""
    try:
        validation = await bill_svc.validate_meter(
            user_id=UUID(str(user.id)),
            service_id=body.service_id,
            meter_number=body.meter_number,
            meter_type=body.meter_type,
        )
    except ProviderPermanentFailure as exc:
        # B21 redacted the user-facing response; B28 redacts the log
        # stream too — _scrub_vtpass_error masks the meter number
        # before it hits Sentry/Datadog aggregation.
        log.warning("validate_meter permanent failure: %s", _scrub_vtpass_error(exc))
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_METER",
                "message": "Meter number could not be validated. Check the number and try again.",
            },
        )
    except ProviderTemporaryFailure as exc:
        log.warning("validate_meter transient failure: %s", _scrub_vtpass_error(exc))
        raise HTTPException(
            status_code=503,
            detail={
                "code": "VTPASS_UNAVAILABLE",
                "message": "Meter validation is temporarily unavailable. Please try again shortly.",
            },
        )

    body_out = MeterValidationResponse(
        service_id=validation.service_id,
        meter_number=validation.meter_number,
        customer_name=validation.customer_name,
        address=validation.address,
        meter_type=validation.meter_type,
    )
    return success(
        body_out.model_dump(mode="json"),
        request_id=getattr(request.state, "request_id", None),
    )


@router.post("/electricity", response_model=None, status_code=200)
@limiter.limit("30/minute", key_func=per_user_or_ip)
async def purchase_electricity(
    request: Request,
    body: ElectricityPurchaseRequest,
    idem_key: str = Depends(require_idempotency_key),
    user: User = Depends(get_current_user),
    _pin_token: str = Depends(require_pin_token),
    bill_svc: BillService = Depends(get_bill_service),
    idem: IdempotencyService = Depends(get_idempotency_service),
    wallet_svc: WalletService = Depends(get_wallet_service),
):
    """Debit-and-deliver a prepaid/postpaid electricity top-up.

    Pre-flight order is cap-first (pure dict lookup) then balance (DB
    read) so obviously-over-cap requests short-circuit without touching
    the wallet row. Both gates run BEFORE the idempotency cache so
    transient rejections are never cached. The InsufficientBalance
    catch after the provider call is defence-in-depth against a
    concurrent debit racing the pre-flight read.
    """
    # ── Pre-flight 1: DisCo cap (S3C-P4b pattern) ───────────────────────
    cap = _cap_for(body.service_id)
    if body.amount > cap:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "DISCO_CAP_EXCEEDED",
                "message": f"Amount exceeds the maximum allowed for {body.service_id}",
                "details": {"max_allowed": str(cap), "disco": body.service_id},
            },
        )

    # ── Pre-flight 2: wallet balance ────────────────────────────────────
    if wallet_svc.balance(user_id=UUID(str(user.id))) < body.amount:
        raise HTTPException(
            status_code=402,
            detail={"code": "INSUFFICIENT_BALANCE",
                    "message": "Wallet balance is not enough for this purchase"},
        )

    # ── Sprint 4 B31 (B-I4 follow-up): unverified-phone visibility ────────
    # B26 auto-injects user.phone from the authenticated profile, which
    # saves the user a redundant input. But if `is_phone_verified` is
    # False — unusual but possible for users who landed at the bills
    # surface before completing KYC-1, or for DB rows seeded outside
    # the normal signup flow — VTPass's SMS-resend fallback on lost
    # tokens silently misroutes. The purchase itself still succeeds
    # (DisCo delivers the token and our email+push carry it to the
    # user), so this is NOT a block, just a signal: log a WARNING so
    # ops can investigate if the pattern becomes prevalent.
    if not user.is_phone_verified:
        log.warning(
            "electricity purchase for user %s with is_phone_verified=False; "
            "VTPass SMS-resend fallback may not reach the user",
            user.id,
        )

    # ── Idempotency gate ────────────────────────────────────────────────
    req_hash = idem.hash_body(
        user_id=str(user.id),
        endpoint="/bills/electricity",
        body=body.model_dump(mode="json"),
    )
    try:
        state, cached = await idem.lookup_or_acquire(
            user_id=str(user.id), key=idem_key, request_hash=req_hash,
        )
    except IdempotencyConflict:
        raise HTTPException(
            status_code=409,
            detail={"code": "IDEMPOTENCY_CONFLICT",
                    "message": "Idempotency key reused with different request"},
        )
    if state == "hit":
        assert cached is not None
        return cached[1]
    if state == "in_flight":
        raise HTTPException(
            status_code=409,
            detail={"code": "TX_IN_FLIGHT",
                    "message": "Transaction is still being processed; retry shortly"},
        )

    try:
        try:
            result = await bill_svc.purchase_electricity(
                user_id=UUID(str(user.id)),
                service_id=body.service_id,
                meter_number=body.meter_number,
                meter_type=body.meter_type,
                # Sprint 4 B26: phone is pulled from the authenticated user
                # profile, not the request body. VTPass still receives it;
                # BillService.purchase_electricity signature is unchanged.
                phone=user.phone,
                amount_ngn=body.amount,
            )
        except InsufficientBalance:
            raise HTTPException(
                status_code=402,
                detail={"code": "INSUFFICIENT_BALANCE",
                        "message": "Wallet balance is not enough for this purchase"},
            )

        tx = result.tx
        meta = tx.meta or {}
        body_out = success(
            ElectricityPurchaseResponse(
                reference=tx.reference,
                status=tx.status.value,
                service_id=body.service_id,
                meter_number=body.meter_number,
                amount=tx.amount,
                token=meta.get("token"),
                units=meta.get("units"),
            ).model_dump(mode="json"),
            request_id=getattr(request.state, "request_id", None),
        )

        await idem.store(
            user_id=str(user.id), key=idem_key, request_hash=req_hash,
            response_status=200, response_body=body_out,
        )
        return body_out
    except HTTPException:
        await idem.release_in_flight(user_id=str(user.id), key=idem_key)
        raise
    except Exception:
        await idem.release_in_flight(user_id=str(user.id), key=idem_key)
        raise


# ── Cable ────────────────────────────────────────────────────────────────


@router.get("/cable/providers", response_model=None)
async def list_cable_providers(
    request: Request,
    user: User = Depends(get_current_user),
    bill_svc: BillService = Depends(get_bill_service),
):
    """Dynamic cable provider list — fetched from VTPass then filtered
    to the three providers our validate-smartcard + bouquet-catalog
    flow supports (DStv / GOtv / StarTimes). ShowMax is surfaced by
    VTPass under the same identifier but is streaming-only — we exclude
    it to avoid confusing the smartcard prompt."""
    catalog = await bill_svc.list_service_catalog(identifier="tv-subscription")
    providers = [
        CableProviderView(
            id=s.service_id,
            name=s.name,
            image=s.image,
            minimum_amount=s.minimum_amount,
            maximum_amount=s.maximum_amount,
        )
        for s in catalog.services
        if s.service_id in _CABLE_SUPPORTED_IDS
    ]
    body = CableProviderListResponse(providers=providers)
    return success(
        body.model_dump(mode="json"),
        request_id=getattr(request.state, "request_id", None),
    )


@router.get("/electricity/discos", response_model=None)
async def list_electricity_discos(
    request: Request,
    user: User = Depends(get_current_user),
    bill_svc: BillService = Depends(get_bill_service),
):
    """NEW in Sprint 5 audit — enumerate the NG DisCos VTPass supports.
    The mobile picker used to ship a hardcoded list that had drifted
    (we had `phed` / `yedc`; live API returns `portharcourt-electric`
    / `yola-electric` + two providers we never listed: `benin-electric`
    and `aba-electric`). Sourcing the canonical list from VTPass makes
    this drift-proof."""
    catalog = await bill_svc.list_service_catalog(identifier="electricity-bill")
    discos = [
        DiscoView(
            id=s.service_id,
            name=s.name,
            image=s.image,
            minimum_amount=s.minimum_amount,
            maximum_amount=s.maximum_amount,
        )
        for s in catalog.services
    ]
    body = DiscoListResponse(discos=discos)
    return success(
        body.model_dump(mode="json"),
        request_id=getattr(request.state, "request_id", None),
    )


@router.post("/cable/validate-smartcard", response_model=None, status_code=200)
@limiter.limit("30/minute", key_func=per_user_or_ip)
async def validate_smartcard(
    request: Request,
    body: SmartcardValidationRequest,
    user: User = Depends(get_current_user),
    bill_svc: BillService = Depends(get_bill_service),
):
    """Smartcard lookup against a cable provider. Not money-moving — no
    pin_token, no Idempotency-Key. Caching (5 min per-user) lives in
    BillService so repeated keystroke-edits don't burn VTPass credits.
    Fresh / inactive smartcards return a 200 with status="inactive" and
    empty plan fields; only upstream permanent failures raise."""
    try:
        validation = await bill_svc.validate_smartcard(
            user_id=UUID(str(user.id)),
            service_id=body.service_id,
            smartcard_number=body.smartcard_number,
        )
    except ProviderPermanentFailure as exc:
        log.warning(
            "validate_smartcard permanent failure: %s", _scrub_vtpass_error(exc),
        )
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_SMARTCARD",
                "message": "Smartcard number could not be validated. Check the number and try again.",
            },
        )
    except ProviderTemporaryFailure as exc:
        log.warning(
            "validate_smartcard transient failure: %s", _scrub_vtpass_error(exc),
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "VTPASS_UNAVAILABLE",
                "message": "Smartcard validation is temporarily unavailable. Please try again shortly.",
            },
        )

    body_out = SmartcardValidationResponse(
        service_id=validation.service_id,
        smartcard_number=validation.smartcard_number,
        customer_name=validation.customer_name,
        current_plan_name=validation.current_plan_name,
        current_plan_code=validation.current_plan_code,
        status=validation.status,
        renewal_amount=validation.renewal_amount_ngn,
    )
    return success(
        body_out.model_dump(mode="json"),
        request_id=getattr(request.state, "request_id", None),
    )


@router.get("/cable/plans", response_model=None)
@limiter.limit("60/minute", key_func=per_user_or_ip)
async def list_cable_plans(
    request: Request,
    provider: str,
    mode: str,
    user: User = Depends(get_current_user),
    bill_svc: BillService = Depends(get_bill_service),
):
    """Return the cable bouquet catalog. `mode` is renew|change; both
    return the full catalog — the renew/change distinction drives the
    mobile UI (renew filters to current_plan_code; change shows all)
    and the VTPass wire service_id at purchase time."""
    try:
        plans = await bill_svc.list_cable_plans(service_id=provider, mode=mode)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_CABLE_MODE", "message": str(exc)},
        )
    body_out = CablePlanListResponse(
        service_id=plans.service_id,
        plans=[
            CablePlanView(
                variation_code=v.variation_code,
                name=v.name,
                price=v.price_ngn,
                validity=v.validity,
            )
            for v in plans.variations
        ],
    )
    return success(
        body_out.model_dump(mode="json"),
        request_id=getattr(request.state, "request_id", None),
    )


@router.post("/cable", response_model=None, status_code=200)
@limiter.limit("30/minute", key_func=per_user_or_ip)
async def purchase_cable(
    request: Request,
    body: CablePurchaseRequest,
    idem_key: str = Depends(require_idempotency_key),
    user: User = Depends(get_current_user),
    _pin_token: str = Depends(require_pin_token),
    bill_svc: BillService = Depends(get_bill_service),
    idem: IdempotencyService = Depends(get_idempotency_service),
):
    """Cable bouquet purchase. Two modes:

      * ``renew``  — BillService reads the cached SmartcardValidation
        (populated by validate-smartcard) and uses its current plan +
        renewal amount. No client-supplied price. Cache miss → 409
        CABLE_RENEWAL_UNAVAILABLE so the mobile layer can re-trigger
        validation.
      * ``change`` — client supplies ``variation_code``; BillService
        server-resolves the price from the catalog (never the client).
    """
    # Schema + mode sanity — we hard-require variation_code on change;
    # surfacing this before the idempotency gate keeps the error
    # identifiable on the client.
    if body.mode == "change" and not body.variation_code:
        raise HTTPException(
            status_code=400,
            detail={"code": "VARIATION_CODE_REQUIRED",
                    "message": "variation_code is required for mode=change"},
        )

    req_hash = idem.hash_body(
        user_id=str(user.id),
        endpoint="/bills/cable",
        body=body.model_dump(mode="json"),
    )
    try:
        state, cached = await idem.lookup_or_acquire(
            user_id=str(user.id), key=idem_key, request_hash=req_hash,
        )
    except IdempotencyConflict:
        raise HTTPException(
            status_code=409,
            detail={"code": "IDEMPOTENCY_CONFLICT",
                    "message": "Idempotency key reused with different request"},
        )
    if state == "hit":
        assert cached is not None
        return cached[1]
    if state == "in_flight":
        raise HTTPException(
            status_code=409,
            detail={"code": "TX_IN_FLIGHT",
                    "message": "Transaction is still being processed; retry shortly"},
        )

    try:
        try:
            result = await bill_svc.purchase_cable(
                user_id=UUID(str(user.id)),
                service_id=body.service_id,
                smartcard_number=body.smartcard_number,
                mode=body.mode,
                phone=user.phone,
                variation_code=body.variation_code,
            )
        except CableRenewalUnavailable as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "CABLE_RENEWAL_UNAVAILABLE", "message": str(exc)},
            )
        except CablePlanNotFound as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "UNKNOWN_CABLE_PLAN", "message": str(exc)},
            )
        except InsufficientBalance:
            raise HTTPException(
                status_code=402,
                detail={"code": "INSUFFICIENT_BALANCE",
                        "message": "Wallet balance is not enough for this purchase"},
            )

        tx = result.tx
        meta = tx.meta or {}
        body_out = success(
            CablePurchaseResponse(
                reference=tx.reference,
                status=tx.status.value,
                service_id=body.service_id,
                smartcard_number=body.smartcard_number,
                mode=body.mode,
                plan_code=meta.get("plan_code", ""),
                plan_name=meta.get("plan_name", ""),
                amount=tx.amount,
            ).model_dump(mode="json"),
            request_id=getattr(request.state, "request_id", None),
        )

        await idem.store(
            user_id=str(user.id), key=idem_key, request_hash=req_hash,
            response_status=200, response_body=body_out,
        )
        return body_out
    except HTTPException:
        await idem.release_in_flight(user_id=str(user.id), key=idem_key)
        raise
    except Exception:
        await idem.release_in_flight(user_id=str(user.id), key=idem_key)
        raise


# ── Data ─────────────────────────────────────────────────────────────────


@router.get("/data/plans", response_model=None)
@limiter.limit("60/minute", key_func=per_user_or_ip)
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
        state, cached = await idem.lookup_or_acquire(
            user_id=str(user.id), key=idem_key, request_hash=req_hash,
        )
    except IdempotencyConflict:
        raise HTTPException(
            status_code=409,
            detail={"code": "IDEMPOTENCY_CONFLICT",
                    "message": "Idempotency key reused with different request"},
        )
    if state == "hit":
        assert cached is not None
        return cached[1]
    if state == "in_flight":
        raise HTTPException(
            status_code=409,
            detail={"code": "TX_IN_FLIGHT",
                    "message": "Transaction is still being processed; retry shortly"},
        )

    try:
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
    except HTTPException:
        await idem.release_in_flight(user_id=str(user.id), key=idem_key)
        raise
    except Exception:
        await idem.release_in_flight(user_id=str(user.id), key=idem_key)
        raise
