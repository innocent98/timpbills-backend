from fastapi import APIRouter
from app.api.v1.endpoints import auth, health, transactions, wallet, webhooks

api_router = APIRouter()

api_router.include_router(health.router, prefix="/health", tags=["health"])
api_router.include_router(auth.router)
api_router.include_router(wallet.router)
api_router.include_router(webhooks.router)
api_router.include_router(transactions.router)
