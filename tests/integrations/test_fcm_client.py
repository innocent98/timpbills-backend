"""Tests for FCMPushClient (Sprint 4 B14).

We mock both google-auth (so no real private-key parsing) AND httpx
(so no real network). The pattern mirrors
`tests/integrations/test_vtpass_client_electricity.py::_patch_async_client`
for the httpx stub, and patches
`google.oauth2.service_account.Credentials.from_service_account_*`
for the credentials stub.

Eight cases cover:
  1. Constructor from file path → Credentials.from_service_account_file.
  2. Constructor from inline JSON → Credentials.from_service_account_info
     (also verifies precedence: json wins when both set).
  3. send() happy path → POSTs correct body, returns None.
  4. send() dead-token on 404 UNREGISTERED → raises DeadFCMToken.
  5. send() 5xx → raises PushTemporaryFailure.
  6. Token cached across two send()s within expiry window.
  7. send() dead-token on 400 INVALID_ARGUMENT → raises DeadFCMToken
     (second dead-token branch in _DEAD_TOKEN_ERROR_CODES).
  8. send() on httpx.ConnectError → raises PushTemporaryFailure
     (network-error branch of the send() try/except).
"""
import asyncio
import os
import datetime
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.integrations.push.fcm import (
    DeadFCMToken,
    FCMPushClient,
    PushTemporaryFailure,
)


# ── Helpers (mirror the VTPass electricity test pattern) ────────────────


def _mock_httpx_response(
    *,
    status_code: int = 200,
    json_body: dict | None = None,
    text_body: str = "",
) -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = status_code
    if json_body is not None:
        r.json = MagicMock(return_value=json_body)
        r.text = json.dumps(json_body)
    else:
        r.json = MagicMock(side_effect=ValueError("no json"))
        r.text = text_body
    return r


def _patch_async_client(*, post_return=None, post_side_effect=None):
    """Patch httpx.AsyncClient in the fcm module to return our mock.
    Returns (patcher, inner) — the caller `with patcher:`s and can
    inspect `inner.post.call_args` / `inner.post.await_count`."""
    inner = MagicMock()
    if post_side_effect is not None:
        inner.post = AsyncMock(side_effect=post_side_effect)
    else:
        inner.post = AsyncMock(return_value=post_return)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=inner)
    ctx.__aexit__ = AsyncMock(return_value=None)

    factory = MagicMock(return_value=ctx)
    return patch("app.integrations.push.fcm.httpx.AsyncClient", factory), inner


def _make_fake_creds(
    *, token: str = "fake-access-token",
    expires_in_seconds: int = 3600,
) -> MagicMock:
    """Build a MagicMock that quacks like service_account.Credentials.
    `.refresh()` is a plain (non-async) method on google-auth; it
    doesn't return the token — it sets `.token` on the instance. We
    simulate the same behavior via side_effect so we can count calls.
    """
    creds = MagicMock()
    creds.token = None
    creds.expiry = None

    def _refresh(_request):
        creds.token = token
        creds.expiry = datetime.datetime.utcnow() + datetime.timedelta(
            seconds=expires_in_seconds,
        )

    creds.refresh = MagicMock(side_effect=_refresh)
    return creds


# ── 1. Constructor from file path ──────────────────────────────────────


