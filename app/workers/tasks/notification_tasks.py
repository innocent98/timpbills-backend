"""Celery task wrapper for NotificationService.dispatch.

The request thread enqueues via `dispatch_delay(...)` and returns
immediately — the worker picks up the job, fans out to email + push,
and logs any failure. We keep the indirection thin so the semantic
surface stays in `NotificationService`.

Wiring:
  from app.workers.tasks.notification_tasks import dispatch_delay
  dispatch_delay(
      user_id=str(user.id),
      user_email=user.email,
      event=NotificationEvent.bill_success,
      context={...},
  )

Callers pass user_email because they already have db access and can
fetch it cheaply; re-opening a SessionLocal inside the worker would
either (a) double-query for no gain in production or (b) miss
transactional visibility under eager mode in tests."""
import asyncio
import threading
from typing import Any, Awaitable

from app.core.logger import log
from app.integrations.email.base import EmailProvider
from app.integrations.push.base import BasePushClient
from app.services.notification_service import (
    NotificationEvent,
    NotificationService,
)
from app.workers.celery_app import celery_app


@celery_app.task(name="app.workers.tasks.notification_tasks.dispatch")
def dispatch(
    *,
    user_id: str,
    user_email: str,
    event: str,
    context: dict[str, Any],
) -> None:
    """Celery entry point. Takes strings + dicts so serialization is
    trivial (Celery default JSON serializer)."""
    try:
        evt = NotificationEvent(event)
    except ValueError:
        log.warning("notify task: unknown event %r — skipping", event)
        return

    email_client, push_client = _resolve_clients()
    svc = NotificationService(
        email_client=email_client, push_client=push_client,
    )
    _run_async(svc.dispatch(
        user_id=user_id, user_email=user_email,
        event=evt, context=context,
    ))


def dispatch_delay(
    *,
    user_id: str,
    user_email: str,
    event: NotificationEvent | str,
    context: dict[str, Any],
) -> None:
    """Non-blocking producer. Accepts the enum (preferred) or its
    string value for callers that come from JSON roundtrips."""
    event_str = event.value if isinstance(event, NotificationEvent) else event
    dispatch.delay(
        user_id=user_id, user_email=user_email,
        event=event_str, context=context,
    )


def _run_async(coro: Awaitable) -> Any:
    """Run an awaitable to completion from a sync context, safe whether
    or not the current thread already has a running event loop.

    Why: In production the Celery worker runs in its own process with
    no loop; `asyncio.run` works. In tests with `task_always_eager=True`,
    `dispatch.delay()` is invoked from inside FastAPI's async handler,
    so a loop IS running and `asyncio.run` raises. Running the coroutine
    on a short-lived background thread sidesteps both cases with one
    code path — and costs ~1ms per dispatch, well under the email /
    push latency we're already accepting."""
    result: dict[str, Any] = {}
    captured: dict[str, BaseException] = {}

    def _target() -> None:
        try:
            result["v"] = asyncio.run(coro)
        except BaseException as exc:   # noqa: BLE001 — propagate everything
            captured["v"] = exc

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join()
    if "v" in captured:
        raise captured["v"]
    return result.get("v")


def _resolve_clients() -> tuple[EmailProvider, BasePushClient]:
    """Pick email + push clients for the Celery worker.

    Important seam (S3C-M9): the worker runs in its OWN process with
    no FastAPI app context, so `app.dependency_overrides[get_email_provider]`
    from tests has no effect here. We reach into the module-level
    singletons directly (`_fake_email_singleton`, the push factory's
    fake). Integration tests that assert on notification side-effects
    must therefore check `app.api.deps._fake_email_singleton.sent`,
    NOT the per-test FakeEmailClient passed through the DI override.
    Already documented in `tests/api/test_notification_wiring.py`.

    Imports are inside the function so importing this module at Celery
    boot doesn't drag provider SDKs into the worker until a task runs."""
    from app.api.deps import _fake_email_singleton, _fake_push_singleton  # noqa: PLC0415
    from app.core.config import settings
    from app.integrations.email.resend import ResendClient
    from app.integrations.push.factory import select_push_client

    if settings.FORCE_FAKE_PROVIDERS or not settings.RESEND_API_KEY:
        email: EmailProvider = _fake_email_singleton
    else:
        email = ResendClient()
    push = select_push_client()
    return email, push
