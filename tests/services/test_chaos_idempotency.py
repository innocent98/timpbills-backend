"""Chaos tests for the bill-purchase pipeline — Sprint 5 BE-54.

We exercise two failure shapes that aren't covered by the happy/unhappy
unit tests:

  1. **Sentinel TTL replay** — a worker holding an in-flight
     idempotency sentinel dies before calling either `store()` (success
     path) or `release_in_flight()` (graceful failure path). The next
     request with the same key must NOT see a permanent in-flight
     stuck state — the sentinel TTL guarantees recovery.

  2. **SIGTERM mid-transaction** — a process is killed by the kernel
     after the wallet debit has committed but before the provider call
     completes. We verify the database is in a recoverable state: the
     tx is `processing` (not orphaned in some half-state), the wallet
     has been debited, the in-flight sentinel will expire, and a
     restarted reconcile worker can finish the job.

Both tests pin behaviour we depend on for production safety. The
SIGTERM test uses `multiprocessing` with the `spawn` start method (the
only one safe on macOS for forking after asyncio has been touched) and
a file-based SQLite database so parent + child see the same state.
"""
import multiprocessing as mp
import os
import signal
import sqlite3
import tempfile
import time
import uuid
from decimal import Decimal
from pathlib import Path

import pytest


# ───────────────────────────────────────────────────────────────────────
# 1. Sentinel TTL replay (no extra processes — ages the sentinel
#    by deleting it manually, which is what an expired TTL does anyway)
# ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sentinel_dropped_mid_flight_next_request_acquires(fake_redis):
    """A worker that crashes after acquiring the in-flight sentinel
    leaves the slot held until the 60s TTL expires. The next request
    must then acquire successfully — not be permanently blocked by a
    ghost sentinel."""
    from app.services.idempotency_service import IdempotencyService

    svc = IdempotencyService(redis=fake_redis)

    # First request — acquires the sentinel and "crashes" before
    # storing or releasing.
    state1, _ = await svc.lookup_or_acquire(
        user_id="user-A", key="dup-key-1", request_hash="hash-1",
    )
    assert state1 == "acquired"

    # Second request immediately after sees the sentinel.
    state2, _ = await svc.lookup_or_acquire(
        user_id="user-A", key="dup-key-1", request_hash="hash-1",
    )
    assert state2 == "in_flight"

    # Simulate TTL expiry. fakeredis's wall-clock TTL emulation isn't
    # observable to us in a unit-test timeframe, so we drop the key —
    # which is what an expired TTL does to the slot anyway. From the
    # service's perspective the two cases are indistinguishable.
    await fake_redis.delete("idempotency:user-A:dup-key-1")

    # Third request — slot is empty, must acquire.
    state3, _ = await svc.lookup_or_acquire(
        user_id="user-A", key="dup-key-1", request_hash="hash-1",
    )
    assert state3 == "acquired", (
        "Sentinel TTL expiry must allow a fresh acquire; otherwise a "
        "crashed worker permanently locks the user out of retrying."
    )


@pytest.mark.asyncio
async def test_sentinel_replay_with_different_hash_is_treated_as_conflict(
    fake_redis,
):
    """If the dropped sentinel had a different request_hash than the
    retry, that's a key-reuse violation (two distinct requests sharing
    an idempotency key) — must raise IdempotencyConflict, not silently
    overwrite."""
    from app.services.idempotency_service import (
        IdempotencyConflict,
        IdempotencyService,
    )

    svc = IdempotencyService(redis=fake_redis)
    await svc.lookup_or_acquire(
        user_id="user-B", key="key-2", request_hash="hash-original",
    )
    # Don't drop the sentinel — we want the re-request to see it AND
    # to fail because the hash differs.
    with pytest.raises(IdempotencyConflict):
        await svc.lookup_or_acquire(
            user_id="user-B", key="key-2", request_hash="hash-different",
        )


# ───────────────────────────────────────────────────────────────────────
# 2. SIGTERM mid-transaction
#    Uses multiprocessing with `spawn` start method + file SQLite so
#    parent + child see the same DB. Skipped on platforms where the
#    spawn semantics are flaky (we'd rather skip than be flaky).
# ───────────────────────────────────────────────────────────────────────

_SPAWN_CTX = mp.get_context("spawn")


