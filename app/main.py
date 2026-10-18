import hmac
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import dashboard, demo_data, vapi, web
from app.booking import as_utc
from app.clinic import SERVICES, speak_datetime
from app.config import Settings, get_settings
from app.db import SessionLocal, get_db, init_db
from app.integrations.sms import send_sms
from app.models import Appointment

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    settings = get_settings()
    if settings.demo_mode:
        with SessionLocal() as db:
            if demo_data.is_empty(db):
                demo_data.reset(db, settings)
    yield


app = FastAPI(title="Recepta · AI Voice Receptionist", version="1.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(web.router)
app.include_router(vapi.router)
app.include_router(dashboard.router)


@app.get("/healthz", tags=["ops"])
def healthz():
    return {"ok": True}


def require_jobs_token(authorization: str | None = Header(default=None), settings: Settings = Depends(get_settings)):
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not settings.jobs_token or not hmac.compare_digest(token, settings.jobs_token):
        raise HTTPException(status_code=401)


@app.post("/jobs/reminders", tags=["ops"], dependencies=[Depends(require_jobs_token)])
def send_reminders(db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    """Text a reminder to everyone with an appointment in the next 24 hours. Call hourly from a cron."""
    now = datetime.now(timezone.utc)
    due = db.scalars(
        select(Appointment).where(
            Appointment.status == "booked",
            Appointment.reminder_sent.is_(False),
            Appointment.start_at > now,
            Appointment.start_at <= now + timedelta(hours=24),
        )
    )
    sent = 0
    for appt in due:
        when = speak_datetime(as_utc(appt.start_at).astimezone(settings.tz))
        send_sms(
            settings,
            appt.phone,
            f"Reminder from {settings.clinic_name}: {SERVICES[appt.service].label} {when}. "
            f"Need to change it? Call {settings.clinic_phone} and our assistant will sort it out (code {appt.code}).",
        )
        appt.reminder_sent = True
        sent += 1
    db.commit()
    return {"reminders_sent": sent}
