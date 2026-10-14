"""Fill the database with a realistic month of activity, for demos and dashboard screenshots.

python scripts/seed_demo.py   (uses DATABASE_URL; refuses to run if call logs already exist)
"""

import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select  # noqa: E402

from app.booking import find_slots  # noqa: E402
from app.clinic import SERVICES  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Appointment, CallLog, Lead, new_code  # noqa: E402

random.seed(7)
NAMES = ["Maria Garcia", "James Carter", "Priya Shah", "Tom Becker", "Aisha Bello", "Kevin Nguyen", "Laura Rossi",
         "Daniel Kim", "Sofia Alvarez", "Michael Brown", "Emma Wilson", "Omar Haddad", "Chloe Martin", "Lucas Silva"]
SCENARIOS = [
    ("booked", "cleaning", "{n} booked a cleaning for {w}. Returning patient, text confirmation sent."),
    ("booked", "new_patient", "{n} is a new patient, booked a new patient visit for {w}. Asked about parking."),
    ("booked", "emergency", "{n} called with a cracked molar and pain, booked an emergency visit for {w}. Staff alerted."),
    ("rescheduled", "checkup", "{n} moved their check-up to {w} because of a work trip."),
    ("cancelled", "whitening", "{n} cancelled a whitening session, will call back to rebook."),
    ("lead", None, "{n} asked about Invisalign pricing and payment plans. Callback requested."),
    ("lead", None, "{n} asked whether Delta Dental PPO is accepted. Callback requested."),
    ("info", None, "{n} asked for Saturday opening hours and the address."),
]


def main() -> None:
    init_db()
    s = get_settings()
    db = SessionLocal()
    if db.scalar(select(func.count()).select_from(CallLog)):
        sys.exit("Database already has call logs; not seeding.")
    now = datetime.now(timezone.utc)
    for i in range(46):
        outcome, service_key, template = random.choice(SCENARIOS)
        name = random.choice(NAMES)
        phone = f"+1555{random.randint(1000000, 9999999)}"
        # ~30% of calls land outside opening hours (evenings), when nobody would pick up
        day = (now - timedelta(days=random.randint(1, 29))).astimezone(s.tz)
        hour = random.choice([19, 20, 21]) if random.random() < 0.3 else random.choice([8, 9, 10, 11, 13, 14, 15, 16])
        started = day.replace(hour=hour, minute=random.randint(0, 59)).astimezone(timezone.utc)
        call_id = f"demo-{i}"
        when_text = ""
        if service_key and outcome in ("booked", "rescheduled"):
            service = SERVICES[service_key]
            slots = find_slots(db, s, service, (now + timedelta(days=random.randint(1, 20))).astimezone(s.tz).date(), limit=5)
            if slots:
                slots = [random.choice(slots)]
                start = slots[0].astimezone(timezone.utc)
                end = start + timedelta(minutes=service.minutes)
                taken = set(db.scalars(select(Appointment.chair).where(
                    Appointment.status == "booked", Appointment.start_at < end, Appointment.end_at > start)))
                chair = min(c for c in range(1, s.clinic_chairs + 1) if c not in taken)
                db.add(Appointment(code=new_code(), patient_name=name, phone=phone, service=service_key, chair=chair,
                                   start_at=start, end_at=end,
                                   is_new_patient=service_key == "new_patient", source_call_id=call_id,
                                   created_at=started))
                db.flush()
                when_text = slots[0].strftime("%A %b %d at %I:%M %p")
        if outcome == "lead":
            db.add(Lead(name=name, phone=phone, reason=template.split(" asked ")[1].split(".")[0].capitalize(),
                        insurance=random.choice(["Delta Dental", "Cigna", "Aetna", ""]), source_call_id=call_id,
                        created_at=started))
        duration = random.randint(55, 260)
        db.add(CallLog(vapi_call_id=call_id, caller=phone, started_at=started,
                       ended_at=started + timedelta(seconds=duration), duration_seconds=duration,
                       ended_reason="customer-ended-call", outcome=outcome,
                       summary=template.format(n=name, w=when_text or "next week"),
                       cost_usd=round(duration / 60 * 0.11, 3), tools_used=[], created_at=started))
    db.commit()
    print("Seeded demo data.")


if __name__ == "__main__":
    main()
