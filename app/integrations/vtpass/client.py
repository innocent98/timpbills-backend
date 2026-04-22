"""Real VTPassClient — async HTTPX + tenacity retry.

VTPass quirks this client hides from the rest of the codebase:

 * Auth is a header pair: `api-key` always, plus `secret-key` for POSTs
   (purchases / queries) and `public-key` for GETs (catalog reads).
 * Response envelope is `{code, content, response_description, requestId,
   amount, transaction_date}`. Success is `code == "000"`; anything else
   is a failure. The delivery state lives in
   `content.transactions.status` — but sometimes `content.transactions`
   is missing on hard failures.
 * Amount in responses is sometimes a string, sometimes a number. We
   coerce to Decimal.
 * A 200 OK with a non-000 `code` is a permanent failure (invalid amount,
   unknown serviceID, etc.) — we translate to ProviderPermanentFailure.
   5xx / timeout / network is a ProviderTemporaryFailure so the reconcile
   worker can requery.
"""
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.core.logger import log
from app.integrations.vtpass.base import (
    BillProvider,
    ProviderPermanentFailure,
    ProviderTemporaryFailure,
)
from app.integrations.vtpass.schemas import (
    BillDeliveryStatus,
    BillPurchaseResponse,
    CablePlanList,
    CablePlanVariation,
    DataPlanList,
    DataPlanVariation,
    MeterValidation,
    SmartcardValidation,
)


# VTPass `code` values we treat as success. Per VTPass docs, "000" is the
# only universal success code; "099" means accepted-but-pending (upstream
# telco hasn't confirmed yet). Everything else is a failure.
_SUCCESS_CODE = "000"
_PENDING_CODE = "099"


def translate_response(
    body: dict[str, Any], *, request_id: str, requested: Decimal
) -> BillPurchaseResponse:
    """Map a VTPass envelope (purchase response, requery response, or
    webhook body — all share the same shape) to our normalized
    BillPurchaseResponse. Used by both VTPassClient and the
    /webhooks/vtpass endpoint.

    Handles the three branches:
      code == 000  → delivered (potentially partial if amount differs)
      code == 099  → pending (reconcile worker will requery)
      otherwise    → failed
    """
    code = str(body.get("code", ""))
    description = str(body.get("response_description", ""))
    content = body.get("content") or {}
    tx = (content.get("transactions") or {}) if isinstance(content, dict) else {}
    tx_id = str(tx.get("transactionId") or tx.get("transaction_id") or "")

    if code == _SUCCESS_CODE:
        delivered_amt = _safe_decimal(
            tx.get("amount")
            or body.get("amount")
            or requested
        )
        return BillPurchaseResponse(
            request_id=request_id, transaction_id=tx_id,
            status=BillDeliveryStatus.delivered, code=code,
            requested_amount_ngn=requested,
            delivered_amount_ngn=delivered_amt,
            description=description,
            raw=body,
        )
    if code == _PENDING_CODE:
        return BillPurchaseResponse(
            request_id=request_id, transaction_id=tx_id,
            status=BillDeliveryStatus.pending, code=code,
            requested_amount_ngn=requested,
            delivered_amount_ngn=Decimal("0.00"),
            description=description or "Pending upstream confirmation",
            raw=body,
        )
    # Anything else is a failure. Log the code for ops triage.
    log.warning(
        "vtpass: purchase failed request_id=%s code=%s description=%s",
        request_id, code, description,
    )
    return BillPurchaseResponse(
        request_id=request_id, transaction_id=tx_id,
        status=BillDeliveryStatus.failed, code=code,
        requested_amount_ngn=requested,
        delivered_amount_ngn=Decimal("0.00"),
        description=description or "Transaction failed",
        raw=body,
    )


