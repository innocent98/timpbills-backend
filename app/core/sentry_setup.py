"""Sentry SDK wiring.

No-op when SENTRY_DSN is unset — dev and tests pay no runtime cost and we
don't need to mock anything. When the DSN is present (staging / prod):

 * FastAPI + Celery integrations capture unhandled exceptions and task
   failures automatically.
 * before_send redacts PII fields (pin, password, bvn, nin, card, cvv,
   secret, authorization) from event data so they never reach Sentry.
 * trace sampling is opt-in; default 5% for cheap request telemetry.

Call `setup_sentry()` once at process start (web + worker).
"""
from __future__ import annotations

from typing import Any

from app.core.config import settings


# Field names (case-insensitive substrings) that must never reach Sentry.
# Matches both top-level keys and nested values inside request/response
# payloads, as well as header names.
_REDACT_SUBSTRINGS = (
    "password",
    "pin",            # matches `pin`, `pin_token`, `new_pin`
    "bvn",
    "nin",
    "card",           # matches `card_number`, `card_cvv`, `card_pan`
    "cvv",
    "secret",
    "token",          # matches auth bearer, refresh, etc.
    "authorization",
    "otp",
)


def _redact_mapping(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: ("[REDACTED]" if _is_sensitive(k) else _redact_mapping(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact_mapping(v) for v in obj]
    return obj


def _is_sensitive(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(s in lowered for s in _REDACT_SUBSTRINGS)


def _before_send(event: dict, _hint: dict) -> dict | None:
    # Walk the common places Sentry stashes request data and redact.
    req = event.get("request")
    if isinstance(req, dict):
        for slot in ("headers", "cookies", "data", "json", "query_string"):
            if slot in req:
                req[slot] = _redact_mapping(req[slot])
    # Extra / contexts can also carry payloads.
    for top in ("extra", "contexts"):
        v = event.get(top)
        if isinstance(v, dict):
            event[top] = _redact_mapping(v)
    return event


def setup_sentry() -> None:
    """Initialize the Sentry SDK if SENTRY_DSN is set. Idempotent."""
    dsn = settings.SENTRY_DSN
    if not dsn:
        return
    # Lazy-import so dev environments without sentry-sdk installed don't
    # crash on boot. (sentry-sdk IS in pyproject; this just keeps import
    # order tidy and makes the tests-without-DSN path fast.)
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration

    sentry_sdk.init(
        dsn=dsn,
        environment=settings.SENTRY_ENVIRONMENT or settings.ENVIRONMENT,
        release=f"{settings.PROJECT_NAME}@{settings.VERSION}",
        integrations=[
            StarletteIntegration(transaction_style="endpoint"),
            FastApiIntegration(transaction_style="endpoint"),
            CeleryIntegration(),
        ],
        traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
        profiles_sample_rate=settings.SENTRY_PROFILES_SAMPLE_RATE,
        send_default_pii=False,
        before_send=_before_send,
    )
