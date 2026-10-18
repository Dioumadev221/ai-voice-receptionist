"""Browser call: one conversational turn at a time, on the same tools the phone agent uses.

Two brains, one toolset:
- LLM mode (LLM_API_KEY set): any OpenAI-compatible model drives the conversation with function calling.
- Rules mode (no key, or the LLM is down): a deterministic dialogue manager parses the caller's words
  and calls the very same tool handlers. No paid service is needed to run the live demo.

The server is stateless: the browser sends back the session blob it received on the previous turn.
Nothing in that blob is trusted for business rules; every tool re-validates against the database.
"""

import json
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clinic import (
    SERVICES,
    BookingError,
    opening_hours_sentence,
    resolve_service,
    speak_datetime,
)
from app.config import Settings
from app.integrations.sms import outbox
from app.models import CallLog
from app.tools import HANDLERS, TOOL_SCHEMAS, CallContext

log = logging.getLogger("converse")
PROMPT_FILE = Path(__file__).resolve().parents[1] / "vapi" / "system_prompt.md"
MAX_TEXT = 400
OUTCOME_RANK = ["info", "lead", "cancelled", "rescheduled", "booked"]


@dataclass
class TurnResult:
    reply: str
    session: dict
    events: list[dict] = field(default_factory=list)
    sms: list[dict] = field(default_factory=list)
    ended: bool = False
    engine: str = "rules"


def new_session() -> dict:
    # 555-01XX numbers are reserved for fiction, so a demo caller can never be a real person
    caller = f"+155501{secrets.randbelow(10**5):05d}"
    return {"id": secrets.token_hex(8), "caller": caller, "state": {}, "messages": [], "transcript": []}


class Toolbox:
    """Runs tool handlers for one turn and records what happened, for the on-screen trace."""

    def __init__(self, db: Session, settings: Settings, ctx: CallContext):
        self.db, self.settings, self.ctx = db, settings, ctx
        self.events: list[dict] = []

    def call(self, name: str, args: dict) -> str:
        started = datetime.now(timezone.utc)
        try:
            result = HANDLERS[name](self.db, self.settings, self.ctx, args)
            ok = True
        except BookingError as e:
            self.db.rollback()
            result, ok = str(e), False
        ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        self.events.append({"tool": name, "args": args, "result": result, "ok": ok, "ms": ms})
        return result


# ------------------------------------------------------------------ public entry point


def converse(db: Session, settings: Settings, session: dict | None, text: str) -> TurnResult:
    session = _sanitize(session)
    text = (text or "").strip()[:MAX_TEXT]
    ctx = CallContext(call_id=f"web-{session['id']}", caller_phone=session["caller"])
    tools = Toolbox(db, settings, ctx)
    sms: list[dict] = []
    token = outbox.set(sms)
    try:
        if not text:  # call just connected
            reply, ended, engine = _greeting(settings), False, "rules"
        else:
            reply, ended, engine = None, False, "rules"
            if settings.llm_api_key:
                try:
                    reply, ended = _llm_turn(settings, session, text, tools)
                    engine = "llm"
                except Exception:
                    log.exception("LLM turn failed, falling back to the rules engine")
            if reply is None:
                reply, ended = RulesEngine(settings, session, tools).turn(text)
    finally:
        outbox.reset(token)

    if text:
        session["transcript"].append({"role": "caller", "text": text})
    session["transcript"].append({"role": "agent", "text": reply})
    session["transcript"] = session["transcript"][-40:]
    _log_call(db, settings, session, ctx, ended)
    return TurnResult(reply=reply, session=session, events=tools.events, sms=sms, ended=ended, engine=engine)


def _greeting(settings: Settings) -> str:
    return f"Thanks for calling {settings.clinic_name}, this is Ava. How can I help you today?"


def _sanitize(session: dict | None) -> dict:
    fresh = new_session()
    if not isinstance(session, dict):
        return fresh
    sid = str(session.get("id", ""))
    caller = str(session.get("caller", ""))
    if not re.fullmatch(r"[0-9a-f]{16}", sid) or not re.fullmatch(r"\+155501\d{5}", caller):
        return fresh
    state = session.get("state") if isinstance(session.get("state"), dict) else {}
    messages = session.get("messages") if isinstance(session.get("messages"), list) else []
    transcript = session.get("transcript") if isinstance(session.get("transcript"), list) else []
    return {"id": sid, "caller": caller, "state": state, "messages": messages[-30:], "transcript": transcript[-40:]}


