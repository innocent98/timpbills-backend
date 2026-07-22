# Dedicated Virtual Accounts (Paystack DVA) — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let KYC-tier-1+ users provision a permanent Paystack Dedicated Virtual Account (Dedicated NUBAN) and fund their wallet by ordinary bank transfer, resolved from the receiving account number on an inbound `charge.success` webhook, crediting gross and locking outbound spend when the credit overshoots the KYC cap.

**Architecture:** New `virtual_accounts` table (one row per user) tracks a Paystack `customer_code` + issued account details through a `pending_identity → pending_assign → active | failed` state machine driven entirely by Paystack webhooks. The existing reference-first Paystack webhook handler is reordered so DVA events (which carry no reference we minted) are branched on **before** the reference-mandatory `400 MALFORMED_WEBHOOK` check and resolved by `customer_code` (lifecycle) or `receiver_bank_account_number` (funding). `wallet_service.credit` gains an `over_cap` policy: the checkout path keeps raising `KycCapExceeded`; the transfer path credits in full and sets a wallet spend-lock, cleared on the next KYC tier upgrade. BVN is never persisted.

**Tech Stack:** FastAPI, SQLAlchemy 2.x (sync `db.query` Session style), Pydantic v2, Alembic, httpx + tenacity (Paystack client), Celery (notification dispatch), pytest with in-memory SQLite + fake providers.

## Global Constraints

Every task's requirements implicitly include these. Values copied verbatim from the spec and codebase conventions:

- **Forward-only migrations.** New Alembic revision in the `202607…` timestamp style; never edit a merged migration. Provide `upgrade()` and `downgrade()`; verify the round-trip (`alembic upgrade head && alembic downgrade -1 && alembic upgrade head`).
- **Sync SQLAlchemy `db.query(...)` style** with `Session`. Row locks via `.with_for_update()`. No async ORM sessions.
- **Money is `NUMERIC(14,2)` naira** (not kobo). Kobo appears only at the Paystack boundary; convert with `Decimal(data["amount"]) / Decimal(100)` on the way in.
- **Response envelope** is always `{"success": bool, "data": ..., "error": ..., "request_id": ...}` via `app.utils.responses.success(...)`. Errors are raised as `HTTPException(status_code=..., detail={"code": "UPPER_SNAKE", "message": "...", "details": {...}})`; the middleware projects `detail` to `error`.
- **Error codes map to HTTP:** `KYC_REQUIRED`→403, `WALLET_SPEND_LOCKED`→423, `NO_VIRTUAL_ACCOUNT`→404, `KYC_LIMIT_EXCEEDED`→422, `MALFORMED_WEBHOOK`→400, `INVALID_SIGNATURE`→401. Codes are stable mobile contracts — do not rename.
- **Fakes, not live providers, in tests.** `FORCE_FAKE_PROVIDERS=True` is autoused in `tests/conftest.py`; the fake Paystack singleton accepts the literal `"FAKE_SIG"` signature. Never hit real Paystack in a test.
- **No em-dashes or en-dashes in any user-facing string / notification copy.** Use plain ASCII (`-`, `,`, `.`, `(`, `)`). Applies to push titles/bodies and `failure_reason` text.
- **BVN is never persisted** and is already on the Sentry redaction list (`app/core/sentry_setup.py::_REDACT_SUBSTRINGS` contains `"bvn"`). It lives only inside the request handler and the outbound Paystack call. Never log it.
- **No `Co-Authored-By` trailer** and no "Generated with" lines in any commit message. Conventional-commit subject, imperative, under 70 chars.

---

## File Structure

| File | Create / Modify | One responsibility |
|---|---|---|
| `app/db/models/_enums.py` | Modify | Add `VirtualAccountStatus` + `SpendLockReason` string enums |
| `app/db/models/virtual_account.py` | Create | `VirtualAccount` ORM model (one row per user) |
| `app/db/models/wallet.py` | Modify | Add `spend_locked` + `spend_locked_reason` columns |
| `app/db/models/__init__.py` | Modify | Register `VirtualAccount` in the model registry |
| `alembic/versions/202607221200_add_virtual_accounts_and_wallet_spend_lock.py` | Create | Migration: new table + 2 wallet columns + 2 enum types |
| `app/core/config.py` | Modify | `PAYSTACK_DVA_*` settings block |
| `app/integrations/paystack/schemas.py` | Modify | Pydantic response models for the new Paystack calls |
| `app/integrations/paystack/base.py` | Modify | Extend `PaymentProvider` Protocol with 6 DVA methods |
| `app/integrations/paystack/client.py` | Modify | Real httpx+tenacity implementations |
| `app/integrations/paystack/fake.py` | Modify | Fake implementations + webhook fixture builders |
| `app/services/wallet_service.py` | Modify | `OverCapPolicy`, `WalletSpendLocked`, `credit(over_cap=...)`, spend-lock helpers |
| `app/services/bill_service.py` | Modify | Spend-lock gate at the top of `_execute_bill` |
| `app/api/v1/endpoints/bills.py` | Modify | Catch `WalletSpendLocked` → 423 in the 4 purchase endpoints |
| `app/services/virtual_account_service.py` | Create | `VirtualAccountService.provision` / `get_for_user` + name split |
| `app/schemas/virtual_account.py` | Create | Request/response Pydantic models for the DVA endpoints |
| `app/api/v1/endpoints/wallet.py` | Modify | `POST`/`GET /wallet/virtual-account` + `GET /wallet/banks` |
| `app/api/deps.py` | Modify | `get_virtual_account_service` DI |
| `app/services/notification_service.py` | Modify | `dva_ready` / `dva_failed` events + `build_dva_context` |
| `app/api/v1/endpoints/webhooks.py` | Modify | Reorder + DVA lifecycle & funding branches |
| `app/services/kyc_service.py` | Modify | Spend-lock unlock hook after a tier upgrade |

Test files (mirror `tests/` layout): `tests/services/test_virtual_account_service.py`, `tests/services/test_wallet_spend_lock.py`, `tests/integrations/test_paystack_dva_fake.py`, `tests/api/test_virtual_account_endpoints.py`, `tests/api/test_webhooks_dva.py`, `tests/api/test_bills_spend_lock.py`, `tests/services/test_kyc_spend_unlock.py`.

---

## Task 1: Enums, VirtualAccount model, wallet spend-lock columns, migration

**Files:**
- Modify: `app/db/models/_enums.py` (append two enums after `TransactionType`)
- Create: `app/db/models/virtual_account.py`
- Modify: `app/db/models/wallet.py:34-37` (add two columns)
- Modify: `app/db/models/__init__.py:1-4` (register model)
- Create: `alembic/versions/202607221200_add_virtual_accounts_and_wallet_spend_lock.py`
- Test: `tests/services/test_virtual_account_service.py` (model persistence portion)

**Interfaces:**
- Produces:
  - `VirtualAccountStatus` enum: members `pending_identity`, `pending_assign`, `active`, `failed`, `deactivated` (string values equal to member names).
  - `SpendLockReason` enum: member `over_cap = "over_cap"`.
  - `VirtualAccount` model with columns `id, user_id, paystack_customer_code, paystack_customer_id, dedicated_account_id, account_number, account_name, bank_name, bank_slug, currency, status (VirtualAccountStatus), failure_reason, created_at, updated_at`.
  - `Wallet.spend_locked: bool`, `Wallet.spend_locked_reason: SpendLockReason | None`.

- [ ] **Step 1: Write the failing test**

`tests/services/test_virtual_account_service.py`:

```python
from decimal import Decimal
from uuid import uuid4

from app.db.models._enums import SpendLockReason, VirtualAccountStatus
from app.db.models.user import KycLevel, User
from app.db.models.virtual_account import VirtualAccount
from app.db.models.wallet import Wallet


def _seed_user(db, kyc=KycLevel.tier_1) -> User:
    u = User(
        email=f"{uuid4().hex[:8]}@t.co",
        phone=f"+23480{uuid4().int % 10**9:09d}",
        full_name="Ada Grace Obi",
        password_hash="x",
        kyc_level=kyc,
        email_verified=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_virtual_account_row_persists(db_session):
    u = _seed_user(db_session)
    va = VirtualAccount(
        user_id=u.id,
        paystack_customer_code="CUS_test_1",
        status=VirtualAccountStatus.pending_identity,
        currency="NGN",
    )
    db_session.add(va)
    db_session.commit()
    db_session.refresh(va)
    assert va.status == VirtualAccountStatus.pending_identity
    assert va.account_number is None


def test_wallet_spend_lock_columns_default(db_session):
    u = _seed_user(db_session)
    w = Wallet(user_id=u.id, balance=Decimal("0.00"), balance_cap=Decimal("300000.00"))
    db_session.add(w)
    db_session.commit()
    db_session.refresh(w)
    assert w.spend_locked is False
    assert w.spend_locked_reason is None
    w.spend_locked = True
    w.spend_locked_reason = SpendLockReason.over_cap
    db_session.commit()
    db_session.refresh(w)
    assert w.spend_locked_reason == SpendLockReason.over_cap
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/test_virtual_account_service.py::test_virtual_account_row_persists -v`
Expected: FAIL with `ModuleNotFoundError: app.db.models.virtual_account` (and `ImportError` for the new enums).

- [ ] **Step 3: Add the enums**

Append to `app/db/models/_enums.py`:

```python
class VirtualAccountStatus(str, enum.Enum):
    pending_identity = "pending_identity"
    pending_assign   = "pending_assign"
    active           = "active"
    failed           = "failed"
    deactivated      = "deactivated"


class SpendLockReason(str, enum.Enum):
    over_cap = "over_cap"
```

- [ ] **Step 4: Create the model**

`app/db/models/virtual_account.py`:

```python
"""Dedicated Virtual Account (Paystack DVA) — one row per user.

We persist only Paystack's durable identity token (customer_code) plus the
issued account details. The BVN and bank account supplied at setup are never
stored: they live only inside the provisioning request handler and the
outbound Paystack call. Resolution of inbound bank-transfer webhooks is by
`account_number` (unique); resolution of the identity/assign lifecycle
webhooks is by `paystack_customer_code`.
"""
import uuid

from sqlalchemy import Column, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base
from app.db.mixins import TimestampMixin
from app.db.models._enums import VirtualAccountStatus


class VirtualAccount(Base, TimestampMixin):
    __tablename__ = "virtual_accounts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        unique=True,
        nullable=False,
        index=True,
    )
    paystack_customer_code = Column(String, nullable=False)
    paystack_customer_id   = Column(String, nullable=True)
    dedicated_account_id   = Column(String, nullable=True)
    account_number = Column(String, nullable=True, unique=True, index=True)
    account_name   = Column(String, nullable=True)
    bank_name      = Column(String, nullable=True)
    bank_slug      = Column(String, nullable=True)
    currency = Column(String, nullable=False, default="NGN")
    status = Column(
        Enum(VirtualAccountStatus, name="virtual_account_status_enum"),
        nullable=False,
    )
    failure_reason = Column(String, nullable=True)
```

