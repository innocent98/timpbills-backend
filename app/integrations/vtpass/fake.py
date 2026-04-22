"""In-memory FakeVTPassClient — deterministic, stateful for the lifetime
of a singleton. Test hooks mirror the Paystack fake's `will_succeed` /
`will_fail` convention. Seeds a default plan catalog so most tests don't
need to stub plans manually.

Sprint 3 usage: the factory returns this in dev/test environments; real
VTPass is only hit when VTPASS_SECRET_KEY is set AND the env is not in
the fake allowlist (matching Paystack's S2C-6 pattern).

Sprint 4 extension: electricity + cable. Same `_execute` pattern —
`purchase_electricity` / `purchase_cable` both reuse `_build_response` so
all four outcomes (success/partial/pending/failed) pick up consistently.
`validate_meter` / `validate_smartcard` raise `ProviderPermanentFailure`
when the matching `will_reject_*` hook is set; no new exception type —
the validation vs purchase distinction is carried by the method return
type, not the exception."""
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from app.integrations.vtpass.base import BillProvider, ProviderPermanentFailure
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

    # Sprint 4 — (service_id, meter_number) tuples flagged for failure by
    # `will_reject_meter`. Kept as a set rather than a dict because the
    # hook is boolean: it's either poisoned or it isn't.
    _rejected_meters: set[tuple[str, str]] = field(default_factory=set)

    # Sprint 4 — (service_id, smartcard_number) tuples flagged by
    # `will_reject_smartcard`. Same rationale as `_rejected_meters`.
    _rejected_smartcards: set[tuple[str, str]] = field(default_factory=set)

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

    def will_reject_meter(self, service_id: str, meter_number: str) -> None:
        """Force `validate_meter` to raise ProviderPermanentFailure for
        this (service_id, meter_number) pair. Sprint 4 — B2."""
        self._rejected_meters.add((service_id, meter_number))

    def will_reject_smartcard(
        self, service_id: str, smartcard_number: str
    ) -> None:
        """Force `validate_smartcard` to raise ProviderPermanentFailure
        for this (service_id, smartcard_number) pair. Sprint 4 — B2."""
        self._rejected_smartcards.add((service_id, smartcard_number))

    # ── Protocol methods: airtime + data ───────────────────────────────

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

    # ── Protocol methods: electricity ──────────────────────────────────

    async def validate_meter(
        self,
        *,
        request_id: str,
        service_id: str,
        meter_number: str,
        meter_type: str,
    ) -> MeterValidation:
        if (service_id, meter_number) in self._rejected_meters:
            # Per B2 plan: reuse ProviderPermanentFailure rather than
            # introduce an InvalidMeter class — the method return type
            # already distinguishes validation from purchase paths.
            raise ProviderPermanentFailure(
                f"invalid meter {meter_number} on {service_id}"
            )
        disco_name = _DEFAULT_DISCOS.get(service_id, service_id.title())
        # Deterministic fabricated customer — derived from the meter
        # number so tests can assert equality across repeated calls
        # without tracking state on the client.
        return MeterValidation(
            service_id=service_id,
            meter_number=meter_number,
            customer_name=f"FAKE CUSTOMER {meter_number[-4:]}",
            address=f"{disco_name} Service Area, Lagos",
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
        # `phone` is a Protocol addition in B3 for the real VTPass client
        # (VTPass requires it in /api/pay for electricity). The fake
        # ignores it — we still record on the (request_id → amount) map
        # and don't fabricate any SMS side-effect.
        _ = phone
        self._requested_amounts[request_id] = amount_ngn
        self._transaction_ids[request_id] = f"vtp_{request_id[:12]}"
        response = self._build_response(request_id, amount_ngn)
        # On successful outcomes (delivered — full or partial), VTPass
        # returns a meter token + kWh units. We mirror that shape in
        # raw so downstream tests can assert against it.
        if response.status == BillDeliveryStatus.delivered:
            token = _fake_token(request_id)
            units = _fake_units(response.delivered_amount_ngn)
            response = response.model_copy(
                update={"raw": {**response.raw, "token": token, "units": units}}
            )
        return response

    # ── Protocol methods: cable TV ─────────────────────────────────────

    async def validate_smartcard(
        self,
        *,
        request_id: str,
        service_id: str,
        smartcard_number: str,
    ) -> SmartcardValidation:
        if (service_id, smartcard_number) in self._rejected_smartcards:
            raise ProviderPermanentFailure(
                f"invalid smartcard {smartcard_number} on {service_id}"
            )
        return SmartcardValidation(
            service_id=service_id,
            smartcard_number=smartcard_number,
            customer_name=f"FAKE SUBSCRIBER {smartcard_number[-4:]}",
            current_plan_name="Fake Compact Plan",
            current_plan_code="fake-compact",
            status="active",
            renewal_amount_ngn=Decimal("5000.00"),
        )

    async def list_cable_plans(self, *, service_id: str) -> CablePlanList:
        return CablePlanList(
            service_id=service_id,
            variations=_DEFAULT_CABLE_PLANS.get(service_id, []),
        )

    async def purchase_cable(
        self,
        *,
        request_id: str,
        service_id: str,
        smartcard_number: str,
        variation_code: str,
        amount_ngn: Decimal,
    ) -> BillPurchaseResponse:
        # Same safety pattern as purchase_data: look up the variation in
        # the seeded catalog so a client-spoofed code can't succeed.
        plans = await self.list_cable_plans(service_id=service_id)
        match = next(
            (v for v in plans.variations if v.variation_code == variation_code),
            None,
        )
        if match is None:
            self.will_fail(request_id)
            return self._build_response(request_id, Decimal("0.00"))
        self._requested_amounts[request_id] = match.price_ngn
        self._transaction_ids[request_id] = f"vtp_{request_id[:12]}"
        return self._build_response(request_id, match.price_ngn)

    # ── Status requery ─────────────────────────────────────────────────

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


# ── Deterministic helpers for electricity response fields ──────────────

def _fake_token(request_id: str) -> str:
    """20-digit meter token derived from the request_id. Deterministic
    (same request_id → same token) so requery-style tests stay stable
    without us having to persist the token."""
    # Strip non-digits, then pad/truncate to 20. `TMP-YYMMDD-…` contains
    # both digits and the stable TMP prefix counter — enough entropy
    # for test uniqueness; real VTPass tokens come from the DisCo.
    digits = "".join(c for c in request_id if c.isdigit())
    # Salt with a fixed prefix so short request_ids still produce 20 chars.
    padded = (digits + "00000000000000000000")[:20]
    # If request_id has no digits at all, padded is all zeros — still
    # valid for the "20 digit string" contract.
    return padded


def _fake_units(amount_ngn: Decimal) -> str:
    """kWh units as a 2-dp decimal string. ₦40/kWh is a typical
    prepaid tariff on Nigerian DisCos — close enough for tests."""
    units = (amount_ngn / Decimal("40")).quantize(Decimal("0.01"))
    return str(units)


# ── Seed catalogs ──────────────────────────────────────────────────────
#
# The fake's catalogs are intentionally illustrative, not exhaustive —
# they cover the provider IDs the real VTPass sandbox uses, enough to
# exercise the catalog-lookup + variation-code paths in BillService
# without pretending to stay in sync with VTPass's live catalog.

# Data plans (Sprint 3 — pre-existing).
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


# Sprint 4 — DisCo directory. Value is the display name used by the
# fake's fabricated address field. No price column because electricity
# is user-amount, not catalog-priced (cf. airtime).
_DEFAULT_DISCOS: dict[str, str] = {
    "ikeja-electric":       "Ikeja Electric",
    "eko-electric":         "Eko Electric",
    "abuja-electric":       "Abuja Electric",
    "ibadan-electric":      "Ibadan Electric",
    "enugu-electric":       "Enugu Electric",
    "portharcourt-electric": "Port Harcourt Electric",
    "kaduna-electric":      "Kaduna Electric",
    "jos-electric":         "Jos Electric",
    "kano-electric":        "Kano Electric",
    "benin-electric":       "Benin Electric",
}


# Sprint 4 — cable bouquet catalog. Prices are indicative of mid-2025
# Nigerian rates; tests only assert shape + specific known names, so
# drift here is OK as long as the name columns match.
_DEFAULT_CABLE_PLANS: dict[str, list[CablePlanVariation]] = {
    "dstv": [
        CablePlanVariation(variation_code="dstv-compact",
                           name="Compact",
                           price_ngn=Decimal("15500.00"),
                           validity="1 month"),
        CablePlanVariation(variation_code="dstv-compact-plus",
                           name="Compact Plus",
                           price_ngn=Decimal("25000.00"),
                           validity="1 month"),
        CablePlanVariation(variation_code="dstv-premium",
                           name="Premium",
                           price_ngn=Decimal("44500.00"),
                           validity="1 month"),
        CablePlanVariation(variation_code="dstv-access",
                           name="Access",
                           price_ngn=Decimal("9000.00"),
                           validity="1 month"),
    ],
    "gotv": [
        CablePlanVariation(variation_code="gotv-jinja",
                           name="Jinja",
                           price_ngn=Decimal("3900.00"),
                           validity="1 month"),
        CablePlanVariation(variation_code="gotv-jolli",
                           name="Jolli",
                           price_ngn=Decimal("5700.00"),
                           validity="1 month"),
        CablePlanVariation(variation_code="gotv-max",
                           name="Max",
                           price_ngn=Decimal("8500.00"),
                           validity="1 month"),
        CablePlanVariation(variation_code="gotv-supa",
                           name="Supa",
                           price_ngn=Decimal("11400.00"),
                           validity="1 month"),
    ],
    "startimes": [
        CablePlanVariation(variation_code="startimes-nova-weekly",
                           name="Nova Weekly",
                           price_ngn=Decimal("600.00"),
                           validity="1 week"),
        CablePlanVariation(variation_code="startimes-basic-monthly",
                           name="Basic Monthly",
                           price_ngn=Decimal("2600.00"),
                           validity="1 month"),
        CablePlanVariation(variation_code="startimes-smart-monthly",
                           name="Smart Monthly",
                           price_ngn=Decimal("3800.00"),
                           validity="1 month"),
        CablePlanVariation(variation_code="startimes-classic-monthly",
                           name="Classic Monthly",
                           price_ngn=Decimal("3000.00"),
                           validity="1 month"),
    ],
    "showmax": [
        CablePlanVariation(variation_code="showmax-entertainment",
                           name="Entertainment",
                           price_ngn=Decimal("3500.00"),
                           validity="1 month"),
        CablePlanVariation(variation_code="showmax-pro",
                           name="Pro",
                           price_ngn=Decimal("6300.00"),
                           validity="1 month"),
    ],
}
