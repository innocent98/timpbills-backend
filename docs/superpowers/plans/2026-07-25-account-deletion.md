# Public Account Deletion — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A logged-out user can request account deletion from a public marketing page with email/phone + password; personal data is erased after a 30-day reversible grace, the financial ledger is retained anonymized.

**Architecture:** One `AccountDeletionService` is the single entry point that starts a deletion (sets the existing `deleted_at` tombstone, revokes tokens, notifies). Both a new public endpoint and the existing authenticated `DELETE /users/me` call it. A daily Celery sweep anonymizes PII on accounts soft-deleted ≥30 days ago while keeping the ledger. A marketing page + form drives the public flow.

**Tech Stack:** FastAPI, sync SQLAlchemy, Pydantic v2, Alembic, Celery+Redis, argon2 (passlib), slowapi; Next.js 16 App Router (marketing).

Spec: `docs/superpowers/specs/2026-07-25-account-deletion-design.md`

## Global Constraints

- No em/en dashes in any user-facing copy (API messages, notification/email templates, marketing page). Use commas, colons, or full stops.
- No `Co-Authored-By` or AI-attribution trailer in any commit message.
- Money is `NUMERIC(14,2)` naira; wallet mutations go through `WalletService` with `SELECT … FOR UPDATE`.
- Error envelope: services raise `ValueError("CODE")`; endpoints map via a local `_ERROR_MAP`/`_raise` to `HTTPException(status_code, detail={"code","message"})`. Success via `success(data, request_id=...)`.
- Public endpoints take `request: Request` and only `Depends(...)` services (no `get_current_user`); rate-limit with `@limiter.limit(...)`.
- Password verify uses `verify_password_async(pw, hash)`; identity lookup is email-OR-phone with `normalize_to_e164` (mirror `auth_service.py:1189`).
- Anonymization must KEEP `wallet`, `transactions`, `virtual_accounts`, `wallet_credit_keys` (AML retention) and only detach identity.
- Current Alembic head: `202607221300` (down_revision for the new migration).

## File Structure

**Backend (`timpbills-backend`):**
- `app/db/models/user.py` — add `anonymized_at` column (Task 1)
- `alembic/versions/202607251200_add_user_anonymized_at.py` — migration (Task 1)
- `app/services/notification_service.py` — new event + category + email/push copy (Task 2)
- `app/services/account_deletion_service.py` — new service (Task 3)
- `app/schemas/account.py` — request/response schemas (Task 3)
- `app/api/v1/endpoints/account.py` — public endpoints (Task 4)
- `app/api/v1/api.py` — include the router (Task 4)
- `app/api/v1/endpoints/users.py` — refactor `soft_delete_me` (Task 5)
- `app/workers/tasks/account_tasks.py` — anonymization sweep (Task 6)
- `app/workers/celery_app.py` — beat schedule entry (Task 6)
- `app/core/config.py` — CORS origins doc (Task 7)
- Tests under `tests/services/`, `tests/api/`, `tests/workers/`

**Marketing (`timpbills-marketing`):**
- `lib/public-api.ts` — public fetch wrapper + base URL (Task 8)
- `.env.local.example` — `NEXT_PUBLIC_API_BASE` (Task 8)
- `app/components/DeleteAccountForm.tsx` — client form (Task 9)
- `app/(marketing)/delete-account/page.tsx` — page (Task 9)

---

### Task 1: Add `anonymized_at` to the user model

**Files:**
- Modify: `app/db/models/user.py` (near `deleted_at` at `:101`)
- Create: `alembic/versions/202607251200_add_user_anonymized_at.py`
- Test: `tests/db/test_user_anonymized_at.py`

**Interfaces:**
- Produces: `User.anonymized_at: Mapped[datetime | None]` — null until the sweep scrubs PII; distinguishes "soft-deleted, in grace" from "anonymized".

- [ ] **Step 1: Write the failing test**

```python
# tests/db/test_user_anonymized_at.py
import uuid
from app.db.models.user import User


def test_user_has_nullable_anonymized_at(db_session):
    u = User(
        id=uuid.uuid4(), email="a@b.co", phone="+2348100000001",
        full_name="A B", password_hash="x", is_active=True,
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    assert u.anonymized_at is None
    # column is settable
    from datetime import UTC, datetime
    u.anonymized_at = datetime.now(UTC)
    db_session.commit()
    assert u.anonymized_at is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `poetry run pytest tests/db/test_user_anonymized_at.py -q -p no:cacheprovider --no-cov`
Expected: FAIL — `AttributeError`/`InvalidRequestError`: no attribute/column `anonymized_at`.

- [ ] **Step 3: Add the column**

In `app/db/models/user.py`, immediately after the `deleted_at` column:

```python
    # Set by the anonymization sweep once PII has been scrubbed (30 days
    # after deleted_at). While deleted_at is set but this is NULL the
    # account is in the reversible grace window; once this is set the row
    # carries no personal data and only the retained ledger remains.
    anonymized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
```

(Match the existing `deleted_at` column's typing style in this file — if it uses `Column(...)` rather than `mapped_column`, mirror that.)

- [ ] **Step 4: Create the migration**

```python
# alembic/versions/202607251200_add_user_anonymized_at.py
"""add users.anonymized_at (account-deletion PII purge marker)

Revision ID: 202607251200
Revises: 202607221300
Create Date: 2026-07-25 12:00:00
"""
import sqlalchemy as sa

from alembic import op