- [ ] **Step 5: Add the wallet columns**

In `app/db/models/wallet.py`, add the imports and columns. Change the import line:

```python
from sqlalchemy import Boolean, CheckConstraint, Column, Enum, ForeignKey, Numeric
```

Add `from app.db.models._enums import SpendLockReason` below the existing imports, and add these two columns after the `balance_cap` column (line 35):

```python
    # Over-cap lock (DVA transfer path). When landed money would push the
    # wallet past the KYC cap we credit in full and set this, gating all
    # outbound spend until the next KYC tier upgrade clears it.
    spend_locked = Column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    spend_locked_reason = Column(
        Enum(SpendLockReason, name="spend_lock_reason_enum"), nullable=True
    )
```

- [ ] **Step 6: Register the model**

Append to `app/db/models/__init__.py`:

```python
from app.db.models.virtual_account import VirtualAccount  # noqa: F401
```

- [ ] **Step 7: Run test to verify it passes**

Run: `pytest tests/services/test_virtual_account_service.py::test_virtual_account_row_persists tests/services/test_virtual_account_service.py::test_wallet_spend_lock_columns_default -v`
Expected: PASS (SQLite maps the enums to VARCHAR+CHECK; `Base.metadata.create_all` in the `db_session` fixture builds the new table).

- [ ] **Step 8: Write the migration**

`alembic/versions/202607221200_add_virtual_accounts_and_wallet_spend_lock.py`:

```python
"""add virtual_accounts table + wallet spend-lock columns

Revision ID: 202607221200
Revises: 202607101300
Create Date: 2026-07-22 12:00:00

Creates virtual_accounts (one DVA per user, unique account_number for inbound
webhook resolution) and adds spend_locked / spend_locked_reason to wallets for
the over-cap transfer lock. Forward-only per PRD 1.7.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "202607221200"
down_revision = "202607101300"
branch_labels = None
depends_on = None

_VA_STATUS = sa.Enum(
    "pending_identity", "pending_assign", "active", "failed", "deactivated",
    name="virtual_account_status_enum",
)
_SPEND_REASON = sa.Enum("over_cap", name="spend_lock_reason_enum")


def upgrade() -> None:
    bind = op.get_bind()
    _VA_STATUS.create(bind, checkfirst=True)
    _SPEND_REASON.create(bind, checkfirst=True)

    op.create_table(
        "virtual_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("paystack_customer_code", sa.String(), nullable=False),
        sa.Column("paystack_customer_id", sa.String(), nullable=True),
        sa.Column("dedicated_account_id", sa.String(), nullable=True),
        sa.Column("account_number", sa.String(), nullable=True),
        sa.Column("account_name", sa.String(), nullable=True),
        sa.Column("bank_name", sa.String(), nullable=True),
        sa.Column("bank_slug", sa.String(), nullable=True),
        sa.Column("currency", sa.String(), nullable=False, server_default="NGN"),
        sa.Column(
            "status",
            _VA_STATUS,
            nullable=False,
        ),
        sa.Column("failure_reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_unique_constraint(
        "uq_virtual_accounts_user_id", "virtual_accounts", ["user_id"]
    )
    op.create_index(
        "ix_virtual_accounts_user_id", "virtual_accounts", ["user_id"]
    )
    op.create_index(
        "ix_virtual_accounts_account_number",
        "virtual_accounts",
        ["account_number"],
        unique=True,
    )

    op.add_column(
        "wallets",
        sa.Column(
            "spend_locked",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "wallets",
        sa.Column("spend_locked_reason", _SPEND_REASON, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("wallets", "spend_locked_reason")
    op.drop_column("wallets", "spend_locked")
    op.drop_index("ix_virtual_accounts_account_number", table_name="virtual_accounts")
    op.drop_index("ix_virtual_accounts_user_id", table_name="virtual_accounts")
    op.drop_constraint(
        "uq_virtual_accounts_user_id", "virtual_accounts", type_="unique"
    )
    op.drop_table("virtual_accounts")

    bind = op.get_bind()
    _SPEND_REASON.drop(bind, checkfirst=True)
    _VA_STATUS.drop(bind, checkfirst=True)
```

- [ ] **Step 9: Verify the migration round-trips**

Run: `alembic upgrade head && alembic downgrade -1 && alembic upgrade head`
Expected: three clean runs, no error. `virtual_accounts` and the two wallet columns exist after the final `upgrade head`.

- [ ] **Step 10: Commit**

```bash
git add app/db/models/_enums.py app/db/models/virtual_account.py app/db/models/wallet.py app/db/models/__init__.py alembic/versions/202607221200_add_virtual_accounts_and_wallet_spend_lock.py tests/services/test_virtual_account_service.py
git commit -m "feat: add virtual_accounts model + wallet spend-lock columns"
```

---

## Task 2: PAYSTACK_DVA_* config

**Files:**
- Modify: `app/core/config.py:82` (after the `PAYSTACK_CARD_FEE_*` block)
- Test: `tests/core/test_config_dva.py` (Create)

**Interfaces:**
- Produces: `settings.PAYSTACK_DVA_PREFERRED_BANK: str`, `settings.PAYSTACK_DVA_FEE_PERCENT: float`, `settings.PAYSTACK_DVA_FEE_CAP_NGN: int`.

- [ ] **Step 1: Write the failing test**

`tests/core/test_config_dva.py`:

```python
from app.core.config import settings


def test_dva_defaults_present():
    assert settings.PAYSTACK_DVA_PREFERRED_BANK == "wema-bank"
    # Accounting/reporting only — never applied to the wallet credit.
    assert isinstance(settings.PAYSTACK_DVA_FEE_PERCENT, float)
    assert isinstance(settings.PAYSTACK_DVA_FEE_CAP_NGN, int)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/core/test_config_dva.py -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'PAYSTACK_DVA_PREFERRED_BANK'`.

- [ ] **Step 3: Add the config block**

In `app/core/config.py`, immediately after the `PAYSTACK_CARD_FEE_CAP_NAIRA` line (line 82):

```python

    # Paystack Dedicated Virtual Accounts (DVA). Timpbills absorbs the DVA
    # fee: the wallet is credited GROSS (the full transferred amount). The
    # fee figures here are for accounting/reporting only and are never
    # applied to a credit. Use "test-bank" in dev/test.
    PAYSTACK_DVA_PREFERRED_BANK: str = "wema-bank"
    PAYSTACK_DVA_FEE_PERCENT: float = 1.0
    PAYSTACK_DVA_FEE_CAP_NGN: int = 300
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/core/test_config_dva.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/core/config.py tests/core/test_config_dva.py
git commit -m "feat: add PAYSTACK_DVA config block"
```

---

## Task 3: Paystack integration — DVA methods, schemas, fake, webhook fixtures

**Files:**
- Modify: `app/integrations/paystack/schemas.py` (append models)
- Modify: `app/integrations/paystack/base.py` (extend Protocol)
- Modify: `app/integrations/paystack/client.py` (real methods)
- Modify: `app/integrations/paystack/fake.py` (fake methods + fixture builders)
- Test: `tests/integrations/test_paystack_dva_fake.py` (Create)

**Interfaces:**
- Consumes: `settings.PAYSTACK_BASE_URL`, `settings.PAYSTACK_SECRET_KEY` (existing).
- Produces (Pydantic models in `schemas.py`):
  - `CreateCustomerResponse(customer_code: str, customer_id: str | None = None)`
  - `AssignDedicatedAccountResponse(status: bool, message: str)`
  - `DedicatedAccountDetails(account_number: str | None, account_name: str | None, bank_name: str | None, bank_slug: str | None, dedicated_account_id: str | None, status: str | None)`
  - `DvaProvider(provider_slug: str, bank_name: str)`
  - `BankListItem(name: str, slug: str, code: str)`
- Produces (Protocol / client / fake methods, all `async` except signature verify):
  - `create_customer(*, email: str, first_name: str, last_name: str, phone: str) -> CreateCustomerResponse`
  - `assign_dedicated_account(*, email: str, first_name: str, middle_name: str, last_name: str, phone: str, preferred_bank: str, country: str, account_number: str, bvn: str, bank_code: str) -> AssignDedicatedAccountResponse`
  - `fetch_dedicated_account(*, account_id: str) -> DedicatedAccountDetails`
  - `requery_dedicated_account(*, account_number: str, provider_slug: str) -> AssignDedicatedAccountResponse`
  - `list_dva_providers() -> list[DvaProvider]`
  - `list_banks(*, country: str = "nigeria") -> list[BankListItem]`
- Produces (fake fixture builders, module-level functions in `fake.py`):
  - `customer_identification_event(*, customer_code, success=True, reason=None, event_id="evt_ci") -> dict`
  - `dedicated_account_assign_event(*, customer_code, success=True, account_number=None, account_name=None, bank_name=None, bank_slug=None, reason=None, event_id="evt_da") -> dict`
  - `dva_charge_event(*, account_number, amount_kobo, sender_name="JOHN DOE", sender_bank="Kuda MFB", sender_account="1234567890", fees=1500, reference="dva-ref", event_id="evt_dva") -> dict`

- [ ] **Step 1: Write the failing test**

`tests/integrations/test_paystack_dva_fake.py`:

```python
import pytest

from app.integrations.paystack.fake import (
    FakePaystackClient,
    customer_identification_event,
    dedicated_account_assign_event,
    dva_charge_event,
)


@pytest.mark.asyncio
async def test_create_customer_is_deterministic_by_email():
    fake = FakePaystackClient()
    a = await fake.create_customer(
        email="ada@x.co", first_name="Ada", last_name="Obi", phone="+2348000000001"
    )
    b = await fake.create_customer(
        email="ada@x.co", first_name="Ada", last_name="Obi", phone="+2348000000001"
    )
    assert a.customer_code == b.customer_code  # idempotent by email
    assert a.customer_code.startswith("CUS_")


@pytest.mark.asyncio
async def test_assign_records_call_and_returns_202_shape():
    fake = FakePaystackClient()
    res = await fake.assign_dedicated_account(
        email="ada@x.co", first_name="Ada", middle_name="Grace", last_name="Obi",
        phone="+2348000000001", preferred_bank="test-bank", country="NG",
        account_number="0123456789", bvn="22222222222", bank_code="035",
    )
    assert res.status is True
    assert fake.assigned == [("ada@x.co", "0123456789", "22222222222", "035")]


@pytest.mark.asyncio
async def test_list_banks_returns_items():
    fake = FakePaystackClient()
    banks = await fake.list_banks(country="nigeria")
    assert any(b.slug == "test-bank" for b in banks)
    assert all(b.code for b in banks)


def test_webhook_fixture_builders_shape():
    ci = customer_identification_event(customer_code="CUS_1", success=True)
    assert ci["event"] == "customeridentification.success"
    assert ci["data"]["customer_code"] == "CUS_1"

    da = dedicated_account_assign_event(
        customer_code="CUS_1", account_number="9988776655",
        account_name="ADA OBI", bank_name="Wema Bank", bank_slug="wema-bank",
    )
    assert da["event"] == "dedicatedaccount.assign.success"
    assert da["data"]["dedicated_account"]["account_number"] == "9988776655"

    ch = dva_charge_event(account_number="9988776655", amount_kobo=500000)
    assert ch["event"] == "charge.success"
    assert ch["data"]["channel"] == "dedicated_nuban"
    assert ch["data"]["authorization"]["receiver_bank_account_number"] == "9988776655"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integrations/test_paystack_dva_fake.py -v`