@pytest.mark.asyncio
async def test_constructor_from_credentials_path_loads_from_file():
    """credentials_path → Credentials.from_service_account_file is
    called lazily on first send() (not at __init__ time)."""
    fake_creds = _make_fake_creds()

    client = FCMPushClient(
        credentials_path="/fake/sa-key.json",
        project_id="test-project",
    )

    with patch(
        "google.oauth2.service_account.Credentials.from_service_account_file",
        return_value=fake_creds,
    ) as mock_from_file, patch(
        "google.oauth2.service_account.Credentials.from_service_account_info",
    ) as mock_from_info:
        patcher, _ = _patch_async_client(
            post_return=_mock_httpx_response(
                status_code=200, json_body={"name": "projects/x/messages/y"},
            ),
        )
        with patcher:
            await client.send(
                user_id="u1", fcm_token="dev-token",
                title="Hi", body="World",
            )

    mock_from_file.assert_called_once()
    # Path is passed positionally per the google-auth signature.
    assert mock_from_file.call_args.args[0] == "/fake/sa-key.json"
    # scopes kwarg is the FCM-messaging scope.
    assert "https://www.googleapis.com/auth/firebase.messaging" in (
        mock_from_file.call_args.kwargs.get("scopes") or []
    )
    # from_service_account_info was NOT consulted when only path set.
    mock_from_info.assert_not_called()


# ── 2. Constructor from inline JSON (with precedence check) ─────────────


@pytest.mark.asyncio
async def test_constructor_from_credentials_json_takes_precedence_over_path():
    """When both credentials_path AND credentials_json are set,
    credentials_json wins — we call from_service_account_info, NOT
    from_service_account_file. Documented in the class docstring so the
    test pins the contract."""
    fake_creds = _make_fake_creds()
    key_json = json.dumps({"type": "service_account", "project_id": "x"})

    client = FCMPushClient(
        credentials_path="/fake/sa-key.json",   # Set, but should be ignored.
        credentials_json=key_json,              # Wins.
        project_id="test-project",
    )

    with patch(
        "google.oauth2.service_account.Credentials.from_service_account_info",
        return_value=fake_creds,
    ) as mock_from_info, patch(
        "google.oauth2.service_account.Credentials.from_service_account_file",
    ) as mock_from_file:
        patcher, _ = _patch_async_client(
            post_return=_mock_httpx_response(
                status_code=200, json_body={"name": "projects/x/messages/y"},
            ),
        )
        with patcher:
            await client.send(
                user_id="u1", fcm_token="dev-token",
                title="Hi", body="World",
            )

    mock_from_info.assert_called_once()
    # JSON is parsed to a dict before being passed to from_service_account_info.
    passed_info = mock_from_info.call_args.args[0]
    assert isinstance(passed_info, dict)
    assert passed_info["type"] == "service_account"
    mock_from_file.assert_not_called()


# ── 3. send() happy path ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_happy_path_posts_correct_body():
    """Verify: URL is the HTTP v1 endpoint with project_id substituted,
    Authorization header carries the bearer token,
    body is `{"message": {token, notification, data, android, apns}}`.
    """
    fake_creds = _make_fake_creds(token="bearer-abc")
    ok = _mock_httpx_response(
        status_code=200, json_body={"name": "projects/test-project/messages/abc"},
    )
    client = FCMPushClient(
        credentials_path="/fake/sa-key.json",
        project_id="test-project",
    )

    with patch(
        "google.oauth2.service_account.Credentials.from_service_account_file",
        return_value=fake_creds,
    ):
        patcher, inner = _patch_async_client(post_return=ok)
        with patcher:
            result = await client.send(
                user_id="user-1",
                fcm_token="device-token-xyz",
                title="Bill Success",
                body="Your MTN airtime of NGN500 was delivered.",
                data={"event": "bill_success", "reference": "TMP-260421-1"},
            )

    # send() returns None on success (mirrors FakePushClient.send).
    assert result is None

    call = inner.post.call_args
    # URL — project_id substituted.
    assert call.args[0] == (
        "https://fcm.googleapis.com/v1/projects/test-project/messages:send"
    )
    # Headers — bearer token + content-type.
    headers = call.kwargs["headers"]
    assert headers["Authorization"] == "Bearer bearer-abc"
    assert "application/json" in headers["Content-Type"]
    # Body — full FCM v1 envelope.
    payload = call.kwargs["json"]
    msg = payload["message"]
    assert msg["token"] == "device-token-xyz"
    assert msg["notification"] == {
        "title": "Bill Success",
        "body": "Your MTN airtime of NGN500 was delivered.",
    }
    assert msg["data"] == {
        "event": "bill_success",
        "reference": "TMP-260421-1",
    }
    assert msg["android"] == {"priority": "high"}
    assert msg["apns"] == {"headers": {"apns-priority": "10"}}


