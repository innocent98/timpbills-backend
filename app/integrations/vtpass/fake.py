"""In-memory FakeVTPassClient — deterministic, stateful for the lifetime
of a singleton. Test hooks mirror the Paystack fake's `will_succeed` /
`will_fail` convention. Seeds a default plan catalog so most tests don't
need to stub plans manually.

Sprint 3 usage: the factory returns this in dev/test environments; real
VTPass is only hit when VTPASS_SECRET_KEY is set AND the env is not in
the fake allowlist (matching Paystack's S2C-6 pattern)."""
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from app.integrations.vtpass.base import BillProvider
from app.integrations.vtpass.schemas import (
    BillDeliveryStatus,
    BillPurchaseResponse,
    DataPlanList,
    DataPlanVariation,
)


_Outcome = Literal["success", "failed", "pending", "partial"]


@dataclass
class FakeVTPassClient(BillProvider):
    # request_id → outcome. Default outcome for unseeded refs is "success"
    # so the happy-path test for a feature doesn't need setup noise.
    _outcomes: dict[str, _Outcome] = field(default_factory=dict)

    # request_id → (delivered_ngn override) — only meaningful for "partial"
    # outcomes. If absent on a partial outcome, delivered = requested - 5.
    _partial_delivered: dict[str, Decimal] = field(default_factory=dict)

    # Mirrors VTPass: our request_id → its internal id. Populated on every
    # successful purchase so requery returns stable ids across calls.
    _transaction_ids: dict[str, str] = field(default_factory=dict)

    # What was requested — needed so requery returns the right amount on
    # subsequent lookups.
    _requested_amounts: dict[str, Decimal] = field(default_factory=dict)

    # ── Test hooks ──────────────────────────────────────────────────────

    def will_succeed(self, request_id: str) -> None:
        self._outcomes[request_id] = "success"

    def will_fail(self, request_id: str) -> None:
        self._outcomes[request_id] = "failed"

    def will_remain_pending(self, request_id: str) -> None:
        """Provider accepts the request but the upstream telco/DisCo
        hasn't confirmed yet — reconcile worker has to requery."""
        self._outcomes[request_id] = "pending"

    def will_partial(
        self, request_id: str, delivered_ngn: Decimal | None = None
    ) -> None:
        """Upstream delivered less than requested. If no explicit amount
        is given, the fake delivers `requested - 5.00` — enough to trigger
        the partial banner without caring about the exact value in tests."""
        self._outcomes[request_id] = "partial"
        if delivered_ngn is not None:
            self._partial_delivered[request_id] = delivered_ngn

    # ── Protocol methods ───────────────────────────────────────────────

    async def purchase_airtime(
        self,
        *,
        request_id: str,
        service_id: str,
        phone: str,
        amount_ngn: Decimal,
    ) -> BillPurchaseResponse:
        self._requested_amounts[request_id] = amount_ngn
        self._transaction_ids[request_id] = f"vtp_{request_id[:12]}"
        return self._build_response(request_id, amount_ngn)

    async def purchase_data(
        self,
        *,
        request_id: str,
        service_id: str,
        phone: str,
        variation_code: str,
    ) -> BillPurchaseResponse:
        # Price comes from the fake's plan catalog — keeping parity with
        # real VTPass which derives it server-side.
        plans = await self.list_data_plans(service_id=service_id)
        match = next(
            (v for v in plans.variations if v.variation_code == variation_code),
            None,
        )
        if match is None:
            # Surface as a permanent failure to force a refund path test.
            self.will_fail(request_id)
            return self._build_response(request_id, Decimal("0.00"))
        self._requested_amounts[request_id] = match.price_ngn
        self._transaction_ids[request_id] = f"vtp_{request_id[:12]}"
        return self._build_response(request_id, match.price_ngn)

    async def list_data_plans(self, *, service_id: str) -> DataPlanList:
        return DataPlanList(
            service_id=service_id,
            variations=_DEFAULT_PLANS.get(service_id, []),
        )

    async def requery(self, *, request_id: str) -> BillPurchaseResponse:
        amount = self._requested_amounts.get(request_id, Decimal("0.00"))
        return self._build_response(request_id, amount)

    # ── Internals ──────────────────────────────────────────────────────

    def _build_response(
        self, request_id: str, requested: Decimal
    ) -> BillPurchaseResponse:
        outcome = self._outcomes.get(request_id, "success")

        if outcome == "success":
            return BillPurchaseResponse(
                request_id=request_id,
                transaction_id=self._transaction_ids.get(request_id, ""),
                status=BillDeliveryStatus.delivered,
                code="000",
                requested_amount_ngn=requested,
                delivered_amount_ngn=requested,
                description="TRANSACTION SUCCESSFUL",
                raw={"code": "000", "status": "delivered", "fake": True},
            )
        if outcome == "partial":
            delivered = self._partial_delivered.get(
                request_id, max(requested - Decimal("5.00"), Decimal("0.00"))
            )
            return BillPurchaseResponse(
                request_id=request_id,
                transaction_id=self._transaction_ids.get(request_id, ""),
                status=BillDeliveryStatus.delivered,
                code="000",
                requested_amount_ngn=requested,
                delivered_amount_ngn=delivered,
                description="PARTIAL DELIVERY",
                raw={"code": "000", "status": "delivered", "partial": True, "fake": True},
            )
        if outcome == "pending":
            return BillPurchaseResponse(
                request_id=request_id,
                transaction_id=self._transaction_ids.get(request_id, ""),
                status=BillDeliveryStatus.pending,
                code="099",
                requested_amount_ngn=requested,
                delivered_amount_ngn=Decimal("0.00"),
                description="PENDING",
                raw={"code": "099", "status": "pending", "fake": True},
            )
        # failed
        return BillPurchaseResponse(
            request_id=request_id,
            transaction_id=self._transaction_ids.get(request_id, ""),
            status=BillDeliveryStatus.failed,
            code="016",
            requested_amount_ngn=requested,
            delivered_amount_ngn=Decimal("0.00"),
            description="TRANSACTION FAILED",
            raw={"code": "016", "status": "failed", "fake": True},
        )


