"""/users/me/push-tokens endpoints — FCM device token registration.

POST is idempotent on (user_id, fcm_token) so the Flutter app can re-
register on every launch without pin/idempotency-key ceremony. DELETE
is owner-scoped; we return 404 whether the row is missing OR owned by
another user so ownership does not leak across a side-channel.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import get_current_user, get_push_tokens_service
from app.core.limiter import limiter, per_user_or_ip
from app.db.models.user import User
from app.schemas.push_tokens import PushTokenRequest, PushTokenResponse
from app.services.push_tokens_service import PushTokensService
from app.utils.responses import success


router = APIRouter(prefix="/users/me/push-tokens", tags=["push-tokens"])


@router.post("", response_model=None, status_code=200)
@limiter.limit("20/minute", key_func=per_user_or_ip)
async def register_push_token(
    request: Request,
    body: PushTokenRequest,
    user: User = Depends(get_current_user),
    svc: PushTokensService = Depends(get_push_tokens_service),
):
    row = svc.upsert_for_user(
        user_id=UUID(str(user.id)),
        fcm_token=body.fcm_token,
        platform=body.platform,
    )
    body_out = PushTokenResponse(
        id=row.id,
        fcm_token=row.fcm_token,
        platform=row.platform,
        last_seen_at=row.last_seen_at,
    )
    return success(
        body_out.model_dump(mode="json"),
        request_id=getattr(request.state, "request_id", None),
    )


@router.delete("/{token_id}", response_model=None, status_code=204)
@limiter.limit("20/minute", key_func=per_user_or_ip)
async def delete_push_token(
    request: Request,
    token_id: UUID,
    user: User = Depends(get_current_user),
    svc: PushTokensService = Depends(get_push_tokens_service),
):
    deleted = svc.delete_for_user(
        user_id=UUID(str(user.id)),
        token_id=token_id,
    )
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail={"code": "PUSH_TOKEN_NOT_FOUND",
                    "message": "Push token not found"},
        )