Expected: FAIL with `ImportError: cannot import name 'customer_identification_event'`.

- [ ] **Step 3: Add the schemas**

Append to `app/integrations/paystack/schemas.py`:

```python
class CreateCustomerResponse(BaseModel):
    customer_code: str
    customer_id: str | None = None


class AssignDedicatedAccountResponse(BaseModel):
    status: bool
    message: str


class DedicatedAccountDetails(BaseModel):
    account_number: str | None = None
    account_name: str | None = None
    bank_name: str | None = None
    bank_slug: str | None = None
    dedicated_account_id: str | None = None
    status: str | None = None


class DvaProvider(BaseModel):
    provider_slug: str
    bank_name: str


class BankListItem(BaseModel):
    name: str
    slug: str
    code: str
```

- [ ] **Step 4: Extend the Protocol**

Replace `app/integrations/paystack/base.py` with:

```python
from typing import Protocol

from app.integrations.paystack.schemas import (
    AssignDedicatedAccountResponse,
    BankListItem,
    CreateCustomerResponse,
    DedicatedAccountDetails,
    DvaProvider,
    InitResponse,
    VerifyResponse,
)


class PaymentProvider(Protocol):
    async def initialize(
        self,
        *,
        amount_kobo: int,
        email: str,
        reference: str,
        callback_url: str | None = None,
        metadata: dict | None = None,
    ) -> InitResponse: ...

    async def verify(self, *, reference: str) -> VerifyResponse: ...

    def verify_signature(self, *, raw_body: bytes, signature: str) -> bool: ...

    # ── Dedicated Virtual Accounts ───────────────────────────────────────
    async def create_customer(
        self, *, email: str, first_name: str, last_name: str, phone: str
    ) -> CreateCustomerResponse: ...

    async def assign_dedicated_account(
        self,
        *,
        email: str,
        first_name: str,
        middle_name: str,
        last_name: str,
        phone: str,
        preferred_bank: str,
        country: str,
        account_number: str,
        bvn: str,
        bank_code: str,
    ) -> AssignDedicatedAccountResponse: ...

    async def fetch_dedicated_account(
        self, *, account_id: str
    ) -> DedicatedAccountDetails: ...

    async def requery_dedicated_account(
        self, *, account_number: str, provider_slug: str
    ) -> AssignDedicatedAccountResponse: ...

    async def list_dva_providers(self) -> list[DvaProvider]: ...

    async def list_banks(self, *, country: str = "nigeria") -> list[BankListItem]: ...
```

- [ ] **Step 5: Add the real client methods**

In `app/integrations/paystack/client.py`, extend the import from `schemas` to include the new models, then append these methods to `PaystackClient` (each reuses the same tenacity decorator config as `initialize`/`verify`):

```python
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
```

Update the client's `schemas` import line to:

```python
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
```

- [ ] **Step 6: Add the fake methods + fixture builders**

In `app/integrations/paystack/fake.py`, extend the `schemas` import and add the tracking fields + methods to `FakePaystackClient`, then the module-level builders:

```python
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
```

Add these fields to the dataclass (alongside `initialized`):

```python
    customers: list[str] = field(default_factory=list)
    assigned: list[tuple[str, str, str, str]] = field(default_factory=list)
```

Add these methods to `FakePaystackClient`:

```python
    async def create_customer(
        self, *, email: str, first_name: str, last_name: str, phone: str
    ) -> CreateCustomerResponse:
        # Deterministic by email so a repeat provision maps to one customer
        # (mirrors Paystack's idempotent POST /customer by email).
        self.customers.append(email)
        digest = f"{abs(hash(email)) % 10**10:010d}"
        return CreateCustomerResponse(
            customer_code=f"CUS_fake_{digest}", customer_id=digest
        )

    async def assign_dedicated_account(
        self, *, email: str, first_name: str, middle_name: str, last_name: str,
        phone: str, preferred_bank: str, country: str, account_number: str,
        bvn: str, bank_code: str,
    ) -> AssignDedicatedAccountResponse:
        self.assigned.append((email, account_number, bvn, bank_code))
        return AssignDedicatedAccountResponse(
            status=True, message="Assign dedicated account in progress"
        )

    async def fetch_dedicated_account(
        self, *, account_id: str
    ) -> DedicatedAccountDetails:
        return DedicatedAccountDetails(
            account_number="9988776655", account_name="ADA OBI",
            bank_name="Test Bank", bank_slug="test-bank",
            dedicated_account_id=account_id, status="active",
        )

    async def requery_dedicated_account(
        self, *, account_number: str, provider_slug: str
    ) -> AssignDedicatedAccountResponse:
        return AssignDedicatedAccountResponse(status=True, message="requery queued")

    async def list_dva_providers(self) -> list[DvaProvider]:
        return [DvaProvider(provider_slug="test-bank", bank_name="Test Bank")]

    async def list_banks(self, *, country: str = "nigeria") -> list[BankListItem]:
        return [
            BankListItem(name="Wema Bank", slug="wema-bank", code="035"),
            BankListItem(name="Test Bank", slug="test-bank", code="000"),
            BankListItem(name="Kuda MFB", slug="kuda-bank", code="50211"),
        ]
```

At module scope, add the fixture builders:

```python
def customer_identification_event(
    *, customer_code: str, success: bool = True, reason: str | None = None,
    event_id: str = "evt_ci",
) -> dict:
    event = "customeridentification.success" if success else "customeridentification.failed"
    data = {"id": event_id, "customer_code": customer_code, "email": "ada@x.co"}
    if not success:
        data["reason"] = reason or "Account resolution failed"
    return {"event": event, "data": data}


def dedicated_account_assign_event(
    *, customer_code: str, success: bool = True, account_number: str | None = None,
    account_name: str | None = None, bank_name: str | None = None,
    bank_slug: str | None = None, reason: str | None = None, event_id: str = "evt_da",
) -> dict:
    event = "dedicatedaccount.assign.success" if success else "dedicatedaccount.assign.failed"
    data: dict = {"id": event_id, "customer": {"customer_code": customer_code}}
    if success:
        data["dedicated_account"] = {
            "id": "dva_1",
            "account_number": account_number,
            "account_name": account_name,
            "bank": {"name": bank_name, "slug": bank_slug},
        }
    else:
        data["reason"] = reason or "Could not assign account"
    return {"event": event, "data": data}


def dva_charge_event(
    *, account_number: str, amount_kobo: int, sender_name: str = "JOHN DOE",
    sender_bank: str = "Kuda MFB", sender_account: str = "1234567890",
    fees: int = 1500, reference: str = "dva-ref", event_id: str = "evt_dva",
) -> dict:
    return {
        "event": "charge.success",
        "data": {
            "id": event_id,
            "reference": reference,
            "amount": amount_kobo,
            "channel": "dedicated_nuban",
            "fees": fees,
            "authorization": {
                "channel": "dedicated_nuban",
                "receiver_bank_account_number": account_number,
                "sender_name": sender_name,
                "sender_bank": sender_bank,
                "sender_bank_account_number": sender_account,
            },
        },
    }
```

- [ ] **Step 7: Run test to verify it passes**

Run: `pytest tests/integrations/test_paystack_dva_fake.py -v`
Expected: PASS (4 tests).

- [ ] **Step 8: Commit**

```bash
git add app/integrations/paystack/schemas.py app/integrations/paystack/base.py app/integrations/paystack/client.py app/integrations/paystack/fake.py tests/integrations/test_paystack_dva_fake.py
git commit -m "feat: add Paystack DVA client methods, fake, and webhook fixtures"
```

---

## Task 4: wallet_service credit over-cap policy + spend-lock helpers

**Files:**
- Modify: `app/services/wallet_service.py:73-107` (extend `credit`); add enum, exception, two helpers
- Test: `tests/services/test_wallet_spend_lock.py` (Create)

**Interfaces:**
- Consumes: `_resolve_cap(kyc_level) -> Decimal | None`, `KycCapExceeded`, `SpendLockReason` (Task 1).
- Produces:
  - `class OverCapPolicy(str, enum.Enum)` with `RAISE = "raise"`, `LOCK = "lock"`.
  - `class WalletSpendLocked(Exception)`.
  - `WalletService.credit(*, user_id: UUID, amount: Decimal, over_cap: OverCapPolicy = OverCapPolicy.RAISE) -> Decimal` (default behaviour unchanged: raises `KycCapExceeded`).
  - `WalletService.raise_if_spend_locked(*, user_id: UUID) -> None` (raises `WalletSpendLocked`).
  - `WalletService.clear_spend_lock_if_within_cap(*, user_id: UUID) -> bool`.

- [ ] **Step 1: Write the failing test**

`tests/services/test_wallet_spend_lock.py`:

```python
from decimal import Decimal
from uuid import uuid4

import pytest

from app.db.models._enums import SpendLockReason
from app.db.models.user import KycLevel, User
from app.db.models.wallet import Wallet
from app.services.wallet_service import (
    KycCapExceeded,
    OverCapPolicy,
    WalletService,
    WalletSpendLocked,
)


def _seed_user(db, kyc=KycLevel.tier_0) -> User:
    u = User(
        email=f"{uuid4().hex[:8]}@t.co",
        phone=f"+23480{uuid4().int % 10**9:09d}",
        full_name="T U",
        password_hash="x",
        kyc_level=kyc,
        email_verified=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_credit_raise_policy_unchanged(db_session):
    u = _seed_user(db_session, kyc=KycLevel.tier_0)  # 50,000 cap
    svc = WalletService(db=db_session)
    svc.get_or_create(user_id=u.id)
    svc.credit(user_id=u.id, amount=Decimal("40000.00"))
    with pytest.raises(KycCapExceeded):
        svc.credit(user_id=u.id, amount=Decimal("20000.00"))


def test_credit_lock_policy_credits_full_and_locks(db_session):
    u = _seed_user(db_session, kyc=KycLevel.tier_0)  # 50,000 cap
    svc = WalletService(db=db_session)
    svc.get_or_create(user_id=u.id)
    new_balance = svc.credit(
        user_id=u.id, amount=Decimal("70000.00"), over_cap=OverCapPolicy.LOCK
    )
    assert new_balance == Decimal("70000.00")  # gross, never rejected
    w = db_session.query(Wallet).filter(Wallet.user_id == u.id).one()
    assert w.spend_locked is True
    assert w.spend_locked_reason == SpendLockReason.over_cap


def test_raise_if_spend_locked(db_session):
    u = _seed_user(db_session)
    svc = WalletService(db=db_session)
    w = svc.get_or_create(user_id=u.id)
    svc.raise_if_spend_locked(user_id=u.id)  # not locked -> no raise
    w.spend_locked = True
    w.spend_locked_reason = SpendLockReason.over_cap
    db_session.commit()
    with pytest.raises(WalletSpendLocked):
        svc.raise_if_spend_locked(user_id=u.id)


def test_clear_spend_lock_when_within_new_cap(db_session):
    u = _seed_user(db_session, kyc=KycLevel.tier_0)
    svc = WalletService(db=db_session)
    svc.get_or_create(user_id=u.id)
    svc.credit(user_id=u.id, amount=Decimal("70000.00"), over_cap=OverCapPolicy.LOCK)
    # Simulate the tier upgrade to tier_1 (300,000 cap) that now covers 70,000.
    u.kyc_level = KycLevel.tier_1
    db_session.commit()
    cleared = svc.clear_spend_lock_if_within_cap(user_id=u.id)
    assert cleared is True
    w = db_session.query(Wallet).filter(Wallet.user_id == u.id).one()
    assert w.spend_locked is False
    assert w.spend_locked_reason is None


def test_clear_spend_lock_leaves_locked_when_still_over(db_session):
    u = _seed_user(db_session, kyc=KycLevel.tier_0)
    svc = WalletService(db=db_session)
    svc.get_or_create(user_id=u.id)
    svc.credit(user_id=u.id, amount=Decimal("400000.00"), over_cap=OverCapPolicy.LOCK)
    u.kyc_level = KycLevel.tier_1  # 300,000 cap still below 400,000
    db_session.commit()
    assert svc.clear_spend_lock_if_within_cap(user_id=u.id) is False
    w = db_session.query(Wallet).filter(Wallet.user_id == u.id).one()
    assert w.spend_locked is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/test_wallet_spend_lock.py -v`
