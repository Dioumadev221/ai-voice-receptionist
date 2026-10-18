import os
from collections.abc import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine(url: str):
    if url.startswith("postgres://"):  # Render/Heroku style URLs
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    kwargs = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["connect_args"] = {"prepare_threshold": None}  # safe behind Neon / PgBouncer poolers
        if os.environ.get("VERCEL"):
            kwargs["poolclass"] = NullPool  # serverless: hold no idle connections between invocations
    return create_engine(url, **kwargs)


engine = _make_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


_ready = False


def get_db() -> Iterator[Session]:
    global _ready
    if not _ready:  # serverless platforms may skip the ASGI lifespan: create tables on first use
        init_db()
        _ready = True
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# On Postgres the database itself refuses two booked appointments on the same chair
# with overlapping times, so a race between two simultaneous calls cannot double-book.
_PG_NO_OVERLAP = """
CREATE EXTENSION IF NOT EXISTS btree_gist;
DO $$ BEGIN
  ALTER TABLE appointments ADD CONSTRAINT appointments_no_overlap
    EXCLUDE USING gist (chair WITH =, tstzrange(start_at, end_at) WITH &&)
    WHERE (status = 'booked');
EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL; END $$;
"""


def init_db() -> None:
    from app import models  # noqa: F401  (register tables)

    Base.metadata.create_all(engine)
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text(_PG_NO_OVERLAP))
