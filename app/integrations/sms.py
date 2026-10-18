import logging
from contextvars import ContextVar

from app.config import Settings

log = logging.getLogger("sms")

# When set (browser demo), every outgoing SMS is also captured here so the page can show it.
outbox: ContextVar[list[dict] | None] = ContextVar("sms_outbox", default=None)


def send_sms(settings: Settings, to: str, body: str) -> bool:
    """Send an SMS through Twilio. Without credentials it logs the message (dry-run) so demos still work."""
    if not to:
        return False
    captured = outbox.get()
    if captured is not None:
        captured.append({"to": to, "body": body})
    if settings.demo_mode or not settings.sms_enabled:
        log.info("[sms dry-run] to=%s body=%s", to, body)
        return False
    try:
        from twilio.rest import Client

        Client(settings.twilio_account_sid, settings.twilio_auth_token).messages.create(
            to=to, from_=settings.twilio_from_number, body=body
        )
        return True
    except Exception:  # an SMS failure must never break the phone call
        log.exception("SMS to %s failed", to)
        return False
