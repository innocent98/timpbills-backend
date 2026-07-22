from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import (
    get_idempotency_service,
    get_paystack_provider,
    get_transaction_service,
    get_virtual_account_service,
    get_wallet_service,
    require_full_auth_gates,
    require_idempotency_key,
    require_pin_token,
)
from app.core.config import settings
from app.core.limiter import limiter, per_user_or_ip
from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.payment import Payment, PaymentStatus
from app.db.models.user import User
from app.integrations.paystack.base import PaymentProvider
from app.schemas.virtual_account import (
    BankListItemResponse,
    ProvisionVirtualAccountRequest,
    VirtualAccountResponse,
)
from app.schemas.wallet import FundWalletRequest, FundWalletResponse, WalletResponse
from app.services.idempotency_service import IdempotencyConflict, IdempotencyService
from app.services.transaction_service import TransactionService
from app.services.virtual_account_service import KycRequired, VirtualAccountService
from app.services.wallet_service import WalletService
from app.utils.responses import success

router = APIRouter(prefix="/wallet", tags=["wallet"])


@router.get("", response_model=None)
@limiter.limit("30/minute", key_func=per_user_or_ip)
async def get_wallet(
    request: Request,
    user: User = Depends(require_full_auth_gates),
    svc: WalletService = Depends(get_wallet_service),
):
    w = svc.get_or_create(user_id=user.id)
    body = WalletResponse(balance=w.balance, balance_cap=w.balance_cap)
    return success(
        body.model_dump(mode="json"),
        request_id=getattr(request.state, "request_id", None),
    )


def _calculate_fee(amount: Decimal) -> Decimal:
    """Paystack local-card fee passed through to the user.

    Wallet funding is break-even for Timpbills per PRD §6.3 — we don't add
    a margin here. The fee shown is exactly what Paystack will deduct from
    the settlement for this transaction, so the user's wallet receives
    their requested amount in full.

        fee = amount * 1.5% + (₦100 when amount ≥ ₦2,500), capped at ₦2,000
    """
    pct = Decimal(str(settings.PAYSTACK_CARD_FEE_PERCENT)) / Decimal("100")
    fixed = Decimal(settings.PAYSTACK_CARD_FEE_FIXED_NAIRA)
    threshold = Decimal(settings.PAYSTACK_CARD_FEE_FIXED_THRESHOLD_NAIRA)
    cap = Decimal(settings.PAYSTACK_CARD_FEE_CAP_NAIRA)

    fee = amount * pct
    if amount >= threshold:
        fee += fixed
    fee = fee.quantize(Decimal("0.01"))
    return min(fee, cap)


