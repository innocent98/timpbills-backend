# Admin API + Notification Logs — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the backend admin API (opaque Redis-session cookie auth, `admin_users` table, read endpoints, refund+requery writes) and a `notification_logs` audit table + read endpoint, then wire the existing `timpbills-marketing` platform-admin dashboard to real data.

**Architecture:** Admin auth is a server-side opaque session in Redis delivered via an httpOnly cookie scoped to `.timpbills.com` (CSRF double-submit on writes), fully separate from the mobile Bearer-JWT path. A thin `AdminService` owns read/metric queries; endpoints stay validation-only. `notification_logs` rows are written at each channel-send boundary. The Next.js dashboard talks to the API directly (no BFF proxy).

**Tech Stack:** FastAPI, SQLAlchemy 2.x (sync ORM), Alembic, Redis (`redis.asyncio`), argon2 via passlib, pytest + httpx `AsyncClient` + fakeredis; Next.js (platform-admin).

**Spec:** `docs/superpowers/specs/2026-06-04-admin-api-notification-logs-design.md`

---

## File Structure

**Backend (`timpbills-backend/`):**
- Create `app/db/models/admin_user.py` — `AdminUser` model + `AdminRole` enum.
- Create `app/db/models/notification_log.py` — `NotificationLog` model + `NotificationChannel`/`NotificationLogStatus` enums.
- Create `app/services/admin_session_store.py` — opaque Redis session store.
- Create `app/services/admin_service.py` — read/metric queries.
- Create `app/services/notification_log_service.py` — log write helper.
- Create `app/api/v1/endpoints/admin_auth.py` — login/logout (kept separate from the existing `admin.py` ops endpoints to keep files focused).
- Modify `app/api/v1/endpoints/admin.py` — re-gate refund trigger; add read + requery endpoints.
- Modify `app/api/deps.py` — `get_admin_session_store`, rewrite `require_admin`, add `require_admin_csrf`.
- Modify `app/core/config.py` — admin cookie/session settings.
- Modify `app/db/models/user.py` — drop `is_admin` column.
- Modify `app/services/notification_service.py` + `app/services/auth_service.py` — wire log writes.
- Modify `app/api/v1/api.py` — include `admin_auth.router`.
- Create `scripts/create_admin.py` — first-admin CLI.
- Create Alembic migrations: `202606040900_add_admin_users.py`, `202606041000_add_notification_logs.py`.
- Tests under `tests/` mirroring existing layout.

**Marketing (`timpbills-marketing/`):**
- Create `lib/admin-api.ts` — typed API client.
- Modify pages under `app/platform-admin/**` — replace `data.ts` usage with live calls; add login.

---

## Conventions to follow (verified in codebase)
- Models: `class X(TimestampMixin, Base)`, UUID PK `default=uuid.uuid4`, `Enum(MyEnum, name="my_enum")`.
- Response envelope: `from app.utils.responses import success` → `success(data, request_id=...)`.
- Error detail shape: `HTTPException(status_code, detail={"code": "...", "message": "..."})`.
- Redis: `from app.api.deps import get_redis` (async client, `decode_responses=True`).
- Password hashing: `from app.core.security import hash_password, verify_password`.
- Migrations live in `alembic/versions/`, named `YYYYMMDDHHMM_slug.py`, latest head is `202605260900`.
- Tests: async tests use `httpx.AsyncClient(transport=ASGITransport(app=app))`, `fakeredis.aioredis.FakeRedis`, `app.dependency_overrides`, `limiter.enabled = False`. See `tests/api/test_admin_refunds.py` for the canonical client fixture.

---

# PHASE A — Admin auth foundation

## Task 1: `admin_users` model + migration (drops `users.is_admin`)

**Files:**
- Create: `app/db/models/admin_user.py`
- Modify: `app/db/models/user.py` (remove `is_admin` column + its comment)
- Modify: `app/db/models/__init__.py` (export `AdminUser` if the package re-exports models)
- Create: `alembic/versions/202606040900_add_admin_users.py`
- Test: `tests/db/test_admin_user_model.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/db/test_admin_user_model.py
from app.db.models.admin_user import AdminRole, AdminUser


def test_admin_user_defaults(db_session):
    admin = AdminUser(
        email="ops@timpbills.com",
        password_hash="x",
        full_name="Ops One",
    )
    db_session.add(admin)
    db_session.commit()
    db_session.refresh(admin)
    assert admin.id is not None
    assert admin.role is AdminRole.superadmin   # default
    assert admin.is_active is True
    assert admin.last_login_at is None


def test_admin_email_is_unique(db_session):
    import pytest
    from sqlalchemy.exc import IntegrityError
    db_session.add(AdminUser(email="dup@x.com", password_hash="a", full_name="A"))
    db_session.commit()
    db_session.add(AdminUser(email="dup@x.com", password_hash="b", full_name="B"))
    with pytest.raises(IntegrityError):
        db_session.commit()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd timpbills-backend && python -m pytest tests/db/test_admin_user_model.py -q`
Expected: FAIL — `ModuleNotFoundError: app.db.models.admin_user`.

- [ ] **Step 3: Create the model**

```python
# app/db/models/admin_user.py
import enum
import uuid

from sqlalchemy import Boolean, Column, DateTime, Enum, String
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base
from app.db.mixins import TimestampMixin


class AdminRole(str, enum.Enum):
    superadmin = "superadmin"
    support = "support"   # reserved for v2 RBAC; unused in v1


class AdminUser(TimestampMixin, Base):
    __tablename__ = "admin_users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    full_name = Column(String, nullable=False)
    role = Column(
        Enum(AdminRole, name="admin_role_enum"),
        nullable=False,
        default=AdminRole.superadmin,
    )
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    last_login_at = Column(DateTime(timezone=True), nullable=True)
```

Then in `app/db/models/user.py` remove the `is_admin` column block (lines defining `is_admin = Column(...)` and its comment). Verify nothing else in `app/` references `user.is_admin`:
Run: `grep -rn "is_admin" app/` — expected: no matches after edit. (If any remain, they belong to `require_admin`, fixed in Task 4 — leave those for now and do Task 4 before running the full suite.)

If `app/db/models/__init__.py` re-exports models for Alembic autodiscovery, add `from app.db.models.admin_user import AdminUser  # noqa: F401`.

- [ ] **Step 4: Run model test to verify it passes**

Run: `python -m pytest tests/db/test_admin_user_model.py -q`
Expected: PASS (uses SQLite `db_session`, no migration needed for the test).

- [ ] **Step 5: Write the Alembic migration**

```python
# alembic/versions/202606040900_add_admin_users.py
"""add admin_users table; drop users.is_admin

Revision ID: 202606040900
Revises: 202605260900
Create Date: 2026-06-04 09:00:00

Admin auth moves off the single-bit users.is_admin flag onto a dedicated
admin_users table (own password hash, role column ready for v2 RBAC).
require_admin is rewritten to an opaque-session cookie path, so nothing
reads users.is_admin after this migration — drop it.

Runbook: immediately after upgrade, create the first admin via
`python scripts/create_admin.py`.

Downgrade: re-adds users.is_admin (default false) and drops admin_users.
Any admin rows are lost on downgrade — re-seed via the CLI.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "202606040900"
down_revision = "202605260900"
branch_labels = None
depends_on = None


def upgrade() -> None:
    admin_role = postgresql.ENUM(
        "superadmin", "support", name="admin_role_enum", create_type=True
    )
    admin_role.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "admin_users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("full_name", sa.String(), nullable=False),
        sa.Column("role", admin_role, nullable=False, server_default="superadmin"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_admin_users_email", "admin_users", ["email"], unique=True)
    op.drop_column("users", "is_admin")


def downgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.drop_index("ix_admin_users_email", table_name="admin_users")
    op.drop_table("admin_users")
    postgresql.ENUM(name="admin_role_enum").drop(op.get_bind(), checkfirst=True)
```

- [ ] **Step 6: Verify migration is loadable & chains**

Run: `python -m alembic history | head -3`
Expected: `202606040900` shown as head, revises `202605260900`. (No DB apply needed in CI; this just confirms the file parses and chains.)

- [ ] **Step 7: Commit**

```bash
git add app/db/models/admin_user.py app/db/models/user.py app/db/models/__init__.py \
        alembic/versions/202606040900_add_admin_users.py tests/db/test_admin_user_model.py
git commit -m "feat(admin): add admin_users table, drop users.is_admin"
```

---

## Task 2: `AdminSessionStore` (opaque Redis session)

