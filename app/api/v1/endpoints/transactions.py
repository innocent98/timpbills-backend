from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.db.models.transaction import Transaction
from app.db.models.user import User
from app.schemas.transaction import TransactionListResponse, TransactionView
from app.utils.responses import success


router = APIRouter(prefix="/transactions", tags=["transactions"])


@router.get("", response_model=None)
async def list_transactions(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    q = db.query(Transaction).filter(Transaction.user_id == user.id)
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
