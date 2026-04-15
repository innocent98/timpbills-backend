from dataclasses import dataclass, field


@dataclass
class SentEmail:
    to: str
    subject: str
    code_or_body: str  # for OTP: just the code; for text: the body


@dataclass
class FakeEmailClient:
    sent: list[SentEmail] = field(default_factory=list)

    async def send_otp(self, *, to: str, code: str) -> None:
        self.sent.append(SentEmail(to=to, subject="Your Timpbills verification code", code_or_body=code))

    async def send_text(self, *, to: str, subject: str, html: str, text: str | None = None) -> None:
        self.sent.append(SentEmail(to=to, subject=subject, code_or_body=text or html))