def _log_call(db: Session, settings: Settings, session: dict, ctx: CallContext, ended: bool) -> None:
    now = datetime.now(timezone.utc)
    row = db.scalar(select(CallLog).where(CallLog.vapi_call_id == ctx.call_id))
    if not row:
        row = CallLog(vapi_call_id=ctx.call_id, caller=session["caller"], started_at=now, tools_used=[], outcome="info")
        db.add(row)
    for o in ctx.outcomes:
        if OUTCOME_RANK.index(o) > OUTCOME_RANK.index(row.outcome or "info"):
            row.outcome = o
    row.ended_at = now
    started = row.started_at if row.started_at.tzinfo else row.started_at.replace(tzinfo=timezone.utc)
    row.duration_seconds = int((now - started).total_seconds())
    row.transcript = "\n".join(f"{'Ava' if t['role'] == 'agent' else 'Caller'}: {t['text']}" for t in session["transcript"])
    row.summary = session["state"].get("summary") or (
        "Caller asked about the clinic; no booking change." if ended else "Browser call in progress."
    )
    if ended:
        row.ended_reason = "customer-ended-call"
    db.commit()


# ------------------------------------------------------------------ LLM mode


def _system_prompt(settings: Settings) -> str:
    now = datetime.now(settings.tz)
    prompt = PROMPT_FILE.read_text()
    prompt = re.sub(r"\{\{\s*\"now\".*?\}\}", now.strftime("%A, %B %d, %Y, %I:%M %p"), prompt)
    prompt = prompt.replace("{{clinic_name}}", settings.clinic_name).replace("{{timezone}}", settings.clinic_timezone)
    return prompt + (
        "\n\n## This call\nThis is a browser call on the clinic website. The caller's number is known to the system, "
        "so never ask for a phone number. Keep each reply under 40 words: it is read aloud."
    )


def _llm_turn(settings: Settings, session: dict, text: str, tools: Toolbox) -> tuple[str, bool]:
    messages = [m for m in session["messages"] if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    messages.append({"role": "user", "content": text})
    convo = [{"role": "system", "content": _system_prompt(settings)}, *messages]
    tool_defs = [{"type": "function", "function": s} for s in TOOL_SCHEMAS]
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    with httpx.Client(timeout=25) as client:
        for _ in range(6):
            r = client.post(url, headers=headers, json={
                "model": settings.llm_model, "messages": convo, "tools": tool_defs, "temperature": 0.2,
            })
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            if not calls:
                reply = (msg.get("content") or "").strip() or "Sorry, could you say that again?"
                break
            convo.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for c in calls:
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                name = c["function"]["name"]
                result = tools.call(name, args) if name in HANDLERS else f"Unknown tool {name}."
                convo.append({"role": "tool", "tool_call_id": c["id"], "content": result})
        else:
            reply = "Sorry, I'm having trouble with the schedule right now. Can I take a message instead?"
    messages.append({"role": "assistant", "content": reply})
    session["messages"] = messages[-30:]
    for ev in tools.events:
        if ev["ok"] and ev["result"].startswith(("BOOKED", "RESCHEDULED", "CANCELLED", "SAVED")):
            session["state"]["summary"] = ev["result"].split(". Confirmation code")[0].split(". Tell the")[0]
    ended = bool(re.search(r"\b(goodbye|have a (great|good|nice) day)\b", reply, re.I))
    return reply, ended


# ------------------------------------------------------------------ rules mode

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
ORDINALS = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2}
OPTION_RE = re.compile(r"\b(?:option|number)\s+(one|two|three|1|2|3)\b")
SLOT_RE = re.compile(r"\[date=(\d{4}-\d{2}-\d{2}) time=(\d{2}:\d{2})\]")


def a_an(label: str) -> str:
    return ("an " if label[:1].lower() in "aeiou" else "a ") + label


def _has(pattern: str, text: str) -> bool:
    return re.search(pattern, text, re.I) is not None


