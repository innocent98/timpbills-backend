import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class SentEmail:
    to: str
    subject: str
    code_or_body: str  # for OTP: just the code; for text: the body


@dataclass
class FakeEmailClient:
    """In-memory fake — captures sent emails and logs them to stdout.

    The stdout log is deliberate: during dev you need to see OTPs when hitting
    the backend through ngrok or a remote tunnel and no real email is delivered.
    Read via `make logs` or `docker compose logs api`.
    """

    sent: list[SentEmail] = field(default_factory=list)

    async def send_otp(self, *, to: str, code: str) -> None:
        self.sent.append(
            SentEmail(to=to, subject="Your Timpbills verification code", code_or_body=code)
        )
        log.warning(
            "\n"
            "┌───────────────────────────────────────────────────┐\n"
            "│ FAKE EMAIL (dev) — copy this code into your app   │\n"
            f"│   to:   {to:<36}     │\n"
            f"│   code: {code:<36}     │\n"
            "└───────────────────────────────────────────────────┘"
        )

    async def send_text(
        self, *, to: str, subject: str, html: str, text: str | None = None
    ) -> None:
        self.sent.append(SentEmail(to=to, subject=subject, code_or_body=text or html))
        log.info("[FAKE EMAIL] to=%s subject=%s", to, subject)
