"""Celery application + beat schedule."""
from celery import Celery
from celery.schedules import crontab

from app.core.config import settings


celery_app = Celery(
    "timpbills",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.workers.tasks.reconcile_tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
)

celery_app.conf.beat_schedule = {
    "reconcile-pending-payments-every-2min": {
        "task": "app.workers.tasks.reconcile_tasks.reconcile_pending_payments",
        "schedule": crontab(minute="*/2"),
    },
}