def is_yes(t: str) -> bool:
    return _has(r"\b(yes|yeah|yep|yup|sure|correct|right|ok|okay|perfect|sounds good|please do|go ahead|confirm|"
                r"that works|that's fine|great|do it|absolutely|definitely)\b", t) and not is_no(t)


def is_no(t: str) -> bool:
    return _has(r"\b(no|nope|nah|not really|don't|do not|wrong|that's not)\b", t)


def detect_intent(t: str) -> str | None:
    if _has(r"(can'?t breathe|cannot breathe|trouble breathing|hard to breathe|can'?t swallow|"
            r"swelling (is )?(spreading|going|up)|swollen (eye|neck)|(eye|neck) .*swell|high fever)", t):
        return "danger"
    if _has(r"\bcancel", t):
        return "cancel"
    if _has(r"\b(reschedul|move (it|my|the)|change (it|my|the)|push (it|my) back|different (time|day)|another (time|day))", t):
        return "reschedule"
    if _has(r"\b(how much|price|pricing|cost|insurance|insured|delta|cigna|aetna|metlife|payment plan|financ|afford)", t):
        return "lead"
    if _has(r"\b(human|real person|a person|someone|receptionist|front desk|staff|manager|speak to)", t):
        return "human"
    if _has(r"\b(hours|open|close|closing|address|where are you|located|location|parking|directions)\b", t):
        return "info"
    if _has(r"\b(bye|goodbye|that'?s all|that is all|nothing else|no thanks|no thank you|i'?m good|all set)\b", t):
        return "bye"
    if _has(r"\b(book|appointment|schedule|come in|see (the|a) dentist|availab|opening|slot|visit)", t):
        return "book"
    try:
        resolve_service(t)
        return "book"
    except BookingError:
        return None


def parse_day(t: str, today: date) -> date | None:
    t = t.lower()
    if _has(r"\b(today|tonight|this (morning|afternoon)|as soon as possible|asap|earliest|soonest|first available|right away|now)\b", t):
        return today
    if "day after tomorrow" in t:
        return today + timedelta(days=2)
    if _has(r"\btomorrow\b", t):
        return today + timedelta(days=1)
    for i, name in enumerate(WEEKDAYS):
        if re.search(rf"\b({name}|{name[:3]})\b", t):
            if "next week" in t:
                return today + timedelta(days=(7 - today.weekday()) + i)
            return today + timedelta(days=(i - today.weekday()) % 7 or 7)
    if "next week" in t:
        return today + timedelta(days=(7 - today.weekday()))
    m = re.search(r"\b(" + "|".join(m[:3] for m in MONTHS) + r")[a-z]*\.? (\d{1,2})(st|nd|rd|th)?\b", t) or None
    if m:
        month, day = [x[:3] for x in MONTHS].index(m.group(1)) + 1, int(m.group(2))
    else:
        m = re.search(r"\b(\d{1,2})(st|nd|rd|th)? (of )?(" + "|".join(x[:3] for x in MONTHS) + r")", t)
        if m:
            month, day = [x[:3] for x in MONTHS].index(m.group(4)) + 1, int(m.group(1))
        else:
            m = re.search(r"\bthe (\d{1,2})(st|nd|rd|th)\b", t)
            if not m:
                return None
            month, day = today.month, int(m.group(1))
            if day < today.day:
                month = month % 12 + 1
    year = today.year + (1 if month < today.month else 0)
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_part(t: str) -> str | None:
    if _has(r"\bmorning\b|\bearly\b|\bbefore (noon|lunch)\b", t):
        return "morning"
    if _has(r"\bafternoon\b|\bevening\b|\bafter (lunch|work|noon)\b|\blater in the day\b", t):
        return "afternoon"
    return None


def parse_clock(t: str) -> tuple[int, int, str] | None:
    """'9:30', '2 pm', 'at 10', or a bare '11' when that's all the caller said. Never the '27' in 'October 27'."""
    low = t.lower()
    m = (re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)", low)
         or re.search(r"\b(\d{1,2}):(\d{2})\b()", low)
         or re.search(r"\bat (\d{1,2})()()\b(?!st|nd|rd|th)", low)
         or re.search(r"\b(?:about|around|maybe|say)\s+(\d{1,2})()()\b(?!st|nd|rd|th)", low)
         or re.search(r"\b(\d{1,2})()()\s+(?:o'?clock|in the (?:morning|afternoon|evening)|at night|tonight)", low))
    if not m and len(low.split()) <= 3:
        m = re.search(r"^\D*\b(\d{1,2})()()\b(?!st|nd|rd|th)\D*$", low)
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").replace(".", "")
    if hour > 23 or minute > 59:
        return None
    return hour, minute, ampm