# Seed data for `list_data_plans` — covers the four major networks.
# Prices are illustrative; real VTPass catalog has ~30 plans per network
# and changes frequently.
_DEFAULT_PLANS: dict[str, list[DataPlanVariation]] = {
    "mtn-data": [
        DataPlanVariation(variation_code="mtn-100mb-daily",
                          name="100MB - 1 day",
                          price_ngn=Decimal("100.00"),
                          validity="1 day"),
        DataPlanVariation(variation_code="mtn-1gb-monthly",
                          name="1GB - 30 days",
                          price_ngn=Decimal("1000.00"),
                          validity="30 days"),
        DataPlanVariation(variation_code="mtn-3gb-monthly",
                          name="3GB - 30 days",
                          price_ngn=Decimal("2500.00"),
                          validity="30 days"),
    ],
    "airtel-data": [
        DataPlanVariation(variation_code="airtel-200mb-daily",
                          name="200MB - 1 day",
                          price_ngn=Decimal("200.00"),
                          validity="1 day"),
        DataPlanVariation(variation_code="airtel-2gb-monthly",
                          name="2GB - 30 days",
                          price_ngn=Decimal("1500.00"),
                          validity="30 days"),
    ],
    "glo-data": [
        DataPlanVariation(variation_code="glo-750mb-weekly",
                          name="750MB - 7 days",
                          price_ngn=Decimal("500.00"),
                          validity="7 days"),
    ],
    "etisalat-data": [
        DataPlanVariation(variation_code="9mobile-500mb-monthly",
                          name="500MB - 30 days",
                          price_ngn=Decimal("500.00"),
                          validity="30 days"),
    ],
}
