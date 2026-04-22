"""Thin Protocol for push-notification providers.

Real FCM (HTTP v1 API, with google-auth JWT) is a follow-up — the
Python 3.14 / grpcio wheel story that blocked firebase-admin in B0
means we'd ship FCM only after either (a) backend moves to Python 3.11
in Docker (which it already is in CI), or (b) we bypass firebase-admin
entirely with raw httpx + a JWT signer. Both are tracked for Sprint 4.

For Sprint 3 the default implementation is FakePushClient; the Protocol
below is the seam every consumer codes against so the real FCM drop-in
is invisible to the notification service."""
from typing import Protocol


class BasePushClient(Protocol):
    async def send(
        self,
        *,
        user_id: str,
        title: str,
        body: str,
        data: dict[str, str] | None = None,
    ) -> None: ...
