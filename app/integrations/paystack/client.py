# app/integrations/paystack/client.py
"""Real Paystack client — HTTPX async + tenacity retry on 5xx."""
from decimal import Decimal

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.integrations.paystack.errors import PaystackError  # re-exported
from app.integrations.paystack.schemas import (
    AssignDedicatedAccountResponse,
    BankListItem,
    CreateCustomerResponse,
    DedicatedAccountDetails,
    DvaProvider,
    InitResponse,
    PaystackAuthorization,
    VerifyResponse,
)
from app.integrations.paystack.signature import verify_paystack_signature


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

    # ── Dedicated Virtual Accounts ───────────────────────────────────────
    @retry(
        reraise=True,
        retry=retry_if_exception_type(httpx.HTTPStatusError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
    )
    async def create_customer(
        self, *, email: str, first_name: str, last_name: str, phone: str
    ) -> CreateCustomerResponse:
        payload = {
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "phone": phone,
        }
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{self._base}/customer", json=payload, headers=self._headers
            )
            r.raise_for_status()
        body = r.json()
        if not body.get("status"):
            raise PaystackError(body.get("message", "create_customer failed"))
        d = body["data"]
        return CreateCustomerResponse(
            customer_code=d["customer_code"], customer_id=str(d.get("id") or "") or None
        )

    @retry(
        reraise=True,
        retry=retry_if_exception_type(httpx.HTTPStatusError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
    )
    async def assign_dedicated_account(
        self, *, email: str, first_name: str, middle_name: str, last_name: str,
        phone: str, preferred_bank: str, country: str, account_number: str,
        bvn: str, bank_code: str,
    ) -> AssignDedicatedAccountResponse:
        payload = {
            "email": email,
            "first_name": first_name,
            "middle_name": middle_name,
            "last_name": last_name,
            "phone": phone,
            "preferred_bank": preferred_bank,
            "country": country,
            "account_number": account_number,
            "bvn": bvn,
            "bank_code": bank_code,
        }
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{self._base}/dedicated_account/assign",
                json=payload, headers=self._headers,
            )
            r.raise_for_status()  # 202 is a success status
        body = r.json()
        return AssignDedicatedAccountResponse(
            status=bool(body.get("status")),
            message=body.get("message", ""),
        )

    @retry(
        reraise=True,
        retry=retry_if_exception_type(httpx.HTTPStatusError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
    )
    async def fetch_dedicated_account(
        self, *, account_id: str
    ) -> DedicatedAccountDetails:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"{self._base}/dedicated_account/{account_id}",
                headers=self._headers,
            )
            r.raise_for_status()
        d = r.json().get("data") or {}
        bank = d.get("bank") or {}
        return DedicatedAccountDetails(
            account_number=d.get("account_number"),
            account_name=d.get("account_name"),
            bank_name=bank.get("name"),
            bank_slug=bank.get("slug"),
            dedicated_account_id=str(d.get("id") or "") or None,
            status=("active" if d.get("active") else None),
        )

    @retry(
        reraise=True,
        retry=retry_if_exception_type(httpx.HTTPStatusError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
    )
    async def requery_dedicated_account(
        self, *, account_number: str, provider_slug: str
    ) -> AssignDedicatedAccountResponse:
        params = {"account_number": account_number, "provider_slug": provider_slug}
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"{self._base}/dedicated_account/requery",
                params=params, headers=self._headers,
            )
            r.raise_for_status()
        body = r.json()
        return AssignDedicatedAccountResponse(
            status=bool(body.get("status")), message=body.get("message", "")
        )

    @retry(
        reraise=True,
        retry=retry_if_exception_type(httpx.HTTPStatusError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
    )
    async def list_dva_providers(self) -> list[DvaProvider]:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"{self._base}/dedicated_account/available_providers",
                headers=self._headers,
            )
            r.raise_for_status()
        data = r.json().get("data") or []
        return [
            DvaProvider(
                provider_slug=p.get("provider_slug", ""),
                bank_name=p.get("bank_name", ""),
            )
            for p in data
        ]

    @retry(
        reraise=True,
        retry=retry_if_exception_type(httpx.HTTPStatusError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
    )
    async def list_banks(self, *, country: str = "nigeria") -> list[BankListItem]:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"{self._base}/bank",
                params={"country": country}, headers=self._headers,
            )
            r.raise_for_status()
        data = r.json().get("data") or []
        return [
            BankListItem(
                name=b.get("name", ""), slug=b.get("slug", ""), code=b.get("code", "")
            )
            for b in data
        ]
