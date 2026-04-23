"""FakePushClient — in-memory capture of sent push messages so tests
can inspect what the notification service dispatched without touching a
real FCM backend."""
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class SentPush:
    user_id: str
    title: str
    body: str
    data: dict[str, str]
    # Populated when NotificationService is in token-aware mode
    # (PushTokensService wired in). None for legacy single-call mode.
    fcm_token: str | None = None


@dataclass
class FakePushClient:
    sent: list[SentPush] = field(default_factory=list)

    async def send(
        self,
        *,
        user_id: str,
        title: str,
        body: str,
        data: dict[str, str] | None = None,
        fcm_token: str | None = None,
    ) -> None:
        self.sent.append(
            SentPush(
                user_id=user_id, title=title, body=body,
                data=data or {}, fcm_token=fcm_token,
            )
        )
        # Dev visibility (matches the FakeEmailClient log style). We
        # deliberately don't emit the raw fcm_token — tokens are device
        # credentials and shouldn't land in logs.
        log.info(
            "[FAKE PUSH] user=%s title=%r device=%s",
            user_id, title, "set" if fcm_token else "none",
        )
