import httpx
from app.core.config import settings


class TermiiClient:
    def __init__(self) -> None:
        self._base = "https://api.ng.termii.com/api"
        self._key = getattr(settings, "TERMII_API_KEY", None)
        self._sender = getattr(settings, "TERMII_SENDER_ID", "Timpbills")

    async def send_otp(self, *, phone: str, code: str) -> None:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{self._base}/sms/send",
                json={
                    "to": phone,
                    "from": self._sender,
                    "sms": f"Your Timpbills code is {code}. It expires in 5 minutes.",
                    "type": "plain",
                    "channel": "generic",
                    "api_key": self._key,
                },
            )
            r.raise_for_status()

    async def send_text(self, *, phone: str, message: str) -> None:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{self._base}/sms/send",
                json={"to": phone, "from": self._sender, "sms": message, "type": "plain", "channel": "generic", "api_key": self._key},
            )
            r.raise_for_status()
