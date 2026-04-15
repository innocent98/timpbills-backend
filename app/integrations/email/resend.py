import resend as resend_sdk

from app.core.config import settings
from app.integrations.email.templates.email_otp import render_email_otp


class ResendClient:
    def __init__(self) -> None:
        resend_sdk.api_key = settings.RESEND_API_KEY
        self._from = f"{settings.EMAIL_FROM_NAME} <{settings.EMAIL_FROM_ADDRESS}>"

    async def send_otp(self, *, to: str, code: str) -> None:
        html, text = render_email_otp(code=code)
        resend_sdk.Emails.send(
            {
                "from": self._from,
                "to": [to],
                "subject": "Your Timpbills verification code",
                "html": html,
                "text": text,
            }
        )

    async def send_text(self, *, to: str, subject: str, html: str, text: str | None = None) -> None:
        resend_sdk.Emails.send(
            {
                "from": self._from,
                "to": [to],
                "subject": subject,
                "html": html,
                "text": text or "",
            }
        )
