"""Unit tests for the account_deletion_requested notification event
(public account-deletion feature, Task 2).

Follows the same pattern as tests/services/test_dva_notifications.py:
exercise the real internal render helpers directly (`_push_copy` for
push, `render_email` + `_email_subject` for the email side) rather than
a public "render by event" API — NotificationService has no such
method; dispatch always renders internally via these two paths.
"""
from app.integrations.email.renderer import render_email
from app.services.notification_service import (
    _EMAIL_TEMPLATES,
    EVENT_CATEGORY,
    NotificationCategory,
    NotificationEvent,
    _email_subject,
    _push_copy,
)


def _ctx() -> dict:
    return {
        "scheduled_date": "25 August 2026",
        "cancel_url": "https://timpbills.com/delete-account",
    }


def test_account_deletion_event_registered_and_categorised():
    evt = NotificationEvent.account_deletion_requested
    assert evt.value == "account_deletion_requested"
    assert EVENT_CATEGORY[evt] == NotificationCategory.transaction_alerts


def test_account_deletion_push_copy_has_no_dashes():
    ctx = _ctx()
    copy = _push_copy(NotificationEvent.account_deletion_requested, ctx)
    assert copy is not None
    assert "25 August 2026" in copy.body
    assert "—" not in copy.body and "–" not in copy.body
    assert "—" not in copy.title and "–" not in copy.title


def test_account_deletion_email_template_registered():
    assert (
        _EMAIL_TEMPLATES[NotificationEvent.account_deletion_requested]
        == "account_deletion_requested"
    )


def test_account_deletion_email_renders_with_no_dashes():
    ctx = _ctx()
    html, text = render_email("account_deletion_requested", ctx)
    for content in (html, text):
        assert "25 August 2026" in content
        assert "https://timpbills.com/delete-account" in content
        assert "Cancel a pending deletion" in content
        assert "—" not in content and "–" not in content

    subject = _email_subject(NotificationEvent.account_deletion_requested, ctx)
    assert subject == "Your Timpbills account deletion request"
    assert "—" not in subject and "–" not in subject
