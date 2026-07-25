"""Public (unauthenticated) account-deletion endpoints for the marketing
web flow. Identity is re-verified with email/phone + password; no bearer
token is issued or required."""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_token_store
from app.core.limiter import limiter
from app.schemas.account import (
    AccountDeletionRequest,
    AccountDeletionResponse,
    CancelDeletionResponse,
)
from app.services.account_deletion_service import AccountDeletionService
from app.services.token_store import TokenStore
from app.utils.responses import success

router = APIRouter(prefix="/account", tags=["account"])

_ERROR_MAP: dict[str, tuple[int, str]] = {
    "INVALID_CREDENTIALS": (401, "The email/phone or password is incorrect."),
    "WALLET_NOT_EMPTY": (
        409,
        "Withdraw your wallet balance before deleting your account.",
    ),
    "ALREADY_ANONYMIZED": (
        409,
        "This account has already been permanently deleted.",
    ),
}


def _raise(code: str) -> None:
    http_code, msg = _ERROR_MAP.get(code, (500, code))
    raise HTTPException(status_code=http_code, detail={"code": code, "message": msg})


def _svc(db: Session, token_store: TokenStore) -> AccountDeletionService:
    return AccountDeletionService(db=db, token_store=token_store)


@router.post("/deletion-request", response_model=None)
@limiter.limit("3/minute")
async def request_deletion(
    request: Request,
    body: AccountDeletionRequest,
    db: Session = Depends(get_db),
    token_store: TokenStore = Depends(get_token_store),
):
    svc = _svc(db, token_store)
    try:
        user = await svc.resolve_and_verify(
            identifier=body.identifier, password=body.password
        )
        scheduled = await svc.request_deletion(user=user)
    except ValueError as e:
        _raise(str(e))
    out = AccountDeletionResponse(scheduled_deletion_at=scheduled)
    return success(out.model_dump(mode="json"),
                   request_id=getattr(request.state, "request_id", None))


@router.post("/deletion-request/cancel", response_model=None)
@limiter.limit("3/minute")
async def cancel_deletion(
    request: Request,
    body: AccountDeletionRequest,
    db: Session = Depends(get_db),
    token_store: TokenStore = Depends(get_token_store),
):
    svc = _svc(db, token_store)
    try:
        user = await svc.resolve_and_verify(
            identifier=body.identifier, password=body.password
        )
        svc.cancel_deletion(user=user)
    except ValueError as e:
        _raise(str(e))
    return success(CancelDeletionResponse(cancelled=True).model_dump(),
                   request_id=getattr(request.state, "request_id", None))
