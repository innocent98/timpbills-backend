"""VTPass response schemas — the typed shape our code sees after the
HTTP boundary. We do NOT reflect VTPass's raw envelope verbatim; we
normalize to a clean domain type so downstream (BillService,
tests, workers) doesn't have to know about their quirks."""
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class BillDeliveryStatus(str, Enum):
    """Normalized delivery outcome. Maps from VTPass `content.transactions.status`
    (`delivered` / `failed` / `pending`) and the top-level `code` field.

    `pending` means VTPass accepted the request but the upstream telco/DisCo
    hasn't confirmed yet — the reconcile worker will requery.
    """
    delivered = "delivered"
    failed    = "failed"
    pending   = "pending"


class BillPurchaseResponse(BaseModel):
    """Normalized purchase response. Produced by both the real
    `VTPassClient.purchase_*` methods and `FakeVTPassClient`.

    Partial delivery (`delivered_amount_ngn < requested_amount_ngn`) is
    represented by status=`delivered` with the two amount fields diverging.
    `BillService` interprets that and creates a refund for the difference.
    """
    model_config = ConfigDict(frozen=True)

    # Our transaction reference (passed as `request_id` to VTPass).
    request_id: str

    # VTPass's own internal id for this transaction. Populated on success;
    # empty string when VTPass rejected before assigning one.
    transaction_id: str = ""

    # Normalized outcome — see BillDeliveryStatus above.
    status: BillDeliveryStatus

    # Top-level VTPass `code`. "000" is success; other codes are per-
    # provider failure reasons. Retained for audit / ops triage.
    code: str

    # What we asked to deliver.
    requested_amount_ngn: Decimal = Field(ge=0)

    # What actually reached the subscriber (airtime wallet or data allocation).
    # Equal to requested on a full success; less on a partial; zero on a
    # hard failure (VTPass returns the amount as 0.00 in that case).
    # ge=0 bound prevents a malformed provider response from producing a
    # negative shortfall (which would refund more than the original debit).
    # S3C-M5.
    delivered_amount_ngn: Decimal = Field(ge=0)

    # Human-readable description from VTPass, e.g. "TRANSACTION SUCCESSFUL".
    description: str = ""

    # Full VTPass response body — persisted on the Transaction row for
    # audit (we never trust it for business logic; use the typed fields
    # above).
    raw: dict


class DataPlanVariation(BaseModel):
    """One row in the VTPass data-plan catalog. Price is authoritative:
    BillService looks this up at purchase time rather than trusting a
    client-supplied price (a classic price-spoof prevention)."""
    model_config = ConfigDict(frozen=True)

    variation_code: str      # "mtn-10mb-100" etc. — opaque id VTPass expects back
    name: str                # "100MB - Daily"
    price_ngn: Decimal
    validity: str | None = None   # e.g. "1 day" (not always present in response)


class DataPlanList(BaseModel):
    """Complete plan catalog for one service (e.g. mtn-data)."""
    model_config = ConfigDict(frozen=True)

    service_id: str                        # "mtn-data"
    variations: list[DataPlanVariation]


# ── Sprint 4: electricity + cable ───────────────────────────────────────

class MeterValidation(BaseModel):
    """Result of a meter-number lookup against a DisCo (Ikeja, EKEDC, …).

    The real VTPass `merchant-verify` endpoint returns the customer's
    registered name + address so the UI can confirm "are you topping up
    the right meter?" before we touch the wallet. `meter_type` round-trips
    the prepaid/postpaid classification the user selected — we echo it
    back rather than re-derive it, because VTPass's response doesn't
    include it and BillService needs it to pick the right `service_id`
    variation downstream."""
    model_config = ConfigDict(frozen=True)

    service_id: str       # DisCo slug, e.g. "ikeja-electric"
    meter_number: str     # the meter/account number the user typed
    customer_name: str    # registered customer name from the DisCo
    address: str          # registered service address
    meter_type: str       # "prepaid" or "postpaid"


class SmartcardValidation(BaseModel):
    """Result of a cable-smartcard lookup (DStv / GOtv / Startimes).

    `current_plan_*` and `renewal_amount_ngn` are populated on active
    smartcards so the UI can surface "your current bouquet is X, renewal
    is ₦Y." Fresh/inactive cards return empty plan fields and a zero
    renewal amount — the ge=0 bound on the amount enforces that invariant
    per S3C-M5."""
    model_config = ConfigDict(frozen=True)

    service_id: str                 # cable slug, e.g. "dstv"
    smartcard_number: str           # the IUC / smartcard number
    customer_name: str              # registered subscriber name
    current_plan_name: str          # may be empty on a fresh/inactive card
    current_plan_code: str          # matching variation_code; may be empty
    status: str                     # "active" / "inactive" / "suspended"
    renewal_amount_ngn: Decimal = Field(ge=0)


class CablePlanVariation(BaseModel):
    """One row in the VTPass cable-bouquet catalog. Structurally mirrors
    `DataPlanVariation` — price is authoritative so BillService can
    reject client-supplied prices (spoof prevention)."""
    model_config = ConfigDict(frozen=True)

    variation_code: str      # "dstv-compact" etc. — opaque id VTPass expects back
    name: str                # "DStv Compact"
    price_ngn: Decimal = Field(ge=0)
    validity: str | None = None   # e.g. "1 month"


class CablePlanList(BaseModel):
    """Complete bouquet catalog for one cable service (e.g. dstv)."""
    model_config = ConfigDict(frozen=True)

    service_id: str                        # "dstv"
    variations: list[CablePlanVariation]


# ── Sprint 5 audit: dynamic service catalog ─────────────────────────────

class ServiceCatalogEntry(BaseModel):
    """One row in the VTPass `/api/services?identifier=X` response.

    Used to enumerate networks / cable providers / DisCos / etc. without
    hardcoding the lists in our codebase. VTPass owns the canonical
    serviceID strings (we previously had `phed` and `yedc` but the live
    API uses `portharcourt-electric` and `yola-electric`); fetching at
    runtime keeps us drift-proof.

    VTPass field names retained verbatim because they ship typos
    (`minimium_amount`, `convinience_fee`); we expose them under cleaner
    Python names but model the JSON aliases so deserialization works.
    """
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    service_id: str = Field(alias="serviceID")
    name: str
    minimum_amount: Decimal = Field(alias="minimium_amount")
    maximum_amount: Decimal = Field(alias="maximum_amount")
    convenience_fee: str = Field(alias="convinience_fee")
    product_type: str           # "flexible" (price-by-amount) | "fix" (price-by-variation)
    image: str                  # absolute URL to the provider logo


class ServiceCatalog(BaseModel):
    """Complete service catalog for a given identifier (electricity-bill,
    airtime, data, tv-subscription)."""
    model_config = ConfigDict(frozen=True)

    identifier: str             # the category key we queried with
    services: list[ServiceCatalogEntry]