**Files:**
- Create: `app/services/admin_session_store.py`
- Test: `tests/services/test_admin_session_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_admin_session_store.py
import pytest
from fakeredis.aioredis import FakeRedis

from app.services.admin_session_store import AdminSessionStore


@pytest.mark.asyncio
async def test_create_get_refresh_delete():
    redis = FakeRedis(decode_responses=True)
    store = AdminSessionStore(redis=redis, ttl_seconds=100)

    sid = await store.create(admin_id="abc", role="superadmin")
    assert isinstance(sid, str) and len(sid) > 20

    data = await store.get(sid)
    assert data["admin_id"] == "abc"
    assert data["role"] == "superadmin"

    # refresh extends TTL (still present)
    await store.refresh(sid)
    assert await store.get(sid) is not None

    await store.delete(sid)
    assert await store.get(sid) is None
    await redis.aclose()


@pytest.mark.asyncio
async def test_get_unknown_returns_none():
    redis = FakeRedis(decode_responses=True)
    store = AdminSessionStore(redis=redis, ttl_seconds=100)
    assert await store.get("nope") is None
    await redis.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/services/test_admin_session_store.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the store**

```python
# app/services/admin_session_store.py
"""Opaque server-side admin sessions in Redis.

Admin auth deliberately does NOT use JWT: an opaque session id in an
httpOnly cookie keeps the token out of browser JS (XSS-safe) and lets us
revoke instantly by deleting one key. Sliding TTL — refreshed on every
authenticated request via ``refresh``.
"""
import json
import secrets
from datetime import UTC, datetime

from redis.asyncio import Redis


class AdminSessionStore:
    def __init__(self, *, redis: Redis, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    @staticmethod
    def _key(sid: str) -> str:
        return f"admin_session:{sid}"

    async def create(self, *, admin_id: str, role: str) -> str:
        sid = secrets.token_urlsafe(32)
        payload = json.dumps({
            "admin_id": admin_id,
            "role": role,
            "created_at": datetime.now(UTC).isoformat(),
        })
        await self._redis.set(self._key(sid), payload, ex=self._ttl)
        return sid

    async def get(self, sid: str) -> dict | None:
        raw = await self._redis.get(self._key(sid))
        if raw is None:
            return None
        return json.loads(raw)

    async def refresh(self, sid: str) -> None:
        await self._redis.expire(self._key(sid), self._ttl)

    async def delete(self, sid: str) -> None:
        await self._redis.delete(self._key(sid))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/services/test_admin_session_store.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/admin_session_store.py tests/services/test_admin_session_store.py
git commit -m "feat(admin): opaque Redis-backed admin session store"
```

---

## Task 3: Admin cookie/session config

**Files:**
- Modify: `app/core/config.py`
- Test: `tests/core/test_admin_config_defaults.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/core/test_admin_config_defaults.py
from app.core.config import settings


def test_admin_cookie_defaults():
    assert settings.ADMIN_SESSION_TTL_SECONDS == 8 * 3600
    assert settings.ADMIN_SESSION_COOKIE_NAME == "admin_session"
    assert settings.ADMIN_CSRF_COOKIE_NAME == "admin_csrf"
    # Secure cookies on by default; tests/dev can override via env.
    assert settings.ADMIN_COOKIE_SECURE is True
    assert settings.ADMIN_COOKIE_DOMAIN is None  # unset locally; ".timpbills.com" in prod env
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/core/test_admin_config_defaults.py -q`
Expected: FAIL — `AttributeError`.

- [ ] **Step 3: Add settings**

In `app/core/config.py`, inside `class Settings(BaseSettings)`, add:

```python
    # --- Admin dashboard auth (opaque session cookie) ---
    ADMIN_SESSION_TTL_SECONDS: int = 8 * 3600
    ADMIN_SESSION_COOKIE_NAME: str = "admin_session"
    ADMIN_CSRF_COOKIE_NAME: str = "admin_csrf"
    ADMIN_COOKIE_SECURE: bool = True
    ADMIN_COOKIE_DOMAIN: str | None = None  # set to ".timpbills.com" in staging/prod
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/core/test_admin_config_defaults.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/core/config.py tests/core/test_admin_config_defaults.py
git commit -m "feat(admin): cookie/session config settings"
```

---

## Task 4: Rewrite `require_admin` + add `require_admin_csrf` + store dep

**Files:**
- Modify: `app/api/deps.py`
- Test: `tests/api/test_admin_auth_deps.py`

- [ ] **Step 1: Write the failing test** (a tiny throwaway router exercised through the app is overkill; test the deps directly)

```python
# tests/api/test_admin_auth_deps.py
import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import HTTPException

from app.api.deps import require_admin, require_admin_csrf
from app.db.models.admin_user import AdminUser
from app.services.admin_session_store import AdminSessionStore


class _Req:
    def __init__(self, headers):
        self.headers = headers


@pytest.mark.asyncio
async def test_require_admin_no_cookie_401(db_session):
    redis = FakeRedis(decode_responses=True)
    store = AdminSessionStore(redis=redis, ttl_seconds=100)
    with pytest.raises(HTTPException) as ei:
        await require_admin(admin_session=None, store=store, db=db_session)
    assert ei.value.status_code == 401
    assert ei.value.detail["code"] == "ADMIN_AUTH_REQUIRED"
    await redis.aclose()


@pytest.mark.asyncio
async def test_require_admin_expired_session_401(db_session):
    redis = FakeRedis(decode_responses=True)
    store = AdminSessionStore(redis=redis, ttl_seconds=100)
    with pytest.raises(HTTPException) as ei:
        await require_admin(admin_session="ghost", store=store, db=db_session)
    assert ei.value.status_code == 401
    assert ei.value.detail["code"] == "ADMIN_SESSION_EXPIRED"
    await redis.aclose()


@pytest.mark.asyncio
async def test_require_admin_happy_path(db_session):
    admin = AdminUser(email="a@x.com", password_hash="h", full_name="A")
    db_session.add(admin)
    db_session.commit()
    db_session.refresh(admin)
    redis = FakeRedis(decode_responses=True)
    store = AdminSessionStore(redis=redis, ttl_seconds=100)
    sid = await store.create(admin_id=str(admin.id), role="superadmin")
    got = await require_admin(admin_session=sid, store=store, db=db_session)
    assert got.id == admin.id
    await redis.aclose()


@pytest.mark.asyncio
async def test_require_admin_disabled_403(db_session):
    admin = AdminUser(email="d@x.com", password_hash="h", full_name="D", is_active=False)
    db_session.add(admin)
    db_session.commit()
    db_session.refresh(admin)
    redis = FakeRedis(decode_responses=True)
    store = AdminSessionStore(redis=redis, ttl_seconds=100)
    sid = await store.create(admin_id=str(admin.id), role="superadmin")
    with pytest.raises(HTTPException) as ei:
        await require_admin(admin_session=sid, store=store, db=db_session)
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "ADMIN_DISABLED"
    await redis.aclose()


def test_csrf_mismatch_403():
    with pytest.raises(HTTPException) as ei:
        require_admin_csrf(request=_Req({"X-CSRF-Token": "a"}), admin_csrf="b")
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "CSRF_FAILED"


def test_csrf_match_ok():
    # returns None, raises nothing
    assert require_admin_csrf(request=_Req({"X-CSRF-Token": "tok"}), admin_csrf="tok") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/api/test_admin_auth_deps.py -q`
Expected: FAIL — `ImportError: cannot import name 'require_admin_csrf'` and the rewritten `require_admin` signature.

- [ ] **Step 3: Rewrite the deps**

In `app/api/deps.py`: delete the existing `require_admin` function (the `is_admin`-flag version) and add the imports + new deps. Add near the top imports: `from fastapi import Cookie, Request`, `from app.db.models.admin_user import AdminUser`, `from app.services.admin_session_store import AdminSessionStore`.

```python
def get_admin_session_store(redis: Redis = Depends(get_redis)) -> AdminSessionStore:
    return AdminSessionStore(
        redis=redis, ttl_seconds=settings.ADMIN_SESSION_TTL_SECONDS
    )


async def require_admin(
    admin_session: str | None = Cookie(
        default=None, alias=settings.ADMIN_SESSION_COOKIE_NAME
    ),
    store: AdminSessionStore = Depends(get_admin_session_store),
    db: Session = Depends(get_db),
) -> AdminUser:
    """Authorize an admin endpoint via the opaque session cookie.

    401 when no/expired session; 403 when the admin row is disabled.
    Refreshes the sliding TTL on every successful call.
    """
    if not admin_session:
        raise HTTPException(
            status_code=401,
            detail={"code": "ADMIN_AUTH_REQUIRED", "message": "Admin auth required"},
        )
    data = await store.get(admin_session)
    if data is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "ADMIN_SESSION_EXPIRED", "message": "Session expired"},
        )
    try:
        admin_uuid = UUID(data["admin_id"])
    except (TypeError, ValueError, KeyError):
        raise HTTPException(
            status_code=401,
            detail={"code": "ADMIN_SESSION_EXPIRED", "message": "Session expired"},
        )
    admin = db.query(AdminUser).filter(AdminUser.id == admin_uuid).first()
    if admin is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "ADMIN_SESSION_EXPIRED", "message": "Session expired"},
        )
    if admin.is_active is False:
        raise HTTPException(
            status_code=403,
            detail={"code": "ADMIN_DISABLED", "message": "Admin account disabled"},
        )
    await store.refresh(admin_session)
    return admin