revision = "202607251200"
down_revision = "202607221300"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("anonymized_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "anonymized_at")
```

- [ ] **Step 5: Run test to verify it passes + migration is linear**

Run: `poetry run pytest tests/db/test_user_anonymized_at.py -q -p no:cacheprovider --no-cov`
Expected: PASS.
Run: `poetry run alembic heads`
Expected: single head `202607251200`.

- [ ] **Step 6: Commit**

```bash
git add app/db/models/user.py alembic/versions/202607251200_add_user_anonymized_at.py tests/db/test_user_anonymized_at.py
git commit -m "feat(db): add users.anonymized_at for account-deletion PII purge"
```

---

### Task 2: Notification event for a deletion request

**Files:**
- Modify: `app/services/notification_service.py` (enum `:48`, `EVENT_CATEGORY` `:107`, email templates `~:202`, push render `~:219`)
- Test: `tests/services/test_notification_account_deletion.py`

**Interfaces:**
- Produces: `NotificationEvent.account_deletion_requested` (category `transaction_alerts`) with an email template and push copy. Context keys: `{"scheduled_date": str, "cancel_url": str}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_notification_account_deletion.py
from app.services.notification_service import (
    NotificationEvent, EVENT_CATEGORY, NotificationCategory,
)


def test_account_deletion_event_registered_and_categorised():
    evt = NotificationEvent.account_deletion_requested
    assert evt.value == "account_deletion_requested"
    assert EVENT_CATEGORY[evt] == NotificationCategory.transaction_alerts
```

Add a second test asserting the rendered email/push copy contains the scheduled date and no dash characters:

```python
def test_account_deletion_copy_has_no_dashes():
    from app.services.notification_service import NotificationService
    # Render via whatever internal helper the service exposes for a given
    # event+context (mirror how existing tests render, e.g. dva copy tests).
    ctx = {"scheduled_date": "25 August 2026", "cancel_url": "https://timpbills.com/delete-account"}
    text = NotificationService.render_push(NotificationEvent.account_deletion_requested, ctx)  # adapt to real helper
    assert "25 August 2026" in text
    assert "—" not in text and "–" not in text
```

(Adapt the render call to the service's actual internal API — inspect how `dva_ready`/`dva_failed` copy is unit-tested and follow that exact pattern. If there is no public render helper, assert on the template dict entries directly.)

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/services/test_notification_account_deletion.py -q -p no:cacheprovider --no-cov`
Expected: FAIL — `AttributeError: account_deletion_requested`.

- [ ] **Step 3: Add the enum member**

In `NotificationEvent` (after `dva_failed`):

```python
    # Public account-deletion request confirmation. Email is the primary
    # channel (the person may have uninstalled the app, which is why they
    # used the web page); push is best-effort. SMS stays reserved per
    # project convention. Copy states the scheduled deletion date and the
    # single cancel path (return to the deletion page).
    account_deletion_requested      = "account_deletion_requested"
```

- [ ] **Step 4: Categorise it**

In `EVENT_CATEGORY`, add:

```python
    NotificationEvent.account_deletion_requested: NotificationCategory.transaction_alerts,
```

- [ ] **Step 5: Add email + push copy**

Add an email template (subject + body) and push copy following the file's existing template structures. Copy (no dashes):

- Subject: `Your Timpbills account deletion request`
- Body (email): `We received a request to delete your Timpbills account. Your account is now scheduled for permanent deletion on {scheduled_date}. If you did not make this request, go to {cancel_url} and choose "Cancel a pending deletion" before that date to keep your account.`
- Push: `Your account is scheduled for deletion on {scheduled_date}. Tap to cancel if this was not you.`

- [ ] **Step 6: Run to verify pass**

Run: `poetry run pytest tests/services/test_notification_account_deletion.py -q -p no:cacheprovider --no-cov`
Expected: PASS.
Run: `poetry run pytest tests/services/test_notification_gating.py -q -p no:cacheprovider --no-cov`
Expected: PASS (the coverage-pinning test now sees the new event categorised).

- [ ] **Step 7: Commit**

```bash
git add app/services/notification_service.py tests/services/test_notification_account_deletion.py
git commit -m "feat(notify): account_deletion_requested event + email/push copy"
```

---

### Task 3: `AccountDeletionService` + schemas

**Files:**
- Create: `app/services/account_deletion_service.py`
- Create: `app/schemas/account.py`
- Test: `tests/services/test_account_deletion_service.py`

**Interfaces:**
- Consumes: `verify_password_async` (`app/core/security.py`), `normalize_to_e164`/`InvalidPhoneFormat` (`app/utils/phone`), `NotificationEvent.account_deletion_requested` (Task 2), `dispatch_delay` (`app/workers/tasks/notification_tasks.py`), `TokenStore.revoke_all`, `Wallet` model.
- Produces:
  - `class AccountDeletionService(db: Session, token_store: TokenStore)`
  - `async resolve_and_verify(*, identifier: str, password: str) -> User` — raises `ValueError("INVALID_CREDENTIALS")` on any failure; returns the user even if `is_active=False`.
  - `async request_deletion(*, user: User) -> datetime` — raises `ValueError("WALLET_NOT_EMPTY")`; returns scheduled deletion datetime (`deleted_at + 30d`).
  - `cancel_deletion(*, user: User) -> None` — raises `ValueError("ALREADY_ANONYMIZED")`.
  - `GRACE_DAYS = 30` module constant.
  - Schemas: `AccountDeletionRequest { identifier: str, password: str }`, `AccountDeletionResponse { scheduled_deletion_at: datetime }`, `CancelDeletionResponse { cancelled: bool }`.

- [ ] **Step 1: Write failing tests**

```python
# tests/services/test_account_deletion_service.py
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.db.models.user import User
from app.db.models.wallet import Wallet
from app.core.security import hash_password
from app.services.account_deletion_service import AccountDeletionService, GRACE_DAYS


class _FakeTokenStore:
    def __init__(self): self.revoked = []
    async def revoke_all(self, *, user_id): self.revoked.append(user_id)


def _user(db, *, pw="Secret123", email="u@e.co", phone="+2348100000009", balance="0.00"):
    u = User(id=uuid.uuid4(), email=email, phone=phone, full_name="U",
             password_hash=hash_password(pw), is_active=True)
    db.add(u); db.flush()
    db.add(Wallet(id=uuid.uuid4(), user_id=u.id, balance=Decimal(balance),
                  balance_cap=Decimal("50000.00")))
    db.commit()
    return u


@pytest.mark.asyncio
async def test_resolve_by_email_and_phone(db_session):
    u = _user(db_session, email="a@b.co", phone="+2348100000001")
    svc = AccountDeletionService(db=db_session, token_store=_FakeTokenStore())
    assert (await svc.resolve_and_verify(identifier="a@b.co", password="Secret123")).id == u.id
    # phone in local format normalises
    assert (await svc.resolve_and_verify(identifier="08100000001", password="Secret123")).id == u.id


@pytest.mark.asyncio
async def test_resolve_bad_password_and_unknown_are_same_error(db_session):
    _user(db_session, email="a@b.co")
    svc = AccountDeletionService(db=db_session, token_store=_FakeTokenStore())
    with pytest.raises(ValueError, match="INVALID_CREDENTIALS"):
        await svc.resolve_and_verify(identifier="a@b.co", password="wrong")
    with pytest.raises(ValueError, match="INVALID_CREDENTIALS"):
        await svc.resolve_and_verify(identifier="nobody@x.co", password="whatever")


@pytest.mark.asyncio
async def test_request_deletion_sets_tombstone_and_returns_plus_30d(db_session, monkeypatch):
    dispatched = {}
    import app.services.account_deletion_service as mod
    monkeypatch.setattr(mod, "dispatch_delay", lambda **kw: dispatched.update(kw))
    u = _user(db_session)
    ts = _FakeTokenStore()
    svc = AccountDeletionService(db=db_session, token_store=ts)
    before = datetime.now(UTC)
    sched = await svc.request_deletion(user=u)
    db_session.refresh(u)
    assert u.is_active is False and u.deleted_at is not None and u.tokens_revoked_at is not None
    assert timedelta(days=GRACE_DAYS) - timedelta(minutes=1) <= (sched - u.deleted_at) <= timedelta(days=GRACE_DAYS) + timedelta(minutes=1)
    assert str(u.id) in ts.revoked
    assert dispatched["event"].value == "account_deletion_requested"


@pytest.mark.asyncio
async def test_request_deletion_blocks_on_nonzero_balance(db_session):
    u = _user(db_session, balance="1500.00")
    svc = AccountDeletionService(db=db_session, token_store=_FakeTokenStore())
    with pytest.raises(ValueError, match="WALLET_NOT_EMPTY"):
        await svc.request_deletion(user=u)
    db_session.refresh(u)
    assert u.deleted_at is None  # unchanged


@pytest.mark.asyncio
async def test_request_deletion_idempotent(db_session, monkeypatch):
    import app.services.account_deletion_service as mod
    monkeypatch.setattr(mod, "dispatch_delay", lambda **kw: None)
    u = _user(db_session)
    svc = AccountDeletionService(db=db_session, token_store=_FakeTokenStore())
    first = await svc.request_deletion(user=u)
    db_session.refresh(u)
    stamp = u.deleted_at
    second = await svc.request_deletion(user=u)
    db_session.refresh(u)
    assert u.deleted_at == stamp and first == second


def test_cancel_restores_before_anonymization(db_session):
    u = _user(db_session)
    u.is_active = False
    u.deleted_at = datetime.now(UTC)
    db_session.commit()
    svc = AccountDeletionService(db=db_session, token_store=_FakeTokenStore())
    svc.cancel_deletion(user=u)
    db_session.refresh(u)
    assert u.is_active is True and u.deleted_at is None


def test_cancel_after_anonymization_raises(db_session):
    u = _user(db_session)
    u.deleted_at = datetime.now(UTC)
    u.anonymized_at = datetime.now(UTC)
    db_session.commit()
    svc = AccountDeletionService(db=db_session, token_store=_FakeTokenStore())
    with pytest.raises(ValueError, match="ALREADY_ANONYMIZED"):
        svc.cancel_deletion(user=u)
```

- [ ] **Step 2: Run to verify fail**

Run: `poetry run pytest tests/services/test_account_deletion_service.py -q -p no:cacheprovider --no-cov`
Expected: FAIL — module `account_deletion_service` not found.

- [ ] **Step 3: Implement the service**

```python
# app/services/account_deletion_service.py
"""Account deletion: shared entry point for the public web flow and the
authenticated DELETE /users/me. Starts a reversible soft-delete (the
existing deleted_at tombstone) and schedules PII anonymization for 30 days
later. The ledger is retained; see the anonymization sweep."""
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.security import verify_password_async
from app.db.models.user import User
from app.db.models.wallet import Wallet
from app.services.token_store import TokenStore
from app.services.notification_service import NotificationEvent
from app.utils.phone import InvalidPhoneFormat, normalize_to_e164
from app.workers.tasks.notification_tasks import dispatch_delay

GRACE_DAYS = 30


class AccountDeletionService:
    def __init__(self, *, db: Session, token_store: TokenStore) -> None:
        self._db = db
        self._token_store = token_store

    async def resolve_and_verify(self, *, identifier: str, password: str) -> User:
        try:
            phone = normalize_to_e164(identifier)
        except InvalidPhoneFormat:
            phone = identifier
        user = (
            self._db.query(User)
            .filter((User.email == identifier) | (User.phone == phone))
            .first()
        )
        # One generic error for both "no such user" and "bad password" so the
        # public endpoint cannot be used to enumerate accounts. Note: we
        # resolve even when is_active is False, so a pending-deletion account
        # can still cancel.
        if not user or not await verify_password_async(password, user.password_hash):
            raise ValueError("INVALID_CREDENTIALS")
        return user

    async def request_deletion(self, *, user: User) -> datetime:
        # Idempotent: an account already inside the grace window returns its
        # existing schedule without re-stamping or re-notifying.
        if user.deleted_at is not None and user.anonymized_at is None:
            return user.deleted_at + timedelta(days=GRACE_DAYS)

        wallet = (
            self._db.query(Wallet)
            .filter(Wallet.user_id == user.id)
            .with_for_update()
            .first()
        )
        if wallet is not None and wallet.balance > 0:
            raise ValueError("WALLET_NOT_EMPTY")

        now = datetime.now(UTC)
        user.is_active = False
        user.deleted_at = now
        user.tokens_revoked_at = now
        self._db.add(user)
        self._db.commit()

        await self._token_store.revoke_all(user_id=str(user.id))

        scheduled = now + timedelta(days=GRACE_DAYS)
        dispatch_delay(
            user_id=str(user.id),
            user_email=user.email,
            event=NotificationEvent.account_deletion_requested,
            context={
                "scheduled_date": scheduled.strftime("%d %B %Y"),
                "cancel_url": "https://timpbills.com/delete-account",
            },
        )
        return scheduled

    def cancel_deletion(self, *, user: User) -> None:
        if user.anonymized_at is not None:
            raise ValueError("ALREADY_ANONYMIZED")
        user.is_active = True
        user.deleted_at = None
        self._db.add(user)
        self._db.commit()
```

Create schemas:

```python
# app/schemas/account.py
from datetime import datetime

from pydantic import BaseModel, Field


class AccountDeletionRequest(BaseModel):
    identifier: str = Field(..., description="Registered email or phone number")
    password: str


class AccountDeletionResponse(BaseModel):
    scheduled_deletion_at: datetime


class CancelDeletionResponse(BaseModel):
    cancelled: bool
```

- [ ] **Step 4: Run to verify pass**

Run: `poetry run pytest tests/services/test_account_deletion_service.py -q -p no:cacheprovider --no-cov`
Expected: PASS (all 7).

- [ ] **Step 5: Commit**

```bash
git add app/services/account_deletion_service.py app/schemas/account.py tests/services/test_account_deletion_service.py
git commit -m "feat(account): AccountDeletionService (resolve/request/cancel) + schemas"
```

---

### Task 4: Public deletion endpoints + router wiring

**Files:**
- Create: `app/api/v1/endpoints/account.py`
- Modify: `app/api/v1/api.py` (import + include)
- Test: `tests/api/test_account_deletion_endpoints.py`

**Interfaces:**
- Consumes: `AccountDeletionService`, schemas (Task 3), `get_db`, `get_token_store`, `limiter`, `success`.
- Produces: `POST /api/v1/account/deletion-request`, `POST /api/v1/account/deletion-request/cancel`.

- [ ] **Step 1: Write failing tests**

```python
# tests/api/test_account_deletion_endpoints.py
import pytest, pytest_asyncio, uuid
from decimal import Decimal
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.api.deps import get_db, get_token_store
from app.core.limiter import limiter
from app.core.security import hash_password
from app.db.models.user import User
from app.db.models.wallet import Wallet


class _FakeTokenStore:
    async def revoke_all(self, *, user_id): pass


@pytest_asyncio.fixture
async def client(db_session, monkeypatch):
    import app.services.account_deletion_service as mod
    monkeypatch.setattr(mod, "dispatch_delay", lambda **kw: None)
    def _get_db():
        yield db_session
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_token_store] = lambda: _FakeTokenStore()
    limiter.enabled = False
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c
    app.dependency_overrides.clear()
    limiter.enabled = True


def _seed(db, *, pw="Secret123", email="a@b.co", phone="+2348100000021", balance="0.00"):
    u = User(id=uuid.uuid4(), email=email, phone=phone, full_name="A",
             password_hash=hash_password(pw), is_active=True)
    db.add(u); db.flush()
    db.add(Wallet(id=uuid.uuid4(), user_id=u.id, balance=Decimal(balance),
                  balance_cap=Decimal("50000.00")))
    db.commit()
    return u


@pytest.mark.asyncio
async def test_deletion_request_success(db_session, client):
    _seed(db_session)
    r = await client.post("/api/v1/account/deletion-request",
                          json={"identifier": "a@b.co", "password": "Secret123"})
    assert r.status_code == 200
    assert "scheduled_deletion_at" in r.json()["data"]


@pytest.mark.asyncio
async def test_deletion_request_bad_password_401(db_session, client):
    _seed(db_session)
    r = await client.post("/api/v1/account/deletion-request",
                          json={"identifier": "a@b.co", "password": "nope"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_CREDENTIALS"


@pytest.mark.asyncio
async def test_deletion_request_nonzero_balance_409(db_session, client):
    _seed(db_session, email="c@d.co", phone="+2348100000022", balance="500.00")
    r = await client.post("/api/v1/account/deletion-request",
                          json={"identifier": "c@d.co", "password": "Secret123"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "WALLET_NOT_EMPTY"


@pytest.mark.asyncio
async def test_cancel_after_request(db_session, client):
    _seed(db_session, email="e@f.co", phone="+2348100000023")
    await client.post("/api/v1/account/deletion-request",
                      json={"identifier": "e@f.co", "password": "Secret123"})
    r = await client.post("/api/v1/account/deletion-request/cancel",
                          json={"identifier": "e@f.co", "password": "Secret123"})
    assert r.status_code == 200 and r.json()["data"]["cancelled"] is True
```

- [ ] **Step 2: Run to verify fail**

Run: `poetry run pytest tests/api/test_account_deletion_endpoints.py -q -p no:cacheprovider --no-cov`
Expected: FAIL — 404 (route not registered).

- [ ] **Step 3: Implement the endpoints**

```python
# app/api/v1/endpoints/account.py
"""Public (unauthenticated) account-deletion endpoints for the marketing
web flow. Identity is re-verified with email/phone + password; no bearer
token is issued or required."""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_token_store
from app.core.limiter import limiter
from app.schemas.account import (
    AccountDeletionRequest,
    AccountDeletionResponse,
    CancelDeletionResponse,
)
from app.services.account_deletion_service import AccountDeletionService
from app.services.token_store import TokenStore
from app.utils.responses import success

router = APIRouter(prefix="/account", tags=["account"])

_ERROR_MAP: dict[str, tuple[int, str]] = {
    "INVALID_CREDENTIALS": (401, "The email/phone or password is incorrect."),
    "WALLET_NOT_EMPTY": (
        409,
        "Withdraw your wallet balance before deleting your account.",
    ),
    "ALREADY_ANONYMIZED": (
        409,
        "This account has already been permanently deleted.",
    ),
}


def _raise(code: str) -> None:
    http_code, msg = _ERROR_MAP.get(code, (500, code))
    raise HTTPException(status_code=http_code, detail={"code": code, "message": msg})


def _svc(db: Session, token_store: TokenStore) -> AccountDeletionService:
    return AccountDeletionService(db=db, token_store=token_store)


@router.post("/deletion-request", response_model=None)
@limiter.limit("3/minute")
async def request_deletion(
    request: Request,
    body: AccountDeletionRequest,
    db: Session = Depends(get_db),
    token_store: TokenStore = Depends(get_token_store),
):
    svc = _svc(db, token_store)
    try:
        user = await svc.resolve_and_verify(
            identifier=body.identifier, password=body.password
        )
        scheduled = await svc.request_deletion(user=user)
    except ValueError as e:
        _raise(str(e))
    out = AccountDeletionResponse(scheduled_deletion_at=scheduled)
    return success(out.model_dump(mode="json"),
                   request_id=getattr(request.state, "request_id", None))


@router.post("/deletion-request/cancel", response_model=None)
@limiter.limit("3/minute")
async def cancel_deletion(
    request: Request,
    body: AccountDeletionRequest,
    db: Session = Depends(get_db),
    token_store: TokenStore = Depends(get_token_store),
):
    svc = _svc(db, token_store)
    try:
        user = await svc.resolve_and_verify(
            identifier=body.identifier, password=body.password
        )
        svc.cancel_deletion(user=user)
    except ValueError as e:
        _raise(str(e))
    return success(CancelDeletionResponse(cancelled=True).model_dump(),
                   request_id=getattr(request.state, "request_id", None))
```

Wire the router in `app/api/v1/api.py`: add `account` to the `from app.api.v1.endpoints import (...)` block and add `api_router.include_router(account.router)` alongside the others.

- [ ] **Step 4: Run to verify pass**

Run: `poetry run pytest tests/api/test_account_deletion_endpoints.py -q -p no:cacheprovider --no-cov`
Expected: PASS (all 4).

- [ ] **Step 5: Commit**

```bash
git add app/api/v1/endpoints/account.py app/api/v1/api.py tests/api/test_account_deletion_endpoints.py
git commit -m "feat(account): public deletion-request + cancel endpoints"
```

---

### Task 5: Route authenticated `DELETE /users/me` through the service

**Files:**
- Modify: `app/api/v1/endpoints/users.py` (`soft_delete_me` `:184-223`)
- Test: `tests/api/test_users_soft_delete.py` (add cases; find the existing test file for `DELETE /users/me` and extend it — search `soft_delete` / `/users/me`)

**Interfaces:**
- Consumes: `AccountDeletionService.request_deletion` (Task 3).
- Behavior change: `DELETE /users/me` now returns `409 WALLET_NOT_EMPTY` when the wallet is non-empty; still `204` on success; also dispatches the deletion notice.

- [ ] **Step 1: Write/extend failing tests**

Add to the existing soft-delete test file (create `tests/api/test_users_soft_delete.py` only if none exists):

```python
@pytest.mark.asyncio
async def test_delete_me_blocked_when_wallet_not_empty(db_session, client, auth_headers):
    # auth_headers belongs to a user whose wallet has a positive balance.
    # (Seed a positive balance for that user before the call.)
    r = await client.delete("/api/v1/users/me", headers=auth_headers)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "WALLET_NOT_EMPTY"


@pytest.mark.asyncio
async def test_delete_me_succeeds_when_wallet_empty(db_session, client, auth_headers):
    r = await client.delete("/api/v1/users/me", headers=auth_headers)
    assert r.status_code == 204
```

(Use whatever login/seed helpers the existing users tests use; keep the existing zero-balance success case green.)

- [ ] **Step 2: Run to verify the new balance test fails**

Run: `poetry run pytest tests/api/test_users_soft_delete.py -q -p no:cacheprovider --no-cov`
Expected: the non-empty-wallet case FAILS with 204 instead of 409 (guard not yet wired).

- [ ] **Step 3: Refactor `soft_delete_me`**

```python
@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def soft_delete_me(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    token_store: TokenStore = Depends(get_token_store),
):
    """Soft-delete the authenticated user (see AccountDeletionService).

    Now shares the public flow's logic: a non-empty wallet is rejected with
    409 WALLET_NOT_EMPTY, and a deletion notice is sent. The 30-day tombstone
    + token revocation are unchanged.
    """
    from app.services.account_deletion_service import AccountDeletionService

    svc = AccountDeletionService(db=db, token_store=token_store)
    try:
        await svc.request_deletion(user=current_user)
    except ValueError as e:
        if str(e) == "WALLET_NOT_EMPTY":
            raise HTTPException(status_code=409, detail={
                "code": "WALLET_NOT_EMPTY",
                "message": "Withdraw your wallet balance before deleting your account.",
            })
        raise
    return None
```

(Ensure `HTTPException` is imported in `users.py`; it usually already is.)

- [ ] **Step 4: Run to verify pass**

Run: `poetry run pytest tests/api/test_users_soft_delete.py -q -p no:cacheprovider --no-cov`
Expected: PASS (both new cases + the pre-existing success case).

- [ ] **Step 5: Commit**

```bash
git add app/api/v1/endpoints/users.py tests/api/test_users_soft_delete.py
git commit -m "feat(users): route DELETE /users/me through AccountDeletionService (balance guard + notice)"
```

---

### Task 6: Anonymization sweep (Celery beat)

**Files:**
- Create: `app/workers/tasks/account_tasks.py`
- Modify: `app/workers/celery_app.py` (beat schedule)
- Test: `tests/workers/test_anonymize_deleted_accounts.py`

**Interfaces:**
- Consumes: `User`, `Wallet`, `PushToken`, `OtpCode`, `KycRecord`, `VirtualAccount` models; `SessionLocal`.
- Produces: `anonymize_deleted_accounts() -> dict` Celery task; beat entry `anonymize-deleted-accounts-daily`.

- [ ] **Step 1: Write failing tests**

```python
# tests/workers/test_anonymize_deleted_accounts.py
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from app.db.models.user import User
from app.db.models.wallet import Wallet
from app.db.models.transaction import Transaction
from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.push_token import PushToken


def _deleted_user(db, *, days_ago, email="d@e.co", phone="+2348100000031"):
    u = User(id=uuid.uuid4(), email=email, phone=phone, full_name="Real Name",
             password_hash="x", is_active=False,
             deleted_at=datetime.now(UTC) - timedelta(days=days_ago))
    db.add(u); db.flush()
    db.add(Wallet(id=uuid.uuid4(), user_id=u.id, balance=Decimal("0.00"),
                  balance_cap=Decimal("50000.00")))
    db.add(Transaction(id=uuid.uuid4(), user_id=u.id, reference=f"T-{uuid.uuid4().hex[:8]}",
                       type=TransactionType.wallet_funding, status=TransactionStatus.success,
                       amount=Decimal("100.00"), fee=Decimal("0.00"), currency="NGN", meta={}))
    db.add(PushToken(id=uuid.uuid4(), user_id=u.id, token=f"tok-{uuid.uuid4().hex[:8]}"))
    db.commit()
    return u


def _run(db):
    from app.workers.tasks import account_tasks as at
    orig = db.close
    db.close = lambda: None
    try:
        with patch.object(at, "SessionLocal", lambda: db):
            return at.anonymize_deleted_accounts()
    finally:
        db.close = orig


def test_over_grace_is_anonymized_ledger_kept(db_session):
    u = _deleted_user(db_session, days_ago=31)
    _run(db_session)
    db_session.expire_all()
    fresh = db_session.query(User).filter(User.id == u.id).one()
    assert fresh.anonymized_at is not None
    assert fresh.full_name == "Deleted User"
    assert fresh.email != "d@e.co" and "deleted" in fresh.email
    assert fresh.phone != "+2348100000031"
    # ledger kept
    assert db_session.query(Transaction).filter(Transaction.user_id == u.id).count() == 1
    assert db_session.query(Wallet).filter(Wallet.user_id == u.id).count() == 1
    # PII child rows gone
    assert db_session.query(PushToken).filter(PushToken.user_id == u.id).count() == 0


def test_inside_grace_untouched(db_session):
    u = _deleted_user(db_session, days_ago=5, email="x@y.co", phone="+2348100000032")
    _run(db_session)
    db_session.expire_all()
    fresh = db_session.query(User).filter(User.id == u.id).one()
    assert fresh.anonymized_at is None and fresh.email == "x@y.co"


def test_rerun_is_noop(db_session):
    u = _deleted_user(db_session, days_ago=31, email="z@y.co", phone="+2348100000033")
    _run(db_session)
    db_session.expire_all()
    first = db_session.query(User).filter(User.id == u.id).one().anonymized_at
    _run(db_session)
    db_session.expire_all()
    assert db_session.query(User).filter(User.id == u.id).one().anonymized_at == first
```

- [ ] **Step 2: Run to verify fail**

Run: `poetry run pytest tests/workers/test_anonymize_deleted_accounts.py -q -p no:cacheprovider --no-cov`
Expected: FAIL — module `account_tasks` not found.

- [ ] **Step 3: Implement the sweep**

```python
# app/workers/tasks/account_tasks.py
"""Anonymize accounts soft-deleted at least GRACE_DAYS ago. Scrubs PII from
the users row and deletes PII child rows, but KEEPS the financial ledger
(wallet, transactions, virtual_accounts, wallet_credit_keys) with identity
detached, per AML retention. Idempotent via the anonymized_at marker."""
from datetime import UTC, datetime, timedelta

from app.db.session import SessionLocal
from app.db.models.user import User
from app.db.models.otp import OtpCode
from app.db.models.push_token import PushToken
from app.db.models.kyc_record import KycRecord
from app.db.models.virtual_account import VirtualAccount
from app.services.account_deletion_service import GRACE_DAYS
from app.workers.celery_app import celery_app

_DELETED_PASSWORD = "!ACCOUNT_DELETED!"  # not a valid hash; never verifies


@celery_app.task(name="app.workers.tasks.account_tasks.anonymize_deleted_accounts")
def anonymize_deleted_accounts() -> dict:
    db = SessionLocal()
    try:
        cutoff = datetime.now(UTC) - timedelta(days=GRACE_DAYS)
        users = (
            db.query(User)
            .filter(User.deleted_at.isnot(None))
            .filter(User.deleted_at <= cutoff)
            .filter(User.anonymized_at.is_(None))
            .limit(200)
            .all()
        )
        count = 0
        for u in users:
            locked = (
                db.query(User).filter(User.id == u.id).with_for_update().one()
            )
            if locked.anonymized_at is not None:
                continue
            # Delete PII child rows.
            db.query(PushToken).filter(PushToken.user_id == locked.id).delete()
            db.query(OtpCode).filter(OtpCode.user_id == locked.id).delete()
            db.query(KycRecord).filter(KycRecord.user_id == locked.id).delete()
            # Detach the DVA from Paystack identity but keep the row.
            for va in db.query(VirtualAccount).filter(
                VirtualAccount.user_id == locked.id
            ):
                va.paystack_customer_code = None
            # Scrub the user row (keep id + retained-ledger FKs).
            locked.email = f"deleted-{locked.id}@deleted.invalid"
            locked.phone = f"deleted:{locked.id}"
            locked.full_name = "Deleted User"
            locked.password_hash = _DELETED_PASSWORD
            locked.pin_hash = None
            locked.date_of_birth = None
            locked.gender = None
            locked.address = None
            locked.avatar_url = None
            locked.referral_code = None
            locked.anonymized_at = datetime.now(UTC)
            db.commit()
            count += 1
        return {"anonymized": count}
    finally:
        db.close()
```

(If `virtual_accounts` has no `paystack_customer_code` NULL-ability, adjust to a placeholder instead. Verify `KycRecord`/`VirtualAccount`/`PushToken`/`OtpCode` import paths against the models found in the scan.)

- [ ] **Step 4: Register the beat schedule**

In `app/workers/celery_app.py` `beat_schedule`, add:

```python
    # Account-deletion PII purge: anonymize accounts soft-deleted 30+ days
    # ago. Daily is ample; the grace window is measured in days.
    "anonymize-deleted-accounts-daily": {
        "task": "app.workers.tasks.account_tasks.anonymize_deleted_accounts",
        "schedule": crontab(hour=3, minute=0),  # 03:00 UTC
    },
```

- [ ] **Step 5: Run to verify pass**

Run: `poetry run pytest tests/workers/test_anonymize_deleted_accounts.py -q -p no:cacheprovider --no-cov`
Expected: PASS (all 3).

- [ ] **Step 6: Commit**

```bash
git add app/workers/tasks/account_tasks.py app/workers/celery_app.py tests/workers/test_anonymize_deleted_accounts.py
git commit -m "feat(account): daily anonymization sweep for soft-deleted accounts"
```

---

### Task 7: CORS origins for the marketing site

**Files:**
- Modify: `app/core/config.py` (comment near `BACKEND_CORS_ORIGINS` `:29`)
- Env: `.env.staging` / `.env.production` (`BACKEND_CORS_ORIGINS`), re-encrypt via `scripts/env.sh`

**Interfaces:** none (config/deploy only).

- [ ] **Step 1: Document the requirement in config**

Add a comment above `BACKEND_CORS_ORIGINS` in `config.py` noting that the deployed env must include the marketing origins (e.g. `https://timpbills.com`, `https://staging.timpbills.com`) so the public deletion page can call the API cross-origin. No default code change (localhost stays for dev).

- [ ] **Step 2: Update encrypted env (deploy step, do at rollout)**

```bash
# staging
./scripts/env.sh decrypt staging   # if needed
# ensure BACKEND_CORS_ORIGINS includes the marketing origin(s), comma-separated
./scripts/env.sh encrypt staging
# repeat for production with the prod marketing origin
```

- [ ] **Step 3: Commit the config comment**

```bash
git add app/core/config.py
git commit -m "docs(config): note marketing origins required in BACKEND_CORS_ORIGINS for deletion page"
```

(The `.enc` env change is committed as part of the rollout, mirroring the DVA env commit.)

---

### Task 8: Marketing public API helper

**Files:**
- Create: `timpbills-marketing/lib/public-api.ts`
- Modify: `timpbills-marketing/.env.local.example` (add `NEXT_PUBLIC_API_BASE`)

**Interfaces:**
- Produces: `requestAccountDeletion(identifier, password): Promise<{ scheduled_deletion_at: string }>` and `cancelAccountDeletion(identifier, password): Promise<{ cancelled: boolean }>`, each throwing `PublicApiError { code, message }` on the `{success:false,error}` envelope.

- [ ] **Step 1: Add the env var**

In `.env.local.example`, add:

```
# Public API base for unauthenticated marketing flows (account deletion).
NEXT_PUBLIC_API_BASE=http://localhost:8000/api/v1
```

- [ ] **Step 2: Implement the helper**

```typescript
// timpbills-marketing/lib/public-api.ts
const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000/api/v1";

export class PublicApiError extends Error {
  code: string;
  constructor(code: string, message: string) {
    super(message);
    this.code = code;
  }
}

async function call<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const json = await res.json().catch(() => null);
  if (!json || json.success === false) {
    const err = json?.error;
    throw new PublicApiError(
      err?.code ?? "REQUEST_FAILED",
      err?.message ?? "Something went wrong. Please try again.",
    );
  }
  return json.data as T;
}

export function requestAccountDeletion(identifier: string, password: string) {
  return call<{ scheduled_deletion_at: string }>(
    "/account/deletion-request",
    { identifier, password },
  );
}

export function cancelAccountDeletion(identifier: string, password: string) {
  return call<{ cancelled: boolean }>(
    "/account/deletion-request/cancel",
    { identifier, password },
  );
}
```

- [ ] **Step 3: Type-check**

Run (from `timpbills-marketing`): `npx tsc --noEmit`
Expected: no new errors in `lib/public-api.ts`.

- [ ] **Step 4: Commit**

```bash
git add lib/public-api.ts .env.local.example
git commit -m "feat(marketing): public API helper for account deletion"
```

---

### Task 9: Marketing delete-account page + form

**Files:**
- Create: `timpbills-marketing/app/components/DeleteAccountForm.tsx`
- Create: `timpbills-marketing/app/(marketing)/delete-account/page.tsx`

**Interfaces:**
- Consumes: `requestAccountDeletion`, `cancelAccountDeletion`, `PublicApiError` (Task 8).

Note: `timpbills-marketing/AGENTS.md` warns this is a non-standard Next.js build — read `node_modules/next/dist/docs/` for the conventions before writing components. Model the form on `app/components/ContactForm.tsx` (the existing `"use client"` + `useState<Status>` pattern) and reuse its CSS classes (`field`, `btn`, `form-status`, `row-2`).

- [ ] **Step 1: Build the form component**

```tsx
// timpbills-marketing/app/components/DeleteAccountForm.tsx
"use client";
import { useState } from "react";
import { requestAccountDeletion, cancelAccountDeletion, PublicApiError } from "@/lib/public-api";

type Status = { kind: "idle" | "loading" } | { kind: "ok"; msg: string } | { kind: "err"; msg: string };

export default function DeleteAccountForm() {
  const [identifier, setIdentifier] = useState("");
  const [password, setPassword] = useState("");
  const [ack, setAck] = useState(false);
  const [status, setStatus] = useState<Status>({ kind: "idle" });

  async function submit(e: React.FormEvent, mode: "delete" | "cancel") {
    e.preventDefault();
    setStatus({ kind: "loading" });
    try {
      if (mode === "delete") {
        const d = await requestAccountDeletion(identifier, password);
        const when = new Date(d.scheduled_deletion_at).toLocaleDateString();
        setStatus({ kind: "ok", msg: `Your account is scheduled for deletion on ${when}. To keep your account, return here and choose Cancel a pending deletion before that date.` });
      } else {
        await cancelAccountDeletion(identifier, password);
        setStatus({ kind: "ok", msg: "Your pending deletion has been cancelled. You can log in as normal." });
      }
    } catch (err) {
      const msg = err instanceof PublicApiError ? err.message : "Something went wrong. Please try again.";
      setStatus({ kind: "err", msg });
    }
  }

  return (
    <form className="field" onSubmit={(e) => submit(e, "delete")}>
      <label>Email or phone number
        <input value={identifier} onChange={(e) => setIdentifier(e.target.value)} required autoComplete="username" />
      </label>
      <label>Password
        <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required autoComplete="current-password" />
      </label>
      <label className="row-2">
        <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
        I understand my account will be scheduled for permanent deletion.
      </label>
      <button className="btn btn-indigo" type="submit" disabled={!ack || status.kind === "loading"}>
        {status.kind === "loading" ? "Please wait" : "Request account deletion"}
      </button>
      <button className="btn" type="button" onClick={(e) => submit(e as unknown as React.FormEvent, "cancel")} disabled={status.kind === "loading"}>
        Cancel a pending deletion
      </button>
      {status.kind === "ok" && <p className="form-status" role="status">{status.msg}</p>}
      {status.kind === "err" && <p className="form-status" role="alert">{status.msg}</p>}
    </form>
  );
}
```

(Adjust class names to the ones ContactForm actually uses; verify against `globals.css`.)

- [ ] **Step 2: Build the page**

```tsx
// timpbills-marketing/app/(marketing)/delete-account/page.tsx
import DeleteAccountForm from "@/app/components/DeleteAccountForm";

export const metadata = {
  title: "Delete your account | Timpbills",
  description: "Request deletion of your Timpbills account and personal data.",
};

export default function DeleteAccountPage() {
  return (
    <main className="container">
      <h1>Delete your account</h1>
      <p>
        Enter your email or phone number and password to request deletion of your
        Timpbills account. Your account is disabled immediately and permanently
        deleted after 30 days. During that time you can cancel by returning to
        this page. Your personal data is erased; records we are legally required
        to keep are retained without your personal details.
      </p>
      <p>
        If your wallet still holds a balance, withdraw it before requesting
        deletion.
      </p>
      <DeleteAccountForm />
    </main>
  );
}
```

(Match the surrounding page markup conventions, e.g. `PageBanner`/`Reveal`, used by sibling `(marketing)` pages so it inherits the site chrome.)

- [ ] **Step 3: Type-check + build**

Run (from `timpbills-marketing`): `npx tsc --noEmit && npm run build`
Expected: builds; `/delete-account` route present.

- [ ] **Step 4: Manual smoke (local)**

With the backend running and `NEXT_PUBLIC_API_BASE` pointed at it: submit with a valid zero-balance test user (success), a bad password (error), a non-empty-wallet user (WALLET_NOT_EMPTY), then cancel.

- [ ] **Step 5: Commit**

```bash
git add "app/(marketing)/delete-account/page.tsx" app/components/DeleteAccountForm.tsx
git commit -m "feat(marketing): public delete-account page and form"
```

---

## Self-Review

**Spec coverage:** §2 public endpoint → Task 4; §3.1 `anonymized_at` → Task 1; §3.2 service (resolve/request/cancel, balance guard, idempotency, token revoke, notify) → Task 3; balance guard on authenticated route → Task 5; §3.4 sweep (scrub, keep ledger, idempotent, re-registration) → Task 6; §3.5 notification → Task 2; §4 marketing UI → Tasks 8+9; §5 CORS → Task 7; §6 security (rate limit, generic error, notice, grace) → Tasks 3+4. All covered.

**Placeholder scan:** All code steps carry real code. The two "adapt to the real render helper" notes (Task 2) and model-path verifications (Task 6) are explicit verification instructions, not vague hand-waving.

**Type consistency:** `resolve_and_verify` / `request_deletion(user=...) -> datetime` / `cancel_deletion(user=...)`, `GRACE_DAYS`, error codes `INVALID_CREDENTIALS` / `WALLET_NOT_EMPTY` / `ALREADY_ANONYMIZED`, and response fields `scheduled_deletion_at` / `cancelled` are used identically across service, endpoints, and marketing helper.

## Parallelization

- Backend Tasks 1→2→3→4→5, and 3→6 (6 depends on `GRACE_DAYS` from 3). Task 7 is independent.
- Marketing Tasks 8→9 depend only on the API contract (paths + shapes fixed in Task 4), so they can run in parallel with backend Tasks 5-7 once Task 4's contract is settled.
