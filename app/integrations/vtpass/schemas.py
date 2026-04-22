"""VTPass response schemas — the typed shape our code sees after the
HTTP boundary. We do NOT reflect VTPass's raw envelope verbatim; we
normalize to a clean domain type so downstream (BillService,
tests, workers) doesn't have to know about their quirks."""
from decimal import Decimal
from enum import Enum
from typing import Optional

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
    validity: Optional[str] = None   # e.g. "1 day" (not always present in response)


class DataPlanList(BaseModel):
    """Complete plan catalog for one service (e.g. mtn-data)."""
    model_config = ConfigDict(frozen=True)

    service_id: str                        # "mtn-data"
    variations: list[DataPlanVariation]