def require_admin_csrf(
    request: Request,
    admin_csrf: str | None = Cookie(
        default=None, alias=settings.ADMIN_CSRF_COOKIE_NAME
    ),
) -> None:
    """Double-submit CSRF check for admin write endpoints. The csrf cookie
    is non-httpOnly so the dashboard JS can echo it in X-CSRF-Token."""
    header = request.headers.get("X-CSRF-Token")
    if not admin_csrf or not header or header != admin_csrf:
        raise HTTPException(
            status_code=403, detail={"code": "CSRF_FAILED", "message": "CSRF check failed"}
        )
```

Confirm `settings` and `UUID` are already imported in `deps.py` (they are — used by `get_current_user`).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/api/test_admin_auth_deps.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/api/deps.py tests/api/test_admin_auth_deps.py
git commit -m "feat(admin): cookie-session require_admin + CSRF dep"
```

---

## Task 5: `POST /admin/login` + `POST /admin/logout`

**Files:**
- Create: `app/api/v1/endpoints/admin_auth.py`
- Modify: `app/api/v1/api.py` (include router)
- Create: `tests/api/conftest.py` (shared `admin_client` + `login_admin` fixtures reused by Tasks 8–15)
- Test: `tests/api/test_admin_login.py`

> **Shared fixtures (define once in `tests/api/conftest.py`):** the `admin_client`
> fixture below, plus a `login_admin` fixture that seeds an `AdminUser`, creates a
> session in the fixture's `fake_redis`, sets the `admin_session` + `admin_csrf` cookies
> on the client, and returns the csrf token. Every admin-API test in Tasks 8–15 depends
> on these two by name — do not redefine them per-module.
>
> ```python
> # tests/api/conftest.py
> import pytest, pytest_asyncio
> from fakeredis.aioredis import FakeRedis
> from httpx import ASGITransport, AsyncClient
> from app.api.deps import get_db, get_redis
> from app.core.limiter import limiter
> from app.core.security import hash_password
> from app.db.models.admin_user import AdminUser
> from app.main import app
> from app.services.admin_session_store import AdminSessionStore
>
> @pytest_asyncio.fixture
> async def admin_ctx(db_session):
>     fake_redis = FakeRedis(decode_responses=True)
>     app.dependency_overrides[get_db] = lambda: (yield db_session)
>     async def _get_redis(): return fake_redis
>     app.dependency_overrides[get_redis] = _get_redis
>     limiter.enabled = False
>     transport = ASGITransport(app=app)
>     async with AsyncClient(transport=transport, base_url="http://test") as c:
>         yield c, db_session, fake_redis
>     limiter.enabled = True
>     await fake_redis.aclose()
>     app.dependency_overrides.clear()
>
> @pytest_asyncio.fixture
> async def admin_client(admin_ctx):
>     c, _db, _r = admin_ctx
>     return c
>
> @pytest_asyncio.fixture
> async def login_admin(admin_ctx):
>     c, db, fake_redis = admin_ctx
>     async def _login(_client=None, _db=None, *, email="ops@x.com"):
>         admin = AdminUser(email=email, password_hash=hash_password("pw"), full_name="Ops")
>         db.add(admin); db.commit(); db.refresh(admin)
>         store = AdminSessionStore(redis=fake_redis, ttl_seconds=3600)
>         sid = await store.create(admin_id=str(admin.id), role="superadmin")
>         csrf = "csrf-test-token"
>         c.cookies.set("admin_session", sid)
>         c.cookies.set("admin_csrf", csrf)
>         return csrf
>     return _login
> ```
>
> NOTE: `lambda: (yield db_session)` is illustrative; implement `override_get_db` as a
> proper generator function as in `tests/api/test_admin_refunds.py`. In tests call
> `await login_admin()` (the seeded admin + cookies attach to the shared `admin_client`).

- [ ] **Step 1: Write the failing test**

```python
# tests/api/test_admin_login.py
import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_db, get_redis
from app.core.limiter import limiter
from app.core.security import hash_password
from app.db.models.admin_user import AdminUser
from app.main import app


@pytest_asyncio.fixture
async def admin_client(db_session):
    def _get_db():
        yield db_session

    fake_redis = FakeRedis(decode_responses=True)

    async def _get_redis():
        return fake_redis

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_redis] = _get_redis
    limiter.enabled = False
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    limiter.enabled = True
    await fake_redis.aclose()
    app.dependency_overrides.clear()


def _seed_admin(db, email="ops@x.com", password="s3cret-pass"):
    admin = AdminUser(email=email, password_hash=hash_password(password), full_name="Ops")
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin


@pytest.mark.asyncio
async def test_login_bad_credentials_401(admin_client, db_session):
    _seed_admin(db_session)
    r = await admin_client.post("/api/v1/admin/login", json={"email": "ops@x.com", "password": "wrong"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "ADMIN_INVALID_CREDENTIALS"


@pytest.mark.asyncio
async def test_login_sets_cookies_and_logout(admin_client, db_session):
    _seed_admin(db_session)
    r = await admin_client.post("/api/v1/admin/login", json={"email": "ops@x.com", "password": "s3cret-pass"})
    assert r.status_code == 200
    assert "admin_session" in r.cookies
    assert "admin_csrf" in r.cookies
    body = r.json()["data"]
    assert body["admin"]["email"] == "ops@x.com"
    assert body["csrf_token"] == r.cookies["admin_csrf"]

    # logout clears the session (needs CSRF header)
    csrf = r.cookies["admin_csrf"]
    r2 = await admin_client.post("/api/v1/admin/logout", headers={"X-CSRF-Token": csrf})
    assert r2.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/api/test_admin_login.py -q`
Expected: FAIL — 404 (route not mounted).

- [ ] **Step 3: Implement the endpoints**

```python
# app/api/v1/endpoints/admin_auth.py
"""Admin auth — login/logout. Opaque session cookie, not JWT.

Kept separate from admin.py (ops endpoints) so each file has one job.
"""
import secrets
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.api.deps import get_admin_session_store, get_db
from app.core.config import settings
from app.core.limiter import limiter
from app.core.security import verify_password
from app.db.models.admin_user import AdminUser
from app.services.admin_session_store import AdminSessionStore
from app.utils.responses import success

router = APIRouter(prefix="/admin", tags=["admin-auth"])


class AdminLoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


def _set_admin_cookies(response: Response, *, sid: str, csrf: str) -> None:
    common = dict(
        domain=settings.ADMIN_COOKIE_DOMAIN,
        secure=settings.ADMIN_COOKIE_SECURE,
        samesite="lax",
        max_age=settings.ADMIN_SESSION_TTL_SECONDS,
        path="/",
    )
    response.set_cookie(
        settings.ADMIN_SESSION_COOKIE_NAME, sid, httponly=True, **common
    )
    # csrf cookie is readable by JS (double-submit), so httponly=False.
    response.set_cookie(
        settings.ADMIN_CSRF_COOKIE_NAME, csrf, httponly=False, **common
    )


@router.post("/login", response_model=None)
@limiter.limit("5/minute")
async def admin_login(
    request: Request,
    response: Response,
    body: AdminLoginRequest,
    db: Annotated[Session, Depends(get_db)],
    store: Annotated[AdminSessionStore, Depends(get_admin_session_store)],
):
    admin = (
        db.query(AdminUser)
        .filter(AdminUser.email == body.email.lower())
        .first()
    )
    if admin is None or not verify_password(body.password, admin.password_hash):
        raise HTTPException(
            status_code=401,
            detail={"code": "ADMIN_INVALID_CREDENTIALS", "message": "Invalid credentials"},
        )
    if admin.is_active is False:
        raise HTTPException(
            status_code=403,
            detail={"code": "ADMIN_DISABLED", "message": "Admin account disabled"},
        )

    sid = await store.create(admin_id=str(admin.id), role=admin.role.value)
    csrf = secrets.token_urlsafe(32)
    admin.last_login_at = datetime.now(UTC)
    db.commit()
    _set_admin_cookies(response, sid=sid, csrf=csrf)

    return success(
        {
            "admin": {
                "id": str(admin.id),
                "email": admin.email,
                "full_name": admin.full_name,
                "role": admin.role.value,
            },
            "csrf_token": csrf,
        },
        request_id=getattr(request.state, "request_id", None),
    )


@router.post("/logout", response_model=None)
async def admin_logout(
    request: Request,
    response: Response,
    store: Annotated[AdminSessionStore, Depends(get_admin_session_store)],
    admin_session: Annotated[
        str | None, Cookie(alias=settings.ADMIN_SESSION_COOKIE_NAME)
    ] = None,
):
    # Logout is idempotent: clear whatever's there. CSRF is enforced at the
    # edge by the dashboard sending X-CSRF-Token; we don't hard-require it
    # here so a user with an already-dead session can still clear cookies.
    if admin_session:
        await store.delete(admin_session)
    response.delete_cookie(settings.ADMIN_SESSION_COOKIE_NAME, path="/", domain=settings.ADMIN_COOKIE_DOMAIN)
    response.delete_cookie(settings.ADMIN_CSRF_COOKIE_NAME, path="/", domain=settings.ADMIN_COOKIE_DOMAIN)
    return success({"logged_out": True}, request_id=getattr(request.state, "request_id", None))
```