def pick_slot(t: str, offered: list[dict]) -> dict | None:
    if not offered:
        return None
    low = t.lower()
    opt = OPTION_RE.search(low)
    if opt:
        idx = {"one": 0, "two": 1, "three": 2}.get(opt.group(1), None)
        idx = int(opt.group(1)) - 1 if idx is None else idx
        return offered[idx] if idx < len(offered) else None
    clock = parse_clock(low)
    if clock:
        hour, minute, ampm = clock
        candidates = [hour] if ampm == "am" else [hour + 12 if hour < 12 else hour] if ampm == "pm" else [hour, hour + 12]
        for slot in offered:
            h, mi = map(int, slot["time"].split(":"))
            if h in candidates and mi == minute:
                return slot
    for word, idx in ORDINALS.items():
        if re.search(rf"\b{word}\b", low) and idx < len(offered):
            return offered[idx]
    if _has(r"\b(last|latest|later one)\b", low):
        return offered[-1]
    if _has(r"\b(earliest|soonest)\b", low):
        return offered[0]
    return None


def parse_name(t: str, awaiting: bool) -> str | None:
    m = re.search(r"\b(?:my name is|my name's|name is|this is|it's|it is|i am|i'm)\s+([a-z][a-z'\-]+(?:\s+[a-z][a-z'\-]+){0,3})", t, re.I)
    raw = m.group(1) if m else (t if awaiting else "")
    raw = re.sub(r"\b(and|for|calling|please|thanks|thank you|the|a)\b.*$", "", raw, flags=re.I).strip(" .,!")
    words = [w for w in raw.split() if w.isalpha() or "'" in w or "-" in w]
    if not words or len(words) > 4 or any(len(w) > 20 for w in words):
        return None
    stop = {"yes", "no", "ok", "okay", "sure", "hi", "hello", "um", "uh", "what", "about", "how", "can", "could",
            "do", "does", "at", "is", "have", "time", "when", "why", "where", "which", "i", "you", "it", "that", "this",
            "morning", "afternoon", "tomorrow", "today", "appointment", "book", "cancel", "move"}
    if any(w.lower() in stop for w in words):
        return None
    return " ".join(w.capitalize() for w in words)


def parse_code(t: str) -> str | None:
    """'PEH3TK', 'peh3tk' or 'P E H 3 T K'. A 6-letter lowercase word is a word, not a code."""
    m = re.search(r"\b([A-Z0-9]{6})\b", t) or re.search(r"\b([a-zA-Z]*\d[a-zA-Z0-9]*)\b", t)
    if m and len(m.group(1)) == 6:
        return m.group(1).upper()
    singles = "".join(re.findall(r"\b([A-Za-z0-9])\b", t)).upper()
    return singles[-6:] if len(singles) >= 6 else None


