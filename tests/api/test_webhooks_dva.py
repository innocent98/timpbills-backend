import json
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.api.deps import (
    get_db,
    get_redis,
    get_token_store,
    get_email_provider,
    reset_fake_sms,
    reset_fake_email,
    reset_fake_paystack,
)
from app.integrations.email.fake import FakeEmailClient
from app.core.limiter import limiter
from fakeredis.aioredis import FakeRedis
from app.services.token_store import RedisTokenStore

import tests.e2e.test_auth_full_flows as _e2e_mod
from tests.e2e.test_auth_full_flows import _seed_logged_in_user

from app.db.models._enums import SpendLockReason, VirtualAccountStatus
from app.db.models.user import User
from app.db.models.virtual_account import VirtualAccount
from app.db.models.wallet import Wallet
from app.integrations.paystack.fake import (
    customer_identification_event,
    dedicated_account_assign_event,
    dva_charge_event,
)

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
    _test_email_client.sent.clear()

    # Patch the e2e module's email client so _seed_logged_in_user reads our emails
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
async def test_dva_transfer_credits_wallet_when_only_top_level_channel_set(db_session, client):
    # Regression: Paystack's spec (and real payloads) may set
    # data.channel == "dedicated_nuban" without also setting
    # data.authorization.channel. The DVA branch must match on EITHER
    # location, or the transfer falls through to the reference/Payment
    # path, finds no Payment, and the money is never credited.
    user, headers, va = await _seed_va(db_session, client, status=VirtualAccountStatus.active)
    va.account_number = "9988776655"
    db_session.commit()

    ev = dva_charge_event(
        account_number="9988776655", amount_kobo=500000, event_id="dva_toplevel_1")
    assert ev["data"]["channel"] == "dedicated_nuban"
    ev["data"]["authorization"]["channel"] = "card-like"

    r = await _post(client, ev)
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
