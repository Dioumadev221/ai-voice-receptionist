"""Deterministic booking engine. The LLM proposes, this module decides.

Every rule a receptionist would enforce (opening hours, lunch, notice period, chair
capacity, no double booking, identity before changes) lives here, not in the prompt.
"""

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.clinic import BookingError, Service, candidate_starts, is_within_hours
from app.config import Settings
from app.models import Appointment

# (start, end) UTC intervals blocked outside this app, e.g. staff events in Google Calendar
BusyProvider = Callable[[datetime, datetime], list[tuple[datetime, datetime]]]


def as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    return a_start < b_end and a_end > b_start


def _booked_between(db: Session, start: datetime, end: datetime, exclude_id: int | None = None) -> list[Appointment]:
    q = select(Appointment).where(
        Appointment.status == "booked",
        Appointment.start_at < end,
        Appointment.end_at > start,
    )
    if exclude_id:
        q = q.where(Appointment.id != exclude_id)
    return list(db.scalars(q))


def _free_chair(settings, start, end, booked, busy) -> int | None:
    if any(_overlaps(start, end, b0, b1) for b0, b1 in busy):
        return None
    taken = {a.chair for a in booked if _overlaps(start, end, as_utc(a.start_at), as_utc(a.end_at))}
    for chair in range(1, settings.clinic_chairs + 1):
        if chair not in taken:
            return chair
    return None


def _earliest_allowed(settings: Settings, service: Service) -> datetime:
    notice = 30 if service.key == "emergency" else settings.min_notice_minutes
    return _now() + timedelta(minutes=notice)


def find_slots(
    db: Session,
    settings: Settings,
    service: Service,
    from_day: date,
    part_of_day: str | None = None,
    limit: int = 3,
    busy_provider: BusyProvider | None = None,
    exclude_id: int | None = None,
    days_to_scan: int = 14,
) -> list[datetime]:
    """Return up to `limit` free clinic-local start times, spread out so they are easy to offer by voice."""
    today_local = _now().astimezone(settings.tz).date()
    from_day = max(from_day, today_local)
    horizon = today_local + timedelta(days=settings.max_days_ahead)
    earliest = _earliest_allowed(settings, service)
    duration = timedelta(minutes=service.minutes)
    part = (part_of_day or "").lower()

    results: list[datetime] = []
    day = from_day
    scanned = 0
    while day <= horizon and scanned < days_to_scan and len(results) < limit:
        scanned += 1
        starts = candidate_starts(settings, day, service.minutes)
        if part == "morning":
            starts = [s for s in starts if s.hour < 12]
        elif part == "afternoon":
            starts = [s for s in starts if s.hour >= 12]
        if starts:
            day_start, day_end = as_utc(starts[0]), as_utc(starts[-1] + duration)
            booked = _booked_between(db, day_start, day_end, exclude_id)
            busy = [(as_utc(a), as_utc(b)) for a, b in (busy_provider(day_start, day_end) if busy_provider else [])]
            last_pick: datetime | None = None
            for s in starts:
                s_utc = as_utc(s)
                if s_utc < earliest:
                    continue
                if last_pick and s - last_pick < timedelta(minutes=90):
                    continue  # spread options across the day
                if _free_chair(settings, s_utc, s_utc + duration, booked, busy):
                    results.append(s)
                    last_pick = s
                    if len(results) >= limit:
                        break
        day += timedelta(days=1)
    return results


def is_free(db: Session, settings: Settings, service: Service, start_local: datetime,
            busy_provider: BusyProvider | None = None, exclude_id: int | None = None) -> bool:
    try:
        validate_slot(settings, service, start_local)
        _assign_chair(db, settings, service, start_local, busy_provider, exclude_id)
        return True
    except BookingError:
        return False


def validate_slot(settings: Settings, service: Service, start_local: datetime) -> None:
    today_local = _now().astimezone(settings.tz).date()
    if as_utc(start_local) < _earliest_allowed(settings, service):
        raise BookingError("That time is too soon or already passed. Let me find you the next available time.")
    if start_local.date() > today_local + timedelta(days=settings.max_days_ahead):
        raise BookingError(f"We only book up to {settings.max_days_ahead} days ahead. Could you pick an earlier date?")
    if not is_within_hours(settings, start_local, service.minutes):
        raise BookingError("We're closed at that time. Would you like me to suggest some open times?")


def _assign_chair(db, settings, service, start_local, busy_provider, exclude_id=None) -> int:
    start = as_utc(start_local)
    end = start + timedelta(minutes=service.minutes)
    booked = _booked_between(db, start, end, exclude_id)
    busy = [(as_utc(a), as_utc(b)) for a, b in (busy_provider(start, end) if busy_provider else [])]
    chair = _free_chair(settings, start, end, booked, busy)
    if chair is None:
        raise BookingError("That time was just taken. Let me find you another one.")
    return chair


def book(
    db: Session,
    settings: Settings,
    *,
    service: Service,
    start_local: datetime,
    patient_name: str,
    phone: str,
    is_new_patient: bool = False,
    notes: str = "",
    call_id: str | None = None,
    busy_provider: BusyProvider | None = None,
) -> Appointment:
    if not patient_name.strip():
        raise BookingError("Can I get the patient's full name for the booking?")
    validate_slot(settings, service, start_local)
    chair = _assign_chair(db, settings, service, start_local, busy_provider)
    start = as_utc(start_local)
    appt = Appointment(
        patient_name=patient_name.strip().title(),
        phone=phone,
        service=service.key,
        chair=chair,
        start_at=start,
        end_at=start + timedelta(minutes=service.minutes),
        is_new_patient=is_new_patient,
        notes=notes,
        source_call_id=call_id,
    )
    db.add(appt)
    _commit_or_conflict(db)
    return appt


def reschedule(
    db: Session,
    settings: Settings,
    appt: Appointment,
    service: Service,
    new_start_local: datetime,
    busy_provider: BusyProvider | None = None,
) -> Appointment:
    validate_slot(settings, service, new_start_local)
    chair = _assign_chair(db, settings, service, new_start_local, busy_provider, exclude_id=appt.id)
    start = as_utc(new_start_local)
    appt.start_at, appt.end_at, appt.chair = start, start + timedelta(minutes=service.minutes), chair
    appt.reminder_sent = False
    _commit_or_conflict(db)
    return appt


def cancel(db: Session, appt: Appointment, reason: str = "") -> Appointment:
    appt.status = "cancelled"
    if reason:
        appt.notes = (appt.notes + f"\nCancelled: {reason}").strip()
    db.commit()
    return appt


def upcoming_for_phone(db: Session, phone: str) -> list[Appointment]:
    q = (
        select(Appointment)
        .where(Appointment.phone == phone, Appointment.status == "booked", Appointment.start_at > _now())
        .order_by(Appointment.start_at)
    )
    return list(db.scalars(q))


def get_by_code(db: Session, code: str) -> Appointment:
    clean = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    appt = db.scalar(select(Appointment).where(Appointment.code == clean))
    if not appt or appt.status != "booked" or as_utc(appt.start_at) < _now():
        raise BookingError("I couldn't find an upcoming appointment with that confirmation code.")
    return appt


def _commit_or_conflict(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError:  # Postgres exclusion constraint caught a concurrent booking
        db.rollback()
        raise BookingError("That time was just taken. Let me find you another one.") from None
