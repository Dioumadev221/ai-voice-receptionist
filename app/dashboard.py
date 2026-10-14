"""Front-desk dashboard: what the AI receptionist did, and what's coming up."""

import hmac
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from jinja2 import Environment, select_autoescape
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.booking import as_utc
from app.clinic import SERVICES, is_within_hours
from app.config import Settings, get_settings
from app.db import get_db
from app.models import Appointment, CallLog, Lead

router = APIRouter(tags=["dashboard"])
basic = HTTPBasic()


def require_admin(creds: HTTPBasicCredentials = Depends(basic), settings: Settings = Depends(get_settings)) -> None:
    if not hmac.compare_digest(creds.password, settings.admin_password):
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})


def pretty_phone(e164: str) -> str:
    d = e164.lstrip("+")
    return f"({d[1:4]}) {d[4:7]}-{d[7:]}" if len(d) == 11 and d.startswith("1") else e164


def build_stats(db: Session, settings: Settings, days: int = 30) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    calls = list(db.scalars(select(CallLog).where(CallLog.created_at >= since)))
    booked = db.scalar(
        select(func.count()).select_from(Appointment).where(Appointment.created_at >= since, Appointment.source_call_id.is_not(None))
    )
    leads = db.scalar(select(func.count()).select_from(Lead).where(Lead.created_at >= since))
    after_hours = sum(
        1 for c in calls if not is_within_hours(settings, as_utc(c.started_at or c.created_at).astimezone(settings.tz), 1)
    )
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


TEMPLATE = Environment(autoescape=select_autoescape()).from_string("""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ clinic }} - AI Receptionist</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#14181f;--muted:#5d6675;--line:#e3e6eb;--accent:#0f766e}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--card:#171b21;--ink:#e8ebef;--muted:#98a1ae;--line:#2a3039;--accent:#2dd4bf}}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--ink)}
main{max-width:1100px;margin:0 auto;padding:24px 16px}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 10px}
.muted{color:var(--muted)}.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:18px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}.kpi b{display:block;font-size:26px}
.wrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}.tag{font-size:12px;padding:2px 8px;border-radius:99px;background:var(--line)}
.tag.booked,.tag.rescheduled{background:var(--accent);color:var(--bg)}a{color:var(--accent)}
</style></head><body><main>
<h1>{{ clinic }} · AI Receptionist</h1><div class="muted">Last 30 days · times in {{ tz }}</div>
<div class="kpis">
<div class="kpi"><b>{{ s.calls }}</b><span class="muted">calls answered</span></div>
<div class="kpi"><b>{{ s.booked }}</b><span class="muted">appointments booked by AI</span></div>
<div class="kpi"><b>{{ s.resolution_rate }}%</b><span class="muted">calls resolved without staff</span></div>
<div class="kpi"><b>{{ s.after_hours }}</b><span class="muted">calls while office closed</span></div>
<div class="kpi"><b>{{ s.leads }}</b><span class="muted">callback leads</span></div>
<div class="kpi"><b>${{ "%.2f"|format(s.cost) }}</b><span class="muted">voice cost ({{ s.avg_minutes }} min avg)</span></div>
</div>
<h2>Upcoming appointments</h2><div class="wrap"><table><tr><th>When</th><th>Patient</th><th>Service</th><th>Phone</th><th>Code</th><th>Chair</th></tr>
{% for a in appts %}<tr><td>{{ a.when }}</td><td>{{ a.name }}{% if a.new %} <span class="tag">new</span>{% endif %}</td><td>{{ a.service }}</td><td>{{ a.phone }}</td><td>{{ a.code }}</td><td>{{ a.chair }}</td></tr>
{% else %}<tr><td colspan="6" class="muted">Nothing booked yet.</td></tr>{% endfor %}</table></div>
<h2>Recent calls</h2><div class="wrap"><table><tr><th>When</th><th>Caller</th><th>Outcome</th><th>Summary</th><th>Recording</th></tr>
{% for c in calls %}<tr><td>{{ c.when }}</td><td>{{ c.caller }}</td><td><span class="tag {{ c.outcome }}">{{ c.outcome }}</span></td><td>{{ c.summary }}</td><td>{% if c.rec %}<a href="{{ c.rec }}">listen</a>{% endif %}</td></tr>
{% else %}<tr><td colspan="5" class="muted">No calls yet.</td></tr>{% endfor %}</table></div>
<h2>Leads to call back</h2><div class="wrap"><table><tr><th>When</th><th>Name</th><th>Phone</th><th>Reason</th><th>Insurance</th><th>Urgency</th></tr>
{% for l in leads %}<tr><td>{{ l.when }}</td><td>{{ l.name }}</td><td>{{ l.phone }}</td><td>{{ l.reason }}</td><td>{{ l.insurance }}</td><td>{{ l.urgency }}</td></tr>
{% else %}<tr><td colspan="6" class="muted">No leads yet.</td></tr>{% endfor %}</table></div>
</main></body></html>""")


@router.get("/", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def dashboard(db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    tz = settings.tz

    def fmt(dt):
        return as_utc(dt).astimezone(tz).strftime("%a %b %d, %I:%M %p") if dt else ""

    now = datetime.now(timezone.utc)
    appts = db.scalars(
        select(Appointment).where(Appointment.status == "booked", Appointment.start_at >= now).order_by(Appointment.start_at).limit(50)
    )
    calls = db.scalars(select(CallLog).order_by(CallLog.created_at.desc()).limit(25))
    leads = db.scalars(select(Lead).order_by(Lead.created_at.desc()).limit(25))
    return TEMPLATE.render(
        clinic=settings.clinic_name,
        tz=settings.clinic_timezone,
        s=build_stats(db, settings),
        appts=[
            dict(when=fmt(a.start_at), name=a.patient_name, new=a.is_new_patient, service=SERVICES[a.service].label,
                 phone=pretty_phone(a.phone), code=a.code, chair=a.chair)
            for a in appts
        ],
        calls=[
            dict(when=fmt(c.started_at or c.created_at), caller=pretty_phone(c.caller), outcome=c.outcome, summary=c.summary, rec=c.recording_url)
            for c in calls
        ],
        leads=[
            dict(when=fmt(l.created_at), name=l.name, phone=pretty_phone(l.phone), reason=l.reason, insurance=l.insurance, urgency=l.urgency)
            for l in leads
        ],
    )


@router.get("/api/stats", dependencies=[Depends(require_admin)])
def stats(db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    return build_stats(db, settings)
