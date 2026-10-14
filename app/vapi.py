"""Vapi server URL: tool calls during the call, and the end-of-call report afterwards."""

import hmac
import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clinic import BookingError
from app.config import Settings, get_settings
from app.db import get_db
from app.models import CallLog
from app.tools import HANDLERS, CallContext

log = logging.getLogger("vapi")
router = APIRouter(prefix="/vapi", tags=["vapi"])

OUTCOME_RANK = ["info", "lead", "cancelled", "rescheduled", "booked"]


def verify_secret(
    settings: Settings = Depends(get_settings),
    x_vapi_secret: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> None:
    if not settings.vapi_secret:
        return  # local development
    supplied = x_vapi_secret or (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied or "", settings.vapi_secret):
        raise HTTPException(status_code=401, detail="invalid secret")


def _tool_calls(message: dict) -> list[tuple[str, str, dict]]:
    """Normalise Vapi's tool call shapes into (id, name, args)."""
    raw = message.get("toolCallList") or [t.get("toolCall", {}) for t in message.get("toolWithToolCallList", [])]
    calls = []
    for tc in raw:
        fn = tc.get("function", {})
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except json.JSONDecodeError:
                args = {}
        calls.append((tc.get("id", ""), fn.get("name", ""), args))
    return calls


def _call_log(db: Session, call: dict) -> CallLog | None:
    call_id = call.get("id")
    if not call_id:
        return None
    row = db.scalar(select(CallLog).where(CallLog.vapi_call_id == call_id))
    if not row:
        row = CallLog(vapi_call_id=call_id, caller=(call.get("customer") or {}).get("number", ""), tools_used=[])
        db.add(row)
        db.flush()
    return row


def _bump_outcome(row: CallLog, outcomes: list[str]) -> None:
    for o in outcomes:
        if OUTCOME_RANK.index(o) > OUTCOME_RANK.index(row.outcome or "info"):
            row.outcome = o


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


@router.post("/webhook", dependencies=[Depends(verify_secret)])
async def webhook(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    payload = await request.json()
    message = payload.get("message", {})
    kind = message.get("type")
    call = message.get("call") or {}

    if kind == "tool-calls":
        ctx = CallContext(call_id=call.get("id"), caller_phone=(call.get("customer") or {}).get("number"))
        results = []
        for tool_call_id, name, args in _tool_calls(message):
            handler = HANDLERS.get(name)
            try:
                if not handler:
                    raise BookingError(f"Unknown tool {name}.")
                results.append({"toolCallId": tool_call_id, "result": handler(db, settings, ctx, args)})
            except BookingError as e:
                # A business-rule refusal is a normal answer the agent should relay, not a tool failure.
                db.rollback()
                results.append({"toolCallId": tool_call_id, "result": str(e)})
            except Exception:
                db.rollback()
                log.exception("tool %s failed", name)
                results.append({"toolCallId": tool_call_id, "error": "System error. Apologise and offer to take a message."})
        row = _call_log(db, call)
        if row:
            row.tools_used = [*(row.tools_used or []), *[n for _, n, _ in _tool_calls(message)]]
            _bump_outcome(row, ctx.outcomes)
            db.commit()
        return {"results": results}  # Vapi requires HTTP 200 even when a tool fails

    if kind == "end-of-call-report":
        row = _call_log(db, call)
        if row:
            artifact = message.get("artifact") or {}
            analysis = message.get("analysis") or {}
            row.ended_reason = message.get("endedReason", "") or ""
            row.summary = analysis.get("summary") or message.get("summary") or ""
            row.transcript = artifact.get("transcript") or message.get("transcript") or ""
            row.recording_url = artifact.get("recordingUrl") or message.get("recordingUrl") or ""
            row.started_at = _parse_ts(message.get("startedAt") or call.get("startedAt"))
            row.ended_at = _parse_ts(message.get("endedAt") or call.get("endedAt"))
            duration = message.get("durationSeconds")
            if duration is None and row.started_at and row.ended_at:
                duration = (row.ended_at - row.started_at).total_seconds()
            row.duration_seconds = int(duration or 0)
            row.cost_usd = message.get("cost")
            db.commit()
        return {"ok": True}

    # status-update, transcript, hang, etc. are acknowledged and ignored
    return {"ok": True}
