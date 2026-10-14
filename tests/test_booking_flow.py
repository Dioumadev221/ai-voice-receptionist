import re

from tests.conftest import CALLER, next_weekday, tool_call


def _code(text):
    return re.search(r"code ((?:[A-Z0-9] ){5}[A-Z0-9])", text).group(1).replace(" ", "")


def test_rejects_missing_secret(client):
    r = client.post("/vapi/webhook", json={"message": {"type": "status-update"}})
    assert r.status_code == 401


def test_availability_respects_hours_and_lunch(client):
    day = next_weekday(1)  # Tuesday
    out = tool_call(client, "check_availability", {"service": "cleaning", "date": day.isoformat()})
    times = re.findall(r"time=(\d\d:\d\d)", out)
    assert times and len(times) <= 3
    for t in times:
        h, m = map(int, t.split(":"))
        start, end = h * 60 + m, h * 60 + m + 60
        assert 8 * 60 <= start and end <= 18 * 60
        assert end <= 12 * 60 or start >= 13 * 60  # never runs into the 12:00-13:00 lunch break


def test_closed_on_sunday(client):
    day = next_weekday(6)
    out = tool_call(client, "book_appointment", {"patient_name": "Ann Lee", "service": "checkup", "date": day.isoformat(), "time": "10:00"})
    assert out.startswith("NOT BOOKED")
    assert "Closest open times" in out


def test_full_lifecycle_book_find_reschedule_cancel(client):
    day = next_weekday(2)  # Wednesday
    out = tool_call(client, "book_appointment", {"patient_name": "maria garcia", "service": "cleaning", "date": day.isoformat(), "time": "09:00", "is_new_patient": True})
    assert out.startswith("BOOKED"), out
    code = _code(out)

    found = tool_call(client, "find_appointment", {})
    assert "Maria Garcia" in found and code in found

    moved = tool_call(client, "reschedule_appointment", {"confirmation_code": code, "new_date": day.isoformat(), "new_time": "2:30 PM"})
    assert moved.startswith("RESCHEDULED"), moved
    assert "2:30 PM" in moved

    cancelled = tool_call(client, "cancel_appointment", {"confirmation_code": code, "reason": "travel"})
    assert cancelled.startswith("CANCELLED")
    assert "No upcoming appointments" in tool_call(client, "find_appointment", {})


def test_no_double_booking_beyond_chair_capacity(client):
    day = next_weekday(3)
    args = {"service": "checkup", "date": day.isoformat(), "time": "10:00"}
    assert tool_call(client, "book_appointment", {**args, "patient_name": "A One"}, caller="+15550000001").startswith("BOOKED")
    assert tool_call(client, "book_appointment", {**args, "patient_name": "B Two"}, caller="+15550000002").startswith("BOOKED")
    third = tool_call(client, "book_appointment", {**args, "patient_name": "C Three"}, caller="+15550000003")
    assert third.startswith("NOT BOOKED")  # 2 chairs configured


def test_overlap_blocks_longer_service(client):
    day = next_weekday(3)
    for i in range(2):
        tool_call(client, "book_appointment", {"patient_name": f"P {i}", "service": "checkup", "date": day.isoformat(), "time": "10:30"}, caller=f"+1555000001{i}")
    # a 60-min cleaning at 10:00 would run into the full 10:30 slot
    out = tool_call(client, "book_appointment", {"patient_name": "Late", "service": "cleaning", "date": day.isoformat(), "time": "10:00"})
    assert out.startswith("NOT BOOKED")


def test_other_caller_cannot_list_my_appointments(client):
    day = next_weekday(4)
    tool_call(client, "book_appointment", {"patient_name": "Jo Doe", "service": "checkup", "date": day.isoformat(), "time": "09:00"})
    other = tool_call(client, "find_appointment", {}, caller="+15559999999")
    assert "Jo Doe" not in other


def test_wrong_code_is_refused(client):
    assert "couldn't find" in tool_call(client, "cancel_appointment", {"confirmation_code": "ZZZZZZ"})


def test_lead_capture_and_call_report_feed_dashboard(client):
    tool_call(client, "capture_lead", {"name": "sam park", "reason": "Invisalign pricing", "insurance": "Delta Dental"}, call_id="call-9")
    report = {
        "message": {
            "type": "end-of-call-report",
            "call": {"id": "call-9", "customer": {"number": CALLER}},
            "endedReason": "customer-ended-call",
            "analysis": {"summary": "Asked about Invisalign pricing, callback requested."},
            "artifact": {"transcript": "AI: Hi...", "recordingUrl": "https://example.com/rec.wav"},
            "durationSeconds": 95,
            "cost": 0.14,
        }
    }
    assert client.post("/vapi/webhook", json=report, headers={"X-Vapi-Secret": "test-secret"}).status_code == 200
    stats = client.get("/api/stats", auth=("admin", "admin")).json()
    assert stats["calls"] == 1 and stats["leads"] == 1 and stats["resolution_rate"] == 100
    page = client.get("/", auth=("admin", "admin"))
    assert page.status_code == 200 and "Sam Park" in page.text and "Invisalign pricing" in page.text
    assert client.get("/", auth=("admin", "wrong")).status_code == 401


def test_reminder_job_requires_token(client):
    assert client.post("/jobs/reminders").status_code == 401
    assert client.post("/jobs/reminders", headers={"Authorization": "Bearer jobs"}).json() == {"reminders_sent": 0}


def test_string_arguments_and_unknown_tool(client):
    out = tool_call(client, "get_clinic_info", "{}")
    assert "BrightSmile" in out
    assert "Unknown tool" in tool_call(client, "launch_rockets", {})


def test_demo_page_renders_without_keys(client):
    r = client.get("/demo")
    assert r.status_code == 200 and "Demo not configured" in r.text
