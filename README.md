# Recepta: AI Voice Receptionist for Dental Clinics

A voice agent that answers every call, books, reschedules and cancels appointments against the clinic's real schedule, texts confirmations and reminders, and hands staff a dashboard of everything it did.

Built with **Vapi** (phone voice), **FastAPI** (tools and conversation API), **PostgreSQL** (bookings), **Google Calendar** (two-way sync) and **Twilio** (SMS).

**Live demo:** talk to Ava from your browser at `/call` and watch the booking land on the public dashboard at `/dashboard` (sample clinic, fictional 555 numbers, reset every day).

![Recepta](docs/media/1-product.png)

| Live call with tool trace | Front-desk dashboard |
| --- | --- |
| ![Call](docs/media/2-live-call.png) | ![Dashboard](docs/media/3-dashboard.png) |

## The problem

A typical dental front desk misses a large share of inbound calls: during lunch, after hours, while checking in a patient. Every missed new-patient call is lost revenue, and most callers won't leave a voicemail. Hiring an after-hours receptionist costs far more than the calls are worth.

## What it does

| Caller says | Agent does |
| --- | --- |
| "I need a cleaning next Tuesday morning" | Checks live availability, offers 3 spread-out times, books the one picked, texts a confirmation code |
| "I need to move my appointment" | Recognises the caller ID (or asks for the code), confirms the name, moves it, re-texts |
| "Cancel my Thursday visit" | Offers to reschedule first, cancels only on explicit yes, frees the slot and the calendar event |
| "I broke a tooth, it really hurts" | Books the earliest emergency slot (shorter notice period) and texts the on-call staff |
| "Swelling is spreading to my eye" | Tells the caller to call 911 / go to the ER. Does not book |
| "How much is Invisalign? Do you take Delta?" | Captures a callback lead and alerts staff. Never quotes a price |
| "Can I talk to a person?" | Transfers the call to the front desk |

After each call, Vapi's end-of-call report (summary, transcript, recording, cost) lands in the dashboard.

## Why it doesn't hallucinate bookings

The LLM handles the conversation. **It never decides whether a slot exists.** Every rule lives in a deterministic booking engine ([app/booking.py](app/booking.py)):

- Opening hours, Saturday hours, lunch break, 2-hour minimum notice (30 min for emergencies), 60-day horizon.
- Chair capacity: two patients can be seen at once, a third is refused.
- Service durations: a 60-minute cleaning at 10:00 is refused if 10:30 is full.
- Staff blocks in Google Calendar (meetings, days off) make those times unavailable.
- **No double booking, even under race conditions**: a PostgreSQL exclusion constraint (`EXCLUDE USING gist (chair WITH =, tstzrange(start_at, end_at) WITH &&)`) rejects overlapping appointments on the same chair at the database level. Tested.
- Identity before changes: a caller can only list appointments tied to their own caller ID; anything else needs the 6-character code sent by SMS.
- When a request is refused, the tool answers `NOT BOOKED ...` with the closest real alternatives, so the agent always has something true to offer.

Tool results are written for speech ("Tuesday, October 21 at 9:30 AM") with machine values alongside (`[date=2026-10-21 time=09:30]`), so the model books exactly what it offered.

## Architecture

```
Caller ──► Phone number (Twilio / Vapi) ──► Vapi assistant (STT · GPT-4.1 · ElevenLabs)
                                                   │ tool-calls / end-of-call-report
                                                   ▼
                                    FastAPI  /vapi/webhook  (secret-verified)
                                      ├── booking engine ── PostgreSQL
                                      ├── Google Calendar (push events, read staff blocks)
                                      ├── Twilio SMS (confirmations, reminders, staff alerts)
                                      └── /dashboard · /jobs/reminders (cron)

Browser ──► /call (Web Speech: speech-to-text, text-to-speech)
              └── POST /api/converse ──► dialogue engine or LLM ──► the same 7 tools
```

