"""Tool handlers the voice agent can call, plus their JSON schemas (single source of truth)."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app import booking
from app.clinic import (
    SERVICES,
    BookingError,
    normalize_phone,
    opening_hours_sentence,
    parse_date,
    parse_time,
    resolve_service,
    speak_datetime,
)
from app.config import Settings
from app.integrations import calendar
from app.integrations.sms import send_sms
from app.models import Lead


@dataclass
class CallContext:
    call_id: str | None = None
    caller_phone: str | None = None  # caller ID from the phone network, when available
    outcomes: list[str] = field(default_factory=list)


def _local(settings: Settings, dt: datetime) -> datetime:
    return booking.as_utc(dt).astimezone(settings.tz)


def _slot_list(settings: Settings, slots: list[datetime]) -> str:
    return "; ".join(f"{speak_datetime(s)} [date={s.date().isoformat()} time={s.strftime('%H:%M')}]" for s in slots)


def _phone(ctx: CallContext, given: str | None) -> str:
    return normalize_phone(given or ctx.caller_phone)


def _alternatives(db, settings, service, day, exclude_id=None) -> str:
    slots = booking.find_slots(db, settings, service, day, busy_provider=calendar.busy_blocks, exclude_id=exclude_id)
    return f" Closest open times: {_slot_list(settings, slots)}." if slots else ""


# ---------------------------------------------------------------- handlers


def check_availability(db: Session, settings: Settings, ctx: CallContext, args: dict) -> str:
    service = resolve_service(args.get("service"))
    day = parse_date(args.get("date") or datetime.now(settings.tz).date().isoformat())
    slots = booking.find_slots(
        db, settings, service, day, part_of_day=args.get("part_of_day"), busy_provider=calendar.busy_blocks
    )
    if not slots:
        return f"No openings for a {service.label} in the next two weeks from that date. Offer to take their details as a lead so the team calls back."
    return f"Open times for a {service.label} ({service.minutes} min): {_slot_list(settings, slots)}. Offer these to the caller."


def book_appointment(db: Session, settings: Settings, ctx: CallContext, args: dict) -> str:
    service = resolve_service(args.get("service"))
    phone = _phone(ctx, args.get("phone"))
    start = datetime.combine(parse_date(args.get("date", "")), parse_time(args.get("time", "")), settings.tz)
    try:
        appt = booking.book(
            db,
            settings,
            service=service,
            start_local=start,
            patient_name=args.get("patient_name", ""),
            phone=phone,
            is_new_patient=bool(args.get("is_new_patient")),
            notes=args.get("notes", ""),
            call_id=ctx.call_id,
            busy_provider=calendar.busy_blocks,
        )
    except BookingError as e:
        raise BookingError(f"NOT BOOKED. {e}{_alternatives(db, settings, service, start.date())}") from None

    appt.calendar_event_id = calendar.upsert_event(appt)
    db.commit()
    when = speak_datetime(_local(settings, appt.start_at))
    send_sms(
        settings,
        appt.phone,
        f"{settings.clinic_name}: your {service.label} is confirmed for {when}. "
        f"Confirmation code {appt.code}. {settings.clinic_address}. Reply or call {settings.clinic_phone} to change it.",
    )
    if service.key == "emergency" and settings.staff_alert_phone:
        send_sms(settings, settings.staff_alert_phone, f"EMERGENCY booked {when}: {appt.patient_name} {appt.phone}. {appt.notes}")
    ctx.outcomes.append("booked")
    code_spoken = " ".join(appt.code)
    return (
        f"BOOKED. {service.label} for {appt.patient_name} on {when}. Confirmation code {code_spoken}. "
        f"A text confirmation was sent to the number ending in {appt.phone[-4:]}."
    )


def find_appointment(db: Session, settings: Settings, ctx: CallContext, args: dict) -> str:
    code = args.get("confirmation_code")
    if code:
        appts = [booking.get_by_code(db, code)]
    elif ctx.caller_phone:
        appts = booking.upcoming_for_phone(db, normalize_phone(ctx.caller_phone))
    else:
        raise BookingError("Ask the caller for the confirmation code from their text message.")
    if not appts:
        return "No upcoming appointments for the number they're calling from. Ask for the confirmation code from their text message."
    lines = [
        f"{a.patient_name}: {SERVICES[a.service].label} on {speak_datetime(_local(settings, a.start_at))} (code {a.code})"
        for a in appts
    ]
    return "Upcoming appointments: " + "; ".join(lines) + ". Confirm the patient's name before changing anything."


def reschedule_appointment(db: Session, settings: Settings, ctx: CallContext, args: dict) -> str:
    appt = booking.get_by_code(db, args.get("confirmation_code", ""))
    service = SERVICES[appt.service]
    new_start = datetime.combine(parse_date(args.get("new_date", "")), parse_time(args.get("new_time", "")), settings.tz)
    try:
        booking.reschedule(db, settings, appt, service, new_start, busy_provider=calendar.busy_blocks)
    except BookingError as e:
        raise BookingError(
            f"NOT CHANGED, the original time is kept. {e}{_alternatives(db, settings, service, new_start.date(), appt.id)}"
        ) from None
    appt.calendar_event_id = calendar.upsert_event(appt)
    db.commit()
    when = speak_datetime(_local(settings, appt.start_at))
    send_sms(settings, appt.phone, f"{settings.clinic_name}: your {service.label} has moved to {when}. Code {appt.code}.")
    ctx.outcomes.append("rescheduled")
    return f"RESCHEDULED. {appt.patient_name}'s {service.label} is now on {when}. Same confirmation code. Text sent."


def cancel_appointment(db: Session, settings: Settings, ctx: CallContext, args: dict) -> str:
    appt = booking.get_by_code(db, args.get("confirmation_code", ""))
    booking.cancel(db, appt, args.get("reason", ""))
    calendar.delete_event(appt)
    when = speak_datetime(_local(settings, appt.start_at))
    send_sms(settings, appt.phone, f"{settings.clinic_name}: your appointment on {when} is cancelled. Call {settings.clinic_phone} to rebook.")
    ctx.outcomes.append("cancelled")
    return f"CANCELLED. The appointment on {when} is cancelled and a text was sent. Offer to rebook now."


def capture_lead(db: Session, settings: Settings, ctx: CallContext, args: dict) -> str:
    if not args.get("name"):
        raise BookingError("Ask for the caller's name before saving their details.")
    lead = Lead(
        name=args["name"].strip().title(),
        phone=_phone(ctx, args.get("phone")),
        email=args.get("email", ""),
        reason=args.get("reason", ""),
        insurance=args.get("insurance", ""),
        urgency="urgent" if args.get("urgency") == "urgent" else "normal",
        source_call_id=ctx.call_id,
    )
    db.add(lead)
    db.commit()
    if settings.staff_alert_phone:
        flag = "URGENT " if lead.urgency == "urgent" else ""
        send_sms(settings, settings.staff_alert_phone, f"{flag}New lead: {lead.name} {lead.phone}. {lead.reason} ({lead.insurance or 'no insurance given'})")
    ctx.outcomes.append("lead")
    return "SAVED. Tell the caller a team member will call them back during opening hours, usually the same day."


def get_clinic_info(db: Session, settings: Settings, ctx: CallContext, args: dict) -> str:
    services = "; ".join(f"{s.label} ({s.minutes} min): {s.description}" for s in SERVICES.values())
    return (
        f"{settings.clinic_name}, {settings.clinic_address}. Phone {settings.clinic_phone}. "
        f"{opening_hours_sentence(settings)} Services: {services}. "
        "Pricing and insurance coverage questions: capture a lead so the front desk can confirm exact costs."
    )


HANDLERS = {
    "check_availability": check_availability,
    "book_appointment": book_appointment,
    "find_appointment": find_appointment,
    "reschedule_appointment": reschedule_appointment,
    "cancel_appointment": cancel_appointment,
    "capture_lead": capture_lead,
    "get_clinic_info": get_clinic_info,
}


# ---------------------------------------------------------------- schemas

_S = {"type": "string"}
_SERVICE = {"type": "string", "enum": list(SERVICES), "description": "Service key."}
_DATE = {"type": "string", "description": "Date as YYYY-MM-DD in the clinic's timezone."}
_TIME = {"type": "string", "description": "24h time as HH:MM, exactly as returned by check_availability."}
_CODE = {"type": "string", "description": "6-character confirmation code, letters and digits."}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "check_availability",
        "description": "Find open appointment times. Always call this before offering or booking a time.",
        "parameters": {
            "type": "object",
            "properties": {
                "service": _SERVICE,
                "date": {**_DATE, "description": "First day to search from (YYYY-MM-DD)."},
                "part_of_day": {"type": "string", "enum": ["morning", "afternoon", "any"]},
            },
            "required": ["service", "date"],
        },
    },
    {
        "name": "book_appointment",
        "description": "Book a time the caller chose from check_availability results. Read back name, service, date and time first.",
        "parameters": {
            "type": "object",
            "properties": {
                "patient_name": {"type": "string", "description": "Patient's full name."},
                "service": _SERVICE,
                "date": _DATE,
                "time": _TIME,
                "phone": {"type": "string", "description": "Only if the caller wants texts on a different number than the one they call from."},
                "is_new_patient": {"type": "boolean"},
                "notes": {"type": "string", "description": "Symptoms or requests worth telling the dentist."},
            },
            "required": ["patient_name", "service", "date", "time"],
        },
    },
    {
        "name": "find_appointment",
        "description": "Look up the caller's upcoming appointments, by the number they call from or by confirmation code.",
        "parameters": {"type": "object", "properties": {"confirmation_code": _CODE}},
    },
    {
        "name": "reschedule_appointment",
        "description": "Move an existing appointment to a new time the caller picked from check_availability.",
        "parameters": {
            "type": "object",
            "properties": {"confirmation_code": _CODE, "new_date": _DATE, "new_time": _TIME},
            "required": ["confirmation_code", "new_date", "new_time"],
        },
    },
    {
        "name": "cancel_appointment",
        "description": "Cancel an appointment after the caller explicitly confirms they want to cancel.",
        "parameters": {
            "type": "object",
            "properties": {"confirmation_code": _CODE, "reason": _S},
            "required": ["confirmation_code"],
        },
    },
    {
        "name": "capture_lead",
        "description": "Save a callback request: pricing, insurance, treatment questions, or anything you can't resolve.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": _S,
                "phone": {"type": "string", "description": "Only if different from the number they call from."},
                "email": _S,
                "reason": {"type": "string", "description": "What they need, in one or two sentences."},
                "insurance": {"type": "string", "description": "Insurance provider, if mentioned."},
                "urgency": {"type": "string", "enum": ["normal", "urgent"]},
            },
            "required": ["name", "reason"],
        },
    },
    {
        "name": "get_clinic_info",
        "description": "Opening hours, address, phone and the list of services.",
        "parameters": {"type": "object", "properties": {}},
    },
]
