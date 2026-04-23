"""Request/response schemas for /bills/*."""
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


# Canonical NG network identifiers. Accepted both upper and lower cased;
# BillService normalizes via `.lower()` for VTPass service IDs.
Network = Literal["MTN", "AIRTEL", "GLO", "ETISALAT", "mtn", "airtel", "glo", "etisalat"]


# ── Airtime ─────────────────────────────────────────────────────────────


class AirtimePurchaseRequest(BaseModel):
    network: Network
    phone: str = Field(min_length=11, max_length=14)
    # Airtime amounts are whole naira; enforce server-side to avoid float
    # edges from VTPass.
    amount: Decimal = Field(gt=Decimal("0"))


class AirtimePurchaseResponse(BaseModel):
    reference: str
    status: str                      # "success" | "failed" | "processing"
    delivered_amount: Decimal
    requested_amount: Decimal
    partial: bool = False            # true when delivered < requested


# ── Data ────────────────────────────────────────────────────────────────


class DataPurchaseRequest(BaseModel):
    network: Network
    phone: str = Field(min_length=11, max_length=14)
    variation_code: str              # opaque VTPass id


class DataPurchaseResponse(BaseModel):
    reference: str
    status: str
    plan_name: str
    price: Decimal


class DataPlanView(BaseModel):
    variation_code: str
    name: str
    price: Decimal
    validity: str | None = None


class DataPlanListResponse(BaseModel):
    service_id: str
    plans: list[DataPlanView]


# ── Electricity ─────────────────────────────────────────────────────────


class MeterValidationRequest(BaseModel):
    service_id: str                       # DisCo slug, e.g. "ikeja-electric"
    # Real NG meter numbers are 11–13 digits; bound keeps fuzz payloads
    # out of the Redis cache key and the downstream VTPass call.
    meter_number: str = Field(min_length=1, max_length=20)
    meter_type: Literal["prepaid", "postpaid"]


class MeterValidationResponse(BaseModel):
    """Mirrors ``app.integrations.vtpass.schemas.MeterValidation`` — the
    API surface stays decoupled from the integration schema so an
    upstream shape change doesn't leak into the client contract."""
    service_id: str
    meter_number: str
    customer_name: str
    address: str
    meter_type: Literal["prepaid", "postpaid"]


class ElectricityPurchaseRequest(BaseModel):
    service_id: str = Field(min_length=1)             # DisCo slug, e.g. "ikeja-electric"
    meter_number: str = Field(min_length=1, max_length=20)
    meter_type: Literal["prepaid", "postpaid"]
    # Sprint 4 B26: `phone` was removed from the request body — the
    # VTPass wire still needs a contact number (DisCos use it for SMS-
    # resend of lost tokens), but that concern belongs to the backend
    # adapter, not the client. The endpoint now auto-injects `user.phone`
    # from the authenticated profile when calling BillService, so the
    # mobile UI doesn't ask the user to type the same number they
    # already verified at KYC-1. If VTPass ever requires a distinct
    # recipient phone for electricity (they don't today), that would
    # be a deliberate re-addition here.
    amount: Decimal = Field(gt=Decimal("0"))


class ElectricityPurchaseResponse(BaseModel):
    reference: str
    status: str                              # value of TransactionStatus
    service_id: str
    meter_number: str
    amount: Decimal
    # Populated from tx.meta["token"] / tx.meta["units"] on delivered
    # responses; None on failed or pending. A pending tx gets populated
    # by the reconcile worker if the upstream DisCo later lands it.
    token: str | None = None
    units: str | None = None


# ── Cable ────────────────────────────────────────────────────────────────


class CableProviderView(BaseModel):
    id: str                                  # "dstv"
    name: str                                # "DStv"


class CableProviderListResponse(BaseModel):
    providers: list[CableProviderView]


class SmartcardValidationRequest(BaseModel):
    service_id: str = Field(min_length=1)    # cable slug, e.g. "dstv"
    smartcard_number: str = Field(min_length=1, max_length=20)


class SmartcardValidationResponse(BaseModel):
    """Mirrors ``app.integrations.vtpass.schemas.SmartcardValidation``."""
    service_id: str
    smartcard_number: str
    customer_name: str
    current_plan_name: str                   # empty on fresh/inactive cards
    current_plan_code: str                   # empty on fresh/inactive cards
    status: str                              # "active" | "inactive" | …
    renewal_amount: Decimal                  # 0 on inactive cards


class CablePlanView(BaseModel):
    variation_code: str
    name: str
    price: Decimal
    validity: str | None = None


class CablePlanListResponse(BaseModel):
    service_id: str
    plans: list[CablePlanView]


class CablePurchaseRequest(BaseModel):
    service_id: str = Field(min_length=1)              # "dstv" etc. — base slug (no -change suffix)
    smartcard_number: str = Field(min_length=1, max_length=20)
    mode: Literal["renew", "change"]
    # Required only for mode=change; BillService enforces. Optional at
    # the schema layer so a renew request can omit it cleanly.
    variation_code: str | None = None


class CablePurchaseResponse(BaseModel):
    reference: str
    status: str
    service_id: str
    smartcard_number: str
    mode: str                                          # "renew" | "change"
    plan_code: str
    plan_name: str
    amount: Decimal


# ── Networks catalog (for GET /bills/airtime/networks) ─────────────────


class NetworkView(BaseModel):
    id: str                          # "mtn"
    name: str                        # "MTN"
    prefixes: list[str]              # ["0803", "0806", ...]


class NetworkListResponse(BaseModel):
    networks: list[NetworkView]
