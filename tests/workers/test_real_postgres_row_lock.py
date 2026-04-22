"""S3C-L1 — prove SELECT FOR UPDATE actually serializes on Postgres.

The rest of the test suite runs against in-memory SQLite with StaticPool.
SQLite PARSES `SELECT FOR UPDATE` but silently ignores it — there is no
row-level locking in SQLite. That means the existing webhook-vs-reconcile
"race" tests (test_reconcile_race.py, the concurrent-finalizer tests in
test_reconcile_bills.py) prove only the *post-lock* status re-check; they
do NOT prove the lock would serialize two real concurrent transactions
against prod-Postgres.

This test closes the gap: spawn two real DB connections to the
docker-compose Postgres service, have connection A acquire FOR UPDATE on
a row, and verify connection B blocks until A commits. With SQLite the
second call would return immediately; with Postgres it waits.

Skipped if Postgres isn't reachable (CI on a pure-sqlite runner, or
running tests without docker compose up).
"""
import threading
import time
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.base import Base
from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.transaction import Transaction
from app.db.models.user import User


def _postgres_url_or_none() -> str | None:
    """Return the configured DATABASE_URL iff it points at a live
    Postgres. Skips the test otherwise."""
    url = settings.DATABASE_URL
    if not url or not url.startswith("postgresql"):
        return None
    try:
        eng = create_engine(url, pool_pre_ping=True)
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        return url
    except Exception:
        return None


@pytest.mark.skipif(
    _postgres_url_or_none() is None,
    reason="Postgres not reachable; S3C-L1 requires real row-level locking",
)
def test_select_for_update_actually_serializes_on_postgres():
    url = _postgres_url_or_none()
    assert url is not None   # guaranteed by the skipif

    engine = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)
    Base.metadata.create_all(engine)   # no-op in an already-migrated DB
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    # Seed a user + tx in a throwaway session.
    setup = Session()
    user = User(
        id=uuid.uuid4(),
        email=f"s3c-l1-{uuid.uuid4().hex[:6]}@t.co",
        phone=f"+234{uuid.uuid4().int % 10**10:010d}",
        full_name="Row Lock Test",
        password_hash="x",
        is_active=True,
    )
    setup.add(user)
    setup.flush()
    tx = Transaction(
        id=uuid.uuid4(),
        user_id=user.id,
        reference=f"TMP-LOCK-{uuid.uuid4().hex[:8]}",
        type=TransactionType.airtime,
        status=TransactionStatus.processing,
        amount=Decimal("500.00"),
        fee=Decimal("0.00"),
        currency="NGN",
    )
    setup.add(tx)
    setup.commit()
    tx_id = tx.id
    setup.close()

    # ── Connection A: acquire FOR UPDATE, hold for 0.5s, commit.
    held = threading.Event()
    released = threading.Event()

    def holder() -> None:
        s = Session()
        try:
            s.query(Transaction).filter(
                Transaction.id == tx_id,
            ).with_for_update().one()
            held.set()
            time.sleep(0.5)
            s.commit()
            released.set()
        finally:
            s.close()

    # ── Connection B: wait for A to hold, then try FOR UPDATE NOWAIT.
    #    With SQLite-style no-op locking, this would succeed instantly.
    #    With real Postgres locking, it raises OperationalError with
    #    'could not obtain lock' in the message.
    def contender() -> tuple[bool, float]:
        held.wait(timeout=2.0)
        t0 = time.monotonic()
        s = Session()
        try:
            s.execute(
                text("SELECT 1 FROM transactions WHERE id = :id FOR UPDATE NOWAIT"),
                {"id": tx_id},
            )
            return (True, time.monotonic() - t0)   # locked without error — BAD
        except OperationalError:
            return (False, time.monotonic() - t0)  # refused — PROOF OF LOCK
        finally:
            s.close()

    t_hold = threading.Thread(target=holder)
    t_hold.start()

    ok_without_refusal, elapsed = contender()
    t_hold.join(timeout=3.0)

    assert not ok_without_refusal, (
        "SELECT FOR UPDATE NOWAIT succeeded on a row that another session "
        "was holding — Postgres row-level locking is NOT working. This "
        "would mean the webhook-vs-reconcile race guards are theatre in "
        "production."
    )
    # Refusal should be immediate (NOWAIT), well under the holder's 0.5s hold.
    assert elapsed < 0.4, (
        f"NOWAIT refusal took {elapsed:.3f}s — expected near-instant."
    )
    assert released.is_set(), "Holder never finished — test harness flake."