Expected: FAIL with `ImportError: cannot import name 'OverCapPolicy'`.

- [ ] **Step 3: Implement the policy, exception, and helpers**

In `app/services/wallet_service.py`, add `import enum` at the top and `from app.db.models._enums import SpendLockReason` to the model imports, then add the enum + exception near the other exceptions:

```python
class OverCapPolicy(str, enum.Enum):
    """How credit() reacts when a credit would push balance past the KYC cap.

    RAISE  - checkout path (default). Raise KycCapExceeded; the caller returns
             422 and Paystack retries until ops raises the tier. Unchanged.
    LOCK   - transfer/DVA path. Landed money is never rejected: credit in full
             and lock outbound spend until the next KYC upgrade covers it.
    """
    RAISE = "raise"
    LOCK = "lock"


class WalletSpendLocked(Exception):
    """Outbound money-move attempted while the wallet is spend-locked."""
```

Replace the `credit` method body's cap branch. The full new `credit`:

```python
    def credit(
        self,
        *,
        user_id: UUID,
        amount: Decimal,
        over_cap: OverCapPolicy = OverCapPolicy.RAISE,
    ) -> Decimal:
        """Credit atomically under SELECT ... FOR UPDATE.

        ``over_cap`` selects the over-cap behaviour (see OverCapPolicy).
        Default RAISE keeps the checkout path identical to before.
        """
        w = (
            self._db.query(Wallet)
            .filter(Wallet.user_id == user_id)
            .with_for_update()
            .first()
        )
        if w is None:
            w = self.get_or_create(user_id=user_id)
            w = (
                self._db.query(Wallet)
                .filter(Wallet.id == w.id)
                .with_for_update()
                .first()
            )

        user = self._db.query(User).filter(User.id == user_id).first()
        if user is not None:
            cap = _resolve_cap(user.kyc_level)
            w.balance_cap = cap if cap is not None else _UNLIMITED_CAP
        else:
            cap = w.balance_cap

        new_balance = w.balance + amount
        if cap is not None and new_balance > cap:
            if over_cap == OverCapPolicy.RAISE:
                raise KycCapExceeded(
                    f"new balance {new_balance} exceeds cap {cap}"
                )
            # LOCK: credit in full, never reject landed money; gate outbound.
            w.balance = new_balance
            w.spend_locked = True
            w.spend_locked_reason = SpendLockReason.over_cap
        else:
            w.balance = new_balance
        self._db.commit()
        return new_balance
```

Add the two helpers to `WalletService` (after `debit`):

```python
    def raise_if_spend_locked(self, *, user_id: UUID) -> None:
        """Guard for every outbound money-move. Raises WalletSpendLocked when
        the wallet is locked (over-cap landed money awaiting a KYC upgrade)."""
        w = self._db.query(Wallet).filter(Wallet.user_id == user_id).first()
        if w is not None and w.spend_locked:
            raise WalletSpendLocked(
                "wallet is spend-locked pending a KYC upgrade"
            )

    def clear_spend_lock_if_within_cap(self, *, user_id: UUID) -> bool:
        """Clear an over-cap spend-lock when the user's current KYC cap now
        covers the balance. Called after a tier upgrade. Returns True if the
        lock was cleared, False if it was left in place or absent."""
        w = (
            self._db.query(Wallet)
            .filter(Wallet.user_id == user_id)
            .with_for_update()
            .first()
        )
        if (
            w is None
            or not w.spend_locked
            or w.spend_locked_reason != SpendLockReason.over_cap
        ):
            return False
        user = self._db.query(User).filter(User.id == user_id).first()
        cap = _resolve_cap(user.kyc_level) if user is not None else w.balance_cap
        if cap is None or w.balance <= cap:
            w.spend_locked = False
            w.spend_locked_reason = None
            self._db.commit()
            return True
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/test_wallet_spend_lock.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Run the existing wallet suite to confirm no regression**

Run: `pytest tests/services/test_wallet_service.py tests/services/test_wallet_kyc_caps.py -v`
Expected: PASS (the default `over_cap=RAISE` keeps existing behaviour).

- [ ] **Step 6: Commit**

```bash
git add app/services/wallet_service.py tests/services/test_wallet_spend_lock.py
git commit -m "feat: add credit over-cap policy and wallet spend-lock helpers"
```

---

## Task 5: Outbound spend gate in bill_service + 423 in bill endpoints

**Files:**
- Modify: `app/services/bill_service.py:85` (import), `:725-733` (gate at top of `_execute_bill`)
- Modify: `app/api/v1/endpoints/bills.py:56` (import), and the four purchase handlers (`purchase_airtime` ~line 207, `purchase_electricity` ~404, `purchase_cable` ~673, `purchase_data` ~788) to catch `WalletSpendLocked`
- Test: `tests/api/test_bills_spend_lock.py` (Create)

**Interfaces:**
- Consumes: `WalletService.raise_if_spend_locked(*, user_id) -> None`, `WalletSpendLocked` (Task 4); `BillService._execute_bill` (existing); `require_pin_token`, `require_full_auth_gates` (existing).
- Produces: bill purchase endpoints return `423 {"code": "WALLET_SPEND_LOCKED"}` when the wallet is locked, before any debit.

- [ ] **Step 1: Write the failing test**

`tests/api/test_bills_spend_lock.py` (mirrors the client fixture pattern of `tests/api/test_webhooks_paystack.py`):

```python
import json
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from fakeredis.aioredis import FakeRedis

from app.main import app
from app.api.deps import (
    get_db, get_redis, get_token_store, get_email_provider,
    reset_fake_sms, reset_fake_email, reset_fake_paystack,
)
from app.core.limiter import limiter
from app.db.models._enums import SpendLockReason
from app.db.models.user import User
from app.db.models.wallet import Wallet
from app.integrations.email.fake import FakeEmailClient
from app.services.token_store import RedisTokenStore

import tests.e2e.test_auth_full_flows as _e2e_mod
from tests.e2e.test_auth_full_flows import _seed_logged_in_user

_test_email_client = FakeEmailClient()


@pytest_asyncio.fixture
async def client(db_session):
    def _get_db():
        try:
            yield db_session
        finally:
            pass

    fake_redis = FakeRedis(decode_responses=True)

    def _get_token_store():
        return RedisTokenStore(redis=fake_redis)

    def _get_email():
        return _test_email_client

    async def _get_redis():
        return fake_redis

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_token_store] = _get_token_store
    app.dependency_overrides[get_email_provider] = _get_email
    app.dependency_overrides[get_redis] = _get_redis
    reset_fake_sms()
    reset_fake_email()
    reset_fake_paystack()
    _orig = _e2e_mod._e2e_email_client
    _e2e_mod._e2e_email_client = _test_email_client
    limiter.enabled = False
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    limiter.enabled = True
    _e2e_mod._e2e_email_client = _orig
    await fake_redis.aclose()
    app.dependency_overrides.clear()


async def _pin_token(client, headers) -> str:
    r = await client.post("/api/v1/auth/pin/verify", json={"pin": "8527"}, headers=headers)
    return r.json()["data"]["pin_token"]