@router.post("/fund", response_model=None, status_code=200)
@limiter.limit("30/minute", key_func=per_user_or_ip)
async def fund_wallet(
    request: Request,
    body: FundWalletRequest,
    idem_key: str = Depends(require_idempotency_key),
    user: User = Depends(require_full_auth_gates),
    pin_token: str = Depends(require_pin_token),
    tx_svc: TransactionService = Depends(get_transaction_service),
    wallet_svc: WalletService = Depends(get_wallet_service),
    idem: IdempotencyService = Depends(get_idempotency_service),
    paystack: PaymentProvider = Depends(get_paystack_provider),
):
    req_hash = idem.hash_body(
        user_id=str(user.id),
        endpoint="/wallet/fund",
        body=body.model_dump(mode="json"),
    )
    try:
        state, cached = await idem.lookup_or_acquire(
            user_id=str(user.id), key=idem_key, request_hash=req_hash
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
        # Pre-flight KYC gate (S3C-P4b). Refuse the fund request before we
        # call Paystack if the credit would eventually overshoot the user's
        # balance cap. Without this, an over-cap amount would: call Paystack
        # → user pays → charge.success webhook tries to credit →
        # WalletService.credit raises KycCapExceeded → 422 + Paystack retries
        # until ops raises the tier — meanwhile the user's money sits at
        # Paystack's merchant balance. Short-circuiting here gives the mobile
        # client a clear error + a `remaining_headroom` to drive an "Upgrade
        # KYC" CTA.
        wallet_row = wallet_svc.get_or_create(user_id=user.id)
        projected = wallet_row.balance + body.amount
        if projected > wallet_row.balance_cap:
            remaining = max(wallet_row.balance_cap - wallet_row.balance, Decimal("0"))
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "KYC_LIMIT_EXCEEDED",
                    "message": (
                        "This amount would push your wallet past your tier cap. "
                        "Fund a smaller amount or upgrade your KYC tier."
                    ),
                    "details": {
                        "remaining_headroom": str(remaining),
                        "balance_cap":        str(wallet_row.balance_cap),
                    },
                },
            )

        fee = _calculate_fee(body.amount)
        tx = tx_svc.create(
            user_id=user.id,
            type=TransactionType.wallet_funding,
            amount=body.amount,
            fee=fee,
        )

        gross_kobo = int((body.amount + fee) * 100)
        init = await paystack.initialize(
            amount_kobo=gross_kobo,
            email=user.email,
            reference=tx.reference,
            callback_url=settings.PAYSTACK_CALLBACK_URL,
            metadata={"transaction_id": str(tx.id), "user_id": str(user.id)},
        )

        payment = Payment(
            transaction_id=tx.id,
            provider="paystack",
            provider_reference=init.reference,
            status=PaymentStatus.pending,
        )
        tx_svc._db.add(payment)
        tx_svc.transition(tx, to_status=TransactionStatus.processing, reason="paystack_init")

        body_out = success(
            FundWalletResponse(
                reference=tx.reference,
                authorization_url=init.authorization_url,
                amount=body.amount,
                fee=fee,
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


@router.post("/virtual-account", response_model=None, status_code=200)
@limiter.limit("10/minute", key_func=per_user_or_ip)
async def provision_virtual_account(
    request: Request,
    body: ProvisionVirtualAccountRequest,
    user: User = Depends(require_full_auth_gates),
    va_svc: VirtualAccountService = Depends(get_virtual_account_service),
):
    # Not a money-move: no X-Pin-Token. Standard bearer auth only.
    try:
        va = await va_svc.provision(
            user=user,
            bvn=body.bvn,
            account_number=body.account_number,
            bank_code=body.bank_code,
            preferred_bank=body.preferred_bank,
        )
    except KycRequired:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "KYC_REQUIRED",
                "message": "Complete KYC tier 1 before setting up an account number.",
            },
        )
    out = VirtualAccountResponse(
        status=va.status.value,
        account_number=va.account_number,
        account_name=va.account_name,
        bank_name=va.bank_name,
        failure_reason=va.failure_reason,
    )
    return success(
        out.model_dump(mode="json"),
        request_id=getattr(request.state, "request_id", None),
    )


@router.get("/virtual-account", response_model=None)
@limiter.limit("30/minute", key_func=per_user_or_ip)
async def get_virtual_account(
    request: Request,
    user: User = Depends(require_full_auth_gates),
    va_svc: VirtualAccountService = Depends(get_virtual_account_service),
):
    va = va_svc.get_for_user(user_id=user.id)
    if va is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "NO_VIRTUAL_ACCOUNT", "message": "No account number set up yet."},
        )
    out = VirtualAccountResponse(
        status=va.status.value,
        account_number=va.account_number,
        account_name=va.account_name,
        bank_name=va.bank_name,
        failure_reason=va.failure_reason,
    )
    return success(
        out.model_dump(mode="json"),
        request_id=getattr(request.state, "request_id", None),
    )


@router.get("/banks", response_model=None)
@limiter.limit("30/minute", key_func=per_user_or_ip)
async def list_banks(
    request: Request,
    user: User = Depends(require_full_auth_gates),
    paystack: PaymentProvider = Depends(get_paystack_provider),
):
    banks = await paystack.list_banks(country="nigeria")
    out = {
        "banks": [
            BankListItemResponse(name=b.name, slug=b.slug, code=b.code).model_dump(mode="json")
            for b in banks
        ]
    }
    return success(out, request_id=getattr(request.state, "request_id", None))
