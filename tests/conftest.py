import os
from datetime import date, timedelta

os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "sqlite:///./test.db")
os.environ["VAPI_SECRET"] = "test-secret"
os.environ["ADMIN_PASSWORD"] = "admin"
os.environ["JOBS_TOKEN"] = "jobs"

import pytest
from fastapi.testclient import TestClient

from app.db import Base, engine, init_db
from app.main import app


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    init_db()
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def next_weekday(weekday: int, min_days: int = 3) -> date:
    d = date.today() + timedelta(days=min_days)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d


CALLER = "+15551234567"


def tool_call(client, name, args, call_id="call-1", caller=CALLER):
    payload = {
        "message": {
            "type": "tool-calls",
            "call": {"id": call_id, "customer": {"number": caller}},
            "toolCallList": [{"id": f"tc-{name}", "type": "function", "function": {"name": name, "arguments": args}}],
        }
    }
    r = client.post("/vapi/webhook", json=payload, headers={"X-Vapi-Secret": "test-secret"})
    assert r.status_code == 200, r.text
    result = r.json()["results"][0]
    assert result["toolCallId"] == f"tc-{name}"
    return result.get("result") or result.get("error")
