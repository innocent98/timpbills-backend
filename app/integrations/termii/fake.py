import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class SentMessage:
    phone: str
    code_or_message: str


@dataclass
class FakeTermiiClient:
    """In-memory fake — captures sent messages and logs them to stdout.

    The stdout log is deliberate: during dev you need to see OTPs when hitting
    the backend through ngrok or a remote tunnel and no real SMS is delivered.
    Read via `make logs` or `docker compose logs api`.
    """

    sent: list[SentMessage] = field(default_factory=list)

    async def send_otp(self, *, phone: str, code: str) -> None:
        self.sent.append(SentMessage(phone=phone, code_or_message=code))
        log.warning(
            "\n"
            "┌───────────────────────────────────────────────────┐\n"
            "│ FAKE SMS (dev) — copy this code into your client  │\n"
            f"│   to:   {phone:<36}     │\n"
            f"│   code: {code:<36}     │\n"
            "└───────────────────────────────────────────────────┘"
        )

    async def send_text(self, *, phone: str, message: str) -> None:
        self.sent.append(SentMessage(phone=phone, code_or_message=message))
        log.info("[FAKE SMS] to=%s message=%s", phone, message)
