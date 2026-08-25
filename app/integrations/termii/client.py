from typing import Any

import httpx

from app.core.config import settings
from app.core.logger import log
from app.integrations.base import SmsSendError


def _send_url() -> str:
    """Build the ``/api/sms/send`` URL from the configured base.

    Termii's send endpoint is always ``{host}/api/sms/send``. Operators
    configure ``TERMII_BASE_URL`` inconsistently — some set the bare host
    (``https://v3.api.termii.com``), some include the ``/api`` suffix
    (``https://api.ng.termii.com/api``). Normalise either form to the
    canonical path so a trailing-``/api`` config can't produce
    ``/api/api/sms/send`` and a bare-host config can't produce a 404 on
    ``/sms/send`` (the bug this replaces, where the base was hardcoded).
    """
    base = settings.TERMII_BASE_URL.rstrip("/")
    if base.endswith("/api"):
        base = base[: -len("/api")]
    return f"{base}/api/sms/send"


def _to_msisdn(phone: str) -> str:
    """Format a stored phone for Termii's ``to`` field.

    Termii requires international format with **no** leading ``+``
    (e.g. ``2349066128757``); it rejects both ``+234...`` and local
    ``0906...`` forms. Stored numbers are E.164 (``+234...``) so we strip
    the ``+``; we also defensively map a local NG form to international in
    case a non-normalised number ever reaches the client.
    """
    s = phone.strip()
    if s.startswith("+"):
        s = s[1:]
    if s.startswith("0") and len(s) == 11:
        s = "234" + s[1:]
    return s


def _raise_on_termii_error(resp: httpx.Response, *, phone: str) -> None:
    """Turn a Termii response into a clear ``SmsSendError`` on failure.

    Two failure shapes must be caught:

    * Transport failure — HTTP 4xx/5xx. ``raise_for_status`` covers these.
    * In-band failure — HTTP 200 with an error body. Termii returns a
      successful send as ``{"code": "ok", "message_id": ...}``; failures
      (insufficient balance, unapproved sender ID, invalid number) come
      back without ``message_id`` and with ``code`` != ``"ok"`` and/or an
      error ``message``. ``raise_for_status`` alone misses these, leaving
      the OTP silently undelivered while the audit row reads ``sent``.

    The OTP code is never logged here — only the phone (masked downstream
    is unnecessary since this is the provider boundary) and Termii's own
    error message.
    """
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise SmsSendError(
            f"termii http {resp.status_code}: {resp.text[:200]}"
        ) from exc

    try:
        body: Any = resp.json()
    except ValueError as exc:
        raise SmsSendError(
            f"termii returned non-JSON body: {resp.text[:200]}"
        ) from exc

    if not isinstance(body, dict):
        raise SmsSendError(f"termii returned unexpected body: {body!r}")

    code = str(body.get("code", "")).lower()
    has_message_id = bool(body.get("message_id"))
    # A genuine success is ``code == "ok"`` with a message_id. Anything
    # else is an in-band failure regardless of the 200 status.
    if code == "ok" and has_message_id:
        return

    message = str(body.get("message") or body.get("code") or "unknown error")
    log.warning(
        "termii: in-band send failure to=%s code=%s message=%s",
        phone, code or "<none>", message,
    )
    raise SmsSendError(f"termii send failed: {message}")


class TermiiClient:
    def __init__(self) -> None:
        self._key = settings.TERMII_API_KEY
        self._sender = settings.TERMII_SENDER_ID or "N-Alert"

    async def send_otp(self, *, phone: str, code: str) -> None:
        """Deliver an OTP code as a one-segment SMS.

        Channel is read from settings (default ``dnd``). The ``dnd``
        channel bypasses the NCC Do-Not-Disturb registry — essential for
        production OTP delivery on Nigerian carriers — at a higher
        per-SMS cost than the cheaper ``generic`` channel.

        The message body is the **Termii-approved N-Alert template** —
        verbatim. The DND/N-Alert route validates outgoing messages against
        the approved wording, so this string must not drift, and the
        "30 minutes" claim is kept truthful by ``OTP_EXPIRE_MINUTES``.
        """
        await self._send(
            phone=phone,
            message=(
                f"Your TimpBills verification code is {code}. "
                f"It expires in 30 minutes."
            ),
            channel=settings.TERMII_OTP_CHANNEL,
        )

    async def send_text(self, *, phone: str, message: str) -> None:
        """Deliver a non-OTP transactional/notification SMS.

        Stays on the ``generic`` channel because it's cheaper and
        non-OTP messaging doesn't have the DND-registry blocking
        problem to the same degree.
        """
        await self._send(phone=phone, message=message, channel="generic")

    async def _send(self, *, phone: str, message: str, channel: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(
                    _send_url(),
                    json={
                        "to": _to_msisdn(phone),
                        "from": self._sender,
                        "sms": message,
                        "type": "plain",
                        "channel": channel,
                        "api_key": self._key,
                    },
                )
        except httpx.HTTPError as exc:
            raise SmsSendError(f"termii network error: {exc}") from exc
        _raise_on_termii_error(r, phone=phone)