@pytest.mark.asyncio
async def test_airtime_blocked_when_wallet_spend_locked(db_session, client):
    _tokens, headers = await _seed_logged_in_user(client)
    user_row = db_session.query(User).filter(User.email == "e@e.co").one()
    # Fund + lock the wallet directly.
    w = Wallet(
        user_id=user_row.id, balance=Decimal("5000.00"),
        balance_cap=Decimal("50000.00"), spend_locked=True,
        spend_locked_reason=SpendLockReason.over_cap,
    )
    db_session.add(w)
    db_session.commit()

    pin = await _pin_token(client, headers)
    r = await client.post(
        "/api/v1/bills/airtime",
        json={"network": "mtn", "phone": "08012345678", "amount": "1000.00"},
        headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 423
    assert r.json()["error"]["code"] == "WALLET_SPEND_LOCKED"
    # Balance untouched (never debited).
    db_session.expire_all()
    assert db_session.query(Wallet).filter(Wallet.user_id == user_row.id).one().balance == Decimal("5000.00")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/api/test_bills_spend_lock.py -v`
Expected: FAIL — currently the purchase proceeds and returns 200/402, not 423.

- [ ] **Step 3: Add the gate in `_execute_bill`**

In `app/services/bill_service.py`, extend the wallet_service import (line 85):

```python
from app.services.wallet_service import (
    InsufficientBalance,
    WalletService,
    WalletSpendLocked,
)
```

In `_execute_bill`, immediately after `tx = self._tx.create(...)` (line 727-729) and before the debit, add:

```python
        # Spend-lock gate: over-cap landed money never blocks inbound credit,
        # but every outbound move is refused until the next KYC upgrade clears
        # the lock. Checked before the debit so no money moves. The tx stays
        # pending with an audit reason so ops can see why we stopped.
        try:
            self._wallet.raise_if_spend_locked(user_id=user_id)
        except WalletSpendLocked:
            self._tx.transition(
                tx, to_status=TransactionStatus.failed,
                reason="wallet_spend_locked_before_provider",
            )
            raise
```

- [ ] **Step 4: Catch `WalletSpendLocked` in the four bill endpoints**

In `app/api/v1/endpoints/bills.py`, extend the import (line 56):

```python
from app.services.wallet_service import (
    InsufficientBalance,
    WalletService,
    WalletSpendLocked,
)
```

In **each** of the four purchase handlers (`purchase_airtime`, `purchase_electricity`, `purchase_cable`, `purchase_data`), the provider call is wrapped in `try: ... except InsufficientBalance:`. Add a sibling `except` immediately before each `except InsufficientBalance:` block:

```python
        except WalletSpendLocked:
            raise HTTPException(
                status_code=423,
                detail={
                    "code": "WALLET_SPEND_LOCKED",
                    "message": (
                        "Your wallet is on hold because a recent transfer put "
                        "your balance above your KYC limit. Upgrade your KYC to "
                        "spend from your wallet."
                    ),
                },
            )
```

Note the surrounding `try:` at the endpoint level re-raises `HTTPException` and releases the idempotency in-flight sentinel (existing `except HTTPException: await idem.release_in_flight(...)` path), so no extra idempotency handling is needed.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/api/test_bills_spend_lock.py -v`
Expected: PASS.

- [ ] **Step 6: Run the bills suite for regressions**

Run: `pytest tests/api -k "bill" -v`
Expected: PASS (no previously-green bill test regresses).

- [ ] **Step 7: Commit**

```bash
git add app/services/bill_service.py app/api/v1/endpoints/bills.py tests/api/test_bills_spend_lock.py
git commit -m "feat: gate outbound bill purchases on wallet spend-lock (423)"
```

---

## Task 6: VirtualAccountService.provision + get_for_user + name split

**Files:**
- Create: `app/services/virtual_account_service.py`
- Test: `tests/services/test_virtual_account_service.py` (append provisioning tests)

**Interfaces:**
- Consumes: `VirtualAccount`, `VirtualAccountStatus` (Task 1); `PaymentProvider.create_customer` / `.assign_dedicated_account` (Task 3); `settings.PAYSTACK_DVA_PREFERRED_BANK` (Task 2); `User` model (`full_name`, `email`, `phone`, `kyc_level.numeric`).
- Produces:
  - `class KycRequired(Exception)`.
  - `split_full_name(full_name: str) -> tuple[str, str, str]` returning `(first, middle, last)`; middle is `""` for two tokens; single token sets `last == first` and logs a warning.
  - `class VirtualAccountService.__init__(self, *, db: Session, paystack: PaymentProvider)`.
  - `VirtualAccountService.get_for_user(*, user_id: UUID) -> VirtualAccount | None`.
  - `async VirtualAccountService.provision(*, user: User, bvn: str, account_number: str, bank_code: str, preferred_bank: str | None = None) -> VirtualAccount`.

- [ ] **Step 1: Write the failing test**

Append to `tests/services/test_virtual_account_service.py`:

```python
import pytest

from app.integrations.paystack.fake import FakePaystackClient
from app.services.virtual_account_service import (
    KycRequired,
    VirtualAccountService,
    split_full_name,
)


def test_split_full_name_variants():
    assert split_full_name("Ada Grace Obi") == ("Ada", "Grace", "Obi")
    assert split_full_name("Ada Obi") == ("Ada", "", "Obi")
    assert split_full_name("Ada Grace Mary Obi") == ("Ada", "Grace Mary", "Obi")
    assert split_full_name("Ada") == ("Ada", "", "Ada")  # single-token fallback


@pytest.mark.asyncio
async def test_provision_requires_kyc_tier_1(db_session):
    u = _seed_user(db_session, kyc=KycLevel.tier_0)
    svc = VirtualAccountService(db=db_session, paystack=FakePaystackClient())
    with pytest.raises(KycRequired):
        await svc.provision(
            user=u, bvn="22222222222", account_number="0123456789", bank_code="035"
        )


@pytest.mark.asyncio
async def test_provision_creates_pending_identity_row_and_assigns(db_session):
    u = _seed_user(db_session, kyc=KycLevel.tier_1)
    fake = FakePaystackClient()
    svc = VirtualAccountService(db=db_session, paystack=fake)
    va = await svc.provision(
        user=u, bvn="22222222222", account_number="0123456789", bank_code="035"
    )
    assert va.status == VirtualAccountStatus.pending_identity
    assert va.paystack_customer_code.startswith("CUS_")
    assert len(fake.assigned) == 1


@pytest.mark.asyncio
async def test_provision_is_idempotent(db_session):
    u = _seed_user(db_session, kyc=KycLevel.tier_1)
    fake = FakePaystackClient()
    svc = VirtualAccountService(db=db_session, paystack=fake)
    va1 = await svc.provision(
        user=u, bvn="22222222222", account_number="0123456789", bank_code="035"
    )
    va2 = await svc.provision(
        user=u, bvn="22222222222", account_number="0123456789", bank_code="035"
    )
    assert va1.id == va2.id
    rows = db_session.query(VirtualAccount).filter(VirtualAccount.user_id == u.id).all()
    assert len(rows) == 1
    assert len(fake.assigned) == 1  # second call returns existing, no re-assign
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/test_virtual_account_service.py -k "provision or split_full_name" -v`
Expected: FAIL with `ModuleNotFoundError: app.services.virtual_account_service`.

- [ ] **Step 3: Implement the service**

`app/services/virtual_account_service.py`:

```python
"""VirtualAccountService — provision a Paystack Dedicated Virtual Account.

Single-step assign. The BVN + bank account supplied at setup are passed
straight to Paystack and never persisted; we store only Paystack's durable
customer_code (obtained from create_customer, idempotent by email) plus the
issued account details written later by the assign webhook.

Provisioning is idempotent: an existing active/pending row is returned as-is
with no second Paystack call. A prior `failed`/`deactivated` row is reused and
reset to `pending_identity` on retry (mobile shows a Retry button).
"""
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logger import log
from app.db.models._enums import VirtualAccountStatus
from app.db.models.user import User
from app.db.models.virtual_account import VirtualAccount
from app.integrations.paystack.base import PaymentProvider

# Statuses where a DVA already exists and must NOT be re-provisioned.
_LIVE_STATUSES = frozenset(
    {
        VirtualAccountStatus.pending_identity,
        VirtualAccountStatus.pending_assign,
        VirtualAccountStatus.active,
    }
)


class KycRequired(Exception):
    """User's KYC tier is below the tier-1 gate for provisioning a DVA."""


def split_full_name(full_name: str) -> tuple[str, str, str]:
    """Split our single ``full_name`` into (first, middle, last).

    First token -> first_name, last token -> last_name, everything between ->
    middle_name (empty string for two tokens). A single token sets
    ``last_name = first_name`` as a fallback and logs a warning.
    """
    tokens = full_name.split()
    if not tokens:
        return ("", "", "")
    if len(tokens) == 1:
        log.warning("dva: single-token full_name %r; using it for last_name too", full_name)
        return (tokens[0], "", tokens[0])
    first = tokens[0]
    last = tokens[-1]
    middle = " ".join(tokens[1:-1])
    return (first, middle, last)


class VirtualAccountService:
    def __init__(self, *, db: Session, paystack: PaymentProvider) -> None:
        self._db = db
        self._paystack = paystack

    def get_for_user(self, *, user_id: UUID) -> VirtualAccount | None:
        return (
            self._db.query(VirtualAccount)
            .filter(VirtualAccount.user_id == user_id)
            .first()
        )

    async def provision(
        self,
        *,
        user: User,
        bvn: str,
        account_number: str,
        bank_code: str,
        preferred_bank: str | None = None,
    ) -> VirtualAccount:
        if user.kyc_level.numeric < 1:
            raise KycRequired()

        existing = self.get_for_user(user_id=user.id)
        if existing is not None and existing.status in _LIVE_STATUSES:
            return existing  # idempotent: no second Paystack call

        first, middle, last = split_full_name(user.full_name)

        customer = await self._paystack.create_customer(
            email=user.email, first_name=first, last_name=last, phone=user.phone,
        )

        if existing is not None:
            va = existing  # retry over a failed/deactivated row
            va.paystack_customer_code = customer.customer_code
            va.paystack_customer_id = customer.customer_id
            va.status = VirtualAccountStatus.pending_identity
            va.failure_reason = None
        else:
            va = VirtualAccount(
                user_id=user.id,
                paystack_customer_code=customer.customer_code,
                paystack_customer_id=customer.customer_id,
                status=VirtualAccountStatus.pending_identity,
                currency="NGN",
            )
            self._db.add(va)
        self._db.commit()

        # 202 — result arrives via customeridentification.* then
        # dedicatedaccount.assign.* webhooks. BVN is never persisted.
        await self._paystack.assign_dedicated_account(
            email=user.email,
            first_name=first,
            middle_name=middle,
            last_name=last,
            phone=user.phone,
            preferred_bank=preferred_bank or settings.PAYSTACK_DVA_PREFERRED_BANK,
            country="NG",
            account_number=account_number,
            bvn=bvn,
            bank_code=bank_code,
        )
        self._db.refresh(va)
        return va
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/test_virtual_account_service.py -v`
Expected: PASS (all tests including the model tests from Task 1).

- [ ] **Step 5: Commit**

```bash
git add app/services/virtual_account_service.py tests/services/test_virtual_account_service.py
git commit -m "feat: add VirtualAccountService provisioning with name split"
```

---

## Task 7: Endpoints POST/GET /wallet/virtual-account + GET /wallet/banks + DI

**Files:**
- Create: `app/schemas/virtual_account.py`
- Modify: `app/api/deps.py:390` (add `get_virtual_account_service` after `get_wallet_service`)
- Modify: `app/api/v1/endpoints/wallet.py` (imports + three routes)
- Test: `tests/api/test_virtual_account_endpoints.py` (Create)

**Interfaces:**
- Consumes: `VirtualAccountService`, `KycRequired` (Task 6); `get_paystack_provider`, `get_db`, `require_full_auth_gates` (existing); `PaymentProvider.list_banks` (Task 3).
- Produces:
  - `get_virtual_account_service(db, paystack) -> VirtualAccountService`.
  - Schemas: `ProvisionVirtualAccountRequest(bvn, account_number, bank_code, preferred_bank?)`, `VirtualAccountResponse(status, account_number, account_name, bank_name, failure_reason)`, `BankListItemResponse(name, slug, code)`.
  - Routes: `POST /api/v1/wallet/virtual-account`, `GET /api/v1/wallet/virtual-account`, `GET /api/v1/wallet/banks`.

- [ ] **Step 1: Write the failing test**

`tests/api/test_virtual_account_endpoints.py` (reuse the `client` fixture shape from Task 5's test file — copy it verbatim into this module):

```python
# ... (identical imports + `client` fixture + `_seed_logged_in_user` import as
#      tests/api/test_bills_spend_lock.py) ...
import pytest
from app.db.models.user import KycLevel, User


async def _promote(db_session, tier=KycLevel.tier_1):
    u = db_session.query(User).filter(User.email == "e@e.co").one()
    u.kyc_level = tier
    db_session.commit()
    return u


@pytest.mark.asyncio
async def test_provision_requires_kyc(db_session, client):
    _tokens, headers = await _seed_logged_in_user(client)  # tier_0 by default
    r = await client.post(
        "/api/v1/wallet/virtual-account",
        json={"bvn": "22222222222", "account_number": "0123456789", "bank_code": "035"},
        headers=headers,
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "KYC_REQUIRED"


@pytest.mark.asyncio
async def test_provision_returns_pending(db_session, client):
    _tokens, headers = await _seed_logged_in_user(client)
    await _promote(db_session)
    r = await client.post(
        "/api/v1/wallet/virtual-account",
        json={"bvn": "22222222222", "account_number": "0123456789", "bank_code": "035"},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "pending_identity"


@pytest.mark.asyncio
async def test_get_virtual_account_404_when_absent(db_session, client):
    _tokens, headers = await _seed_logged_in_user(client)
    r = await client.get("/api/v1/wallet/virtual-account", headers=headers)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NO_VIRTUAL_ACCOUNT"


@pytest.mark.asyncio
async def test_list_banks(db_session, client):
    _tokens, headers = await _seed_logged_in_user(client)
    r = await client.get("/api/v1/wallet/banks", headers=headers)
    assert r.status_code == 200
    slugs = [b["slug"] for b in r.json()["data"]]
    assert "test-bank" in slugs
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/api/test_virtual_account_endpoints.py -v`
Expected: FAIL — the routes 404 as unregistered (FastAPI has no matching path) or the schema import fails.

- [ ] **Step 3: Add the schemas**

`app/schemas/virtual_account.py`:

```python
from pydantic import BaseModel, Field


class ProvisionVirtualAccountRequest(BaseModel):
    bvn: str = Field(pattern=r"^\d{11}$")
    account_number: str = Field(pattern=r"^\d{10}$")
    bank_code: str = Field(min_length=3, max_length=6)
    preferred_bank: str | None = None


class VirtualAccountResponse(BaseModel):
    status: str
    account_number: str | None = None
    account_name: str | None = None
    bank_name: str | None = None
    failure_reason: str | None = None


class BankListItemResponse(BaseModel):
    name: str
    slug: str
    code: str
```

- [ ] **Step 4: Add the DI provider**

In `app/api/deps.py`, after `get_wallet_service` (line 393), add:

```python
from app.services.virtual_account_service import VirtualAccountService


def get_virtual_account_service(
    db: Session = Depends(get_db),
    paystack: PaymentProvider = Depends(get_paystack_provider),
) -> VirtualAccountService:
    return VirtualAccountService(db=db, paystack=paystack)
```

(`PaymentProvider` and `get_paystack_provider` are already imported in deps.py at the B6 block.)

- [ ] **Step 5: Add the routes**

In `app/api/v1/endpoints/wallet.py`, extend the deps import to add `get_virtual_account_service`, add the new imports, and append three routes:

```python
from app.api.deps import get_virtual_account_service  # add to existing import group
from app.schemas.virtual_account import (
    BankListItemResponse,
    ProvisionVirtualAccountRequest,
    VirtualAccountResponse,
)
from app.services.virtual_account_service import KycRequired, VirtualAccountService
```

Routes appended to the router:

```python
@router.post("/virtual-account", response_model=None, status_code=200)
@limiter.limit("10/minute", key_func=per_user_or_ip)
async def provision_virtual_account(
    request: Request,
    body: ProvisionVirtualAccountRequest,
    user: User = Depends(require_full_auth_gates),
    va_svc: VirtualAccountService = Depends(get_virtual_account_service),
):
    # Not a money-move: no X-Pin-Token. Standard bearer auth only.
    try:
        va = await va_svc.provision(
            user=user,
            bvn=body.bvn,
            account_number=body.account_number,
            bank_code=body.bank_code,
            preferred_bank=body.preferred_bank,
        )
    except KycRequired:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "KYC_REQUIRED",
                "message": "Complete KYC tier 1 before setting up an account number.",
            },
        )
    out = VirtualAccountResponse(
        status=va.status.value,
        account_number=va.account_number,
        account_name=va.account_name,
        bank_name=va.bank_name,
        failure_reason=va.failure_reason,
    )
    return success(
        out.model_dump(mode="json"),
        request_id=getattr(request.state, "request_id", None),
    )


@router.get("/virtual-account", response_model=None)
@limiter.limit("30/minute", key_func=per_user_or_ip)
async def get_virtual_account(
    request: Request,
    user: User = Depends(require_full_auth_gates),
    va_svc: VirtualAccountService = Depends(get_virtual_account_service),
):
    va = va_svc.get_for_user(user_id=user.id)
    if va is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "NO_VIRTUAL_ACCOUNT", "message": "No account number set up yet."},
        )
    out = VirtualAccountResponse(
        status=va.status.value,
        account_number=va.account_number,
        account_name=va.account_name,
        bank_name=va.bank_name,
        failure_reason=va.failure_reason,
    )
    return success(
        out.model_dump(mode="json"),
        request_id=getattr(request.state, "request_id", None),
    )


@router.get("/banks", response_model=None)
@limiter.limit("30/minute", key_func=per_user_or_ip)
async def list_banks(
    request: Request,
    user: User = Depends(require_full_auth_gates),
    paystack: PaymentProvider = Depends(get_paystack_provider),
):
    banks = await paystack.list_banks(country="nigeria")
    out = [
        BankListItemResponse(name=b.name, slug=b.slug, code=b.code).model_dump(mode="json")
        for b in banks
    ]
    return success(out, request_id=getattr(request.state, "request_id", None))
```

Note: `GET /banks` must be declared before any `GET /{something}` catch-all — the wallet router has no path parameter routes, so ordering with the two `virtual-account` routes is unambiguous.

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/api/test_virtual_account_endpoints.py -v`
Expected: PASS (4 tests).

- [ ] **Step 7: Commit**

```bash
git add app/schemas/virtual_account.py app/api/deps.py app/api/v1/endpoints/wallet.py tests/api/test_virtual_account_endpoints.py
git commit -m "feat: add DVA provision/get/banks wallet endpoints + DI"
```

---

## Task 8: Notifications — dva_ready / dva_failed events

**Files:**
- Modify: `app/services/notification_service.py` (`NotificationEvent`, `EVENT_CATEGORY`, `_EMAIL_TEMPLATES`, `_push_copy`, add `build_dva_context`)
- Test: `tests/services/test_dva_notifications.py` (Create)

**Interfaces:**
- Consumes: existing `NotificationEvent`, `EVENT_CATEGORY`, `_EMAIL_TEMPLATES`, `_push_copy`, `_PushCopy`.
- Produces:
  - `NotificationEvent.dva_ready = "dva_ready"`, `NotificationEvent.dva_failed = "dva_failed"`.
  - `build_dva_context(*, status: str, account_number: str | None = None, bank_name: str | None = None, reason: str | None = None) -> dict[str, Any]`.
  - Push copy for both events (no email template: both mapped to `None`).

- [ ] **Step 1: Write the failing test**

`tests/services/test_dva_notifications.py`:

```python
from app.services.notification_service import (
    EVENT_CATEGORY,
    NotificationCategory,
    NotificationEvent,
    _push_copy,
    build_dva_context,
)


def test_dva_events_categorised():
    assert EVENT_CATEGORY[NotificationEvent.dva_ready] == NotificationCategory.transaction_alerts
    assert EVENT_CATEGORY[NotificationEvent.dva_failed] == NotificationCategory.transaction_alerts


def test_dva_ready_push_copy_has_account_and_no_dashes():
    ctx = build_dva_context(status="active", account_number="9988776655", bank_name="Wema Bank")
    copy = _push_copy(NotificationEvent.dva_ready, ctx)
    assert copy is not None
    assert "9988776655" in copy.body
    assert "—" not in copy.body and "–" not in copy.body  # no em/en dash


def test_dva_failed_push_copy_carries_reason():
    ctx = build_dva_context(status="failed", reason="Name did not match BVN")
    copy = _push_copy(NotificationEvent.dva_failed, ctx)
    assert copy is not None
    assert "Name did not match BVN" in copy.body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/test_dva_notifications.py -v`
Expected: FAIL with `AttributeError: dva_ready` on `NotificationEvent`.

- [ ] **Step 3: Add the events and copy**

In `app/services/notification_service.py`:

Add to `NotificationEvent` (after `kyc_verification_failed`):

```python
    dva_ready  = "dva_ready"
    dva_failed = "dva_failed"
```

Add to `EVENT_CATEGORY`:

```python
    NotificationEvent.dva_ready:                   NotificationCategory.transaction_alerts,
    NotificationEvent.dva_failed:                  NotificationCategory.transaction_alerts,
```

Add to `_EMAIL_TEMPLATES` (push + in-app only per spec section 9; no email):

```python
    NotificationEvent.dva_ready:                   None,
    NotificationEvent.dva_failed:                  None,
```

Add to `_push_copy`, before the final `return None`:

```python
    if event is NotificationEvent.dva_ready:
        acct = ctx.get("account_number", "")
        bank = ctx.get("bank_name", "your bank")
        return _PushCopy(
            title="Your account number is ready",
            body=f"Transfer to {acct} ({bank}) to top up your wallet instantly.",
        )
    if event is NotificationEvent.dva_failed:
        reason = ctx.get("reason") or "We could not set up your account number."
        return _PushCopy(
            title="Account setup failed",
            body=f"{reason}. Please try again from the app.",
        )
```

Add the context builder near the other `build_*` helpers:

```python
def build_dva_context(
    *,
    status: str,
    account_number: str | None = None,
    bank_name: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Shape the push/in-app context for dva_ready / dva_failed. No em/en
    dashes in any string that reaches the user."""
    return {
        "status": status,
        "account_number": account_number or "",
        "bank_name": bank_name or "",
        "reason": reason or "",
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/test_dva_notifications.py -v`
Expected: PASS.

- [ ] **Step 5: Run the notification gating coverage test**

Run: `pytest tests/services -k "notification" -v`
Expected: PASS (the `EVENT_CATEGORY`-covers-every-event pin now includes the two new events).

- [ ] **Step 6: Commit**

```bash
git add app/services/notification_service.py tests/services/test_dva_notifications.py
git commit -m "feat: add dva_ready and dva_failed notification events"
```

---

## Task 9: Webhook — identity/assign lifecycle branches + reorder

**Files:**
- Modify: `app/api/v1/endpoints/webhooks.py` (imports; reorder the MALFORMED reference check; add lifecycle branches)
- Test: `tests/api/test_webhooks_dva.py` (Create)

**Interfaces:**
- Consumes: `VirtualAccount`, `VirtualAccountStatus` (Task 1); `NotificationEvent.dva_ready/dva_failed`, `build_dva_context` (Task 8); `WebhookEvent` dedupe (existing); `dispatch_delay` (existing).
- Produces (behaviour): `customeridentification.success` -> `pending_assign`; `customeridentification.failed` -> `failed` + reason + `dva_failed`; `dedicatedaccount.assign.success` -> store account details + `active` + `dva_ready`; `dedicatedaccount.assign.failed` -> `failed` + reason + `dva_failed`. All evaluated before the reference-mandatory `400 MALFORMED_WEBHOOK` check.

- [ ] **Step 1: Write the failing test**

`tests/api/test_webhooks_dva.py` (reuse the `client` fixture shape from `tests/api/test_webhooks_paystack.py` — copy that fixture verbatim):

```python
# ... (identical imports + `client` fixture + `_seed_logged_in_user` import as
#      tests/api/test_webhooks_paystack.py) ...
import json
import pytest
from app.db.models._enums import VirtualAccountStatus
from app.db.models.user import User
from app.db.models.virtual_account import VirtualAccount
from app.integrations.paystack.fake import (
    customer_identification_event,
    dedicated_account_assign_event,
)


async def _seed_va(db_session, client, *, status=VirtualAccountStatus.pending_identity):
    _tokens, headers = await _seed_logged_in_user(client)
    user = db_session.query(User).filter(User.email == "e@e.co").one()
    va = VirtualAccount(
        user_id=user.id, paystack_customer_code="CUS_hook_1",
        status=status, currency="NGN",
    )
    db_session.add(va)
    db_session.commit()
    return user, headers, va


async def _post(client, body):
    return await client.post(
        "/api/v1/webhooks/paystack",
        content=json.dumps(body).encode(),
        headers={"x-paystack-signature": "FAKE_SIG"},
    )


@pytest.mark.asyncio
async def test_identification_success_moves_to_pending_assign(db_session, client):
    _u, _h, va = await _seed_va(db_session, client)
    r = await _post(client, customer_identification_event(customer_code="CUS_hook_1", success=True, event_id="ci_1"))
    assert r.status_code == 200
    db_session.expire_all()
    assert db_session.query(VirtualAccount).filter(VirtualAccount.id == va.id).one().status == VirtualAccountStatus.pending_assign


@pytest.mark.asyncio
async def test_identification_failed_sets_failed_reason(db_session, client):
    _u, _h, va = await _seed_va(db_session, client)
    r = await _post(client, customer_identification_event(
        customer_code="CUS_hook_1", success=False, reason="BVN mismatch", event_id="ci_2"))
    assert r.status_code == 200
    db_session.expire_all()
    row = db_session.query(VirtualAccount).filter(VirtualAccount.id == va.id).one()
    assert row.status == VirtualAccountStatus.failed
    assert row.failure_reason == "BVN mismatch"


@pytest.mark.asyncio
async def test_assign_success_stores_account_and_activates(db_session, client):
    _u, _h, va = await _seed_va(db_session, client, status=VirtualAccountStatus.pending_assign)
    r = await _post(client, dedicated_account_assign_event(
        customer_code="CUS_hook_1", account_number="9988776655",
        account_name="TEST USER", bank_name="Wema Bank", bank_slug="wema-bank",
        event_id="da_1"))
    assert r.status_code == 200
    db_session.expire_all()
    row = db_session.query(VirtualAccount).filter(VirtualAccount.id == va.id).one()
    assert row.status == VirtualAccountStatus.active
    assert row.account_number == "9988776655"
    assert row.bank_slug == "wema-bank"


@pytest.mark.asyncio
async def test_unknown_customer_code_is_200_noop(db_session, client):
    await _seed_logged_in_user(client)
    r = await _post(client, customer_identification_event(customer_code="CUS_unknown", event_id="ci_x"))
    assert r.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/api/test_webhooks_dva.py -v`
Expected: FAIL — identity events currently hit the `400 MALFORMED_WEBHOOK` check (no `data.reference`), returning 400 not 200.

- [ ] **Step 3: Add imports**

In `app/api/v1/endpoints/webhooks.py`, extend the imports:

```python
from app.db.models._enums import (
    TransactionStatus,
    TransactionType,
    VirtualAccountStatus,
)
from app.db.models.virtual_account import VirtualAccount
from app.services.notification_service import (
    NotificationEvent,
    build_dva_context,
    build_wallet_funded_context,
)
```

- [ ] **Step 4: Reorder + add lifecycle branches**

In `paystack_webhook`, restructure the top of the body. The current guard is:

```python
    if not event_id or not reference:
        raise HTTPException(status_code=400, detail={
            "code": "MALFORMED_WEBHOOK", "message": "Missing data.id or data.reference"
        })
```

Replace it so only `event_id` is mandatory up front (needed for dedupe of every event, including DVA), and move the dedupe insert above the reference check. The new order is:

1. Keep the `event_id`-only guard:

```python
    if not event_id:
        raise HTTPException(status_code=400, detail={
            "code": "MALFORMED_WEBHOOK", "message": "Missing data.id"
        })
```

2. Keep the existing `WebhookEvent` insert + `db.flush()` / `IntegrityError` dedupe block exactly as-is (it uses `event_id` only).

3. Immediately after the dedupe block, insert the DVA lifecycle handling (before the `payment = db.query(...)` lookup):

```python
    data = payload.get("data", {})

    # ── DVA identity + assign lifecycle (resolved by customer_code) ──────
    # These events carry no reference we minted, so they are handled BEFORE
    # the reference-mandatory MALFORMED check below.
    if event_type in (
        "customeridentification.success",
        "customeridentification.failed",
        "dedicatedaccount.assign.success",
        "dedicatedaccount.assign.failed",
    ):
        customer_code = (
            data.get("customer_code")
            or (data.get("customer") or {}).get("customer_code")
        )
        va = (
            db.query(VirtualAccount)
            .filter(VirtualAccount.paystack_customer_code == customer_code)
            .first()
        )
        if va is None:
            log.warning(
                "paystack webhook: DVA event for unknown customer_code=%s event=%s",
                customer_code, event_id,
            )
            we.processed = True
            db.commit()
            return success({"ok": True, "note": "unknown_customer"})

        notify_event: NotificationEvent | None = None
        notify_ctx: dict | None = None

        if event_type == "customeridentification.success":
            va.status = VirtualAccountStatus.pending_assign
        elif event_type == "customeridentification.failed":
            va.status = VirtualAccountStatus.failed
            va.failure_reason = data.get("reason") or "Identity verification failed"
            notify_event = NotificationEvent.dva_failed
            notify_ctx = build_dva_context(status="failed", reason=va.failure_reason)
        elif event_type == "dedicatedaccount.assign.success":
            acct = data.get("dedicated_account") or {}
            bank = acct.get("bank") or {}
            va.account_number = acct.get("account_number")
            va.account_name = acct.get("account_name")
            va.bank_name = bank.get("name")
            va.bank_slug = bank.get("slug")
            va.dedicated_account_id = str(acct.get("id") or "") or None
            va.status = VirtualAccountStatus.active
            va.failure_reason = None
            notify_event = NotificationEvent.dva_ready
            notify_ctx = build_dva_context(
                status="active",
                account_number=va.account_number,
                bank_name=va.bank_name,
            )
        else:  # dedicatedaccount.assign.failed
            va.status = VirtualAccountStatus.failed
            va.failure_reason = data.get("reason") or "Account assignment failed"
            notify_event = NotificationEvent.dva_failed
            notify_ctx = build_dva_context(status="failed", reason=va.failure_reason)

        we.processed = True
        db.commit()

        if notify_event is not None:
            from app.db.models.user import User  # local import, mirrors existing style
            user = db.query(User).filter(User.id == va.user_id).first()
            if user is not None:
                dispatch_delay(
                    user_id=str(va.user_id),
                    user_email=user.email,
                    event=notify_event,
                    context=notify_ctx,
                )
        return success({"ok": True})
```

4. Only after the DVA lifecycle block (and the DVA funding block from Task 10) do we reach the reference-mandatory check. Add it just before the existing `payment = db.query(Payment)...` lookup:

```python
    if not reference:
        raise HTTPException(status_code=400, detail={
            "code": "MALFORMED_WEBHOOK", "message": "Missing data.reference"
        })
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/api/test_webhooks_dva.py -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Run the existing Paystack webhook suite for regressions**

Run: `pytest tests/api/test_webhooks_paystack.py -v`
Expected: PASS (the reordered checks preserve the reference-first checkout flow; `test_rejects_bad_signature` and all charge tests still pass).

- [ ] **Step 7: Commit**

```bash
git add app/api/v1/endpoints/webhooks.py tests/api/test_webhooks_dva.py
git commit -m "feat: handle Paystack DVA identity/assign lifecycle webhooks"
```

---

## Task 10: Webhook — charge.success dedicated_nuban funding

**Files:**
- Modify: `app/api/v1/endpoints/webhooks.py` (add the funding branch after the lifecycle branch; imports for `TransactionService`, `OverCapPolicy`)
- Test: `tests/api/test_webhooks_dva.py` (append funding tests)

**Interfaces:**
- Consumes: `VirtualAccount` (Task 1); `WalletService.credit(over_cap=OverCapPolicy.LOCK)` (Task 4); `TransactionService.create/transition` (existing); `build_wallet_funded_context(channel=...)` (existing); `NotificationEvent.wallet_funded` (existing); fixture `dva_charge_event` (Task 3).
- Produces (behaviour): `charge.success` with `data.channel == "dedicated_nuban"` resolves the DVA by `receiver_bank_account_number`, synthesizes a `wallet_funding` transaction, credits gross under `OverCapPolicy.LOCK`, transitions the tx to success, and dispatches `wallet_funded` with `channel="transfer"`. Unknown account -> `200 {"status": "unknown_account"}`.

- [ ] **Step 1: Write the failing test**

Append to `tests/api/test_webhooks_dva.py`:

```python
from decimal import Decimal
from app.db.models.wallet import Wallet
from app.db.models._enums import SpendLockReason
from app.integrations.paystack.fake import dva_charge_event


@pytest.mark.asyncio
async def test_dva_transfer_credits_wallet_gross(db_session, client):
    user, headers, va = await _seed_va(db_session, client, status=VirtualAccountStatus.active)
    va.account_number = "9988776655"
    db_session.commit()

    r = await _post(client, dva_charge_event(
        account_number="9988776655", amount_kobo=500000, event_id="dva_credit_1"))
    assert r.status_code == 200

    w = await client.get("/api/v1/wallet", headers=headers)
    assert w.json()["data"]["balance"] == "5000.00"  # gross, fee absorbed


@pytest.mark.asyncio
async def test_dva_transfer_unknown_account_is_noop(db_session, client):
    await _seed_logged_in_user(client)
    r = await _post(client, dva_charge_event(
        account_number="0000000000", amount_kobo=500000, event_id="dva_unknown_1"))
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "unknown_account"


@pytest.mark.asyncio
async def test_dva_transfer_over_cap_credits_full_and_locks(db_session, client):
    user, headers, va = await _seed_va(db_session, client, status=VirtualAccountStatus.active)
    va.account_number = "9988776655"
    db_session.commit()
    # tier_0 cap = 50,000. A 70,000 transfer overshoots -> credit full + lock.
    r = await _post(client, dva_charge_event(
        account_number="9988776655", amount_kobo=7000000, event_id="dva_overcap_1"))
    assert r.status_code == 200
    db_session.expire_all()
    w = db_session.query(Wallet).filter(Wallet.user_id == user.id).one()
    assert w.balance == Decimal("70000.00")
    assert w.spend_locked is True
    assert w.spend_locked_reason == SpendLockReason.over_cap


@pytest.mark.asyncio
async def test_dva_transfer_replayed_event_is_deduped(db_session, client):
    user, headers, va = await _seed_va(db_session, client, status=VirtualAccountStatus.active)
    va.account_number = "9988776655"
    db_session.commit()
    ev = dva_charge_event(account_number="9988776655", amount_kobo=500000, event_id="dva_dup_1")
    await _post(client, ev)
    await _post(client, ev)  # replay
    db_session.expire_all()
    w = db_session.query(Wallet).filter(Wallet.user_id == user.id).one()
    assert w.balance == Decimal("5000.00")  # credited exactly once
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/api/test_webhooks_dva.py -k "dva_transfer" -v`
Expected: FAIL — `charge.success` with a `dedicated_nuban` channel currently falls through to the reference lookup and returns `unknown_reference` (no credit).

- [ ] **Step 3: Extend imports**

In `app/api/v1/endpoints/webhooks.py`, ensure these are imported:

```python
from decimal import Decimal
from app.services.wallet_service import KycCapExceeded, OverCapPolicy, WalletService
```

(`TransactionService` is already imported; `WalletService` is already imported — add `OverCapPolicy` to that line.)

- [ ] **Step 4: Add the funding branch**

In `paystack_webhook`, immediately after the DVA lifecycle branch (still before the reference-mandatory check), add:

```python
    # ── DVA inbound transfer funding (resolved by receiver account) ──────
    authorization = data.get("authorization") or {}
    if event_type == "charge.success" and authorization.get("channel") == "dedicated_nuban":
        acct = authorization.get("receiver_bank_account_number")
        va = (
            db.query(VirtualAccount)
            .filter(VirtualAccount.account_number == acct)
            .first()
        )
        if va is None:
            log.warning(
                "paystack webhook: dedicated_nuban transfer to unknown account=%s event=%s",
                acct, event_id,
            )
            we.processed = True
            db.commit()
            return success({"status": "unknown_account"})

        # kobo -> naira, GROSS (Timpbills absorbs the DVA fee).
        amount = Decimal(data.get("amount", 0)) / Decimal(100)

        tx_svc = TransactionService(db=db)
        tx = tx_svc.create(
            user_id=va.user_id,
            type=TransactionType.wallet_funding,
            amount=amount,
            meta={
                "funding_channel": "dedicated_nuban",
                "paystack_event_id": event_id,
                "sender_name": authorization.get("sender_name"),
                "sender_bank": authorization.get("sender_bank"),
                "sender_account_masked": authorization.get("sender_bank_account_number"),
                "paystack_fee": data.get("fees"),
            },
        )
        # LOCK policy: landed money is never rejected. Over-cap credits in full
        # and locks outbound spend until the next KYC upgrade covers it.
        new_balance = wallet_svc.credit(
            user_id=va.user_id, amount=amount, over_cap=OverCapPolicy.LOCK,
        )
        tx_svc.transition(
            tx,
            to_status=TransactionStatus.success,
            reason="paystack.webhook.dedicated_nuban",
            context={"paystack_event_id": event_id},
        )

        we.processed = True
        db.commit()

        from app.db.models.user import User
        user = db.query(User).filter(User.id == va.user_id).first()
        if user is not None:
            dispatch_delay(
                user_id=str(va.user_id),
                user_email=user.email,
                event=NotificationEvent.wallet_funded,
                context=build_wallet_funded_context(
                    amount=amount,
                    balance=new_balance,
                    reference=tx.reference,
                    channel="transfer",
                ),
            )
        return success({"ok": True})
```

Note: there is no `_claim_payment` step and no `paystack.verify` call — a DVA inflow has no pre-existing `Payment` row, and the `WebhookEvent` unique insert (already flushed above) is the idempotency guard. The synthesized tx is created only after that insert succeeds, so a replayed `data.id` short-circuits at the dedupe block and never re-credits.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/api/test_webhooks_dva.py -v`
Expected: PASS (all lifecycle + funding tests).

- [ ] **Step 6: Run the full webhook + wallet suites for regressions**

Run: `pytest tests/api/test_webhooks_paystack.py tests/api/test_webhooks_vtpass.py tests/api/test_wallet_fund.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/api/v1/endpoints/webhooks.py tests/api/test_webhooks_dva.py
git commit -m "feat: credit wallet on dedicated_nuban transfer webhook with over-cap lock"
```

---

## Task 11: KYC unlock hook in confirm_verification

**Files:**
- Modify: `app/services/kyc_service.py:257-271` (add unlock after the tier upgrade commit)
- Test: `tests/services/test_kyc_spend_unlock.py` (Create)

**Interfaces:**
- Consumes: `WalletService.clear_spend_lock_if_within_cap(*, user_id) -> bool` (Task 4); `confirm_verification` success path (existing).
- Produces (behaviour): after a KYC pass that upgrades the user's tier, an over-cap spend-lock is cleared iff the new cap covers the balance; otherwise left locked.

- [ ] **Step 1: Write the failing test**

`tests/services/test_kyc_spend_unlock.py`:

```python
from decimal import Decimal
from uuid import uuid4

import pytest

from app.db.models._enums import SpendLockReason
from app.db.models.kyc_record import KycRecord
from app.db.models.user import KycLevel, User
from app.db.models.wallet import Wallet
from app.services.kyc_service import KycService
from app.services.wallet_service import OverCapPolicy, WalletService


def _seed_user(db, kyc=KycLevel.tier_1) -> User:
    u = User(
        email=f"{uuid4().hex[:8]}@t.co",
        phone=f"+23480{uuid4().int % 10**9:09d}",
        full_name="T U", password_hash="x", kyc_level=kyc, email_verified=True,
        date_of_birth=None,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.mark.asyncio
async def test_bvn_pass_clears_spend_lock_when_new_cap_covers_balance(db_session, monkeypatch):
    # User at tier_1 (cap 300,000). Lock the wallet with a 400,000 balance
    # (over the tier_1 cap). A BVN pass upgrades to tier_2 (cap 500,000),
    # which now covers 400,000 -> lock clears.
    u = _seed_user(db_session, kyc=KycLevel.tier_1)
    wsvc = WalletService(db=db_session)
    wsvc.get_or_create(user_id=u.id)
    wsvc.credit(user_id=u.id, amount=Decimal("400000.00"), over_cap=OverCapPolicy.LOCK)

    ref = "KYC-BVN-locktest"
    db_session.add(KycRecord(
        user_id=u.id, verification_type="bvn", provider="dojah",
        provider_reference=ref, status="pending", tier_before=1, masked_id=None,
    ))
    db_session.commit()

    # Stub the Dojah provider to return a clean pass for this reference.
    from app.integrations.dojah.schemas import KycVerificationResult

    class _FakeProvider:
        async def fetch_verification(self, *, reference_id):
            return KycVerificationResult(
                verification_type="bvn", status="success", id_verified=True,
                liveness_passed=True, face_match=True, face_match_confidence=99,
                masked_id="12", provider_reference=reference_id, identity_dob=None,
            )

    import app.services.kyc_service as kyc_mod
    monkeypatch.setattr(kyc_mod, "get_kyc_provider", lambda: _FakeProvider())

    svc = KycService(db=db_session)
    await svc.confirm_verification(reference_id=ref)

    db_session.expire_all()
    w = db_session.query(Wallet).filter(Wallet.user_id == u.id).one()
    assert w.spend_locked is False
    assert w.spend_locked_reason is None
```

Note: `KycVerificationResult` fields are confirmed against `app/integrations/dojah/schemas.py` — required fields are `verification_type, status, id_verified, liveness_passed, face_match, face_match_confidence, masked_id, provider_reference`; `identity_name/identity_dob/failure_reason` are optional.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/test_kyc_spend_unlock.py -v`
Expected: FAIL — the lock is not cleared (assert `w.spend_locked is False` fails; it stays True).

- [ ] **Step 3: Add the unlock hook**

In `app/services/kyc_service.py`, in the success branch of `confirm_verification`, capture whether an upgrade actually happens and clear the lock after commit. Replace the block:

```python
        next_tier = _TIER_AFTER_PASS[record.verification_type]
        record.status = "success"
        record.tier_after = next_tier.numeric
        record.failure_reason = None
        if user is not None and next_tier.numeric > user.kyc_level.numeric:
            user.kyc_level = next_tier
        self._db.commit()
        if was_pending and user is not None:
            _notify_kyc_verification_result(user=user, record=record)
        return record
```

with:

```python
        next_tier = _TIER_AFTER_PASS[record.verification_type]
        record.status = "success"
        record.tier_after = next_tier.numeric
        record.failure_reason = None
        upgraded = user is not None and next_tier.numeric > user.kyc_level.numeric
        if upgraded:
            user.kyc_level = next_tier
        self._db.commit()
        # Over-cap spend-lock unlock (DVA): a tier upgrade may now cover a
        # balance that landed over the old cap. Clear the lock iff the new
        # cap covers the balance; leave it locked otherwise. Never mutate the
        # wallet outside WalletService.
        if upgraded and user is not None:
            from app.services.wallet_service import WalletService  # noqa: PLC0415
            WalletService(db=self._db).clear_spend_lock_if_within_cap(user_id=user.id)
        if was_pending and user is not None:
            _notify_kyc_verification_result(user=user, record=record)
        return record
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/test_kyc_spend_unlock.py -v`
Expected: PASS.

- [ ] **Step 5: Run the KYC service suite for regressions**

Run: `pytest tests/services -k "kyc" -v`
Expected: PASS (the unlock is gated on `upgraded`, so non-upgrading confirms are untouched).

- [ ] **Step 6: Commit**

```bash
git add app/services/kyc_service.py tests/services/test_kyc_spend_unlock.py
git commit -m "feat: clear wallet spend-lock on KYC tier upgrade"
```

---

## Final verification gate (run after all tasks)

- [ ] **Full suite green:** `pytest`
- [ ] **Lint clean:** `ruff check app tests`
- [ ] **Types clean (where used):** `mypy app/services/virtual_account_service.py app/services/wallet_service.py`
- [ ] **Migration round-trip:** `alembic upgrade head && alembic downgrade -1 && alembic upgrade head`
- [ ] **Whole-feature review** (Opus) for cross-cutting concerns: webhook branch ordering (DVA before reference-mandatory), transaction boundaries in the funding branch (dedupe insert flushed before the synth tx), async correctness (no sync DB call blocks in an async route beyond the established pattern), error-envelope consistency (all new 4xx/423 use `{code, message}`), and BVN never persisted or logged (grep `bvn` across the diff; it must appear only as a request field and the outbound Paystack call param).
</content>
</invoke>
