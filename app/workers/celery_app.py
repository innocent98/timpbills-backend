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
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    # When FORCE_FAKE_PROVIDERS is on (tests + some dev setups), run
    # `.delay()` calls synchronously so integration tests can assert on
    # notification side-effects without spinning up a real worker. This
    # is the canonical Celery test-harness pattern; it doesn't affect
    # prod where the flag is False.
    task_always_eager=settings.FORCE_FAKE_PROVIDERS,
    task_eager_propagates=settings.FORCE_FAKE_PROVIDERS,
)

celery_app.conf.beat_schedule = {
    "reconcile-pending-payments-every-2min": {
        "task": "app.workers.tasks.reconcile_tasks.reconcile_pending_payments",
        "schedule": crontab(minute="*/2"),
    },
    "reconcile-pending-bills-every-2min": {
        "task": "app.workers.tasks.reconcile_tasks.reconcile_pending_bills",
        "schedule": crontab(minute="*/2"),
    },
}