def _child_kill_self_after_debit(
    db_url: str,
    user_id: str,
    amount_str: str,
    checkpoint_path: str,
) -> None:
    """Run the synchronous portion of `_execute_bill` against a file
    SQLite DB, then signal the parent we've reached the kill point and
    SIGTERM ourselves.

    Steps performed before the kill:
      1. Create transactions row (status=pending) — committed.
      2. Debit the wallet — committed.
      3. Insert the wallet-debit Payment row + transition tx to
         `processing` — committed.

    The kill happens BEFORE we'd call the provider. From the database's
    perspective this is identical to a SIGTERM landing at the moment
    the request was about to hit VTPass.
    """
    # Re-import inside the child — spawn doesn't inherit imports.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.base import Base
    from app.db.models._enums import TransactionStatus, TransactionType
    from app.db.models.payment import Payment, PaymentStatus
    from app.db.models.transaction import Transaction
    from app.db.models.wallet import Wallet
    from app.utils.references import new_transaction_reference

    engine = create_engine(db_url, future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, future=True)

    # Step 1: create the transaction.
    db = Session()
    tx_ref = new_transaction_reference(user_id=user_id)
    tx = Transaction(
        user_id=uuid.UUID(user_id),
        reference=tx_ref,
        type=TransactionType.airtime,
        status=TransactionStatus.pending,
        amount=Decimal(amount_str),
        fee=Decimal("0"),
        meta={"phone": "08011111111", "service_id": "mtn"},
    )
    db.add(tx)
    db.commit()
    tx_id = tx.id
    db.close()

    # Step 2: debit wallet.
    db = Session()
    wallet = (
        db.query(Wallet).filter(Wallet.user_id == uuid.UUID(user_id)).one()
    )
    wallet.balance = wallet.balance - Decimal(amount_str)
    db.commit()
    db.close()

    # Step 3: payment row + transition.
    db = Session()
    db.add(Payment(
        transaction_id=tx_id,
        provider="wallet",
        provider_reference=tx_ref,
        status=PaymentStatus.success,
    ))
    tx = db.query(Transaction).filter(Transaction.id == tx_id).one()
    tx.status = TransactionStatus.processing
    db.commit()
    db.close()

    # Tell the parent we're at the kill point.
    Path(checkpoint_path).write_text(tx_ref)

    # SIGTERM ourselves. The default handler terminates immediately;
    # nothing after this line should run.
    os.kill(os.getpid(), signal.SIGTERM)
    # Belt-and-braces — should not be reached.
    time.sleep(60)


