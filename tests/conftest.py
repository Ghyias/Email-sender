"""Pytest fixtures — no network / Ollama required."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

# Force Fernet key before importing app.security
os.environ.setdefault(
    "MAILPILOT_FERNET_KEY",
    "test-fernet-key-must-be-32b-padded====",  # will be replaced below
)

from cryptography.fernet import Fernet

_KEY = Fernet.generate_key().decode()
os.environ["MAILPILOT_FERNET_KEY"] = _KEY

from app.db import init_db, reset_engine  # noqa: E402
from app.llm.ollama_client import FakeOllamaClient, set_ollama_client  # noqa: E402
from app.security import reset_fernet_cache  # noqa: E402


@pytest.fixture()
def db_engine(tmp_path: Path):
    reset_engine()
    reset_fernet_cache()
    db_path = tmp_path / "test.db"
    from sqlalchemy import create_engine, event

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_conn, _) -> None:  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    init_db(engine)
    yield engine
    engine.dispose()
    reset_engine()
    reset_fernet_cache()


@pytest.fixture()
def db(db_engine) -> Session:
    factory = sessionmaker(bind=db_engine, autoflush=False, autocommit=False, future=True)
    session = factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def fake_ollama():
    client = FakeOllamaClient()
    set_ollama_client(client)
    yield client
    set_ollama_client(None)
