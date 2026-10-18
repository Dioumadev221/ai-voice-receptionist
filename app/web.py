"""Public product site: landing page, browser demo line, and the conversation API behind it."""

import hmac
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import demo_data
from app.config import Settings, get_settings
from app.converse import converse
from app.db import get_db
from app.templating import render

router = APIRouter()
_hits: dict[str, deque] = defaultdict(deque)

ICONS = {
    "book": '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="4" width="18" height="17" rx="2"/><path d="M8 2v4M16 2v4M3 10h18M9 15l2 2 4-4"/></svg>',
    "move": '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M17 1l4 4-4 4"/><path d="M3 11V9a4 4 0 0 1 4-4h14M7 23l-4-4 4-4"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/></svg>',
    "cancel": '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="4" width="18" height="17" rx="2"/><path d="M8 2v4M16 2v4M3 10h18M10 14l4 4M14 14l-4 4"/></svg>',
    "urgent": '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0zM12 9v4M12 17h.01"/></svg>',
    "lead": '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
    "bell": '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 0 1-3.4 0"/></svg>',
}
FEATURES = [
    (ICONS["book"], "Books appointments", "Checks the live schedule, offers three real times, reads the booking back and texts a confirmation code."),
    (ICONS["move"], "Moves and cancels", "Recognises the caller's number, confirms the patient, and offers to reschedule before cancelling."),
    (ICONS["urgent"], "Triages emergencies", "Pain or a broken tooth gets the earliest slot and a text to staff. Danger signs get a 911 instruction."),
    (ICONS["lead"], "Takes callback requests", "Pricing and insurance questions go to the callback list with the patient's details. Ava never guesses a price."),
    (ICONS["bell"], "Sends reminders", "A text 24 hours before every visit, with a way to move it. Fewer empty chairs."),
    (ICONS["book"].replace("M9 15l2 2 4-4", "M12 14v3M12 14h3"), "Answers the basics", "Hours, address, parking, services. And transfers to your front desk whenever a caller asks."),
]


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def landing(request: Request):
    return render(request, "landing.html", features=FEATURES)


@router.get("/call", response_class=HTMLResponse, include_in_schema=False)
def call_page(request: Request):
    return render(request, "call.html")


@router.get("/demo", include_in_schema=False)
def old_demo():
    return RedirectResponse("/call", status_code=301)


class TurnIn(BaseModel):
    session: dict | None = None
    text: str = Field(default="", max_length=400)


def _rate_limit(request: Request, settings: Settings) -> None:
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[0].strip()
    q, now = _hits[ip], time.monotonic()
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= settings.demo_turns_per_ip_per_hour:
        raise HTTPException(429, "Too many requests, try again later.")
    q.append(now)


@router.post("/api/converse", tags=["demo"])
def converse_turn(body: TurnIn, request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    """One turn of a browser call. Same tools, same booking engine as the phone line."""
    _rate_limit(request, settings)
    if settings.demo_mode:
        demo_data.ensure_fresh(db, settings)
    result = converse(db, settings, body.session, body.text)
    return {"reply": result.reply, "session": result.session, "events": result.events, "sms": result.sms,
            "ended": result.ended, "engine": result.engine}


@router.get("/api/cron/daily", include_in_schema=False)
def cron_daily(db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
               authorization: str | None = Header(default=None), user_agent: str | None = Header(default=None)):
    """Vercel Cron: rebuild the sample clinic every day so the demo always looks like a live practice."""
    if settings.cron_secret:
        if not hmac.compare_digest(authorization or "", f"Bearer {settings.cron_secret}"):
            raise HTTPException(401)
    elif not (user_agent or "").startswith("vercel-cron"):  # without a secret, only the sample data can be reset
        raise HTTPException(401)
    if not settings.demo_mode:
        return {"demo_reset": False}
    return {"demo_reset": demo_data.reset(db, settings)}
