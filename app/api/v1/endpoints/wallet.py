from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import (
    get_current_user,
    get_idempotency_service,
    get_paystack_provider,
    get_transaction_service,
    get_wallet_service,
    require_idempotency_key,
    require_pin_token,
)
from app.core.config import settings
from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.payment import Payment, PaymentStatus
from app.db.models.user import User
from app.integrations.paystack.base import PaymentProvider
from app.schemas.wallet import FundWalletRequest, FundWalletResponse, WalletResponse
from app.services.idempotency_service import IdempotencyConflict, IdempotencyService
from app.services.transaction_service import TransactionService
from app.services.wallet_service import WalletService
from app.utils.responses import success


router = APIRouter(prefix="/wallet", tags=["wallet"])


@router.get("", response_model=None)
async def get_wallet(
    request: Request,
    user: User = Depends(get_current_user),
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
async def fund_wallet(
    request: Request,
    body: FundWalletRequest,
    idem_key: str = Depends(require_idempotency_key),
    user: User = Depends(get_current_user),
    pin_token: str = Depends(require_pin_token),
    tx_svc: TransactionService = Depends(get_transaction_service),
    idem: IdempotencyService = Depends(get_idempotency_service),
    paystack: PaymentProvider = Depends(get_paystack_provider),
):
    req_hash = idem.hash_body(
        user_id=str(user.id),
        endpoint="/wallet/fund",
        body=body.model_dump(mode="json"),
    )
    try:
        cached = await idem.lookup(
            user_id=str(user.id), key=idem_key, request_hash=req_hash
        )
    except IdempotencyConflict:
        raise HTTPException(
            status_code=409,
            detail={"code": "IDEMPOTENCY_CONFLICT",
                    "message": "Idempotency key reused with different request"},
        )
    if cached is not None:
        return cached[1]

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