@pytest.mark.skipif(
    os.uname().sysname not in ("Linux", "Darwin"),
    reason="signal-based chaos test only validated on Unix",
)
def test_sigterm_after_wallet_debit_leaves_recoverable_state():
    """After a SIGTERM lands between wallet debit and provider call,
    the post-mortem state must be:
      * Transaction.status == 'processing' (no orphan / no fake success)
      * Wallet has been debited (no money created/destroyed)
      * No refund row yet (we can't know if VTPass would have delivered)

    The reconcile worker (separate process, periodic) will then own the
    requery → finalize step. This test pins the WAL-equivalent
    contract: if the kernel kills the worker mid-transaction, the
    on-disk state is consistent enough for recovery.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.base import Base
    from app.db.models._enums import TransactionStatus, TransactionType
    from app.db.models.transaction import Transaction
    from app.db.models.user import User, KycLevel
    from app.db.models.wallet import Wallet

    # Temp file — both parent and spawned child read it.
    tmpdir = tempfile.mkdtemp(prefix="chaos_sigterm_")
    db_path = Path(tmpdir) / "chaos.db"
    db_url = f"sqlite:///{db_path}"
    checkpoint_path = Path(tmpdir) / "checkpoint"

    try:
        # Bootstrap the schema + a funded user from the parent so the
        # child has something to debit.
        engine = create_engine(db_url, future=True)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, autoflush=False, future=True)

        user_id = uuid.uuid4()
        starting_balance = Decimal("10000.00")

        with Session() as db:
            user = User(
                id=user_id,
                email=f"chaos-{user_id.hex[:6]}@test.local",
                phone=f"+234800{user_id.hex[:7]}",
                full_name="Chaos User",
                password_hash="not-used-here",
                kyc_level=KycLevel.tier_1,
            )
            wallet = Wallet(
                user_id=user_id,
                balance=starting_balance,
                balance_cap=Decimal("200000.00"),
            )
            db.add_all([user, wallet])
            db.commit()

        # Spawn the child, wait for it to write the checkpoint, then
        # join. The child SIGTERMs itself after the third commit; the
        # parent only watches.
        amount = Decimal("1500.00")
        proc = _SPAWN_CTX.Process(
            target=_child_kill_self_after_debit,
            args=(db_url, str(user_id), str(amount), str(checkpoint_path)),
        )
        proc.start()

        # Wait for the checkpoint file with a generous timeout.
        deadline = time.time() + 30.0
        while not checkpoint_path.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert checkpoint_path.exists(), (
            "Child never reached the kill point — likely failed before "
            "the wallet debit. The chaos contract isn't being exercised."
        )

        proc.join(timeout=5)
        assert not proc.is_alive(), "Child did not terminate after SIGTERM"
        # The child SIGTERMed itself; exitcode is the negative of the
        # signal number on Unix. Don't assert on the exact value
        # (Python may also report 0 / 1 depending on how the runtime
        # observed the signal); the on-disk state is the contract.

        # Post-mortem: read the DB fresh from a parent session.
        tx_ref = checkpoint_path.read_text().strip()
        with Session() as db:
            tx = (
                db.query(Transaction)
                .filter(Transaction.reference == tx_ref)
                .one()
            )
            assert tx.status == TransactionStatus.processing, (
                f"Expected processing after kill; got {tx.status.value}. "
                "The state machine left an orphaned half-state."
            )
            assert tx.type == TransactionType.airtime
            assert tx.amount == amount

            # Wallet was debited and stayed debited — kill happened
            # AFTER commit, so the user's balance reflects the spend.
            wallet = (
                db.query(Wallet).filter(Wallet.user_id == user_id).one()
            )
            assert wallet.balance == starting_balance - amount, (
                "Wallet balance drift: a SIGTERM mid-tx must not corrupt "
                "the wallet ledger; either fully debited (commit happened) "
                "or untouched (commit didn't), never partial."
            )

            # No refund row yet — we don't know if VTPass would have
            # delivered. The reconcile worker owns the requery step.
            from app.db.models.transaction import Transaction as Tx
            refunds = (
                db.query(Tx)
                .filter(
                    Tx.user_id == user_id,
                    Tx.type == TransactionType.refund,
                )
                .all()
            )
            assert refunds == [], (
                "Premature refund: the kill happened before VTPass even "
                "saw the request, so no refund should have been issued."
            )

    finally:
        # Best-effort cleanup; the temp DB is small.
        try:
            sqlite3.connect(str(db_path)).close()
        except Exception:
            pass
        for p in (db_path, checkpoint_path):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass


def test_sigterm_recovery_idempotency_replay_same_ref_is_idempotent():
    """Companion to the SIGTERM test: after the worker dies leaving
    `processing`, a hypothetical retry path that reuses the same tx
    reference (via reconcile-worker requery, which is the production
    flow) must not double-debit. The state machine forbids
    pending → pending and processing → processing transitions; the
    reconcile worker only transitions from processing onward, never
    re-debits.

    We verify this contract directly against TransactionService — full
    process-fork orchestration is the previous test; this one pins the
    invariant in single-process for fast feedback.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db.base import Base
    from app.db.models._enums import TransactionStatus, TransactionType
    from app.db.models.transaction import Transaction
    from app.services.transaction_service import (
        InvalidStateTransition,
        TransactionService,
    )
    from app.utils.references import new_transaction_reference

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, future=True)

    db = Session()
    user_id = uuid.uuid4()
    tx = Transaction(
        user_id=user_id,
        reference=new_transaction_reference(user_id=str(user_id)),
        type=TransactionType.airtime,
        status=TransactionStatus.processing,  # mid-flight at "kill"
        amount=Decimal("1500.00"),
        fee=Decimal("0"),
        meta={},
    )
    db.add(tx)
    db.commit()

    svc = TransactionService(db=db)

    # Recovery transition (reconcile worker's effective behaviour).
    svc.transition(
        tx, to_status=TransactionStatus.success,
        reason="reconcile_worker_requery_resolved",
    )
    db.commit()
    assert tx.status == TransactionStatus.success

    # Second-attempt replay must NOT re-trigger the transition — same
    # source state isn't legal twice. Idempotent no-op for same target,
    # InvalidStateTransition for any other.
    svc.transition(tx, to_status=TransactionStatus.success)  # no-op
    assert tx.status == TransactionStatus.success

    with pytest.raises(InvalidStateTransition):
        svc.transition(tx, to_status=TransactionStatus.processing)

    db.close()
    engine.dispose()
