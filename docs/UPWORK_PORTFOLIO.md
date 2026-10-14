# Upwork portfolio entry (copy-paste)

Profile → Portfolio → Add a project. Fields in Upwork's order.

## Project title
AI Voice Receptionist That Books Dental Appointments 24/7 (Vapi + FastAPI)

## Your role
AI & Backend Developer: architecture, voice agent, backend, integrations, deployment

## Project description (Upwork limit: 600 characters)
Dental clinics lose patients every time a call goes unanswered. I built an AI phone receptionist that answers every call, books, reschedules and cancels appointments against the real schedule, texts confirmations and reminders, and alerts staff for emergencies. The LLM runs the conversation; a deterministic booking engine and a PostgreSQL constraint decide what can be booked, so it never double-books or invents a slot. Two-way Google Calendar sync, Twilio SMS, call summaries and a front-desk dashboard. Deployed with Docker.

## Skills
Vapi · FastAPI · PostgreSQL · AI Agent Development · Twilio
(if more slots: Google Calendar API, OpenAI API, Docker, Voice AI)

## Content blocks (add in this order)
1. **Video**: the 90-second Loom (call → SMS → dashboard → refusal with alternatives).
2. **Image**: `docs/img/dashboard.png`
3. **Link**: live demo `https://<your-app>/demo` (prospects call the agent from their browser)
4. **Link**: GitHub repo `https://github.com/Dioumadev221/ai-voice-receptionist`
5. **Text** (below)

---

**Problem**
Small clinics miss a large share of inbound calls: lunch breaks, after hours, a receptionist busy with a patient. Callers rarely leave voicemails; they call the next clinic. An after-hours receptionist is too expensive for the volume.

**Solution**
A voice agent ("Ava") on the clinic's phone number:
- Books, reschedules and cancels appointments in a natural conversation, with SMS confirmation codes.
- Recognises returning callers by caller ID; anything else requires their confirmation code.
- Triages emergencies: earliest slot and a text to on-call staff, or a 911 instruction when symptoms are dangerous.
- Captures pricing and insurance questions as callback leads instead of guessing.
- Transfers to a human on request.
- Sends 24-hour reminder texts automatically.
- Gives staff a dashboard: calls answered, bookings, after-hours calls, leads, transcripts and recordings.

**How it stays reliable**
The model never decides availability. A deterministic rules engine enforces opening hours, lunch, notice periods, chair capacity and service durations, and reads staff blocks from Google Calendar. A PostgreSQL exclusion constraint makes double booking impossible even when two callers grab the same slot at the same second. When a request is refused, the agent gets real alternatives to offer, not an error.

**Stack**
Vapi (Deepgram STT, GPT-4.1, ElevenLabs voice) · Python / FastAPI · PostgreSQL · SQLAlchemy · Google Calendar API (service account) · Twilio SMS · Docker · Render · GitHub Actions CI

**Results**
- End-to-end booking in a single call, typically under 2 minutes.
- 12 automated tests, including the double-booking race on real PostgreSQL, run on every push.
- Running cost around $0.10-0.15 per call minute, plus ~$15/month hosting and number.
- New clinic configured in under an hour: hours, chairs, services and timezone are settings.

---

## Proposal snippet (reuse in cover letters for voice / booking jobs)

> I recently built an AI voice receptionist that books, moves and cancels appointments over the phone with Vapi + FastAPI, synced to Google Calendar with SMS confirmations. You can call it from your browser here: <demo link>. The key design choice: the AI handles the conversation, but a rules engine and the database decide what can be booked, so it never double-books or invents availability. I'd apply the same approach to your [clinic / salon / service business]. Quick question: which calendar or practice management system do you use today?
