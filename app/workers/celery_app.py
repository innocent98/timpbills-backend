"""Celery application + beat schedule."""
from celery import Celery
from celery.schedules import crontab

from app.core.config import settings
from app.core.sentry_setup import setup_sentry

# Worker process needs its own Sentry init; web + worker run as separate
# processes so they don't share the SDK state.
setup_sentry()


celery_app = Celery(
    "timpbills",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=[
        "app.workers.tasks.reconcile_tasks",
        "app.workers.tasks.notification_tasks",
        "app.workers.tasks.referral_tasks",
        "app.workers.tasks.account_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
)
# Eager mode lives in tests/conftest.py — the Celery module is
# imported before the autouse fixture can flip FORCE_FAKE_PROVIDERS,
# so a settings-based read here would be dead code (prod reads False,
# tests are overridden by conftest regardless). Keeping the flag
# exclusively in the test harness makes the production config less
# "clever" and the one place that toggles eager behavior obvious.

celery_app.conf.beat_schedule = {
    "reconcile-pending-payments-every-2min": {
        "task": "app.workers.tasks.reconcile_tasks.reconcile_pending_payments",
        "schedule": crontab(minute="*/2"),
    },
    "reconcile-pending-bills-every-2min": {
        "task": "app.workers.tasks.reconcile_tasks.reconcile_pending_bills",
        "schedule": crontab(minute="*/2"),
    },
    # Backend must-fix #2 — recover DVA inbound-funding credits dropped by a
    # crash mid-webhook (dedup row committed before the wallet credit). Same
    # 2-minute cadence; idempotent per tx.reference so it never double-credits.
    "reconcile-dva-funding-every-2min": {
        "task": "app.workers.tasks.reconcile_tasks.reconcile_dva_funding",
        "schedule": crontab(minute="*/2"),
    },
    # Sprint 5b — nightly referral sweep: re-evaluates pending /
    # referee_cap_pending / clawback_pending rows. Cadence is daily
    # because each bucket is naturally a "tomorrow" problem (daily-cap
    # reset, KYC upgrade, refund-window settle).
    "sweep-referrals-nightly": {
        "task": "app.workers.tasks.referral_tasks.sweep_referrals",
        "schedule": crontab(hour=2, minute=15),  # 02:15 UTC
    },
    # Account-deletion PII purge: anonymize accounts soft-deleted 30+ days
    # ago. Daily is ample; the grace window is measured in days.
    "anonymize-deleted-accounts-daily": {
        "task": "app.workers.tasks.account_tasks.anonymize_deleted_accounts",
        "schedule": crontab(hour=3, minute=0),  # 03:00 UTC
    },
}
