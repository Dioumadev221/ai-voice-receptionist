"""Sample clinic data for the public demo. Clearly fictional (555-01XX numbers), wiped and rebuilt daily."""

import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.clinic import SERVICES, candidate_starts
from app.config import Settings
from app.models import Appointment, CallLog, Lead, new_code

FIRST = ["Maria", "James", "Priya", "Tom", "Aisha", "Kevin", "Laura", "Daniel", "Sofia", "Michael", "Emma", "Omar",
         "Chloe", "Lucas", "Grace", "Ethan", "Hannah", "Noah", "Zoe", "Ryan", "Nina", "Carlos", "Mei", "Jordan"]
LAST = ["Garcia", "Carter", "Shah", "Becker", "Bello", "Nguyen", "Rossi", "Kim", "Alvarez", "Brown", "Wilson",
        "Haddad", "Martin", "Silva", "Okafor", "Patel", "Lee", "Moreau", "Novak", "Ortiz", "Walsh", "Chen"]
WEIGHTS = {"checkup": 5, "cleaning": 6, "new_patient": 3, "filling": 3, "whitening": 1, "consultation": 2, "emergency": 1}
LEAD_REASONS = [
    ("Asked about Invisalign pricing and monthly payment plans", "Delta Dental"),
    ("Wants to know if Cigna PPO covers two cleanings a year", "Cigna"),
    ("Asked for the cost of a dental implant consultation", ""),
    ("Asked whether the clinic sees children under 5", "Aetna"),
    ("Wants a quote for in-office whitening before a wedding", ""),
    ("Asked if MetLife is accepted for a crown", "MetLife"),
]


def _phone(rng: random.Random) -> str:
    return f"+155501{rng.randrange(10**5):05d}"


def _a(label: str) -> str:
    return ("an " if label[:1].lower() in "aeiou" else "a ") + label


def _name(rng: random.Random) -> str:
    return f"{rng.choice(FIRST)} {rng.choice(LAST)}"


def is_empty(db: Session) -> bool:
    return not db.scalar(select(func.count()).select_from(CallLog))


def ensure_fresh(db: Session, settings: Settings) -> None:
    """Rebuild the sample clinic if it is empty or has gone stale (e.g. a missed daily cron)."""
    soon = datetime.now(timezone.utc) + timedelta(days=4)
    future = db.scalar(select(func.count()).select_from(Appointment).where(Appointment.start_at > soon))
    if is_empty(db) or not future:
        reset(db, settings)


def reset(db: Session, settings: Settings, seed: int | None = None) -> dict:
    """Wipe everything and rebuild a believable month for the sample clinic."""
    rng = random.Random(seed if seed is not None else datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    for model in (Appointment, Lead, CallLog):
        db.execute(delete(model))
    tz = settings.tz
    now = datetime.now(timezone.utc)
    today = now.astimezone(tz).date()
    services = [k for k, w in WEIGHTS.items() for _ in range(w)]

    # Upcoming schedule: roughly half the chair capacity over the next two weeks, leaving real gaps
    booked: list[tuple[datetime, datetime, int]] = []
    upcoming = 0
    for offset in range(0, 15):
        day = today + timedelta(days=offset)
        fill = 0.25 if offset < 2 else 0.45
        for start in candidate_starts(settings, day, 30):
            if start.astimezone(timezone.utc) < now + timedelta(hours=3) or rng.random() > fill:
                continue
            key = rng.choice(services)
            svc = SERVICES[key]
            if start not in candidate_starts(settings, day, svc.minutes):
                continue
            s_utc = start.astimezone(timezone.utc)
            e_utc = s_utc + timedelta(minutes=svc.minutes)
            taken = {c for b0, b1, c in booked if b0 < e_utc and b1 > s_utc}
            free = [c for c in range(1, settings.clinic_chairs + 1) if c not in taken]
            if not free:
                continue
            booked.append((s_utc, e_utc, free[0]))
            by_ai = rng.random() < 0.55
            made_day = (now - timedelta(days=rng.randint(1, 12))).astimezone(tz)
            made_hour = rng.choice([7, 19, 20, 21]) if rng.random() < 0.3 else rng.choice([8, 9, 10, 11, 13, 14, 15, 16])
            made = made_day.replace(hour=made_hour, minute=rng.randint(0, 59)).astimezone(timezone.utc)
            call_id = f"demo-appt-{upcoming}" if by_ai else None
            name, phone = _name(rng), _phone(rng)
            db.add(Appointment(code=new_code(), patient_name=name, phone=phone, service=key, chair=free[0],
                               start_at=s_utc, end_at=e_utc, is_new_patient=key == "new_patient",
                               source_call_id=call_id, created_at=made, reminder_sent=offset == 0))
            if by_ai:
                when = start.strftime("%a %b %d at %I:%M %p").replace(" 0", " ")
                db.add(_call(rng, call_id, phone, made, "booked", f"{name} booked {_a(svc.label)} for {when}."))
            upcoming += 1

    # Past month of calls that did not create an upcoming appointment
    others = 0
    for i in range(38):
        made = now - timedelta(days=rng.randint(1, 29))
        local = made.astimezone(tz)
        after_hours = rng.random() < 0.3
        hour = rng.choice([7, 19, 20, 21]) if after_hours else rng.choice([8, 9, 10, 11, 13, 14, 15, 16])
        made = local.replace(hour=hour, minute=rng.randint(0, 59)).astimezone(timezone.utc)
        name, phone = _name(rng), _phone(rng)
        roll = rng.random()
        if roll < 0.35:
            reason, insurance = rng.choice(LEAD_REASONS)
            db.add(Lead(name=name, phone=phone, reason=reason, insurance=insurance, created_at=made,
                        source_call_id=f"demo-call-{i}", status=rng.choice(["new", "new", "called back"])))
            db.add(_call(rng, f"demo-call-{i}", phone, made, "lead", f"{name}: {reason.lower()}. Callback requested."))
        elif roll < 0.55:
            label = SERVICES[rng.choice(services)].label
            db.add(_call(rng, f"demo-call-{i}", phone, made, "rescheduled", f"{name} moved {_a(label)} to later in the week."))
        elif roll < 0.68:
            label = SERVICES[rng.choice(services)].label
            db.add(_call(rng, f"demo-call-{i}", phone, made, "cancelled",
                         f"{name} cancelled {_a(label)}; offered to rebook, will call back."))
        else:
            q = rng.choice(["Saturday opening hours", "parking near the clinic", "the clinic address",
                            "whether walk-ins are accepted"])
            db.add(_call(rng, f"demo-call-{i}", phone, made, "info", f"{name} asked about {q}."))
        others += 1
    db.commit()
    return {"appointments": upcoming, "calls": upcoming + others}


def _call(rng: random.Random, call_id: str, phone: str, started: datetime, outcome: str, summary: str) -> CallLog:
    duration = rng.randint(50, 230)
    return CallLog(vapi_call_id=call_id, caller=phone, started_at=started, ended_at=started + timedelta(seconds=duration),
                   duration_seconds=duration, ended_reason="customer-ended-call", outcome=outcome, summary=summary,
                   cost_usd=round(duration / 60 * 0.11, 3), tools_used=[], created_at=started)