The browser demo line uses the exact tools and booking engine the phone agent uses. With `LLM_API_KEY` set it runs an OpenAI-compatible model with function calling; without it, a deterministic dialogue engine drives the same tools, so the public demo costs nothing to run.

| Path | Purpose |
| --- | --- |
| `app/booking.py` | Scheduling rules, slot search, booking / reschedule / cancel |
| `app/tools.py` | The 7 tools the agent can call, and their JSON schemas (single source of truth) |
| `app/vapi.py` | Vapi webhook: auth, tool dispatch, call logging |
| `app/integrations/` | Google Calendar and Twilio, both fail-safe (an outage never breaks a call) |
| `app/converse.py` | Browser conversation: LLM tool loop, or rules-based dialogue engine |
| `app/web.py` | Product site, `/call` demo line, `/api/converse`, daily demo reset |
| `app/dashboard.py` | Front-desk dashboard (HTTP Basic, public in demo mode) |
| `app/demo_data.py` | Sample clinic data, rebuilt daily in demo mode |
| `vapi/system_prompt.md` | The agent's instructions |
| `scripts/vapi_setup.py` | Creates / updates the Vapi assistant from code |
| `scripts/seed_demo.py` | Rebuild the sample clinic data |

## Run it locally

```bash
cp .env.example .env            # defaults work with no external accounts
docker compose up --build       # API + PostgreSQL on http://localhost:8000
docker compose exec api python scripts/seed_demo.py
# Site: http://localhost:8000 · demo line: /call · dashboard: /dashboard (admin / ADMIN_PASSWORD)
# Set DEMO_MODE=true to make the dashboard public with daily-reset sample data
```

Simulate a tool call exactly as Vapi sends it:

```bash
curl -s localhost:8000/vapi/webhook -H "X-Vapi-Secret: $VAPI_SECRET" -H 'content-type: application/json' -d '{
  "message": {"type": "tool-calls", "call": {"id": "test-1", "customer": {"number": "+15551234567"}},
    "toolCallList": [{"id": "tc1", "type": "function",
      "function": {"name": "check_availability", "arguments": {"service": "cleaning", "date": "2026-10-20"}}}]}}'
```

Tests (SQLite by default, PostgreSQL to include the race-condition test):

```bash
pip install -r requirements-dev.txt
pytest -q
TEST_DATABASE_URL=postgresql://user:pass@localhost/voice pytest -q
```

## Deploy

**Public demo, free tier (Vercel + Neon).** Import the repository in Vercel, add a Neon Postgres database from the Storage tab (it sets `DATABASE_URL`), set `DEMO_MODE=true`, deploy. `vercel.json` routes everything to the FastAPI app and schedules the daily demo reset. Optional: `LLM_API_KEY` (and `LLM_BASE_URL`, `LLM_MODEL` for any OpenAI-compatible provider) to let a language model drive the demo line.

**Production for a clinic.** `render.yaml` deploys the API, a managed PostgreSQL and an hourly reminder cron on Render in one step. Any Docker host works (Railway, Fly.io, a VPS). Then:

```bash
python scripts/vapi_setup.py --server-url https://<your-app>
```

creates the Vapi assistant with all tools wired to `/vapi/webhook`. Attach a phone number to it in Vapi and call.

## Running cost (per clinic)

| Item | Approx. |
| --- | --- |
| Vapi + STT + LLM + voice | ~$0.10-0.15 per call minute |
| Phone number | ~$1-2 / month |
| SMS | ~$0.01 per message (US, plus carrier fees) |
| Hosting (API + Postgres) | ~$13 / month |

A clinic taking 600 calls a month at 2.5 minutes each spends roughly $200 on voice, against the cost of a part-time receptionist or the revenue of a few missed new patients.

## Adapting it

Clinic hours, chairs, timezone and services are configuration (`.env` and `app/clinic.py`). The same backend fits physiotherapy, veterinary, beauty salons or any appointment-based business by editing the service catalogue and the prompt.

## Credits

Fonts: Fraunces and Manrope, SIL Open Font License 1.1.
