"""Front-desk dashboard: the week's schedule, every call, and the callback list."""

import hmac
from datetime import datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import demo_data
from app.booking import as_utc
from app.clinic import SERVICES, close_time, is_within_hours
from app.config import Settings, get_settings
from app.db import get_db
from app.models import Appointment, CallLog, Lead
from app.templating import render

router = APIRouter(tags=["dashboard"])
basic = HTTPBasic(auto_error=False)

SHORT = {"checkup": "check-up", "new_patient": "new patient", "whitening": "whitening", "emergency": "emergency",
         "consultation": "consult", "cleaning": "cleaning", "filling": "filling"}
SERVICE_COLORS = {
    "checkup": "#1f4fe0", "cleaning": "#0e8a8a", "new_patient": "#6e46d9", "filling": "#b8650f",
    "whitening": "#c9417a", "consultation": "#3d6a8f", "emergency": "#cf2e35",
}
ROW_PX = 34  # one 30-minute row


def require_admin(creds: HTTPBasicCredentials | None = Depends(basic), settings: Settings = Depends(get_settings)) -> None:
    """Public in demo mode (sample clinic); HTTP Basic (admin / ADMIN_PASSWORD) for a real clinic."""
    if settings.demo_mode:
        return
    if not creds or not hmac.compare_digest(creds.password, settings.admin_password):
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})


def pretty_phone(e164: str) -> str:
    d = (e164 or "").lstrip("+")
    return f"({d[1:4]}) {d[4:7]}-{d[7:]}" if len(d) == 11 and d.startswith("1") else e164


def build_stats(db: Session, settings: Settings, days: int = 30) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    calls = list(db.scalars(select(CallLog).where(CallLog.created_at >= since)))
    booked = db.scalar(
        select(func.count()).select_from(Appointment).where(Appointment.created_at >= since, Appointment.source_call_id.is_not(None))
    )
    leads = db.scalar(select(func.count()).select_from(Lead).where(Lead.created_at >= since))
    after_hours = sum(1 for c in calls if _after_hours(settings, c))
    durations = [c.duration_seconds for c in calls if c.duration_seconds]
    resolved = sum(1 for c in calls if c.outcome != "info")
    return {
        "calls": len(calls),
        "booked": booked or 0,
        "leads": leads or 0,
        "after_hours": after_hours,
        "resolution_rate": round(100 * resolved / len(calls)) if calls else 0,
        "avg_minutes": round(sum(durations) / len(durations) / 60, 1) if durations else 0,
        "cost": round(sum(c.cost_usd or 0 for c in calls), 2),
    }


def _after_hours(settings: Settings, c: CallLog) -> bool:
    return not is_within_hours(settings, as_utc(c.started_at or c.created_at).astimezone(settings.tz), 1)


def _short_name(name: str) -> str:
    parts = name.split()
    return f"{parts[0]} {parts[-1][0]}." if len(parts) > 1 else name


def _fmt(settings: Settings, dt) -> str:
    return as_utc(dt).astimezone(settings.tz).strftime("%a %b %d, %I:%M %p").replace(" 0", " ") if dt else ""


def _schedule(db: Session, settings: Settings, n_days: int = 5) -> dict:
    tz = settings.tz
    now_local = datetime.now(tz)
    day, days = now_local.date(), []
    while len(days) < n_days:
        if day.weekday() in settings.open_weekdays:
            days.append(day)
        day += timedelta(days=1)
    start = datetime.combine(days[0], time.min, tz).astimezone(timezone.utc)
    end = datetime.combine(days[-1] + timedelta(days=1), time.min, tz).astimezone(timezone.utc)
    appts = list(db.scalars(select(Appointment).where(
        Appointment.status == "booked", Appointment.start_at >= start, Appointment.start_at < end)))
    open_min = settings.open_hour.hour * 60 + settings.open_hour.minute
    close_min = settings.close_hour.hour * 60 + settings.close_hour.minute

    def y(minutes: int) -> int:
        return int((minutes - open_min) / 30 * ROW_PX)

    cols = []
    for d in days:
        items = []
        for a in appts:
            s = as_utc(a.start_at).astimezone(tz)
            if s.date() != d:
                continue
            mins = s.hour * 60 + s.minute
            dur = int((as_utc(a.end_at) - as_utc(a.start_at)).total_seconds() // 60)
            items.append({
                "top": y(mins) + 1, "h": max(int(dur / 30 * ROW_PX) - 3, 26), "left": 0 if a.chair == 1 else 50,
                "name": _short_name(a.patient_name), "full_name": a.patient_name, "label": SHORT.get(a.service, a.service), "chair": a.chair,
                "time": s.strftime("%I:%M").lstrip("0"), "color": SERVICE_COLORS.get(a.service, "#1f4fe0"),
                "ai": bool(a.source_call_id),
            })
        c = close_time(settings, d)
        c_min = c.hour * 60 + c.minute
        cols.append({
            "name": "Today" if d == now_local.date() else d.strftime("%A"), "date": d.strftime("%b %d"),
            "today": d == now_local.date(), "appts": items, "count": len(items),
            "close_top": y(c_min) if c_min < close_min else None,
        })
    hours = []
    for m in range(open_min, close_min, 30):
        hours.append(datetime.combine(days[0], time(m // 60, m % 60)).strftime("%I %p").lstrip("0") if m % 60 == 0 else "")
    lunch_s = settings.lunch_start.hour * 60 + settings.lunch_start.minute
    lunch_e = settings.lunch_end.hour * 60 + settings.lunch_end.minute
    return {
        "days": cols, "hours": hours, "grid_height": y(close_min),
        "lunch_top": y(lunch_s), "lunch_h": y(lunch_e) - y(lunch_s),
        "legend": [(k, SHORT[k], SERVICE_COLORS[k]) for k in SERVICES],
    }


@router.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def dashboard(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    if settings.demo_mode:
        demo_data.ensure_fresh(db, settings)
    calls = db.scalars(select(CallLog).order_by(CallLog.started_at.desc().nulls_last(), CallLog.created_at.desc()).limit(30))
    leads = db.scalars(select(Lead).order_by(Lead.created_at.desc()).limit(25))
    return render(
        request, "dashboard.html",
        s=build_stats(db, settings),
        demo=settings.demo_mode,
        address=settings.clinic_address,
        chairs=settings.clinic_chairs,
        tz=settings.clinic_timezone.replace("_", " "),
        now_label=datetime.now(settings.tz).strftime("%I:%M %p").lstrip("0"),
        calls=[
            dict(when=_fmt(settings, c.started_at or c.created_at), caller=pretty_phone(c.caller), outcome=c.outcome,
                 summary=c.summary, transcript=c.transcript, after_hours=_after_hours(settings, c),
                 length=f"{c.duration_seconds // 60}:{c.duration_seconds % 60:02d}" if c.duration_seconds else "")
            for c in calls
        ],
        leads=[
            dict(when=_fmt(settings, lead.created_at), name=lead.name, phone=pretty_phone(lead.phone), reason=lead.reason,
                 insurance=lead.insurance, urgency=lead.urgency, status=lead.status)
            for lead in leads
        ],
        **_schedule(db, settings),
    )


@router.get("/api/stats", dependencies=[Depends(require_admin)])
def stats(db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    return build_stats(db, settings)
