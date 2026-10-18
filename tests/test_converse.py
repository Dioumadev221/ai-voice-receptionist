"""Browser call, rules engine (no LLM key): full conversations against the real booking engine."""

import re
from datetime import date

from app.converse import parse_code, parse_day, parse_name, pick_slot


class Call:
    def __init__(self, client):
        self.client, self.session, self.events, self.sms = client, None, [], []
        self.reply = self.say("")

    def say(self, text):
        r = self.client.post("/api/converse", json={"session": self.session, "text": text})
        assert r.status_code == 200, r.text
        data = r.json()
        self.session = data["session"]
        self.events += data["events"]
        self.sms += data["sms"]
        self.ended = data["ended"]
        self.reply = data["reply"]
        return self.reply


def test_book_by_voice_end_to_end(client):
    c = Call(client)
    assert "Ava" in c.reply
    r = c.say("Hi, I'd like to book a cleaning next Wednesday morning")
    assert "Which one works best" in r and c.events[-1]["tool"] == "check_availability"
    r = c.say("the second one please")
    assert "full name" in r
    r = c.say("Maria Garcia")
    assert "Shall I book it" in r and "Maria Garcia" in r
    r = c.say("yes please")
    assert "You're all set" in r and c.events[-1]["tool"] == "book_appointment"
    assert c.sms and "confirmed" in c.sms[-1]["body"]
    code = re.search(r"code ([A-Z0-9]{6})", c.sms[-1]["body"]).group(1)

    # same caller asks to move it: recognised by caller ID, no code needed
    r = c.say("Actually can I move my appointment to Thursday afternoon")
    assert c.events[-2]["tool"] == "find_appointment" or "Which one" in r
    if "What day" in r:
        r = c.say("Thursday afternoon")
    assert "Which one works best" in r
    r = c.say("the first one")
    assert r.startswith("Move your")
    r = c.say("yes")
    assert r.startswith("Done.") and c.events[-1]["tool"] == "reschedule_appointment"

    # dashboard shows the browser call and the booking
    page = client.get("/dashboard", auth=("admin", "admin")).text
    assert "Maria Garcia" in page
    r = c.say("no that's all, thanks")
    assert c.ended

    # a different caller cancels with the code
    d = Call(client)
    r = d.say("I want to cancel my appointment")
    assert "confirmation code" in r
    r = d.say(f"it's {code}")
    assert "cancel it" in r
    r = d.say("yes cancel it")
    assert r.startswith("Done.") and d.events[-1]["tool"] == "cancel_appointment"


def test_emergency_and_danger(client):
    c = Call(client)
    r = c.say("I chipped a tooth and it really hurts")
    assert "sorry you're in pain" in r.lower()
    assert c.events[-1]["args"]["service"] == "emergency"
    d = Call(client)
    r = d.say("my face is swollen and the swelling is spreading to my eye, I can't swallow")
    assert "911" in r and d.ended and not d.events


def test_pricing_goes_to_callback_list(client):
    c = Call(client)
    r = c.say("How much is Invisalign? Do you take Delta Dental?")
    assert "name" in r
    r = c.say("Sam Park")
    assert "call you back" in r and c.events[-1]["tool"] == "capture_lead"
    assert c.events[-1]["args"]["insurance"] == "Delta Dental"


def test_hours_and_closed_day(client):
    c = Call(client)
    assert "Monday to Friday" in c.say("what are your hours?")
    r = c.say("can I get a checkup on Sunday")
    assert "Which one works best" in r  # Sunday is closed: offers the closest real openings instead
    assert "Sunday" not in r.split("Which")[0]


def test_tampered_session_is_reset(client):
    r = client.post("/api/converse", json={"session": {"id": "x", "caller": "+15551234567", "state": {}}, "text": "hi"})
    assert re.fullmatch(r"\+155501\d{5}", r.json()["session"]["caller"])


def test_parsers():
    today = date(2026, 10, 14)  # a Wednesday
    assert parse_day("tomorrow", today) == date(2026, 10, 15)
    assert parse_day("next monday", today) == date(2026, 10, 19)
    assert parse_day("wednesday", today) == date(2026, 10, 21)
    assert parse_day("october 30th", today) == date(2026, 10, 30)
    assert parse_day("the 2nd of january", today) == date(2027, 1, 2)
    offered = [{"date": "2026-10-20", "time": "08:00"}, {"date": "2026-10-20", "time": "09:30"}, {"date": "2026-10-20", "time": "14:00"}]
    assert pick_slot("the second one", offered)["time"] == "09:30"
    assert pick_slot("9:30 works", offered)["time"] == "09:30"
    assert pick_slot("2 pm", offered)["time"] == "14:00"
    assert pick_slot("option three", offered)["time"] == "14:00"
    assert parse_name("my name is john smith", False) == "John Smith"
    assert parse_name("Aisha Bello", True) == "Aisha Bello"
    assert parse_name("yes", True) is None
    assert parse_code("my code is PEH3TK") == "PEH3TK"
    assert parse_code("p e h 3 t k") == "PEH3TK"
    assert parse_code("cancel please") is None


def test_llm_mode_runs_tools_and_falls_back(client, monkeypatch):
    """With an LLM key, the model drives the turn through the same tools; if the API fails, rules take over."""
    import httpx

    from app import converse
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    replies = iter([
        {"tool_calls": [{"id": "t1", "type": "function", "function": {"name": "get_clinic_info", "arguments": "{}"}}]},
        {"content": "We're open Monday to Friday, 8 to 6."},
    ])

    class FakeResp:
        def __init__(self, msg):
            self.msg = msg

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": self.msg}]}

    class FakeClient:  # replaces only the client app.converse uses, not the test client
        post_impl = staticmethod(lambda url, **kw: FakeResp(next(replies)))

        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, **kw):
            return FakeClient.post_impl(url, **kw)

    monkeypatch.setattr(converse.httpx, "Client", FakeClient)
    c = Call(client)
    r = c.say("when are you open?")
    assert r.startswith("We're open") and c.events[-1]["tool"] == "get_clinic_info"

    def boom(url, **kw):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(FakeClient, "post_impl", staticmethod(boom))
    assert "Monday to Friday" in c.say("what are your hours?")  # rules engine answered


def test_caller_asks_for_a_time_that_was_not_offered(client):
    c = Call(client)
    c.say("I'd like a check-up next Thursday")
    r = c.say("do you have 3:30 pm?")
    assert c.events[-1]["args"].get("time") == "15:30"
    assert "full name" in r or "isn't available" in r
    r = c.say("what about 11 at night")
    assert "isn't available" in r or "full name" in r


def test_unclear_caller_is_helped_not_looped(client):
    c = Call(client)
    r1 = c.say("it's okay all right")
    r2 = c.say("I like to do it")
    r3 = c.say("hmm")
    assert len({r1, r2, r3}) == 3
    assert "call you back" in r3
    c.say("Maria Garcia")
    assert c.events[-1]["tool"] == "capture_lead"


def test_a_day_alone_starts_a_booking(client):
    c = Call(client)
    r = c.say("Next Tuesday tomorrow")
    assert "What is the visit for" in r
    r = c.say("a cleaning")
    assert "Which one works best" in r