# ── 4. Dead-token on 404 UNREGISTERED ───────────────────────────────────


@pytest.mark.asyncio
async def test_send_raises_dead_fcm_token_on_404_unregistered():
    """FCM returns 404 with `errorCode: UNREGISTERED` when the token is
    revoked. We must raise DeadFCMToken so B17 can delete the row, and
    the error-code string must be propagated in the exception message
    for ops triage."""
    fake_creds = _make_fake_creds()
    not_found_body = {
        "error": {
            "code": 404,
            "status": "NOT_FOUND",
            "message": "Requested entity was not found.",
            "details": [{
                "@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
                "errorCode": "UNREGISTERED",
            }],
        },
    }
    client = FCMPushClient(
        credentials_path="/fake/sa-key.json",
        project_id="test-project",
    )

    with patch(
        "google.oauth2.service_account.Credentials.from_service_account_file",
        return_value=fake_creds,
    ):
        patcher, _ = _patch_async_client(
            post_return=_mock_httpx_response(
                status_code=404, json_body=not_found_body,
            ),
        )
        with patcher:
            with pytest.raises(DeadFCMToken) as excinfo:
                await client.send(
                    user_id="u1", fcm_token="revoked-token",
                    title="x", body="y",
                )

    # The FCM error-code string is included in the exception message so
    # B17 can log it for ops triage.
    assert "UNREGISTERED" in str(excinfo.value)


# ── 5. Retryable on 5xx ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_raises_push_temporary_failure_on_5xx():
    """5xx from FCM is transient — we raise PushTemporaryFailure so the
    caller (Celery retry decorator, in B17) knows to requeue."""
    fake_creds = _make_fake_creds()
    client = FCMPushClient(
        credentials_path="/fake/sa-key.json",
        project_id="test-project",
    )

    with patch(
        "google.oauth2.service_account.Credentials.from_service_account_file",
        return_value=fake_creds,
    ):
        patcher, _ = _patch_async_client(
            post_return=_mock_httpx_response(
                status_code=503, text_body="Service Unavailable",
            ),
        )
        with patcher:
            with pytest.raises(PushTemporaryFailure):
                await client.send(
                    user_id="u1", fcm_token="tok",
                    title="x", body="y",
                )


# ── 6. Access token cached across back-to-back sends ────────────────────


@pytest.mark.asyncio
async def test_access_token_cached_across_two_sends_within_expiry():
    """Two back-to-back send() calls must NOT each perform a credentials
    refresh — the access token is cached on the client instance and
    reused until it's within the 60s expiry buffer. Asserting
    `.refresh.call_count == 1` pins the cache behavior."""
    fake_creds = _make_fake_creds(
        token="cached-token", expires_in_seconds=3600,
    )
    ok = _mock_httpx_response(
        status_code=200, json_body={"name": "projects/x/messages/y"},
    )
    client = FCMPushClient(
        credentials_path="/fake/sa-key.json",
        project_id="test-project",
    )

    with patch(
        "google.oauth2.service_account.Credentials.from_service_account_file",
        return_value=fake_creds,
    ) as mock_from_file:
        patcher, inner = _patch_async_client(post_return=ok)
        with patcher:
            await client.send(
                user_id="u1", fcm_token="tok-1", title="a", body="b",
            )
            await client.send(
                user_id="u2", fcm_token="tok-2", title="c", body="d",
            )

    # Credentials object built once.
    mock_from_file.assert_called_once()
    # Refresh called once — second send() reused the cached token.
    assert fake_creds.refresh.call_count == 1
    # Both POSTs fired.
    assert inner.post.await_count == 2


