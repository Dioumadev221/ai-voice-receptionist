"""Two-way Google Calendar sync through a service account.

- Appointments booked by the agent are pushed to the clinic calendar.
- Anything staff put on that calendar themselves (meetings, days off) blocks those times for the agent.
"""

import json
import logging
import os
from datetime import date, datetime, time, timezone
from functools import lru_cache

from app.config import Settings, get_settings
from app.models import Appointment

log = logging.getLogger("calendar")
SCOPES = ["https://www.googleapis.com/auth/calendar"]


@lru_cache
def _service(raw_key: str):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    info = json.load(open(raw_key)) if os.path.exists(raw_key) else json.loads(raw_key)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _event_body(settings: Settings, appt: Appointment) -> dict:
    from app.clinic import SERVICES

    service = SERVICES.get(appt.service)
    label = service.label.title() if service else appt.service
    return {
        "summary": f"{label} - {appt.patient_name}",
        "description": (
            f"Phone: {appt.phone}\nConfirmation: {appt.code}\nChair: {appt.chair}\n"
            f"New patient: {'yes' if appt.is_new_patient else 'no'}\nNotes: {appt.notes or '-'}\n\nBooked by the AI receptionist."
        ),
        "start": {"dateTime": appt.start_at.astimezone(timezone.utc).isoformat(), "timeZone": settings.clinic_timezone},
        "end": {"dateTime": appt.end_at.astimezone(timezone.utc).isoformat(), "timeZone": settings.clinic_timezone},
        "extendedProperties": {"private": {"appointment_id": str(appt.id)}},
    }


def upsert_event(appt: Appointment) -> str | None:
    settings = get_settings()
    if not settings.calendar_enabled:
        return None
    try:
        events = _service(settings.google_service_account_json).events()
        body = _event_body(settings, appt)
        if appt.calendar_event_id:
            events.update(calendarId=settings.google_calendar_id, eventId=appt.calendar_event_id, body=body).execute()
            return appt.calendar_event_id
        return events.insert(calendarId=settings.google_calendar_id, body=body).execute()["id"]
    except Exception:
        log.exception("Calendar upsert failed for appointment %s", appt.id)
        return appt.calendar_event_id


def delete_event(appt: Appointment) -> None:
    settings = get_settings()
    if not (settings.calendar_enabled and appt.calendar_event_id):
        return
    try:
        _service(settings.google_service_account_json).events().delete(
            calendarId=settings.google_calendar_id, eventId=appt.calendar_event_id
        ).execute()
    except Exception:
        log.exception("Calendar delete failed for appointment %s", appt.id)


def busy_blocks(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Events on the clinic calendar that were NOT created by this app (staff blocks)."""
    settings = get_settings()
    if not settings.calendar_enabled:
        return []
    try:
        items = (
            _service(settings.google_service_account_json)
            .events()
            .list(
                calendarId=settings.google_calendar_id,
                timeMin=start.astimezone(timezone.utc).isoformat(),
                timeMax=end.astimezone(timezone.utc).isoformat(),
                singleEvents=True,
                maxResults=250,
            )
            .execute()
            .get("items", [])
        )
    except Exception:
        log.exception("Calendar busy lookup failed; continuing with database only")
        return []
    blocks = []
    for ev in items:
        if ev.get("extendedProperties", {}).get("private", {}).get("appointment_id"):
            continue  # one of ours, already in the database
        if ev.get("transparency") == "transparent":
            continue  # events marked "free" don't block
        if "dateTime" in ev.get("start", {}):
            blocks.append((datetime.fromisoformat(ev["start"]["dateTime"]), datetime.fromisoformat(ev["end"]["dateTime"])))
        else:  # all-day event, e.g. "Dr. Smith off" blocks the whole day
            day_start = datetime.combine(date.fromisoformat(ev["start"]["date"]), time.min, settings.tz)
            day_end = datetime.combine(date.fromisoformat(ev["end"]["date"]), time.min, settings.tz)
            blocks.append((day_start, day_end))
    return blocks
