import logging

from app.config import Settings

log = logging.getLogger("sms")


def send_sms(settings: Settings, to: str, body: str) -> bool:
    """Send an SMS through Twilio. Without credentials it logs the message (dry-run) so demos still work."""
    if not to:
        return False
    if not settings.sms_enabled:
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
