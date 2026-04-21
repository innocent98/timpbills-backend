from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.limiter import limiter, per_user_or_ip
from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.transaction import Transaction
from app.db.models.user import User
from app.schemas.transaction import TransactionEventView, TransactionEventsResponse, TransactionListResponse, TransactionView
from app.services.transaction_service import TransactionService
from app.utils.responses import success


router = APIRouter(prefix="/transactions", tags=["transactions"])


def _parse_types(values: list[str] | None) -> list[TransactionType] | None:
    """Convert raw ?type= values into TransactionType enums. Unknown values
    raise 400 rather than silently filtering nothing."""
    if not values:
        return None
    out: list[TransactionType] = []
    for v in values:
        try:
            out.append(TransactionType(v))
        except ValueError:
            raise HTTPException(status_code=400, detail={
                "code": "INVALID_TX_TYPE",
                "message": f"Unknown transaction type: {v}",
            })
    return out


def _parse_statuses(values: list[str] | None) -> list[TransactionStatus] | None:
    if not values:
        return None
    out: list[TransactionStatus] = []
    for v in values:
        try:
            out.append(TransactionStatus(v))
        except ValueError:
            raise HTTPException(status_code=400, detail={
                "code": "INVALID_TX_STATUS",
                "message": f"Unknown transaction status: {v}",
            })
    return out


@router.get("", response_model=None)
@limiter.limit("60/minute", key_func=per_user_or_ip)
async def list_transactions(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    type: list[str] | None = Query(default=None, description="Filter by type; repeatable"),
    status: list[str] | None = Query(default=None, description="Filter by status; repeatable"),
    date_from: datetime | None = Query(default=None, description="Inclusive lower bound on created_at (ISO 8601)"),
    date_to: datetime | None = Query(default=None, description="Exclusive upper bound on created_at (ISO 8601)"),
):
    types = _parse_types(type)
    statuses = _parse_statuses(status)

    q = db.query(Transaction).filter(Transaction.user_id == user.id)
    if types:
        q = q.filter(Transaction.type.in_(types))
    if statuses:
        q = q.filter(Transaction.status.in_(statuses))
    if date_from is not None:
        q = q.filter(Transaction.created_at >= date_from)
    if date_to is not None:
        q = q.filter(Transaction.created_at < date_to)

    total = q.count()
    rows = (
        q.order_by(Transaction.created_at.desc())
         .offset(offset).limit(limit).all()
    )
    items = [
        TransactionView(
            reference=t.reference,
            type=t.type.value,
            status=t.status.value,
            amount=t.amount,
            fee=t.fee,
            currency=t.currency,
            created_at=t.created_at,
            meta=t.meta,
        )
        for t in rows
    ]
    body = TransactionListResponse(items=items, total=total)
    return success(body.model_dump(mode="json"), request_id=getattr(request.state, "request_id", None))


@router.get("/{reference}", response_model=None)
async def get_transaction(
    reference: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    t = (
        db.query(Transaction)
        .filter(Transaction.user_id == user.id, Transaction.reference == reference)
        .first()
    )
    if not t:
        raise HTTPException(status_code=404, detail={
            "code": "TRANSACTION_NOT_FOUND", "message": "Transaction not found"
        })
    view = TransactionView(
        reference=t.reference, type=t.type.value, status=t.status.value,
        amount=t.amount, fee=t.fee, currency=t.currency,
        created_at=t.created_at, meta=t.meta,
    )
    return success(view.model_dump(mode="json"), request_id=getattr(request.state, "request_id", None))


@router.get("/{reference}/events", response_model=None)
async def get_transaction_events(
    reference: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tx = (
        db.query(Transaction)
        .filter(
            Transaction.user_id == user.id,
            Transaction.reference == reference,
        )
        .first()
    )
    if not tx:
        raise HTTPException(status_code=404, detail={
            "code": "TRANSACTION_NOT_FOUND",
            "message": "Transaction not found",
        })

    tx_svc = TransactionService(db=db)
    events = tx_svc.events_for(tx)
    items = [
        TransactionEventView(
            at=e.created_at,
            from_status=e.from_status.value if e.from_status else None,
            to_status=e.to_status.value,
            reason=e.reason,
            context=e.context or {},
        )
        for e in events
    ]
    body = TransactionEventsResponse(items=items)
    return success(
        body.model_dump(mode="json"),
        request_id=getattr(request.state, "request_id", None),
    )
