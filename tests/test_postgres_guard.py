"""Runs only against Postgres: proves the database itself blocks a double booking race."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal, engine
from app.models import Appointment

pytestmark = pytest.mark.skipif(engine.dialect.name != "postgresql", reason="needs PostgreSQL")


def _appt(start, chair=1):
    return Appointment(patient_name="X", phone="+15550000000", service="checkup", chair=chair, start_at=start, end_at=start + timedelta(minutes=30))


def test_exclusion_constraint_rejects_overlap_on_same_chair():
    start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=3)
    with SessionLocal() as db:
        db.add(_appt(start))
        db.commit()
        db.add(_appt(start + timedelta(minutes=15)))  # simulates a concurrent call that passed the app-level check
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        db.add(_appt(start, chair=2))  # other chair is fine
        db.commit()
