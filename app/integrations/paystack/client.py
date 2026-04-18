# app/integrations/paystack/client.py
"""Real Paystack client — HTTPX async + tenacity retry on 5xx."""
from decimal import Decimal

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.integrations.paystack.schemas import InitResponse, VerifyResponse, PaystackAuthorization
from app.integrations.paystack.signature import verify_paystack_signature


class PaystackError(Exception):
    pass


class PaystackClient:
    def __init__(self) -> None:
        if not settings.PAYSTACK_SECRET_KEY:
            raise RuntimeError("PAYSTACK_SECRET_KEY must be set for real client")
        self._secret = settings.PAYSTACK_SECRET_KEY
        self._base = settings.PAYSTACK_BASE_URL
        self._headers = {
            "Authorization": f"Bearer {self._secret}",
            "Content-Type": "application/json",
        }

    @retry(
        reraise=True,
        retry=retry_if_exception_type(httpx.HTTPStatusError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
    )
    async def initialize(
        self, *, amount_kobo: int, email: str, reference: str,
        callback_url: str | None = None, metadata: dict | None = None,
    ) -> InitResponse:
        payload = {
            "amount": amount_kobo,
            "email": email,
            "reference": reference,
        }
        if callback_url:
            payload["callback_url"] = callback_url
        if metadata:
            payload["metadata"] = metadata
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{self._base}/transaction/initialize",
                json=payload, headers=self._headers,
            )
            r.raise_for_status()
        body = r.json()
        if not body.get("status"):
            raise PaystackError(body.get("message", "init failed"))
        data = body["data"]
        return InitResponse(
            authorization_url=data["authorization_url"],
            access_code=data["access_code"],
            reference=data["reference"],
        )

    @retry(
        reraise=True,
        retry=retry_if_exception_type(httpx.HTTPStatusError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
    )
    async def verify(self, *, reference: str) -> VerifyResponse:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"{self._base}/transaction/verify/{reference}",
                headers=self._headers,
            )
            r.raise_for_status()
        body = r.json()
        if not body.get("status"):
            raise PaystackError(body.get("message", "verify failed"))
        d = body["data"]
        auth = d.get("authorization") or {}
        return VerifyResponse(
            reference=d["reference"],
            status=d["status"],
            amount=Decimal(d["amount"]) / Decimal(100),
            paid_at=d.get("paid_at"),
            authorization=PaystackAuthorization(
                channel=auth.get("channel"),
                last4=auth.get("last4"),
                bank=auth.get("bank"),
            ) if auth else None,
        )

    def verify_signature(self, *, raw_body: bytes, signature: str) -> bool:
        return verify_paystack_signature(
            raw_body=raw_body, signature=signature, secret=self._secret
        )
