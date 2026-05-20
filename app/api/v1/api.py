from fastapi import APIRouter

from app.api.v1.endpoints import (
    admin,
    auth,
    bills,
    health,
    push_tokens,
    referrals,
    transactions,
    wallet,
    webhooks,
)

api_router = APIRouter()

api_router.include_router(health.router, prefix="/health", tags=["health"])
api_router.include_router(auth.router)
api_router.include_router(wallet.router)
api_router.include_router(webhooks.router)
api_router.include_router(transactions.router)
api_router.include_router(bills.router)
api_router.include_router(push_tokens.router)
api_router.include_router(referrals.router)
api_router.include_router(admin.router)
