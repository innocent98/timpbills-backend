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
    # SQLite (this fixture) strips tzinfo on round-trip even for
    # DateTime(timezone=True) columns; Postgres preserves it. Same
    # normalization the codebase already applies at comparison sites in
    # auth_service._ensure_aware_utc / api.deps._token_iat_predates_revocation
    # / pin_service.PinService -- not a change to the assertion itself.
    deleted_at = u.deleted_at if u.deleted_at.tzinfo is not None else u.deleted_at.replace(tzinfo=UTC)
    assert timedelta(days=GRACE_DAYS) - timedelta(minutes=1) <= (sched - deleted_at) <= timedelta(days=GRACE_DAYS) + timedelta(minutes=1)
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


def test_cancel_without_pending_deletion_raises_and_leaves_is_active(db_session):
    # An account that is inactive for a reason OTHER than self-deletion
    # (e.g. a future admin ban: is_active=False, deleted_at IS NULL) must
    # not be reactivatable via the public, unauthenticated cancel endpoint.
    u = _user(db_session)
    u.is_active = False
    db_session.commit()
    svc = AccountDeletionService(db=db_session, token_store=_FakeTokenStore())
    with pytest.raises(ValueError, match="NOT_PENDING_DELETION"):
        svc.cancel_deletion(user=u)
    db_session.refresh(u)
    assert u.is_active is False  # untouched, not flipped back to True


@pytest.mark.asyncio
async def test_request_deletion_on_already_anonymized_user_raises(db_session):
    u = _user(db_session)
    u.deleted_at = datetime.now(UTC) - timedelta(days=40)
    u.anonymized_at = datetime.now(UTC) - timedelta(days=10)
    db_session.commit()
    svc = AccountDeletionService(db=db_session, token_store=_FakeTokenStore())
    with pytest.raises(ValueError, match="ALREADY_ANONYMIZED"):
        await svc.request_deletion(user=u)