In `app/api/v1/api.py`: add `admin_auth` to the import tuple and `api_router.include_router(admin_auth.router)`.

> NOTE: `@limiter.limit` requires `request: Request` as a parameter — it's present. The `admin_client` fixture sets `limiter.enabled = False` so the decorator is inert in tests.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/api/test_admin_login.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/api/v1/endpoints/admin_auth.py app/api/v1/api.py tests/api/test_admin_login.py
git commit -m "feat(admin): login/logout with opaque session cookies"
```

---

## Task 6: Re-gate the existing refund-trigger endpoint to the new auth

**Files:**
- Modify: `app/api/v1/endpoints/admin.py` (swap `User`→`AdminUser` dep; add CSRF dep)
- Modify: `tests/api/test_admin_refunds.py` (replace `is_admin` + bearer with cookie session)
- Test: existing `tests/api/test_admin_refunds.py`

- [ ] **Step 1: Update the endpoint**

In `app/api/v1/endpoints/admin.py`:
- Change import `from app.db.models.user import User` usage: the `admin` param becomes `AdminUser`. Add `from app.db.models.admin_user import AdminUser` and `from app.api.deps import require_admin_csrf`.
- Change the handler signature to depend on the rewritten `require_admin` (returns `AdminUser`) and add a CSRF dependency:

```python
@router.post(
    "/refunds/{reference}/trigger",
    response_model=None,
    dependencies=[Depends(require_admin_csrf)],
)
async def admin_trigger_refund(
    reference: str,
    body: ManualRefundRequest,
    request: Request,
    admin: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    bill_svc: Annotated[BillService, Depends(get_bill_service)],
):
```

The body references `admin.id` and `admin.email` — `AdminUser` has both, so the audit-context block is unchanged.

- [ ] **Step 2: Rewrite the refund test's auth helpers**

In `tests/api/test_admin_refunds.py`, replace `_make_admin` (which set `user.is_admin = True`) and the bearer-header usage with a cookie-session helper. Add at module scope:

```python
from app.core.security import hash_password
from app.db.models.admin_user import AdminUser
from app.services.admin_session_store import AdminSessionStore


async def _login_admin(client, db, fake_redis, email="ops@x.com"):
    """Seed an admin + a live session, attach cookies to the client.
    Returns the X-CSRF-Token value for write calls."""
    admin = AdminUser(email=email, password_hash=hash_password("pw"), full_name="Ops")
    db.add(admin)
    db.commit()
    db.refresh(admin)
    store = AdminSessionStore(redis=fake_redis, ttl_seconds=3600)
    sid = await store.create(admin_id=str(admin.id), role="superadmin")
    csrf = "csrf-test-token"
    client.cookies.set("admin_session", sid)
    client.cookies.set("admin_csrf", csrf)
    return csrf
```

The module's `client` fixture already builds a `fake_redis` and overrides `get_redis`; expose that `fake_redis` to tests (return it from the fixture or store on a module global). Simplest: change the fixture to `yield c, fake_redis` and unpack in tests, OR keep a module-level `_fake_redis` set inside the fixture. Update the existing auth-contract tests:
- `test_admin_refund_unauthenticated_rejects_401`: now means "no cookie" → still 401 (code `ADMIN_AUTH_REQUIRED`).
- Replace `test_admin_refund_non_admin_rejects_403` (the old "authenticated mobile user, not admin" case no longer applies — there's no `is_admin`). Replace it with `test_admin_refund_disabled_admin_rejects_403`: seed an admin with `is_active=False`, create a session, assert 403 `ADMIN_DISABLED`.
- Admin happy-path + idempotent-retry tests: call `_login_admin(...)` then POST with `headers={"X-CSRF-Token": csrf}`.

- [ ] **Step 3: Run the refund tests to verify they pass**

Run: `python -m pytest tests/api/test_admin_refunds.py -q`
Expected: PASS (all auth-contract + refund-behavior cases).

- [ ] **Step 4: Commit**

```bash
git add app/api/v1/endpoints/admin.py tests/api/test_admin_refunds.py
git commit -m "refactor(admin): re-gate refund trigger to session auth + CSRF"
```

---

## Task 7: `scripts/create_admin.py` CLI

**Files:**
- Create: `scripts/create_admin.py`
- Test: `tests/scripts/test_create_admin.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/scripts/test_create_admin.py
from app.db.models.admin_user import AdminUser
from scripts.create_admin import create_admin


def test_create_admin_inserts(db_session):
    admin = create_admin(
        db_session, email="boss@x.com", password="longpassword", full_name="Boss"
    )
    assert admin.id is not None
    assert db_session.query(AdminUser).filter_by(email="boss@x.com").count() == 1


def test_create_admin_idempotent(db_session):
    create_admin(db_session, email="boss@x.com", password="longpassword", full_name="Boss")
    again = create_admin(db_session, email="boss@x.com", password="other", full_name="Boss2")
    # returns the existing row, does not duplicate
    assert db_session.query(AdminUser).filter_by(email="boss@x.com").count() == 1
    assert again.full_name == "Boss"  # unchanged
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/scripts/test_create_admin.py -q`
Expected: FAIL — module not found. (Add `tests/scripts/__init__.py` if the suite needs it; mirror existing test package layout.)

- [ ] **Step 3: Implement the CLI**

```python
# scripts/create_admin.py
"""Create the first (or another) admin_users row.

Usage (interactive):
    python scripts/create_admin.py

Idempotent: if the email already exists, prints a notice and exits 0
without modifying the existing row.
"""
import argparse
import getpass

from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.db.models.admin_user import AdminUser
from app.db.session import SessionLocal


