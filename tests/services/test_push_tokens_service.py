"""Tests for PushTokensService (B15)."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.db.models.push_token import PushToken
from app.services.push_tokens_service import PushTokensService


def test_upsert_new_token_creates_row(db_session):
    svc = PushTokensService(db=db_session)
    user_id = uuid4()

    row = svc.upsert_for_user(
        user_id=user_id,
        fcm_token="fcm-abc-123",
        platform="ios",
    )

    assert row.id is not None
    assert row.user_id == user_id
    assert row.fcm_token == "fcm-abc-123"
    assert row.platform == "ios"
    assert row.last_seen_at is not None
    # Exactly one row exists in the DB.
    all_rows = db_session.query(PushToken).all()
    assert len(all_rows) == 1


def test_upsert_existing_token_same_user_updates_last_seen(db_session):
    svc = PushTokensService(db=db_session)
    user_id = uuid4()

    first = svc.upsert_for_user(
        user_id=user_id,
        fcm_token="fcm-same",
        platform="android",
    )
    # Backdate last_seen_at so we can detect the bump.
    first.last_seen_at = datetime.now(timezone.utc) - timedelta(days=1)
    db_session.commit()
    old_seen = first.last_seen_at

    second = svc.upsert_for_user(
        user_id=user_id,
        fcm_token="fcm-same",
        platform="android",
    )

    assert second.id == first.id
    assert second.last_seen_at > old_seen
    # Still one row total.
    assert db_session.query(PushToken).count() == 1


def test_upsert_existing_token_different_user_reassigns(db_session):
    svc = PushTokensService(db=db_session)
    user_a = uuid4()
    user_b = uuid4()

    first = svc.upsert_for_user(
        user_id=user_a,
        fcm_token="fcm-shared-device",
        platform="ios",
    )
    original_id = first.id

    reassigned = svc.upsert_for_user(
        user_id=user_b,
        fcm_token="fcm-shared-device",
        platform="ios",
    )

    # Same row, new owner.
    assert reassigned.id == original_id
    assert reassigned.user_id == user_b
    # No duplicate.
    assert db_session.query(PushToken).count() == 1
    # Old user has no tokens anymore.
    assert svc.list_for_user(user_id=user_a) == []
    # New user sees this one.
    b_tokens = svc.list_for_user(user_id=user_b)
    assert len(b_tokens) == 1
    assert b_tokens[0].id == original_id


def test_list_for_user_orders_by_last_seen_desc(db_session):
    svc = PushTokensService(db=db_session)
    user_id = uuid4()

    older = svc.upsert_for_user(
        user_id=user_id, fcm_token="fcm-older", platform="android"
    )
    newer = svc.upsert_for_user(
        user_id=user_id, fcm_token="fcm-newer", platform="ios"
    )
    middle = svc.upsert_for_user(
        user_id=user_id, fcm_token="fcm-middle", platform="android"
    )

    # Override last_seen_at so ordering is deterministic regardless of
    # sub-millisecond insert timing on fast hardware.
    now = datetime.now(timezone.utc)
    older.last_seen_at = now - timedelta(hours=2)
    middle.last_seen_at = now - timedelta(hours=1)
    newer.last_seen_at = now
    db_session.commit()

    result = svc.list_for_user(user_id=user_id)

    assert [r.fcm_token for r in result] == [
        "fcm-newer",
        "fcm-middle",
        "fcm-older",
    ]


def test_delete_for_user_owner_succeeds_non_owner_fails(db_session):
    svc = PushTokensService(db=db_session)
    owner = uuid4()
    stranger = uuid4()

    row = svc.upsert_for_user(
        user_id=owner, fcm_token="fcm-to-delete", platform="ios"
    )

    # Stranger cannot delete owner's token.
    assert svc.delete_for_user(user_id=stranger, token_id=row.id) is False
    assert db_session.query(PushToken).count() == 1

    # Owner can.
    assert svc.delete_for_user(user_id=owner, token_id=row.id) is True
    assert db_session.query(PushToken).count() == 0

    # Deleting again returns False (row is gone).
    assert svc.delete_for_user(user_id=owner, token_id=row.id) is False


def test_delete_by_fcm_token_removes_regardless_of_user(db_session):
    svc = PushTokensService(db=db_session)
    user_id = uuid4()

    svc.upsert_for_user(
        user_id=user_id, fcm_token="fcm-dead", platform="android"
    )
    assert db_session.query(PushToken).count() == 1

    assert svc.delete_by_fcm_token(fcm_token="fcm-dead") is True
    assert db_session.query(PushToken).count() == 0

    # Missing token: False, no crash.
    assert svc.delete_by_fcm_token(fcm_token="fcm-never-existed") is False


# ── B24 regression: concurrent-INSERT race falls through to UPDATE ──────


def test_upsert_survives_concurrent_insert_race(db_session, monkeypatch):
    """Sprint 4 B24 review: the original SELECT-then-INSERT sequence
    could surface ``IntegrityError`` to the caller when two concurrent
    upserts for the same ``fcm_token`` both observed no row during
    their SELECT windows — one INSERT would win, the other would hit
    the ``fcm_token`` unique constraint and bubble up as a 500.

    We simulate the race by having an existing row in the DB (the
    "winner" of the race), then monkeypatching the first SELECT to
    return ``None`` (the "loser" never saw the winner). The service
    must:
      - Attempt INSERT (since SELECT returned None).
      - Catch IntegrityError on commit, rollback.
      - Re-query (this second SELECT is NOT patched — returns the
        real row), transition to UPDATE path, reassign to our user.
      - Return the reassigned row without raising.
    """
    # Pre-insert a row owned by user_a — this is the "race winner"
    # that exists in the DB but our SELECT will pretend not to see.
    user_a = uuid4()
    user_b = uuid4()
    winner = PushToken(
        user_id=user_a,
        fcm_token="race-token",
        platform="android",
        last_seen_at=datetime.now(timezone.utc),
    )
    db_session.add(winner)
    db_session.commit()
    winner_id = winner.id

    svc = PushTokensService(db=db_session)

    # Monkeypatch the FIRST `one_or_none()` on the query chain to
    # return None — simulating the TOCTOU race window. Subsequent
    # queries (the post-rollback refetch) run normally.
    original_query = db_session.query
    call_count = {"n": 0}

    def _race_query(*args, **kwargs):
        q = original_query(*args, **kwargs)
        if call_count["n"] == 0:
            call_count["n"] += 1
            original_filter = q.filter

            def _filter_one_or_none_first(*fargs, **fkwargs):
                filtered = original_filter(*fargs, **fkwargs)
                # Only the very first SELECT returns a spoofed None.
                original_one = filtered.one_or_none

                def _spoof_none():
                    # Restore real behavior for subsequent calls.
                    filtered.one_or_none = original_one
                    return None

                filtered.one_or_none = _spoof_none
                return filtered

            q.filter = _filter_one_or_none_first
        return q

    monkeypatch.setattr(db_session, "query", _race_query)

    # Drive the upsert. INSERT will fail (unique constraint), the
    # service must recover via the UPDATE path and return the winner's
    # row reassigned to user_b.
    result = svc.upsert_for_user(
        user_id=user_b,
        fcm_token="race-token",
        platform="ios",
    )

    # Reassigned to our user; same row as the pre-existing winner.
    assert result.id == winner_id
    assert result.user_id == user_b
    assert result.platform == "ios"

    # Still exactly one row — no orphaned duplicate from the failed
    # INSERT attempt.
    assert db_session.query(PushToken).count() == 1
