from typing import Protocol


class SmsSendError(Exception):
    """The SMS provider declined or failed to send a message.

    Raised for both transport-level failures (4xx/5xx, network) and the
    in-band failures Termii signals with an HTTP 200 + non-``ok`` body
    (insufficient balance, unapproved sender ID, invalid number). The
    OTP funnel (``AuthService._send_otp_sms``) catches this to mark the
    ``notification_logs`` row failed; the original behaviour of
    propagating to the caller is preserved.
    """


class SmsProvider(Protocol):
    async def send_otp(self, *, phone: str, code: str) -> None: ...
    async def send_text(self, *, phone: str, message: str) -> None: ...
