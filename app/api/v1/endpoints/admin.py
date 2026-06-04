"""Admin-only endpoints — Sprint 5 BE-52.

v1 surface is intentionally minimal: a single manual-refund trigger
that ops can hit on a stuck/disputed transaction. Sprint 8 builds the
full admin dashboard UI on top of this and any sibling endpoints we
add here.

Auth model: every route under /admin requires `require_admin`, which
authenticates via an opaque session cookie backed by Redis and resolves
the acting `AdminUser` from the `admin_users` table. No/expired session
→ 401; a disabled admin row → 403. Write endpoints additionally carry
`require_admin_csrf` for a double-submit CSRF check (cookie value echoed
in `X-CSRF-Token`).
"""
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api._filters import parse_enum_or_400
from app.api.deps import (
    get_bill_service,
    get_db,
    require_admin,
    require_admin_csrf,
)
from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.admin_user import AdminUser
from app.db.models.transaction import Transaction
from app.db.models.transaction_event import TransactionEvent
from app.services.admin_service import AdminService
from app.services.bill_service import BillService
from app.utils.responses import success

router = APIRouter(prefix="/admin", tags=["admin"])


class ManualRefundRequest(BaseModel):
    """Body for `POST /admin/refunds/{reference}/trigger`.

    `reason` is mandatory — every admin write logs an audit row in
    `transaction_events` with this string + the actor admin id, so
    downstream review can reconstruct why a refund was forced. We
    cap the length so a runaway log injection can't bloat the
    audit table.
    """

    reason: str = Field(min_length=3, max_length=500)


@router.post(
    "/refunds/{reference}/trigger",
    response_model=None,
    # require_admin is listed first (and re-declared as a param below for
    # the actor row) so authentication resolves BEFORE the CSRF check:
    # FastAPI solves route-level dependencies ahead of path-operation
    # parameter dependencies, so an unauthenticated POST must hit
    # require_admin's 401 ADMIN_AUTH_REQUIRED rather than CSRF's 403.
    # The dependency cache dedupes require_admin to a single resolution.
    dependencies=[Depends(require_admin), Depends(require_admin_csrf)],
)
async def admin_trigger_refund(
    reference: str,
    body: ManualRefundRequest,
    request: Request,
    admin: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    bill_svc: Annotated[BillService, Depends(get_bill_service)],
):
    """Force a refund on a transaction.

    Idempotent: if the underlying tx already has a refund row (any prior
    refund path — webhook, reconcile, sync-purchase failure, or a prior
    admin trigger), we return 200 with a `was_created=false` payload and
    do nothing. We still write an audit `transaction_events` row marking
    the admin attempt — ops should be able to see "Adebayo tried to
    re-trigger this on 2026-04-29 and there was already a refund."

    Non-bill transactions (e.g. wallet_funding, refund itself) reject
    with 400; refund_engine fan-out for those is owned by the relevant
    sprint (wallet refunds → S2 webhook reconciliation, not this
    endpoint).
    """
    tx = (
        db.query(Transaction)
        .filter(Transaction.reference == reference)
        .first()
    )
    if tx is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "TRANSACTION_NOT_FOUND", "message": "Transaction not found"},
        )

    # Refunds and wallet-funding don't flow through BillService refund
    # path. Be explicit; "trying to refund a refund" is operator error
    # and silent success would obscure it.
    if tx.type in (TransactionType.refund, TransactionType.wallet_funding):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "UNREFUNDABLE_TX_TYPE",
                "message": f"Cannot manually refund tx of type {tx.type.value}",
            },
        )

    # Use TransactionService.create_refund directly so we can read
    # back `was_created` — the BillService wrapper drops it. Same
    # idempotency-by-original_reference semantics as the live failure
    # path; a second admin trigger after a webhook refund is a no-op.
    refund, was_created = bill_svc._tx.create_refund(  # noqa: SLF001
        original_tx=tx, amount=tx.amount,
        reason=f"admin_manual_refund: {body.reason}",
    )
    if was_created:
        bill_svc._wallet.credit(user_id=tx.user_id, amount=refund.amount)  # noqa: SLF001

    # Audit row on the original tx — every admin action gets one, even
    # the no-op idempotent path. context records the admin actor + the
    # refund row's reference so the audit trail is self-contained.
    audit_event = TransactionEvent(
        transaction_id=tx.id,
        from_status=tx.status,
        to_status=tx.status,  # placeholder; the real transition (if any)
                              # writes its own event below via TransactionService.transition
        reason=f"admin_manual_refund_attempt: {body.reason}",
        context={
            "actor_admin_user_id": str(admin.id),
            "actor_admin_email":   admin.email,
            "refund_reference":    refund.reference,
            "refund_was_created":  was_created,
        },
    )
    db.add(audit_event)
    db.commit()

    # If we just created the refund row AND the original tx isn't yet
    # in a refund-related state, walk it through success/failed →
    # refund_pending → refunded so the user-facing history reads right.
    # The state machine in TransactionService rejects illegal transitions,
    # so we gate the call site rather than try/excepting after the fact.
    if was_created and tx.status in (TransactionStatus.success, TransactionStatus.failed):
        bill_svc._tx.transition(  # noqa: SLF001
            tx, to_status=TransactionStatus.refund_pending,
            reason=f"admin_manual_refund: {body.reason}",
            context={"actor_admin_user_id": str(admin.id)},
        )
        bill_svc._tx.transition(  # noqa: SLF001
            tx, to_status=TransactionStatus.refunded,
            reason=f"admin_manual_refund: {body.reason}",
            context={"actor_admin_user_id": str(admin.id)},
        )

    return success(
        {
            "transaction_reference": tx.reference,
            "transaction_status":    tx.status.value,
            "refund_reference":      refund.reference,
            "refund_amount":         str(refund.amount),
            "was_created":           was_created,
        },
        request_id=getattr(request.state, "request_id", None),
    )