class RulesEngine:
    def __init__(self, settings: Settings, session: dict, tools: Toolbox):
        self.s, self.session, self.tools = settings, session, tools
        self.st: dict = session["state"]
        self.today = datetime.now(settings.tz).date()

    # -- helpers
    def done(self, text: str, summary: str | None = None) -> tuple[str, bool]:
        if summary:
            self.st["summary"] = summary
        self.st["step"] = "anything_else"
        return text + " Is there anything else I can help you with?", False

    def offer(self, service_key: str, day: date, part: str | None, step: str, intro: str = "") -> tuple[str, bool]:
        result = self.tools.call("check_availability", {
            "service": service_key, "date": day.isoformat(), "part_of_day": part or "any",
        })
        slots = [{"date": d, "time": t} for d, t in SLOT_RE.findall(result)]
        if not slots:
            self.st["step"] = "lead_name"
            self.st["lead_reason"] = f"Wants a {SERVICES[service_key].label}, no opening found"
            return ("I'm sorry, I don't see any openings for that in the next two weeks. "
                    "I can have the front desk call you to find a time. What's your name?"), False
        self.st.update(offered=slots, step=step)
        return intro + self.speak_options(slots, day) + " Which one works best for you?", False

    def speak_options(self, slots: list[dict], asked: date) -> str:
        dts = [datetime.fromisoformat(f"{x['date']}T{x['time']}") for x in slots]
        first = dts[0].date()
        lead = "" if first == asked else "The closest openings are "
        if len({d.date() for d in dts}) == 1:
            day = speak_datetime(dts[0]).split(" at ")[0]
            times = [speak_datetime(d).split(" at ")[1] for d in dts]
            listing = ", ".join(times[:-1]) + (" or " if len(times) > 1 else "") + times[-1]
            return f"{lead or 'I have '}{day} at {listing}."
        groups: list[tuple[str, list[str]]] = []
        for d in dts:
            day, hour = speak_datetime(d).split(" at ")
            if groups and groups[-1][0] == day:
                groups[-1][1].append(hour)
            else:
                groups.append((day, [hour]))
        parts = [f"{day} at {' or '.join(hours)}" for day, hours in groups]
        return f"{lead or 'I have '}" + ", or ".join(parts) + "."

    @staticmethod
    def slot_text(slot: dict) -> str:
        return speak_datetime(datetime.fromisoformat(f"{slot['date']}T{slot['time']}"))

    # -- main
    def turn(self, text: str) -> tuple[str, bool]:
        reply, ended = self._turn(text)
        # Asking the exact same question twice in a row sounds broken: acknowledge the miss instead.
        if reply == self.st.get("last_reply") and not ended:
            reply = "Sorry, I didn't catch that. " + reply
        self.st["last_reply"] = reply
        return reply, ended

    def _turn(self, text: str) -> tuple[str, bool]:
        step = self.st.get("step")
        intent = detect_intent(text)

        if intent == "danger":
            self.st["step"] = None
            return ("That sounds serious. Please call 911 or go to the nearest emergency room right now. "
                    "Swelling that spreads or trouble breathing needs immediate care. Take care."), True

        # An answer to the question we just asked comes first, unless the caller clearly changes topic.
        handler = getattr(self, f"on_{step}", None) if step else None
        switching = intent in ("cancel", "reschedule", "lead", "human", "info", "bye")
        if step == "cancel_confirm" and intent in ("cancel", "reschedule"):
            switching = False
        if handler and not switching:
            out = handler(text, intent)
            if out:
                return out
        # ...otherwise follow what the caller is asking for now.
        return self.route(text, intent)

    def route(self, text: str, intent: str | None) -> tuple[str, bool]:
        if intent == "bye":
            self.st["step"] = None
            return f"Thanks for calling {self.s.clinic_name}. Have a great day!", True
        if intent == "info":
            if _has(r"\b(address|where|located|location|directions|parking|find you)\b", text):
                return self.done(f"We're at {self.s.clinic_address}. There's free parking behind the building.",
                                 summary="Caller asked for the clinic address and parking.")
            return self.done(opening_hours_sentence(self.s), summary="Caller asked about opening hours.")
        if intent in ("lead", "human"):
            self.st["lead_reason"] = text
            self.st["step"] = "lead_name"
            if intent == "human":
                return ("On the phone line I'd transfer you to the front desk right now. "
                        "Here, I can have someone call you back. What's your name?"), False
            return ("Exact costs depend on your treatment and insurance, so I'll have the front desk call you back "
                    "with real numbers. Can I get your name?"), False
        if intent in ("cancel", "reschedule"):
            return self.start_manage(intent, text)
        # A day or time with no other request ("next Tuesday morning") is someone trying to book.
        if intent == "book" or (intent is None and (parse_day(text, self.today) or parse_part(text))):
            self.st["misses"] = 0
            return self.start_booking(text)
        if self.st.get("step") == "anything_else" and is_no(text):
            self.st["step"] = None
            return f"Thanks for calling {self.s.clinic_name}. Have a great day!", True
        return self.not_understood()

    def not_understood(self) -> tuple[str, bool]:
        """Never repeat the same line: rephrase, then give examples, then offer a human callback."""
        misses = self.st["misses"] = self.st.get("misses", 0) + 1
        if misses == 1:
            return ("Sorry, I didn't quite catch that. Are you calling to book a visit, change an appointment, "
                    "or with a question about the clinic?"), False
        if misses == 2:
            return ("Let me make it easier. You can say, for example: I'd like a cleaning on Tuesday, "
                    "I need to move my appointment, or what are your hours?"), False
        self.st["misses"] = 0
        self.st["lead_reason"] = "Caller could not be understood by the assistant; wants help"
        self.st["step"] = "lead_name"
        return ("I'm sorry, I'm having trouble understanding. I can have someone from the front desk call you back. "
                "What's your name?"), False

    # -- booking
    def start_booking(self, text: str) -> tuple[str, bool]:
        try:
            service = resolve_service(text)
        except BookingError:
            service = None
        if _has(r"\b(new patient|first time|never been|first visit)\b", text):
            self.st["new_patient"] = True
        name = parse_name(text, awaiting=False)
        if name and _has(r"\bmy name", text):
            self.st["name"] = name
        if not service:
            self.st["step"] = "book_service"
            self.st["pending_text"] = text
            return "Sure. What is the visit for: a check-up, a cleaning, a filling, whitening, or something else?", False
        return self.book_for(service.key, text)

    def book_for(self, service_key: str, text: str) -> tuple[str, bool]:
        self.st["service"] = service_key
        if self.st.get("new_patient") and service_key in ("checkup", "cleaning"):
            service_key = self.st["service"] = "new_patient"
        day = parse_day(text, self.today) or parse_day(self.st.get("pending_text", ""), self.today) or self.today
        part = parse_part(text) or parse_part(self.st.get("pending_text", ""))
        intro = f"Sure, {a_an(SERVICES[service_key].label)}. "
        if service_key == "emergency":
            intro = "I'm sorry you're in pain. Let's get you seen as soon as possible. "
            day = self.today
        elif parse_clock(text) and len(text.split()) > 3:
            slot, refusal = self.exact_time(text, service_key)
            if slot:
                self.st["slot"] = slot
                if self.st.get("name"):
                    return self.confirm_booking()
                self.st["step"] = "book_name"
                return f"Good news, {self.slot_text(slot)} is free. Can I get the patient's full name?", False
            if refusal:
                self.st["step"] = "book_slot"
                return refusal, False
        return self.offer(service_key, day, part, "book_slot", intro)

    def on_book_service(self, text: str, intent: str | None):
        try:
            return self.book_for(resolve_service(text).key, text)
        except BookingError:
            return "Sorry, which service is that for? For example a cleaning, a check-up or an emergency visit.", False

    def exact_time(self, text: str, service_key: str) -> tuple[dict | None, str | None]:
        """Caller named a time we didn't offer ("9:30 please"): check that exact slot."""
        clock = parse_clock(text)
        if not clock:
            return None, None
        hour, minute, ampm = clock
        if ampm == "pm" and hour < 12:
            hour += 12
        elif not ampm and hour < 12 and re.search(r"\b(night|evening|tonight)\b", text.lower()):
            hour += 12
        elif not ampm and 1 <= hour <= 6:
            hour += 12  # "at 2" in a clinic means 2 PM
        offered = self.st.get("offered") or []
        anchor = self.st.get("slot") or (offered[0] if offered else None)
        day = parse_day(text, self.today) or (date.fromisoformat(anchor["date"]) if anchor else self.today)
        result = self.tools.call("check_availability", {"service": service_key, "date": day.isoformat(), "time": f"{hour:02d}:{minute:02d}"})
        found = SLOT_RE.findall(result)
        if result.startswith("AVAILABLE") and found:
            return {"date": found[0][0], "time": found[0][1]}, None
        alternatives = [{"date": d, "time": t} for d, t in found]
        if alternatives:
            self.st["offered"] = alternatives
            return None, "That time isn't available. " + self.speak_options(alternatives, day) + " Would one of those work?"
        return None, "That time isn't available, and I don't see anything close. Would another day work?"

    def on_book_slot(self, text: str, intent: str | None):
        slot = pick_slot(text, self.st.get("offered", []))
        if not slot:
            slot, refusal = self.exact_time(text, self.st["service"])
            if refusal:
                return refusal, False
        if slot:
            self.st["slot"] = slot
            if self.st.get("name"):
                return self.confirm_booking()
            self.st["step"] = "book_name"
            return f"{self.slot_text(slot)}, great. Can I get the patient's full name?", False
        day, part = parse_day(text, self.today), parse_part(text)
        if day or part:
            return self.offer(self.st["service"], day or self.today, part, "book_slot")
        options = self.st.get("offered") or []
        if options:
            return "Sorry, which time would you like? " + self.speak_options(options, self.today) + " Or I can look at another day.", False
        return "Which day and time would suit you?", False

    def on_book_name(self, text: str, intent: str | None):
        if re.search(r"\d", text) or parse_day(text, self.today):  # caller changed their mind about the time
            self.st["step"] = "book_slot"
            return self.on_book_slot(text, intent)
        name = parse_name(text, awaiting=True)
        if not name:
            return "Sorry, I didn't catch the name. Could you say the patient's first and last name?", False
        self.st["name"] = name
        return self.confirm_booking()

    def confirm_booking(self):
        self.st["step"] = "book_confirm"
        label = SERVICES[self.st["service"]].label
        return f"Let me confirm: {a_an(label)} for {self.st['name']} on {self.slot_text(self.st['slot'])}. Shall I book it?", False

    def on_book_confirm(self, text: str, intent: str | None):
        if is_no(text):
            self.st["step"] = "book_slot"
            return "No problem. Would you like one of the other times, or a different day?", False
        if not is_yes(text):
            return "Should I go ahead and book it? Just say yes or no.", False
        slot, service = self.st["slot"], self.st["service"]
        result = self.tools.call("book_appointment", {
            "patient_name": self.st["name"], "service": service, "date": slot["date"], "time": slot["time"],
            "is_new_patient": bool(self.st.get("new_patient") or service == "new_patient"),
        })
        if result.startswith("BOOKED"):
            code = re.search(r"code ((?:[A-Z0-9] ){5}[A-Z0-9])", result).group(1)
            label, when = SERVICES[service].label, self.slot_text(slot)
            self.st.pop("offered", None)
            return self.done(
                f"You're all set, {self.st['name'].split()[0]}. Your {label} is booked for {when}. "
                f"Your confirmation code is {code}, and I've just texted it to you.",
                summary=f"Booked {a_an(label)} for {self.st['name']} on {when}.",
            )
        alternatives = [{"date": d, "time": t} for d, t in SLOT_RE.findall(result)]
        self.st.update(step="book_slot", offered=alternatives)
        if alternatives:
            return "Ah, that time was just taken. " + self.speak_options(alternatives, self.today) + " Which one would you like?", False
        return "Sorry, that time is no longer available. Would you like another day?", False

    # -- reschedule / cancel
    def start_manage(self, intent: str, text: str):
        self.st["manage"] = intent
        code = parse_code(text)
        result = self.tools.call("find_appointment", {"confirmation_code": code} if code else {})
        found = re.findall(r"([^:;]+?): (.+?) on (.+?) \(code ([A-Z0-9]{6})\)", result.replace("Upcoming appointments: ", ""))
        if not found:
            self.st["step"] = "manage_code"
            if code:
                spaced = " ".join(code)
                return (f"I couldn't find an upcoming appointment with the code {spaced}. "
                        "Could you check the text message and read it again?"), False
            return ("I don't see an appointment under the number you're calling from. "
                    "What's the six-character confirmation code from your text message?"), False
        name, label, when, code = found[0]
        self.st.update(appt={"name": name.strip(), "label": label, "when": when, "code": code,
                             "service": next((k for k, v in SERVICES.items() if v.label == label), "checkup")})
        if intent == "cancel":
            self.st["step"] = "cancel_confirm"
            return (f"I have {name.strip()}'s {label} on {when}. Do you want me to cancel it, "
                    "or would you rather move it to another day?"), False
        self.st["step"] = "resched_day"
        day = parse_day(text, self.today)
        if day:
            return self.on_resched_day(text, intent)
        return f"I have {name.strip()}'s {label} on {when}. What day would work better?", False

    def on_manage_code(self, text: str, intent: str | None):
        code = parse_code(text)
        if not code:
            return "Could you read me the six letters and numbers of the code, one at a time?", False
        return self.start_manage(self.st.get("manage", "reschedule"), text)

    def on_cancel_confirm(self, text: str, intent: str | None):
        if intent == "reschedule" or _has(r"\b(move|another day|reschedul|different)", text):
            self.st.update(manage="reschedule", step="resched_day")
            return "Sure. What day would work better?", False
        if is_yes(text) or _has(r"\bcancel", text):
            appt = self.st["appt"]
            result = self.tools.call("cancel_appointment", {"confirmation_code": appt["code"], "reason": "Cancelled by phone"})
            if result.startswith("CANCELLED"):
                return self.done(f"Done. Your {appt['label']} on {appt['when']} is cancelled, and I've sent you a text.",
                                 summary=f"Cancelled {appt['name']}'s {appt['label']} on {appt['when']}.")
            return result, False
        if is_no(text):
            return self.done("No problem, your appointment stays as it is.")
        return "Should I cancel it, or move it to another day?", False

    def on_resched_day(self, text: str, intent: str | None):
        day, part = parse_day(text, self.today), parse_part(text)
        if not day and not part:
            return "Which day would you like instead? For example, next Tuesday morning.", False
        return self.offer(self.st["appt"]["service"], day or self.today, part, "resched_slot")

    def on_resched_slot(self, text: str, intent: str | None):
        slot = pick_slot(text, self.st.get("offered", []))
        if not slot:
            slot, refusal = self.exact_time(text, self.st["appt"]["service"])
            if refusal:
                return refusal, False
        if not slot:
            day, part = parse_day(text, self.today), parse_part(text)
            if day or part:
                return self.offer(self.st["appt"]["service"], day or self.today, part, "resched_slot")
            return "Which of those times would you like? " + self.speak_options(self.st.get("offered") or [], self.today), False
        self.st.update(slot=slot, step="resched_confirm")
        return f"Move your {self.st['appt']['label']} to {self.slot_text(slot)}?", False

    def on_resched_confirm(self, text: str, intent: str | None):
        if is_no(text):
            self.st["step"] = "resched_slot"
            return "Okay. Which other time would you prefer?", False
        if not is_yes(text):
            return "Should I move it? Yes or no.", False
        appt, slot = self.st["appt"], self.st["slot"]
        result = self.tools.call("reschedule_appointment", {
            "confirmation_code": appt["code"], "new_date": slot["date"], "new_time": slot["time"],
        })
        if result.startswith("RESCHEDULED"):
            when = self.slot_text(slot)
            return self.done(f"Done. Your {appt['label']} is now on {when}. Same confirmation code, and I've texted you the new time.",
                             summary=f"Moved {appt['name']}'s {appt['label']} to {when}.")
        alternatives = [{"date": d, "time": t} for d, t in SLOT_RE.findall(result)]
        self.st.update(step="resched_slot", offered=alternatives)
        return "That time just went. " + (self.speak_options(alternatives, self.today) if alternatives else "") + " Which would you like?", False

    # -- callback lead
    def on_lead_name(self, text: str, intent: str | None):
        name = parse_name(text, awaiting=True)
        if not name:
            return "Sorry, what name should the team ask for?", False
        reason = self.st.get("lead_reason", "Callback requested")
        insurance = next((i.title() for i in ("delta dental", "cigna", "aetna", "metlife", "humana", "guardian")
                          if i in reason.lower()), "")
        urgent = "urgent" if _has(r"\b(urgent|asap|pain|today)\b", reason) else "normal"
        result = self.tools.call("capture_lead", {"name": name, "reason": reason, "insurance": insurance, "urgency": urgent})
        if result.startswith("SAVED"):
            return self.done(f"Thanks, {name.split()[0]}. A team member will call you back during opening hours, usually the same day.",
                             summary=f"{name} asked: \"{reason[:120]}\". Callback requested.")
        return result, False

    def on_anything_else(self, text: str, intent: str | None):
        if intent is None and is_no(text):
            self.st["step"] = None
            return f"Thanks for calling {self.s.clinic_name}. Have a great day!", True
        return None
