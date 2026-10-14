"""Clinic catalogue and pure scheduling helpers (no I/O, easy to unit test)."""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from app.config import Settings


@dataclass(frozen=True)
class Service:
    key: str
    label: str
    minutes: int
    description: str


SERVICES: dict[str, Service] = {
    s.key: s
    for s in [
        Service("checkup", "check-up and exam", 30, "Routine exam with the dentist."),
        Service("cleaning", "cleaning", 60, "Hygienist cleaning and polish."),
        Service("new_patient", "new patient visit", 90, "Full exam, X-rays and cleaning for first-time patients."),
        Service("filling", "filling", 60, "Cavity filling."),
        Service("whitening", "teeth whitening", 60, "In-office whitening session."),
        Service("consultation", "consultation", 30, "Implants, Invisalign or cosmetic consultation."),
        Service("emergency", "emergency visit", 30, "Pain, swelling, broken tooth. Same-day priority."),
    ]
}

SERVICE_ALIASES = {
    "exam": "checkup", "check up": "checkup", "check-up": "checkup", "checkup": "checkup",
    "cleaning": "cleaning", "hygiene": "cleaning",
    "new patient": "new_patient", "first visit": "new_patient",
    "filling": "filling", "cavity": "filling",
    "whitening": "whitening",
    "consult": "consultation", "consultation": "consultation", "invisalign": "consultation", "implant": "consultation",
    "emergency": "emergency", "pain": "emergency", "toothache": "emergency", "broken": "emergency",
}


class BookingError(Exception):
    """Raised with a sentence the voice agent can say back to the caller as-is."""


def resolve_service(raw: str | None) -> Service:
    key = (raw or "").strip().lower().replace("-", "_")
    if key in SERVICES:
        return SERVICES[key]
    text = key.replace("_", " ")
    for alias, target in SERVICE_ALIASES.items():
        if alias in text:
            return SERVICES[target]
    options = ", ".join(s.label for s in SERVICES.values())
    raise BookingError(f"I couldn't match that to one of our services. We offer: {options}.")


def normalize_phone(raw: str | None) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) < 8:
        raise BookingError("I need a valid phone number to continue. Could you repeat it digit by digit?")
    return "+" + digits


def parse_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw.strip()[:10])
    except (ValueError, AttributeError):
        raise BookingError("I didn't catch the date. Which day works for you?") from None


def parse_time(raw: str) -> time:
    value = (raw or "").strip().upper().replace(".", "")
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I %p", "%I:%M%p", "%I%p"):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    raise BookingError("I didn't catch the time. What time would you like?")


def close_time(settings: Settings, day: date) -> time:
    return settings.saturday_close_hour if day.weekday() == 5 else settings.close_hour


def is_within_hours(settings: Settings, start: datetime, minutes: int) -> bool:
    """start is a clinic-local aware datetime."""
    day = start.date()
    if day.weekday() not in settings.open_weekdays:
        return False
    end = start + timedelta(minutes=minutes)
    if end.date() != day:
        return False
    if start.time() < settings.open_hour or end.time() > close_time(settings, day):
        return False
    lunch_start = datetime.combine(day, settings.lunch_start, start.tzinfo)
    lunch_end = datetime.combine(day, settings.lunch_end, start.tzinfo)
    return not (start < lunch_end and end > lunch_start)


def candidate_starts(settings: Settings, day: date, minutes: int) -> list[datetime]:
    tz = settings.tz
    step = timedelta(minutes=settings.slot_step_minutes)
    cursor = datetime.combine(day, settings.open_hour, tz)
    last = datetime.combine(day, close_time(settings, day), tz)
    out = []
    while cursor + timedelta(minutes=minutes) <= last:
        if is_within_hours(settings, cursor, minutes):
            out.append(cursor)
        cursor += step
    return out


def speak_datetime(dt: datetime) -> str:
    """'Tuesday, October 21 at 9:30 AM' - formatted for text-to-speech."""
    hour = dt.strftime("%I").lstrip("0")
    minute = "" if dt.minute == 0 else dt.strftime(":%M")
    return f"{dt.strftime('%A, %B')} {dt.day} at {hour}{minute} {dt.strftime('%p')}"


def opening_hours_sentence(settings: Settings) -> str:
    def fmt(t: time) -> str:
        return datetime.combine(date.today(), t).strftime("%I:%M %p").lstrip("0")

    return (
        f"We're open Monday to Friday from {fmt(settings.open_hour)} to {fmt(settings.close_hour)}, "
        f"and Saturday from {fmt(settings.open_hour)} to {fmt(settings.saturday_close_hour)}. "
        f"We're closed for lunch from {fmt(settings.lunch_start)} to {fmt(settings.lunch_end)}."
    )
