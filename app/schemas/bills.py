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
    meter_number: str
    meter_type: Literal["prepaid", "postpaid"]


class MeterValidationResponse(BaseModel):
    """Mirrors ``app.integrations.vtpass.schemas.MeterValidation`` — the
    API surface stays decoupled from the integration schema so an
    upstream shape change doesn't leak into the client contract."""
    service_id: str
    meter_number: str
    customer_name: str
    address: str
    meter_type: str


# ── Networks catalog (for GET /bills/airtime/networks) ─────────────────


class NetworkView(BaseModel):
    id: str                          # "mtn"
    name: str                        # "MTN"
    prefixes: list[str]              # ["0803", "0806", ...]


class NetworkListResponse(BaseModel):
    networks: list[NetworkView]
