"""Real FCM HTTP v1 push client (Sprint 4 B14).

This is the production counterpart to FakePushClient. It uses
`google.oauth2.service_account.Credentials` to exchange the
service-account private key for an OAuth2 access token, then POSTs
a well-formed FCM HTTP v1 message to
`https://fcm.googleapis.com/v1/projects/{project_id}/messages:send`.

Two points worth flagging for future maintainers:

  1. **fcm_token kwarg on `send()`** — the Sprint 3 `BasePushClient`
     Protocol has `send(*, user_id, title, body, data)` with NO
     `fcm_token` parameter, because FakePushClient was user-oriented
     (one entry per user). A real FCM call NEEDS the device token —
     that's THE addressing primitive. Per plan §3.8 resolution, we
     extend this class's `send()` with a REQUIRED `fcm_token: str`
     kwarg. We do NOT touch `BasePushClient` in B14 — B17 will either
     tighten the Protocol or leave the wrapper (`NotificationService.
     _maybe_push`) responsible for iterating tokens + calling `send`
     per token. For now, calling `FCMPushClient.send(...)` without
     `fcm_token` is a TypeError, which is the correct failure mode.

  2. **Dead-token detection** — FCM returns 404 `UNREGISTERED`
     (registered-but-revoked) or 400 `INVALID_ARGUMENT` (malformed
     token) when a token should be purged from our `push_tokens`
     table. We raise `DeadFCMToken` (new exception class) with the
     FCM error code in the message so B17 can log / classify it.
     Upstream callers are expected to catch and delete the row.

     5xx is surfaced as `PushTemporaryFailure` — the caller can
     choose to retry (typically via Celery autoretry). We defined
     this locally rather than reusing VTPass's
     `ProviderTemporaryFailure` because the push domain is distinct
     and shouldn't accidentally couple to the bill-provider error
     hierarchy; any bill-provider-specific retry policy should not
     apply to push. Match-pattern cost is one extra import per
     caller; correctness gain is worth it.

Lazy imports: google.auth is imported inside methods to keep module
load fast for tests that don't exercise FCM (mirrors the pattern in
`app/workers/tasks/notification_tasks.py::_resolve_clients`).
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
from datetime import timezone
from typing import Any

import httpx

from app.core.config import settings
from app.core.logger import log


# FCM HTTP v1 endpoint template. Project ID is substituted at send-time.
_FCM_SEND_URL = (
    "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
)

# Scope required by the FCM HTTP v1 API.
_FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

# Refresh the access token when it's within this many seconds of expiry.
# 60s buffer avoids racing a token that's still "valid" but will expire
# mid-request on the wire.
_TOKEN_REFRESH_BUFFER_SECONDS = 60

# FCM error codes that indicate the device token is gone. B17 deletes
# the push_tokens row when we raise DeadFCMToken.
_DEAD_TOKEN_ERROR_CODES = {"UNREGISTERED", "NOT_FOUND", "INVALID_ARGUMENT"}


class DeadFCMToken(RuntimeError):
    """The FCM token is revoked / malformed — caller should delete the
    push_tokens row. Message includes the upstream FCM error-code string
    so B17 can log it for ops triage."""


class PushTemporaryFailure(RuntimeError):
    """Transient failure (5xx / network). Caller may retry — typically
    via Celery autoretry. Separate from VTPass's ProviderTemporaryFailure
    because push and bill-provider retry policies should not share an
    exception hierarchy."""


class FCMPushClient:
    """Production FCM HTTP v1 push client.

    Construction modes (pick ONE):
      - `credentials_path`: path to a service-account JSON key file.
      - `credentials_json`: the JSON contents as a string.
    If both are provided, `credentials_json` wins (inline takes
    precedence — useful when the path was set at image-build time but
    ops rotated keys into a secret-manager-backed env var).

    When neither constructor arg is supplied the client falls back to
    `settings.FCM_CREDENTIALS_PATH` / `settings.FCM_CREDENTIALS_JSON`
    (matches how VTPassClient reads `settings.VTPASS_*` in __init__).
    `project_id` defaults to `settings.FCM_PROJECT_ID` similarly.

    `send()` takes `fcm_token` as a required kwarg — see module
    docstring for the rationale (B14 spec extension beyond
    `BasePushClient`).
    """

    def __init__(
        self,
        *,
        credentials_path: str | None = None,
        credentials_json: str | None = None,
        project_id: str | None = None,
    ) -> None:
        # Fall back to settings so callers (the factory, in B17) can
        # pass nothing and get the canonical config.
        self._credentials_path = credentials_path or settings.FCM_CREDENTIALS_PATH
        self._credentials_json = credentials_json or settings.FCM_CREDENTIALS_JSON
        self._project_id = project_id or settings.FCM_PROJECT_ID

        if not self._credentials_path and not self._credentials_json:
            raise RuntimeError(
                "FCMPushClient: one of FCM_CREDENTIALS_PATH or "
                "FCM_CREDENTIALS_JSON must be set"
            )
        if not self._project_id:
            raise RuntimeError(
                "FCMPushClient: FCM_PROJECT_ID must be set"
            )

        # Cached service-account Credentials object (built lazily on
        # first send()). `.refresh()` is what actually populates .token.
        self._creds: Any | None = None

        # Lock serializing token refresh across concurrent send() calls.
        # Initialized lazily on first _get_access_token() invocation — the
        # class can be constructed outside any running event loop (e.g.
        # at module import / factory wiring time), and asyncio.Lock()
        # binds to the running loop at construction time.
        self._refresh_lock: asyncio.Lock | None = None

    # ── Public API ──────────────────────────────────────────────────────

    async def send(
        self,
        *,
        user_id: str,
        fcm_token: str,
        title: str,
        body: str,
        data: dict[str, str] | None = None,
    ) -> None:
        """Send a push via FCM HTTP v1.

        Raises:
          DeadFCMToken — FCM reports the token is revoked or malformed.
          PushTemporaryFailure — 5xx / network error; safe to retry.
          RuntimeError — any other unexpected 4xx / bad response shape.

        Returns None on success (mirrors FakePushClient.send)."""
        access_token = await self._get_access_token()
        url = _FCM_SEND_URL.format(project_id=self._project_id)
        payload = _build_fcm_message(
            fcm_token=fcm_token, title=title, body=body, data=data,
        )
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        }
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                response = await c.post(url, json=payload, headers=headers)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.NetworkError) as exc:
            raise PushTemporaryFailure(f"fcm network error: {exc}") from exc

        self._handle_response(response, user_id=user_id)

    # ── Internals ───────────────────────────────────────────────────────

    async def _get_access_token(self) -> str:
        """Return a valid FCM access token, refreshing iff the cached
        one is absent / expired / within `_TOKEN_REFRESH_BUFFER_SECONDS`
        of expiring. Caches on `self._creds` so back-to-back sends don't
        each re-exchange the JWT.

        Concurrency: serialized by `self._refresh_lock` so two concurrent
        `send()` callers (FastAPI handler + Celery task is the real
        failure mode) don't both hit the token endpoint. The fast-path
        (cached-and-valid) bypass avoids lock contention on the hot
        path. `credentials.refresh()` is SYNCHRONOUS blocking I/O — it
        must run in a worker thread so it doesn't stall the event loop
        for the ~200-500ms JWT round-trip.
        """
        # Lazy-init the lock on first call — cannot construct in __init__
        # because the class may be built outside any running event loop.
        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()

        # Fast-path: cached creds still valid — skip the lock entirely.
        # Concurrent callers all read cached state; no race because
        # _creds/_creds.token are only *mutated* inside the locked block
        # below, and a stale read here just sends us into the lock where
        # the double-check protects the invariant.
        if self._creds is not None and not self._needs_refresh(self._creds):
            token = getattr(self._creds, "token", None)
            if token:
                return str(token)

        async with self._refresh_lock:
            # Double-check inside the lock — another coroutine may have
            # refreshed while we were waiting for the lock.
            if self._creds is not None and not self._needs_refresh(self._creds):
                token = getattr(self._creds, "token", None)
                if token:
                    return str(token)

            # Lazy imports — keep module load cheap for non-FCM test runs.
            from google.auth.transport.requests import Request  # noqa: PLC0415
            from google.oauth2 import service_account  # noqa: PLC0415

            if self._creds is None:
                # credentials_json wins if both set (see class docstring).
                if self._credentials_json:
                    info = json.loads(self._credentials_json)
                    self._creds = service_account.Credentials.from_service_account_info(
                        info, scopes=[_FCM_SCOPE],
                    )
                else:
                    self._creds = service_account.Credentials.from_service_account_file(
                        self._credentials_path, scopes=[_FCM_SCOPE],
                    )

            if self._needs_refresh(self._creds):
                # refresh() is synchronous blocking I/O — run it on a
                # worker thread so the event loop stays responsive.
                await asyncio.to_thread(self._creds.refresh, Request())

            token = getattr(self._creds, "token", None)
            if not token:
                raise RuntimeError(
                    "FCMPushClient: credentials.refresh() did not yield a token"
                )
            return str(token)

    @staticmethod
    def _needs_refresh(creds: Any) -> bool:
        """Return True iff we should refresh. Treats an absent token or
        an expiry inside the buffer window as needing refresh."""
        if getattr(creds, "token", None) is None:
            return True
        expiry = getattr(creds, "expiry", None)
        if expiry is None:
            # No expiry known — refresh to be safe (first call).
            return True
        # google-auth stores expiry as a naive UTC datetime. We build
        # a tz-aware "now" (datetime.utcnow is deprecated in 3.12+ and
        # becomes an error on newer Pythons) and strip the tzinfo for
        # the comparison — tz-aware vs tz-naive would raise TypeError.
        # If a future google-auth version makes `expiry` tz-aware, this
        # .replace() should be dropped.
        now = _dt.datetime.now(timezone.utc).replace(tzinfo=None)
        if expiry <= now + _dt.timedelta(seconds=_TOKEN_REFRESH_BUFFER_SECONDS):
            return True
        return False

    def _handle_response(self, r: httpx.Response, *, user_id: str) -> None:
        """Translate FCM HTTP v1 response codes to our exception domain.

        Success: 200 OK, body is `{"name": "projects/.../messages/..."}`.
        Dead token: 404 UNREGISTERED/NOT_FOUND or 400 INVALID_ARGUMENT.
        Retryable: 5xx → PushTemporaryFailure.
        Other 4xx: RuntimeError (permanent but not dead-token — e.g.
        quota or malformed payload — surfacing the body so ops can
        triage).
        """
        if r.status_code == 200:
            return

        # Try to pull the FCM error code out of the body — the HTTP v1
        # API shape is `{"error": {"code": 400, "message": "...",
        # "status": "INVALID_ARGUMENT", "details": [{"errorCode":
        # "UNREGISTERED"}]}}`. `errorCode` under details is the FCM-
        # specific enum (more useful than the gRPC-style `status`).
        error_code = _extract_fcm_error_code(r)

        if r.status_code in (400, 404) and error_code in _DEAD_TOKEN_ERROR_CODES:
            log.info(
                "fcm: dead token user=%s status=%s error_code=%s",
                user_id, r.status_code, error_code,
            )
            raise DeadFCMToken(
                f"fcm {r.status_code} {error_code}: {r.text[:200]}"
            )

        if r.status_code >= 500:
            raise PushTemporaryFailure(
                f"fcm {r.status_code}: {r.text[:200]}"
            )

        # Other 4xx — malformed payload, quota, permission, etc. Not a
        # dead-token case; don't want to delete the push_tokens row.
        raise RuntimeError(
            f"fcm {r.status_code} {error_code or 'unknown'}: {r.text[:200]}"
        )


# ── Helpers ─────────────────────────────────────────────────────────────

def _build_fcm_message(
    *,
    fcm_token: str,
    title: str,
    body: str,
    data: dict[str, str] | None,
) -> dict[str, Any]:
    """Construct the FCM HTTP v1 `{"message": {...}}` envelope.

    Android `priority=high` + APNS `apns-priority: 10` ensure the push
    is delivered immediately on both platforms (matches the payload
    shape in the ticket spec). `data` values must be strings per the
    FCM v1 contract — callers should pre-stringify but we guard here
    too so a stray int doesn't 400 at the wire.
    """
    message: dict[str, Any] = {
        "token": fcm_token,
        "notification": {
            "title": title,
            "body": body,
        },
        "android": {"priority": "high"},
        "apns": {"headers": {"apns-priority": "10"}},
    }
    if data:
        message["data"] = {k: str(v) for k, v in data.items()}
    return {"message": message}


def _extract_fcm_error_code(r: httpx.Response) -> str | None:
    """Pull the FCM error code string out of a non-2xx response body.

    FCM HTTP v1 error shape:
      {"error": {"status": "INVALID_ARGUMENT",
                 "details": [{"@type": "...FcmError", "errorCode": "UNREGISTERED"}]}}
    We prefer `details[*].errorCode` (FCM-specific), then fall back to
    `status` (gRPC canonical status). Returns None if the body isn't
    JSON or doesn't carry either field.
    """
    try:
        body = r.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    error = body.get("error") or {}
    if not isinstance(error, dict):
        return None
    details = error.get("details") or []
    if isinstance(details, list):
        for d in details:
            if isinstance(d, dict) and d.get("errorCode"):
                return str(d["errorCode"])
    status = error.get("status")
    return str(status) if status else None
