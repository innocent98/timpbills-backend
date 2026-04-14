from dataclasses import dataclass, field


@dataclass
class SentMessage:
    phone: str
    code_or_message: str


@dataclass
class FakeTermiiClient:
    sent: list[SentMessage] = field(default_factory=list)

    async def send_otp(self, *, phone: str, code: str) -> None:
        self.sent.append(SentMessage(phone=phone, code_or_message=code))

    async def send_text(self, *, phone: str, message: str) -> None:
        self.sent.append(SentMessage(phone=phone, code_or_message=message))
