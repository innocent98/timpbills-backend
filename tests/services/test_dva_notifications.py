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
