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
    ) -> None:
        self.sent.append(
            SentPush(user_id=user_id, title=title, body=body, data=data or {})
        )
        # Dev visibility (matches the FakeEmailClient log style).
        log.info("[FAKE PUSH] user=%s title=%r", user_id, title)