class VTPassClient(BillProvider):
    """Production client. Constructed only when VTPass credentials are set
    AND the env is not in the fake allowlist (factory.py enforces that)."""

    def __init__(self) -> None:
        if not settings.VTPASS_SECRET_KEY or not settings.VTPASS_API_KEY:
            raise RuntimeError(
                "VTPASS_SECRET_KEY + VTPASS_API_KEY must be set for real client"
            )
        self._api_key = settings.VTPASS_API_KEY
        self._secret = settings.VTPASS_SECRET_KEY
        self._public = settings.VTPASS_PUBLIC_KEY or ""
        self._base = settings.VTPASS_BASE_URL.rstrip("/")

    # ── Public API ──────────────────────────────────────────────────────

    async def purchase_airtime(
        self, *, request_id: str, service_id: str, phone: str, amount_ngn: Decimal
    ) -> BillPurchaseResponse:
        body = await self._post_pay({
            "request_id":  request_id,
            "serviceID":   service_id,
            "billersCode": phone,
            "amount":      str(int(amount_ngn)),  # VTPass wants a whole number
            "phone":       phone,
        })
        return translate_response(body, request_id=request_id, requested=amount_ngn)

    async def purchase_data(
        self, *, request_id: str, service_id: str, phone: str, variation_code: str
    ) -> BillPurchaseResponse:
        # Resolve the price from the catalog — never trust a client amount.
        plans = await self.list_data_plans(service_id=service_id)
        match = next(
            (v for v in plans.variations if v.variation_code == variation_code),
            None,
        )
        if match is None:
            raise ProviderPermanentFailure(
                f"Unknown data plan variation_code={variation_code!r} for service_id={service_id!r}"
            )
        body = await self._post_pay({
            "request_id":    request_id,
            "serviceID":     service_id,
            "billersCode":   phone,
            "variation_code": variation_code,
            "phone":         phone,
        })
        return translate_response(body, request_id=request_id, requested=match.price_ngn)

    async def list_data_plans(self, *, service_id: str) -> DataPlanList:
        body = await self._get("/api/service-variations", {"serviceID": service_id})
        content = body.get("content") or {}
        raw_vars = content.get("variations") or []
        variations: list[DataPlanVariation] = []
        for v in raw_vars:
            price = _safe_decimal(v.get("variation_amount", "0"))
            variations.append(DataPlanVariation(
                variation_code=str(v.get("variation_code", "")),
                name=str(v.get("name", "")),
                price_ngn=price,
                validity=v.get("validity") or None,
            ))
        return DataPlanList(service_id=service_id, variations=variations)

    # ── Electricity ────────────────────────────────────────────────────

    async def validate_meter(
        self,
        *,
        request_id: str,
        service_id: str,
        meter_number: str,
        meter_type: str,
    ) -> MeterValidation:
        """Look up a meter's registered customer name + address on the
        DisCo before letting the user top up. VTPass `merchant-verify`
        is an authenticated POST (secret-key header). A non-000 `code`
        means the meter is invalid on this DisCo — that's deterministic
        (the number either exists or doesn't), so we raise
        `ProviderPermanentFailure` rather than a temporary failure."""
        body = await self._post(
            "/api/merchant-verify",
            {
                "billersCode": meter_number,
                "serviceID":   service_id,
                "type":        meter_type,  # "prepaid" or "postpaid"
            },
            use_secret_key=True,
        )
        code = str(body.get("code", ""))
        if code != _SUCCESS_CODE:
            desc = str(body.get("response_description", "")) or "invalid meter"
            raise ProviderPermanentFailure(
                f"vtpass validate_meter {service_id}/{meter_number}: "
                f"code={code} {desc}"
            )
        content = body.get("content") or {}
        # VTPass echoes back the meter number under `Meter_Number` — but
        # we trust the user's input over the provider's echo so the
        # response round-trips cleanly even if VTPass normalizes (strips
        # leading zeros etc.).
        return MeterValidation(
            service_id=service_id,
            meter_number=meter_number,
            customer_name=str(content.get("Customer_Name", "")),
            address=str(content.get("Address", "")),
            meter_type=meter_type,
        )

    async def purchase_electricity(
        self,
        *,
        request_id: str,
        service_id: str,
        meter_number: str,
        meter_type: str,
        amount_ngn: Decimal,
        phone: str | None = None,
    ) -> BillPurchaseResponse:
        """Top up a prepaid meter or settle a postpaid bill.

        VTPass uses `variation_code` on /api/pay to carry the
        prepaid/postpaid classification for electricity (not a separate
        endpoint). `phone` is a REQUIRED field on VTPass's side — it
        drives their SMS notification which we disable but the field
        must still be present. BillService (B5) passes the authenticated
        user's phone; we fall back to an empty string here if it's None
        so the wire payload is always well-formed.
        """
        body = await self._post_pay({
            "request_id":     request_id,
            "serviceID":      service_id,
            "billersCode":    meter_number,
            "variation_code": meter_type,  # "prepaid" | "postpaid"
            "amount":         str(int(amount_ngn)),  # VTPass: whole NGN
            "phone":          phone or "",
        })
        response = translate_response(
            body, request_id=request_id, requested=amount_ngn
        )
        # Surface token + kWh units to the caller's `raw` on success.
        # VTPass lands them under content.transactions.{token, units} on
        # prepaid purchases; absent on postpaid. BillService persists the
        # raw dict so the UI (and a later receipt regeneration) can pull
        # them back out without us growing the typed schema.
        if response.status == BillDeliveryStatus.delivered:
            content = body.get("content") or {}
            tx = content.get("transactions") or {}
            token = tx.get("token")
            units = tx.get("units")
            if token is not None or units is not None:
                merged_raw = dict(response.raw)
                if token is not None:
                    merged_raw["token"] = token
                if units is not None:
                    merged_raw["units"] = units
                response = response.model_copy(update={"raw": merged_raw})
        return response

    # ── Cable TV ───────────────────────────────────────────────────────

    async def validate_smartcard(
        self,
        *,
        request_id: str,
        service_id: str,
        smartcard_number: str,
    ) -> SmartcardValidation:
        """Look up a cable smartcard's subscriber + current bouquet
        before charging. Unlike meter validation there's no prepaid/
        postpaid distinction for cable, so no `type` field on the wire.
        Non-000 code → permanent failure (the smartcard number is
        deterministically invalid on this provider)."""
        body = await self._post(
            "/api/merchant-verify",
            {
                "billersCode": smartcard_number,
                "serviceID":   service_id,
            },
            use_secret_key=True,
        )
        code = str(body.get("code", ""))
        if code != _SUCCESS_CODE:
            desc = str(body.get("response_description", "")) or "invalid smartcard"
            raise ProviderPermanentFailure(
                f"vtpass validate_smartcard {service_id}/{smartcard_number}: "
                f"code={code} {desc}"
            )
        content = body.get("content") or {}
        # Status is surfaced as a string — "active" / "Inactive" /
        # "suspended" etc. — we preserve it verbatim so BillService can
        # expose the raw label to the UI without us having to maintain
        # a normalization map. Lowercase for internal consumers.
        return SmartcardValidation(
            service_id=service_id,
            smartcard_number=smartcard_number,
            customer_name=str(content.get("Customer_Name", "")),
            current_plan_name=str(content.get("Current_Bouquet", "")),
            current_plan_code=str(content.get("Current_Bouquet_Code", "")),
            status=str(content.get("Status", "")).lower() or "unknown",
            renewal_amount_ngn=_safe_decimal(content.get("Renewal_Amount", "0")),
        )

    async def list_cable_plans(self, *, service_id: str) -> CablePlanList:
        """Fetch the bouquet catalogue for a cable provider. VTPass
        lists all variations under the base service_id — the `-change`
        suffix is only used at purchase time (switch vs renew), never
        at listing time."""
        body = await self._get(
            "/api/service-variations", {"serviceID": service_id}
        )
        content = body.get("content") or {}
        raw_vars = content.get("variations") or []
        variations: list[CablePlanVariation] = []
        for v in raw_vars:
            price = _safe_decimal(v.get("variation_amount", "0"))
            variations.append(CablePlanVariation(
                variation_code=str(v.get("variation_code", "")),
                name=str(v.get("name", "")),
                price_ngn=price,
                validity=v.get("validity") or None,
            ))
        return CablePlanList(service_id=service_id, variations=variations)

    async def purchase_cable(
        self,
        *,
        request_id: str,
        service_id: str,
        smartcard_number: str,
        variation_code: str,
        amount_ngn: Decimal,
    ) -> BillPurchaseResponse:
        """Renew or switch a cable subscription. Caller controls the
        renew-vs-switch mode by choosing `service_id`: pass the bare
        provider slug (`dstv`) to renew the current bouquet, or the
        `-change` variant (`dstv-change`) to switch to `variation_code`.
        That decision lives in BillService (B8) — the client just
        forwards whatever service_id it's given."""
        body = await self._post_pay({
            "request_id":     request_id,
            "serviceID":      service_id,
            "billersCode":    smartcard_number,
            "variation_code": variation_code,
            "amount":         str(int(amount_ngn)),
        })
        return translate_response(
            body, request_id=request_id, requested=amount_ngn
        )

    async def requery(self, *, request_id: str) -> BillPurchaseResponse:
        body = await self._post(
            "/api/requery",
            {"request_id": request_id},
            use_secret_key=True,
        )
        # Requery doesn't know the originally-requested amount — we surface
        # whatever VTPass reports. BillService knows the expected amount
        # from its own Transaction row.
        requested = _safe_decimal(body.get("amount", "0"))
        return translate_response(body, request_id=request_id, requested=requested)

    # ── Internals ──────────────────────────────────────────────────────

    @retry(
        reraise=True,
        retry=retry_if_exception_type(ProviderTemporaryFailure),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
    )
    async def _post_pay(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/api/pay", payload, use_secret_key=True)

    async def _post(
        self, path: str, payload: dict[str, Any], *, use_secret_key: bool
    ) -> dict[str, Any]:
        headers = {
            "api-key": self._api_key,
            "secret-key" if use_secret_key else "public-key": (
                self._secret if use_secret_key else self._public
            ),
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(f"{self._base}{path}", json=payload, headers=headers)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.NetworkError) as exc:
            raise ProviderTemporaryFailure(f"vtpass network error: {exc}") from exc
        return self._handle_response(r)

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "api-key": self._api_key,
            "public-key": self._public,
        }
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(f"{self._base}{path}", params=params, headers=headers)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.NetworkError) as exc:
            raise ProviderTemporaryFailure(f"vtpass network error: {exc}") from exc
        return self._handle_response(r)

    @staticmethod
    def _handle_response(r: httpx.Response) -> dict[str, Any]:
        if r.status_code >= 500:
            raise ProviderTemporaryFailure(
                f"vtpass {r.status_code}: {r.text[:200]}"
            )
        if r.status_code >= 400:
            raise ProviderPermanentFailure(
                f"vtpass {r.status_code}: {r.text[:200]}"
            )
        try:
            return r.json()
        except ValueError as exc:
            raise ProviderPermanentFailure(
                f"vtpass returned non-JSON body: {r.text[:200]}"
            ) from exc

def _safe_decimal(v: Any) -> Decimal:
    """Coerce VTPass amount values (sometimes str, sometimes int/float) to
    a Decimal. On failure returns 0 rather than raising — the caller
    treats zero-delivery as a failed transaction, which is the safe
    interpretation of unparseable amounts."""
    if v is None:
        return Decimal("0.00")
    try:
        return Decimal(str(v)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return Decimal("0.00")