def create_admin(db: Session, *, email: str, password: str, full_name: str) -> AdminUser:
    email = email.strip().lower()
    existing = db.query(AdminUser).filter(AdminUser.email == email).first()
    if existing is not None:
        return existing
    admin = AdminUser(
        email=email, password_hash=hash_password(password), full_name=full_name
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin


def main() -> None:
    parser = argparse.ArgumentParser(description="Create an admin user")
    parser.add_argument("--email")
    parser.add_argument("--full-name")
    args = parser.parse_args()

    email = args.email or input("Admin email: ").strip()
    full_name = args.full_name or input("Full name: ").strip()
    password = getpass.getpass("Password: ")
    if len(password) < 10:
        raise SystemExit("Password must be at least 10 characters.")

    db = SessionLocal()
    try:
        admin = create_admin(db, email=email, password=password, full_name=full_name)
        print(f"Admin ready: {admin.email} (id={admin.id})")
    finally:
        db.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/scripts/test_create_admin.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/create_admin.py tests/scripts/test_create_admin.py tests/scripts/__init__.py
git commit -m "feat(admin): create_admin CLI for first-admin seeding"
```

---

# PHASE B — Read endpoints

## Task 8: `AdminService.overview` + `GET /admin/overview`

**Files:**
- Create: `app/services/admin_service.py`
- Modify: `app/api/v1/endpoints/admin.py`
- Test: `tests/services/test_admin_overview.py`, `tests/api/test_admin_overview_api.py`

- [ ] **Step 1: Write the failing service test**

```python
# tests/services/test_admin_overview.py
from datetime import UTC, datetime
from decimal import Decimal

from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.transaction import Transaction
from app.services.admin_service import AdminService


def _tx(db, user_id, *, type_, status, amount):
    from app.utils.references import new_transaction_reference
    tx = Transaction(
        user_id=user_id, reference=new_transaction_reference(user_id=str(user_id)),
        type=type_, status=status, amount=Decimal(amount), fee=Decimal("0.00"), meta={},
    )
    db.add(tx); db.commit(); return tx


def test_overview_counts_and_success_rate(db_session):
    import uuid
    uid = uuid.uuid4()
    _tx(db_session, uid, type_=TransactionType.airtime, status=TransactionStatus.success, amount="1000")
    _tx(db_session, uid, type_=TransactionType.data, status=TransactionStatus.success, amount="500")
    _tx(db_session, uid, type_=TransactionType.airtime, status=TransactionStatus.failed, amount="200")
    svc = AdminService(db=db_session)
    ov = svc.overview(days=7)
    assert ov["transaction_count"] == 3
    assert ov["success_rate"] == round(2 / 3, 4)
    assert ov["volume_ngn"] == "1500.00"   # successful volume only
    types = {m["type"]: m["pct"] for m in ov["service_mix"]}
    assert set(types) <= {"airtime", "data"}
    assert "daily_volume" in ov
    assert ov["needs_attention"]["refunds_awaiting"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/services/test_admin_overview.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `AdminService.overview`**

```python
# app/services/admin_service.py
"""Read-side queries for the admin dashboard. Endpoints stay thin; all
aggregation + listing logic lives here. Computes ONLY metrics backed by
real data — no avg-processing-time (not persisted), no flight metrics."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.transaction import Transaction

_SUCCESS = TransactionStatus.success
_AWAITING = (TransactionStatus.refund_pending, TransactionStatus.refund_failed)
_PENDING = (TransactionStatus.pending, TransactionStatus.processing)


class AdminService:
    def __init__(self, *, db: Session) -> None:
        self._db = db

    def overview(self, *, days: int = 7) -> dict:
        since = datetime.now(UTC) - timedelta(days=days)
        q = self._db.query(Transaction).filter(Transaction.created_at >= since)

        total = q.count()
        success_count = q.filter(Transaction.status == _SUCCESS).count()
        success_rate = round(success_count / total, 4) if total else 0.0

        volume = (
            self._db.query(func.coalesce(func.sum(Transaction.amount), 0))
            .filter(Transaction.created_at >= since, Transaction.status == _SUCCESS)
            .scalar()
        )

        refund_rows = q.filter(Transaction.type == TransactionType.refund)
        refund_count = refund_rows.count()
        refund_total = (
            self._db.query(func.coalesce(func.sum(Transaction.amount), 0))
            .filter(Transaction.created_at >= since, Transaction.type == TransactionType.refund)
            .scalar()
        )

        # service mix — share of successful volume by type
        mix_rows = (
            self._db.query(Transaction.type, func.count(Transaction.id))
            .filter(Transaction.created_at >= since, Transaction.status == _SUCCESS)
            .group_by(Transaction.type)
            .all()
        )
        mix_total = sum(c for _, c in mix_rows) or 1
        service_mix = [
            {"type": t.value, "pct": round(c / mix_total, 4)} for t, c in mix_rows
        ]

        # daily success/failed counts
        daily: dict[str, dict[str, int]] = {}
        for tx in q.with_entities(Transaction.created_at, Transaction.status).all():
            day = tx.created_at.date().isoformat()
            bucket = daily.setdefault(day, {"success": 0, "failed": 0})
            if tx.status == _SUCCESS:
                bucket["success"] += 1
            elif tx.status == TransactionStatus.failed:
                bucket["failed"] += 1
        daily_volume = [
            {"date": d, "success": v["success"], "failed": v["failed"]}
            for d, v in sorted(daily.items())
        ]

        refunds_awaiting = (
            self._db.query(Transaction)
            .filter(Transaction.status.in_(_AWAITING))
            .count()
        )
        pending_over_5min = (
            self._db.query(Transaction)
            .filter(
                Transaction.status.in_(_PENDING),
                Transaction.created_at < datetime.now(UTC) - timedelta(minutes=5),
            )
            .count()
        )

        return {
            "range_days": days,
            "volume_ngn": f"{Decimal(volume):.2f}",
            "transaction_count": total,
            "success_rate": success_rate,
            "refund_count": refund_count,
            "refund_total_ngn": f"{Decimal(refund_total):.2f}",
            "service_mix": service_mix,
            "daily_volume": daily_volume,
            "needs_attention": {
                "refunds_awaiting": refunds_awaiting,
                "transactions_pending_over_5min": pending_over_5min,
            },
        }
```

- [ ] **Step 4: Run service test to verify it passes**

Run: `python -m pytest tests/services/test_admin_overview.py -q`
Expected: PASS.

- [ ] **Step 5: Add the endpoint + API test**

In `app/api/v1/endpoints/admin.py` add (and `from app.services.admin_service import AdminService`, `from app.db.models.admin_user import AdminUser`):

```python
@router.get("/overview", response_model=None)
async def admin_overview(
    request: Request,
    admin: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    days: int = 7,
):
    days = max(1, min(days, 90))
    data = AdminService(db=db).overview(days=days)
    return success(data, request_id=getattr(request.state, "request_id", None))
```

```python
# tests/api/test_admin_overview_api.py  (reuse the admin_client + _login_admin helpers
# from test_admin_login.py by importing them, or replicate the fixture here)
import pytest


@pytest.mark.asyncio
async def test_overview_requires_auth(admin_client):
    r = await admin_client.get("/api/v1/admin/overview")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "ADMIN_AUTH_REQUIRED"
```

Provide a session-authed happy-path test asserting `200` and the presence of `success_rate`, `daily_volume`, `needs_attention` keys, using a `_login_admin`-style helper that seeds an `AdminUser`, creates a session in the fixture's `fake_redis`, and sets the `admin_session` cookie on the client.

- [ ] **Step 6: Run API test to verify it passes**

Run: `python -m pytest tests/api/test_admin_overview_api.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/services/admin_service.py app/api/v1/endpoints/admin.py \
        tests/services/test_admin_overview.py tests/api/test_admin_overview_api.py
git commit -m "feat(admin): overview metrics endpoint"
```

---

## Task 9: `GET /admin/transactions` (list) + `/{reference}` (detail)

**Files:**
- Modify: `app/services/admin_service.py` (add `list_transactions`, `get_transaction_detail`)
- Modify: `app/api/v1/endpoints/admin.py`
- Test: `tests/api/test_admin_transactions.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/api/test_admin_transactions.py — uses the admin_client + _login_admin helpers
import pytest


@pytest.mark.asyncio
async def test_transactions_requires_auth(admin_client):
    r = await admin_client.get("/api/v1/admin/transactions")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_transactions_list_filters_by_status(admin_client, db_session, login_admin):
    # login_admin: fixture/helper that seeds admin + session + cookies, returns csrf
    await login_admin(admin_client, db_session)
    # seed a user + two txns (one success, one failed) via helpers, then:
    r = await admin_client.get("/api/v1/admin/transactions?status=failed&limit=10&offset=0")
    assert r.status_code == 200
    data = r.json()["data"]
    assert "items" in data and "total" in data
    assert all(i["status"] == "failed" for i in data["items"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/api/test_admin_transactions.py -q`
Expected: FAIL — 404.

- [ ] **Step 3: Implement service methods**

Add to `AdminService`:

```python
    def list_transactions(
        self, *, limit: int, offset: int, type_: str | None = None,
        status: str | None = None, date_from: datetime | None = None,
        date_to: datetime | None = None, user_id: str | None = None,
        q: str | None = None,
    ) -> dict:
        from app.db.models.user import User
        query = (
            self._db.query(Transaction, User)
            .join(User, User.id == Transaction.user_id)
        )
        if type_:
            query = query.filter(Transaction.type == TransactionType(type_))
        if status:
            query = query.filter(Transaction.status == TransactionStatus(status))
        if date_from:
            query = query.filter(Transaction.created_at >= date_from)
        if date_to:
            query = query.filter(Transaction.created_at <= date_to)
        if user_id:
            query = query.filter(Transaction.user_id == user_id)
        if q:
            like = f"%{q}%"
            query = query.filter(
                (Transaction.reference.ilike(like)) | (User.full_name.ilike(like))
            )
        total = query.count()
        rows = (
            query.order_by(Transaction.created_at.desc())
            .limit(limit).offset(offset).all()
        )
        items = [
            {
                "reference": tx.reference,
                "type": tx.type.value,
                "status": tx.status.value,
                "amount": f"{tx.amount:.2f}",
                "customer_name": user.full_name,
                "created_at": tx.created_at.isoformat(),
            }
            for tx, user in rows
        ]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def get_transaction_detail(self, *, reference: str) -> dict | None:
        from app.db.models.payment import Payment
        from app.db.models.transaction_event import TransactionEvent
        from app.db.models.user import User
        tx = (
            self._db.query(Transaction)
            .filter(Transaction.reference == reference)
            .first()
        )
        if tx is None:
            return None
        user = self._db.query(User).filter(User.id == tx.user_id).first()
        events = (
            self._db.query(TransactionEvent)
            .filter(TransactionEvent.transaction_id == tx.id)
            .order_by(TransactionEvent.created_at.asc())
            .all()
        )
        payment = (
            self._db.query(Payment)
            .filter(Payment.transaction_id == tx.id)
            .first()
        )
        return {
            "reference": tx.reference,
            "type": tx.type.value,
            "status": tx.status.value,
            "amount": f"{tx.amount:.2f}",
            "fee": f"{tx.fee:.2f}",
            "meta": tx.meta,
            "created_at": tx.created_at.isoformat(),
            "user": None if user is None else {
                "id": str(user.id), "full_name": user.full_name,
                "email": user.email, "phone": user.phone,
                "kyc_tier": user.kyc_level.numeric,
            },
            "payment": None if payment is None else {
                "provider": payment.provider,
                "provider_reference": payment.provider_reference,
                "status": payment.status.value,
                "method": getattr(payment, "method", None),
                "last4": getattr(payment, "last4", None),
                "bank_name": getattr(payment, "bank_name", None),
            },
            "events": [
                {
                    "from_status": e.from_status.value if e.from_status else None,
                    "to_status": e.to_status.value if e.to_status else None,
                    "reason": e.reason,
                    "context": e.context,
                    "created_at": e.created_at.isoformat(),
                }
                for e in events
            ],
        }
```

> Confirm `Payment` field names (`method`/`last4`/`bank_name`) against `app/db/models/payment.py` — the audit found `last4` and `bank_name` exist; `getattr(..., None)` guards any naming drift. Confirm `TransactionEvent.from_status`/`to_status` are enum columns (they are; `admin.py` sets them).

- [ ] **Step 4: Add the endpoints**

```python
@router.get("/transactions", response_model=None)
async def admin_list_transactions(
    request: Request,
    admin: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    limit: int = 20, offset: int = 0,
    type: str | None = None, status: str | None = None,
    date_from: str | None = None, date_to: str | None = None,
    user_id: str | None = None, q: str | None = None,
):
    from datetime import datetime
    limit = max(1, min(limit, 100))
    df = datetime.fromisoformat(date_from) if date_from else None
    dt = datetime.fromisoformat(date_to) if date_to else None
    data = AdminService(db=db).list_transactions(
        limit=limit, offset=offset, type_=type, status=status,
        date_from=df, date_to=dt, user_id=user_id, q=q,
    )
    return success(data, request_id=getattr(request.state, "request_id", None))


@router.get("/transactions/{reference}", response_model=None)
async def admin_transaction_detail(
    reference: str,
    request: Request,
    admin: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    data = AdminService(db=db).get_transaction_detail(reference=reference)
    if data is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "TRANSACTION_NOT_FOUND", "message": "Transaction not found"},
        )
    return success(data, request_id=getattr(request.state, "request_id", None))
```

> ROUTE ORDER: define `/transactions` and `/transactions/{reference}` — FastAPI matches the static prefix fine, but ensure neither shadows the existing requery route added in Task 15 (`/transactions/{reference}/requery` is more specific and registers cleanly).

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/api/test_admin_transactions.py -q`
Expected: PASS (provide list + detail + 404 + auth cases; seed via existing user/tx helpers).

- [ ] **Step 6: Commit**

```bash
git add app/services/admin_service.py app/api/v1/endpoints/admin.py tests/api/test_admin_transactions.py
git commit -m "feat(admin): transactions list + detail endpoints"
```

---

## Task 10: `GET /admin/users` (list) + `/{id}` (detail)

**Files:**
- Modify: `app/services/admin_service.py` (`list_users`, `get_user_detail`)
- Modify: `app/api/v1/endpoints/admin.py`
- Test: `tests/api/test_admin_users.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/api/test_admin_users.py — uses admin_client + login_admin helpers
import pytest


@pytest.mark.asyncio
async def test_users_requires_auth(admin_client):
    r = await admin_client.get("/api/v1/admin/users")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_users_list_returns_full_pii(admin_client, db_session, login_admin):
    await login_admin(admin_client, db_session)
    # seed a user with known email/phone via existing helper
    r = await admin_client.get("/api/v1/admin/users?limit=10")
    assert r.status_code == 200
    data = r.json()["data"]
    assert "items" in data and "total" in data
    # PII returned in full (not masked) — admin is a trusted surface
    if data["items"]:
        assert "@" in data["items"][0]["email"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/api/test_admin_users.py -q`
Expected: FAIL — 404.

- [ ] **Step 3: Implement service methods**

```python
    def list_users(
        self, *, limit: int, offset: int, q: str | None = None,
        tier: str | None = None, status: str | None = None,
    ) -> dict:
        from app.db.models.user import KycLevel, User
        from app.db.models.wallet import Wallet
        query = self._db.query(User, Wallet).outerjoin(Wallet, Wallet.user_id == User.id)
        if q:
            like = f"%{q}%"
            query = query.filter(
                User.full_name.ilike(like) | User.email.ilike(like) | User.phone.ilike(like)
            )
        if tier:
            query = query.filter(User.kyc_level == KycLevel(tier))
        if status == "active":
            query = query.filter(User.is_active.is_(True))
        elif status == "deleted":
            query = query.filter(User.deleted_at.isnot(None))
        total = query.count()
        rows = query.order_by(User.created_at.desc()).limit(limit).offset(offset).all()
        items = [
            {
                "id": str(u.id),
                "full_name": u.full_name,
                "email": u.email,
                "phone": u.phone,
                "kyc_tier": u.kyc_level.numeric,
                "wallet_balance": f"{(w.balance if w else 0):.2f}",
                "status": "deleted" if u.deleted_at else ("active" if u.is_active else "disabled"),
                "created_at": u.created_at.isoformat(),
            }
            for u, w in rows
        ]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def get_user_detail(self, *, user_id: str) -> dict | None:
        import uuid
        from app.db.models.user import User
        from app.db.models.wallet import Wallet
        try:
            uid = uuid.UUID(user_id)
        except (TypeError, ValueError):
            return None
        u = self._db.query(User).filter(User.id == uid).first()
        if u is None:
            return None
        w = self._db.query(Wallet).filter(Wallet.user_id == uid).first()
        recent = (
            self._db.query(Transaction)
            .filter(Transaction.user_id == uid)
            .order_by(Transaction.created_at.desc())
            .limit(10).all()
        )
        referred_count = (
            self._db.query(User).filter(User.referred_by_user_id == uid).count()
        )
        return {
            "id": str(u.id),
            "full_name": u.full_name,
            "email": u.email,
            "phone": u.phone,
            "kyc_tier": u.kyc_level.numeric,
            "email_verified": u.email_verified,
            "phone_verified": u.is_phone_verified,
            "status": "deleted" if u.deleted_at else ("active" if u.is_active else "disabled"),
            "created_at": u.created_at.isoformat(),
            "wallet_balance": f"{(w.balance if w else 0):.2f}",
            "wallet_cap": f"{(w.balance_cap if w else 0):.2f}",
            "referral": {"code": u.referral_code, "referred_count": referred_count},
            "recent_transactions": [
                {
                    "reference": t.reference, "type": t.type.value,
                    "status": t.status.value, "amount": f"{t.amount:.2f}",
                    "created_at": t.created_at.isoformat(),
                }
                for t in recent
            ],
        }
```

- [ ] **Step 4: Add the endpoints**

```python
@router.get("/users", response_model=None)
async def admin_list_users(
    request: Request,
    admin: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    limit: int = 20, offset: int = 0,
    q: str | None = None, tier: str | None = None, status: str | None = None,
):
    limit = max(1, min(limit, 100))
    data = AdminService(db=db).list_users(
        limit=limit, offset=offset, q=q, tier=tier, status=status
    )
    return success(data, request_id=getattr(request.state, "request_id", None))


@router.get("/users/{user_id}", response_model=None)
async def admin_user_detail(
    user_id: str,
    request: Request,
    admin: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    data = AdminService(db=db).get_user_detail(user_id=user_id)
    if data is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "USER_NOT_FOUND", "message": "User not found"},
        )
    return success(data, request_id=getattr(request.state, "request_id", None))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/api/test_admin_users.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/admin_service.py app/api/v1/endpoints/admin.py tests/api/test_admin_users.py
git commit -m "feat(admin): users list + detail endpoints (read-only)"
```

---

## Task 11: `GET /admin/refunds` (list)

**Files:**
- Modify: `app/services/admin_service.py` (`list_refunds`)
- Modify: `app/api/v1/endpoints/admin.py`
- Test: `tests/api/test_admin_refunds_list.py`

- [ ] **Step 1: Write the failing test** — assert auth required (401) + that a seeded refund-type transaction appears with `status` filter honored.

- [ ] **Step 2: Run test to verify it fails** (`python -m pytest tests/api/test_admin_refunds_list.py -q` → 404).

- [ ] **Step 3: Implement `list_refunds`** — refunds are modeled as `Transaction` rows of `type == refund` linked to the original via `meta`/reason. Query refund-type transactions, join the originating user, return `{id(reference), original_reference, type, amount, customer, reason, status, age, manual}`. Derive `manual` from the audit `reason` prefix (`admin_manual_refund`) by checking the linked `TransactionEvent`, else False. Derive `age` from `created_at`.

> Confirm how refunds are stored before coding: inspect `app/services/transaction_service.py::create_refund` to see whether a refund is a `Transaction(type=refund)` row and how it references the original. Implement `list_refunds` to match that actual shape. If a dedicated `refunds` table exists, query that instead — follow the real model.

- [ ] **Step 4: Add the endpoint**

```python
@router.get("/refunds", response_model=None)
async def admin_list_refunds(
    request: Request,
    admin: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    limit: int = 20, offset: int = 0, status: str | None = None,
):
    limit = max(1, min(limit, 100))
    data = AdminService(db=db).list_refunds(limit=limit, offset=offset, status=status)
    return success(data, request_id=getattr(request.state, "request_id", None))
```

- [ ] **Step 5: Run tests** (`python -m pytest tests/api/test_admin_refunds_list.py -q` → PASS).

- [ ] **Step 6: Commit**

```bash
git add app/services/admin_service.py app/api/v1/endpoints/admin.py tests/api/test_admin_refunds_list.py
git commit -m "feat(admin): refunds list endpoint"
```

---

# PHASE C — notification_logs

## Task 12: `notification_logs` model + migration

**Files:**
- Create: `app/db/models/notification_log.py`
- Modify: `app/db/models/__init__.py` (export if needed)
- Create: `alembic/versions/202606041000_add_notification_logs.py`
- Test: `tests/db/test_notification_log_model.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/db/test_notification_log_model.py
from app.db.models.notification_log import (
    NotificationChannel, NotificationLog, NotificationLogStatus,
)


def test_notification_log_row(db_session):
    log = NotificationLog(
        user_id=None, event="otp", channel=NotificationChannel.sms,
        status=NotificationLogStatus.pending, provider="termii",
    )
    db_session.add(log)
    db_session.commit()
    db_session.refresh(log)
    assert log.id is not None
    assert log.status is NotificationLogStatus.pending
    assert log.sent_at is None
```

- [ ] **Step 2: Run test to verify it fails** (module not found).

- [ ] **Step 3: Implement the model**

```python
# app/db/models/notification_log.py
import enum
import uuid

from sqlalchemy import Column, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base
from app.db.mixins import TimestampMixin


class NotificationChannel(str, enum.Enum):
    push = "push"
    email = "email"
    sms = "sms"


class NotificationLogStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    failed = "failed"


class NotificationLog(TimestampMixin, Base):
    __tablename__ = "notification_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    event = Column(String, nullable=False, index=True)
    channel = Column(Enum(NotificationChannel, name="notification_channel_enum"), nullable=False, index=True)
    status = Column(Enum(NotificationLogStatus, name="notification_log_status_enum"), nullable=False, index=True)
    provider = Column(String, nullable=False)
    provider_reference = Column(String, nullable=True)
    error = Column(Text, nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
```

- [ ] **Step 4: Run model test to verify it passes** (PASS on SQLite).

- [ ] **Step 5: Write the migration** `202606041000_add_notification_logs.py` (revises `202606040900`): create the two enums (`checkfirst=True`), create `notification_logs` with the columns above + indexes on `user_id`, `event`, `channel`, `status`, `created_at`. `downgrade()` drops the table + enums.

- [ ] **Step 6: Verify migration chains** (`python -m alembic history | head -3` → `202606041000` head).

- [ ] **Step 7: Commit**

```bash
git add app/db/models/notification_log.py app/db/models/__init__.py \
        alembic/versions/202606041000_add_notification_logs.py tests/db/test_notification_log_model.py
git commit -m "feat(notify): notification_logs table"
```

---

## Task 13: Write path — log every channel send (incl. OTP SMS)

**Files:**
- Create: `app/services/notification_log_service.py`
- Modify: `app/services/notification_service.py` (record email + push sends)
- Modify: `app/services/auth_service.py` (record OTP SMS sends)
- Test: `tests/services/test_notification_log_service.py`, `tests/services/test_notification_dispatch_logs.py`

- [ ] **Step 1: Write the failing service test**

```python
# tests/services/test_notification_log_service.py
from app.db.models.notification_log import (
    NotificationChannel, NotificationLog, NotificationLogStatus,
)
from app.services.notification_log_service import NotificationLogService


def test_record_pending_then_mark_sent(db_session):
    svc = NotificationLogService(db=db_session)
    row = svc.record_pending(user_id=None, event="otp", channel=NotificationChannel.sms, provider="termii")
    assert row.status is NotificationLogStatus.pending
    svc.mark_sent(row, provider_reference="msg-1")
    db_session.refresh(row)
    assert row.status is NotificationLogStatus.sent
    assert row.sent_at is not None
    assert row.provider_reference == "msg-1"


def test_mark_failed(db_session):
    svc = NotificationLogService(db=db_session)
    row = svc.record_pending(user_id=None, event="bill_success", channel=NotificationChannel.email, provider="resend")
    svc.mark_failed(row, error="smtp down")
    db_session.refresh(row)
    assert row.status is NotificationLogStatus.failed
    assert row.error == "smtp down"
```

- [ ] **Step 2: Run test to verify it fails** (module not found).

- [ ] **Step 3: Implement `NotificationLogService`**

```python
# app/services/notification_log_service.py
"""Audit-log writer for every notification channel send. Best-effort:
a logging failure must never break the send it's recording, so callers
wrap writes defensively (the service itself just does the DB work)."""
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.db.models.notification_log import (
    NotificationChannel, NotificationLog, NotificationLogStatus,
)


class NotificationLogService:
    def __init__(self, *, db: Session) -> None:
        self._db = db

    def record_pending(
        self, *, user_id, event: str, channel: NotificationChannel, provider: str
    ) -> NotificationLog:
        row = NotificationLog(
            user_id=user_id, event=event, channel=channel,
            status=NotificationLogStatus.pending, provider=provider,
        )
        self._db.add(row)
        self._db.commit()
        self._db.refresh(row)
        return row

    def mark_sent(self, row: NotificationLog, *, provider_reference: str | None = None) -> None:
        row.status = NotificationLogStatus.sent
        row.sent_at = datetime.now(UTC)
        if provider_reference:
            row.provider_reference = provider_reference
        self._db.commit()

    def mark_failed(self, row: NotificationLog, *, error: str) -> None:
        row.status = NotificationLogStatus.failed
        row.error = error[:2000]
        self._db.commit()
```

- [ ] **Step 4: Run service test to verify it passes** (PASS).

- [ ] **Step 5: Wire into `NotificationService`**

In `notification_service.py`, the service already receives `db` (worker mode). In `_maybe_email` and `_maybe_push`, wrap the provider call: when `self._db` is set, `record_pending(...)` before the send, `mark_sent`/`mark_failed` after, all inside the existing try/except so a logging error degrades gracefully. Use `NotificationChannel.email` / `.push`; `provider` = `"resend"` / `"fcm"`. Pass `user_id` (email path may use `None` if not available — push path has `user_id`).

Add a focused test `tests/services/test_notification_dispatch_logs.py`: construct `NotificationService(email_client=FakeEmailClient(), push_client=FakePushClient(), db=db_session)`, dispatch a `bill_success` event for a seeded user, assert `notification_logs` has an email row `sent` (and a push row, given the fake push succeeds).

- [ ] **Step 6: Wire OTP SMS logging in `auth_service.py`**

At each site where `auth_service` sends an SMS OTP via the SMS provider, wrap with `NotificationLogService(db=self._db)`: `record_pending(user_id=..., event="otp", channel=NotificationChannel.sms, provider="termii")`, then `mark_sent`/`mark_failed`. Keep it inside the existing send flow; a log failure must not block OTP delivery (wrap in try/except logging a warning). Add an assertion to an existing OTP-send test (or a new `tests/services/test_otp_send_logs.py`) that a `notification_logs` SMS row is written on send.

- [ ] **Step 7: Run the notification tests**

Run: `python -m pytest tests/services/test_notification_log_service.py tests/services/test_notification_dispatch_logs.py tests/services/test_otp_send_logs.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/services/notification_log_service.py app/services/notification_service.py \
        app/services/auth_service.py tests/services/test_notification_log_service.py \
        tests/services/test_notification_dispatch_logs.py tests/services/test_otp_send_logs.py
git commit -m "feat(notify): write notification_logs on every channel send"
```

---

## Task 14: `GET /admin/notifications`

**Files:**
- Modify: `app/services/admin_service.py` (`list_notifications`)
- Modify: `app/api/v1/endpoints/admin.py`
- Test: `tests/api/test_admin_notifications.py`

- [ ] **Step 1: Write the failing test** — auth-required 401 + a seeded `NotificationLog` row appears, filterable by `channel`/`status`.

- [ ] **Step 2: Run test to verify it fails** (404).

- [ ] **Step 3: Implement `list_notifications`** in `AdminService`:

```python
    def list_notifications(
        self, *, limit: int, offset: int, channel: str | None = None,
        event: str | None = None, status: str | None = None, user_id: str | None = None,
    ) -> dict:
        from app.db.models.notification_log import (
            NotificationChannel, NotificationLog, NotificationLogStatus,
        )
        query = self._db.query(NotificationLog)
        if channel:
            query = query.filter(NotificationLog.channel == NotificationChannel(channel))
        if status:
            query = query.filter(NotificationLog.status == NotificationLogStatus(status))
        if event:
            query = query.filter(NotificationLog.event == event)
        if user_id:
            query = query.filter(NotificationLog.user_id == user_id)
        total = query.count()
        rows = query.order_by(NotificationLog.created_at.desc()).limit(limit).offset(offset).all()
        items = [
            {
                "id": str(r.id),
                "user_id": str(r.user_id) if r.user_id else None,
                "event": r.event,
                "channel": r.channel.value,
                "status": r.status.value,
                "provider": r.provider,
                "provider_reference": r.provider_reference,
                "error": r.error,
                "created_at": r.created_at.isoformat(),
                "sent_at": r.sent_at.isoformat() if r.sent_at else None,
            }
            for r in rows
        ]
        return {"items": items, "total": total, "limit": limit, "offset": offset}
```

- [ ] **Step 4: Add the endpoint**

```python
@router.get("/notifications", response_model=None)
async def admin_list_notifications(
    request: Request,
    admin: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    limit: int = 20, offset: int = 0,
    channel: str | None = None, event: str | None = None,
    status: str | None = None, user_id: str | None = None,
):
    limit = max(1, min(limit, 100))
    data = AdminService(db=db).list_notifications(
        limit=limit, offset=offset, channel=channel, event=event,
        status=status, user_id=user_id,
    )
    return success(data, request_id=getattr(request.state, "request_id", None))
```

- [ ] **Step 5: Run tests** (PASS).

- [ ] **Step 6: Commit**

```bash
git add app/services/admin_service.py app/api/v1/endpoints/admin.py tests/api/test_admin_notifications.py
git commit -m "feat(admin): notifications log read endpoint"
```

---

# PHASE D — requery write

## Task 15: `POST /admin/transactions/{reference}/requery`

**Files:**
- Modify: `app/api/v1/endpoints/admin.py`
- Test: `tests/api/test_admin_requery.py`

- [ ] **Step 1: Write the failing test** — seed a `pending` airtime tx; with the fake VTPass returning `success` on requery, POST `/requery` (with CSRF header) → 200, tx transitions to `success`. Also: a terminal tx (already `success`) → 200 no-op echoing current state. Auth/CSRF: missing session → 401; missing CSRF header → 403.

- [ ] **Step 2: Run test to verify it fails** (404).

- [ ] **Step 3: Implement the endpoint** — mirror the reconcile-task logic (`app/workers/tasks/reconcile_tasks.py`): for bill-type txns call `BillProvider.requery(request_id=reference)`; for `wallet_funding` call `PaymentProvider.verify(reference=...)`. Apply the resolved status via `bill_svc._tx.transition(...)` / `create_refund` exactly as the reconcile path does, writing a `transaction_events` audit row with `context={"actor_admin_user_id": str(admin.id)}`. No-op (200, current state) if already terminal. Gate with `dependencies=[Depends(require_admin_csrf)]` + `admin: Annotated[AdminUser, Depends(require_admin)]`. Inject providers via the existing `get_vtpass_provider` / `get_paystack_provider` deps.

> Read `reconcile_tasks.py::_reconcile` and `_reconcile_bills` first and reuse their exact transition/refund sequence so behavior matches the automated path. Keep the endpoint thin — if the logic is non-trivial, add a `requery_transaction` method to `AdminService` (or a small helper) rather than fattening the endpoint.

- [ ] **Step 4: Run tests** (PASS).

- [ ] **Step 5: Commit**

```bash
git add app/api/v1/endpoints/admin.py tests/api/test_admin_requery.py
git commit -m "feat(admin): requery pending transaction endpoint"
```

---

## Task 16: Backend full-suite gate

- [ ] **Step 1: Run the whole backend suite**

Run: `python -m pytest -q`
Expected: all green. Investigate and fix any regression (the most likely is a stale `is_admin` reference in a test other than `test_admin_refunds.py`, or a route-order shadowing on `/transactions/{reference}` vs `/transactions/{reference}/requery`).

- [ ] **Step 2: Lint**

Run: `ruff check app/ tests/ scripts/`
Expected: clean (fix any issues).

- [ ] **Step 3: Commit any fixes**

```bash
git add -A && git commit -m "test(admin): full-suite green + lint"
```

---

# PHASE E — platform-admin wiring (`timpbills-marketing`)

> ⚠️ **Before any Next.js code:** read the relevant guide in `node_modules/next/dist/docs/` (per repo `AGENTS.md` — this Next.js diverges from training data). Confirm the current route-handler, server-component data-fetching, and cookie APIs there before writing them. The API client below is framework-agnostic and safe to write directly.

## Task 17: Typed API client + admin login

**Files:**
- Create: `lib/admin-api.ts`
- Create: admin login page (path per the project's routing convention — confirm from docs)
- Modify: `app/platform-admin/layout.tsx` (redirect unauthenticated → login)
- Env: add `NEXT_PUBLIC_ADMIN_API_BASE` (e.g. `https://api.timpbills.com/api/v1` / `http://localhost:8000/api/v1`)

- [ ] **Step 1: Write the API client (framework-agnostic)**

```typescript
// lib/admin-api.ts
const BASE = process.env.NEXT_PUBLIC_ADMIN_API_BASE ?? "http://localhost:8000/api/v1";

function getCsrf(): string {
  if (typeof document === "undefined") return "";
  const m = document.cookie.match(/(?:^|; )admin_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const isWrite = init.method && init.method !== "GET";
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(isWrite ? { "X-CSRF-Token": getCsrf() } : {}),
      ...(init.headers ?? {}),
    },
  });
  const json = await res.json();
  if (!res.ok || json.success === false) {
    throw new Error(json?.error?.code ?? `HTTP_${res.status}`);
  }
  return json.data as T;
}

export const adminApi = {
  login: (email: string, password: string) =>
    call<{ admin: { id: string; email: string; full_name: string; role: string }; csrf_token: string }>(
      "/admin/login", { method: "POST", body: JSON.stringify({ email, password }) }),
  logout: () => call("/admin/logout", { method: "POST" }),
  overview: (days = 7) => call(`/admin/overview?days=${days}`),
  transactions: (qs: string) => call(`/admin/transactions?${qs}`),
  transaction: (ref: string) => call(`/admin/transactions/${ref}`),
  users: (qs: string) => call(`/admin/users?${qs}`),
  user: (id: string) => call(`/admin/users/${id}`),
  refunds: (qs: string) => call(`/admin/refunds?${qs}`),
  notifications: (qs: string) => call(`/admin/notifications?${qs}`),
  triggerRefund: (ref: string, reason: string) =>
    call(`/admin/refunds/${ref}/trigger`, { method: "POST", body: JSON.stringify({ reason }) }),
  requery: (ref: string) =>
    call(`/admin/transactions/${ref}/requery`, { method: "POST" }),
};
```

> Server components that fetch during SSR cannot read `document.cookie`; for those, forward the incoming request cookies (read via the framework's cookies API — confirm from the local docs) and pass them as a `Cookie` header on the fetch. Prefer client-component fetching for the admin tables to keep cookie handling simple, since the dashboard is interactive anyway.

- [ ] **Step 2: Build the login page** — a client component with email/password fields calling `adminApi.login`; on success, the API has set the `admin_session` + `admin_csrf` cookies on `.timpbills.com`, so redirect to `/platform-admin/overview`. On failure show the error code mapped to a friendly message.

- [ ] **Step 3: Gate the dashboard** — in `app/platform-admin/layout.tsx` (or middleware, per the local docs), redirect to the login page when no `admin_session` cookie is present.

- [ ] **Step 4: Manual verification** — `npm run build` succeeds; with the backend running + an admin seeded via `scripts/create_admin.py`, login sets cookies and redirects.

- [ ] **Step 5: Commit**

```bash
cd ../timpbills-marketing
git add lib/admin-api.ts app/platform-admin
git commit -m "feat(admin-ui): API client + admin login/auth gate"
```

## Task 18: Wire dashboard pages to live data + correct the UI

**Files:** all pages under `app/platform-admin/` that import from `data.ts`.

- [ ] **Step 1: Overview** — replace `TRANSACTIONS`-derived mock KPIs with `adminApi.overview()`. **Remove** the avg-processing-time KPI tile and the Amadeus-latency alert (no backing data). Render `success_rate`, `volume_ngn`, `transaction_count`, refund totals, `service_mix`, `daily_volume` (VolumeChart), and `needs_attention` counts.
- [ ] **Step 2: Transactions** — list page → `adminApi.transactions(qs)` with the existing filters; detail page → `adminApi.transaction(ref)` rendering the event timeline + payment + user summary. Add the "Requery" action (calls `adminApi.requery`) on pending rows.
- [ ] **Step 3: Users** — list → `adminApi.users(qs)`; detail → `adminApi.user(id)`. Render full (unmasked) email/phone. No write controls.
- [ ] **Step 4: Refunds** — list → `adminApi.refunds(qs)`; wire the manual-trigger action → `adminApi.triggerRefund(ref, reason)` (CSRF header is automatic).
- [ ] **Step 5: Notifications** — add a notifications page backed by `adminApi.notifications(qs)` (channel/status/event filters).
- [ ] **Step 6: Bookings** — leave on a "ships with flights" deferred state (no API; do not wire). Keep the page so the nav is stable.
- [ ] **Step 7: Delete dead mock** — remove `app/platform-admin/data.ts` (and its imports) once every page is live, EXCEPT any pieces the bookings deferred-state page still needs; if bookings keeps using mock data, leave only the `BOOKINGS`/`Booking` exports and delete the rest.
- [ ] **Step 8: Verify** — `npm run build` clean; manual click-through against the running backend: overview loads real metrics, transactions/users/refunds/notifications list + detail work, refund trigger + requery succeed.
- [ ] **Step 9: Commit**

```bash
git add app/platform-admin
git commit -m "feat(admin-ui): wire dashboard to live admin API; drop mock data"
```

---

## Definition of Done
- All backend tasks committed on `develop`; `python -m pytest -q` green; `ruff check` clean.
- Migrations `202606040900` + `202606041000` chain from `202605260900` and apply cleanly; first admin created via `scripts/create_admin.py`.
- platform-admin builds and runs against the live API: login, overview, transactions (+ detail + requery), users (+ detail), refunds (+ trigger), notifications all functional; bookings shows deferred state.
- No fabricated metrics; PII returned in full on the admin surface.