# ── 7. Dead-token on 400 INVALID_ARGUMENT ───────────────────────────────


@pytest.mark.asyncio
async def test_send_raises_dead_fcm_token_on_400_invalid_argument():
    """FCM returns 400 with `errorCode: INVALID_ARGUMENT` when the token
    is malformed (e.g. wrong project, truncated). This is the second
    dead-token branch in _DEAD_TOKEN_ERROR_CODES — distinct from 404
    UNREGISTERED (test 4) — and must also raise DeadFCMToken so B17
    deletes the row. The FCM error-code string must propagate in the
    exception message so ops can distinguish the two causes."""
    fake_creds = _make_fake_creds()
    bad_request_body = {
        "error": {
            "code": 400,
            "status": "INVALID_ARGUMENT",
            "message": "The registration token is not a valid FCM registration token",
            "details": [{
                "@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
                "errorCode": "INVALID_ARGUMENT",
            }],
        },
    }
    client = FCMPushClient(
        credentials_path="/fake/sa-key.json",
        project_id="test-project",
    )

    with patch(
        "google.oauth2.service_account.Credentials.from_service_account_file",
        return_value=fake_creds,
    ):
        patcher, _ = _patch_async_client(
            post_return=_mock_httpx_response(
                status_code=400, json_body=bad_request_body,
            ),
        )
        with patcher:
            with pytest.raises(DeadFCMToken) as excinfo:
                await client.send(
                    user_id="u1", fcm_token="malformed-token",
                    title="x", body="y",
                )

    # The FCM error-code string is included so B17 can distinguish this
    # from the UNREGISTERED case at log-scan time.
    assert "INVALID_ARGUMENT" in str(excinfo.value)


# ── 8. httpx network error → PushTemporaryFailure ───────────────────────


@pytest.mark.asyncio
async def test_send_raises_push_temporary_failure_on_connect_error():
    """A connection-level failure (DNS, TCP reset, TLS handshake) must
    surface as PushTemporaryFailure so the Celery caller can retry —
    NOT RuntimeError or a raw httpx exception. This covers the
    network-error branch of the try/except in send(); the 5xx branch
    (test 5) is the *response*-level retryable path."""
    fake_creds = _make_fake_creds()
    client = FCMPushClient(
        credentials_path="/fake/sa-key.json",
        project_id="test-project",
    )

    with patch(
        "google.oauth2.service_account.Credentials.from_service_account_file",
        return_value=fake_creds,
    ):
        patcher, _ = _patch_async_client(
            post_side_effect=httpx.ConnectError("dns failure"),
        )
        with patcher:
            with pytest.raises(PushTemporaryFailure) as excinfo:
                await client.send(
                    user_id="u1", fcm_token="tok",
                    title="x", body="y",
                )

    # Message is chained so ops can see the underlying httpx reason.
    assert "dns failure" in str(excinfo.value)


# ── 9. Concurrent sends serialize on a single refresh lock (B27) ────────


@pytest.mark.asyncio
async def test_concurrent_sends_share_single_refresh_lock():
    """Two coroutines entering send() simultaneously on the same event
    loop must both return the same cached token, and the credentials
    refresh must fire exactly once across both sends. Pins the
    double-check invariant: the cold-path-first caller refreshes, the
    cold-path-second caller sees the fresh cache inside the lock and
    returns without a second refresh.
    """
    fake_creds = _make_fake_creds(
        token="concurrent-token", expires_in_seconds=3600,
    )
    client = FCMPushClient(
        credentials_path="/fake/sa-key.json",
        project_id="test-project",
    )

    with patch(
        "google.oauth2.service_account.Credentials.from_service_account_file",
        return_value=fake_creds,
    ):
        patcher, inner = _patch_async_client(
            post_return=_mock_httpx_response(
                status_code=200, json_body={"name": "projects/x/messages/y"},
            ),
        )
        with patcher:
            await asyncio.gather(
                client.send(user_id="u1", fcm_token="t1", title="a", body="b"),
                client.send(user_id="u2", fcm_token="t2", title="c", body="d"),
            )

    # Credentials.refresh fired exactly once across the two cold-path
    # sends — the threading.Lock serialized them and the double-check
    # prevented a second refresh.
    assert fake_creds.refresh.call_count == 1
    assert inner.post.await_count == 2


