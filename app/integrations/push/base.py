"""Thin Protocol for push-notification providers.

FakePushClient is the in-memory dev/test implementation. FCMPushClient
(app/integrations/push/fcm.py) is the production HTTP v1 client. The
Protocol accepts an optional `fcm_token` — NotificationService sets it
when operating in token-aware mode (real FCM, one send per registered
device); legacy Sprint 3 callers omit it and the Fake treats it as
metadata for test inspection."""
from typing import Protocol


class BasePushClient(Protocol):
    async def send(
        self,
        *,
        user_id: str,
        title: str,
        body: str,
        data: dict[str, str] | None = None,
        fcm_token: str | None = None,
    ) -> None: ...