@router.get("/overview", response_model=None)
async def admin_overview(
    request: Request,
    admin: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    days: int = 7,
):
    days = max(1, min(days, 90))
    data = AdminService(db=db).overview(days=days)
    return success(data, request_id=getattr(request.state, "request_id", None))


@router.get("/transactions", response_model=None)
async def admin_list_transactions(
    request: Request,
    admin: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    date_from: datetime | None = Query(
        default=None, description="Inclusive lower bound (ISO 8601)"
    ),
    date_to: datetime | None = Query(
        default=None, description="Exclusive upper bound (ISO 8601)"
    ),
    user_id: str | None = Query(default=None),
    q: str | None = Query(default=None),
):
    """Filterable, paginated transaction list for the ops dashboard.

    ``limit`` is bounded 1..100 (422 on violation) so a runaway client
    can't pull the whole table in one page. ``date_from`` is inclusive,
    ``date_to`` exclusive. Bad ``type``/``status`` values are a 400
    ``INVALID_FILTER`` (client error), never a 500.
    """
    type_enum = parse_enum_or_400(TransactionType, type, field="type")
    status_enum = parse_enum_or_400(TransactionStatus, status, field="status")
    data = AdminService(db=db).list_transactions(
        limit=limit, offset=offset, type_=type_enum, status=status_enum,
        date_from=date_from, date_to=date_to, user_id=user_id, q=q,
    )
    return success(data, request_id=getattr(request.state, "request_id", None))


@router.get("/transactions/{reference}", response_model=None)
async def admin_transaction_detail(
    reference: str,
    request: Request,
    admin: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    """Full investigation payload for one transaction — tx + user summary
    + linked payment + ordered event timeline. 404 on unknown reference."""
    data = AdminService(db=db).get_transaction_detail(reference=reference)
    if data is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "TRANSACTION_NOT_FOUND", "message": "Transaction not found"},
        )
    return success(data, request_id=getattr(request.state, "request_id", None))