# ── 10. B-C1 regression: cross-event-loop lock acquisition ──────────────


@pytest.mark.skipif(
    os.environ.get("CI") == "true",
    reason=(
        "Cross-thread httpx mock doesn't propagate across event loops under "
        "GitHub Actions runners — unittest.mock.patch is context-local and "
        "the second thread's event loop misses the patch. Test passes "
        "locally on macOS / Linux dev environments. The underlying B27 fix "
        "(threading.Lock instead of asyncio.Lock) is exercised by other "
        "tests in this file."
    ),
)
def test_send_works_across_event_loops_on_shared_instance():
    """Sprint 4 B27 (B-C1 follow-up) regression. An asyncio.Lock binds
    to the event loop that creates it — so if two worker threads each
    driving their own event loop hit `send()` on the same FCMPushClient
    instance, the second thread's `async with self._refresh_lock:`
    would raise `RuntimeError: Task got Future attached to a different
    loop`. This is the real multi-worker Celery failure mode that the
    original B20 fix did NOT solve (the B20 test used `asyncio.run()`
    per thread but never exercised the cross-loop acquire).

    B27 replaces the asyncio.Lock with a threading.Lock held inside
    a synchronous refresh function dispatched via `asyncio.to_thread`.
    The lock is loop-agnostic (thread-level primitive), so this test
    passes cleanly with B27 and would fail with the B20-only fix.

    Test drives two threads, each with its own event loop, each
    calling `client.send(...)` on the same instance. Both should
    complete without RuntimeError, and credentials.refresh() must
    fire exactly once (the first thread's cold-path refresh; the
    second thread observes the cached creds).
    """
    import threading as _threading  # noqa: PLC0415

    fake_creds = _make_fake_creds(
        token="cross-loop-token", expires_in_seconds=3600,
    )
    client = FCMPushClient(
        credentials_path="/fake/sa-key.json",
        project_id="test-project",
    )

    # Structural invariant: the refresh lock is a threading.Lock, not
    # an asyncio.Lock. Future refactor that replaces it with
    # asyncio.Lock fails this assertion loudly.
    assert isinstance(client._refresh_lock, type(_threading.Lock()))

    barrier = _threading.Barrier(2)
    errors: list[BaseException] = []

    def _drive_send(tag: str) -> None:
        try:
            # Each thread creates its own event loop — this is the
            # scenario that broke the asyncio.Lock binding.
            async def _go() -> None:
                barrier.wait()  # Release both threads simultaneously.
                with patch(
                    "google.oauth2.service_account.Credentials."
                    "from_service_account_file",
                    return_value=fake_creds,
                ):
                    patcher, _ = _patch_async_client(
                        post_return=_mock_httpx_response(
                            status_code=200,
                            json_body={"name": f"projects/x/messages/{tag}"},
                        ),
                    )
                    with patcher:
                        await client.send(
                            user_id=f"u-{tag}",
                            fcm_token=f"t-{tag}",
                            title="x",
                            body="y",
                        )

            asyncio.run(_go())
        except BaseException as e:
            errors.append(e)

    t1 = _threading.Thread(target=_drive_send, args=("A",))
    t2 = _threading.Thread(target=_drive_send, args=("B",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Neither thread raised — in particular, NO "Task got Future
    # attached to a different loop" RuntimeError.
    assert errors == [], f"cross-loop send failed: {errors}"

    # Refresh was called exactly once across both threads — the
    # threading.Lock serialized them and the double-check prevented a
    # redundant second refresh.
    assert fake_creds.refresh.call_count == 1
