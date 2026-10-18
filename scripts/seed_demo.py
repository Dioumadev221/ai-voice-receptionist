"""Rebuild the sample clinic (a month of calls, two weeks of schedule). Wipes existing data.

python scripts/seed_demo.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.demo_data import reset  # noqa: E402

if __name__ == "__main__":
    init_db()
    with SessionLocal() as db:
        print(reset(db, get_settings()))
