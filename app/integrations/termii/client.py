import httpx

from app.core.config import settings


class TermiiClient:
    def __init__(self) -> None:
        self._base = "https://api.ng.termii.com/api"
        self._key = getattr(settings, "TERMII_API_KEY", None)
        self._sender = getattr(settings, "TERMII_SENDER_ID", "Timpbills")

    async def send_otp(self, *, phone: str, code: str) -> None:
        """Deliver an OTP code as a one-segment SMS.

        Channel is read from settings (default ``dnd``). The ``dnd``
        channel bypasses the NCC Do-Not-Disturb registry — essential for
        production OTP delivery on Nigerian carriers — at a higher
        per-SMS cost than the cheaper ``generic`` channel.
        """
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{self._base}/sms/send",
                json={
                    "to": phone,
                    "from": self._sender,
                    "sms": (
                        f"Your Timpbills code is {code}. It expires in 5 "
                        f"minutes. Do not share this code."
                    ),
                    "type": "plain",
                    "channel": settings.TERMII_OTP_CHANNEL,
                    "api_key": self._key,
                },
            )
            r.raise_for_status()

    async def send_text(self, *, phone: str, message: str) -> None:
        """Deliver a non-OTP transactional/notification SMS.

        Stays on the ``generic`` channel because it's cheaper and
        non-OTP messaging doesn't have the DND-registry blocking
        problem to the same degree.
        """
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{self._base}/sms/send",
                json={
                    "to": phone,
                    "from": self._sender,
                    "sms": message,
                    "type": "plain",
                    "channel": "generic",
                    "api_key": self._key,
                },
            )
            r.raise_for_status()
