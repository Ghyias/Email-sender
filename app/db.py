"""Database engine, sessions, WAL, FTS5 setup."""

from __future__ import annotations

import logging
import secrets
from contextlib import contextmanager
from typing import Any, Generator, Iterator

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import DB_PATH, ensure_data_dirs
from app.models import Base, Setting, Template, UserProfile

logger = logging.getLogger(__name__)

_engine: Engine | None = None
SessionLocal: sessionmaker[Session] | None = None

DEFAULT_SETTINGS: dict[str, Any] = {
    "timezone": "Asia/Shanghai",
    "sync_interval_minutes": 5,
    "sync_window_days": 365,
    "ollama_url": "http://127.0.0.1:11434",
    "drafting_model": "qwen3.5:4b",
    "embedding_model": "qwen3-embedding:0.6b",
    "temperature": 0.4,
    "dry_run": True,
    "default_daily_cap": 30,
    "default_min_delay_s": 90,
    "default_max_delay_s": 240,
    "attachment_filename_pattern": "{name}_CV.pdf",
    "pause_all_sending": False,
    "session_secret": "",
}

WESTERN_TEMPLATE = {
    "name": "Western PhD enquiry (EN)",
    "language": "en",
    "region_type": "western",
    "subject_template": "PhD enquiry — {{ contact.full_name }} / {{ contact.institution }}",
    "body_template": (
        "Dear {{ contact.full_name }},\n\n"
        "{{ llm.personal_paragraph }}\n\n"
        "I am writing to enquire about PhD opportunities in your group. "
        "My CV is attached.\n\n"
        "Kind regards,\n{{ user.full_name }}"
    ),
    "llm_instructions": (
        "Write a concise personal paragraph referencing 1–2 specific items from the "
        "professor's research. Professional tone, no flattery. Mention the CV is attached."
    ),
    "forbidden_phrases": ["GPA", "CGPA", "grade point"],
    "max_words": 250,
}

CHINA_TEMPLATE = {
    "name": "China / CSC enquiry (EN, simpler wording)",
    "language": "en",
    "region_type": "china",
    "subject_template": "PhD application enquiry — {{ contact.full_name }}",
    "body_template": (
        "Dear Professor {{ contact.full_name }},\n\n"
        "{{ llm.personal_paragraph }}\n\n"
        "I plan to apply for a CSC scholarship. My CV is attached.\n\n"
        "Best regards,\n{{ user.full_name }}"
    ),
    "llm_instructions": (
        "Use short sentences and simple vocabulary. Mention CSC scholarship plan. "
        "Reference 1 specific research topic from the contact data. Mention CV attached."
    ),
    "forbidden_phrases": ["GPA", "CGPA", "grade point"],
    "max_words": 180,
}


def get_engine(db_path: str | None = None) -> Engine:
    """Create or return the global engine (WAL mode)."""
    global _engine, SessionLocal
    ensure_data_dirs()
    path = db_path or str(DB_PATH)
    if _engine is not None and db_path is None:
        return _engine
    url = f"sqlite:///{path}"
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _connection_record) -> None:  # noqa: ANN001
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    if db_path is None:
        _engine = engine
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return engine


def init_db(engine: Engine | None = None) -> None:
    """Create tables, FTS5, seed defaults."""
    eng = engine or get_engine()
    Base.metadata.create_all(eng)
    _setup_fts5(eng)
    with Session(eng) as session:
        _seed_defaults(session)
        session.commit()


def _setup_fts5(engine: Engine) -> None:
    """Create FTS5 virtual table and sync triggers for messages."""
    stmts = [
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            subject, body_text, from_name, from_addr,
            content='messages', content_rowid='id'
        )
        """,
        """
        CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, subject, body_text, from_name, from_addr)
            VALUES (new.id, new.subject, new.body_text, new.from_name, new.from_addr);
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, subject, body_text, from_name, from_addr)
            VALUES ('delete', old.id, old.subject, old.body_text, old.from_name, old.from_addr);
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, subject, body_text, from_name, from_addr)
            VALUES ('delete', old.id, old.subject, old.body_text, old.from_name, old.from_addr);
            INSERT INTO messages_fts(rowid, subject, body_text, from_name, from_addr)
            VALUES (new.id, new.subject, new.body_text, new.from_name, new.from_addr);
        END
        """,
    ]
    with engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))


def _seed_defaults(session: Session) -> None:
    """Seed settings, templates, and empty user profile."""
    for key, value in DEFAULT_SETTINGS.items():
        existing = session.get(Setting, key)
        if existing is None:
            if key == "session_secret":
                value = secrets.token_urlsafe(32)
            session.add(Setting(key=key, value=value))

    if session.scalar(select(Template).limit(1)) is None:
        for tpl in (WESTERN_TEMPLATE, CHINA_TEMPLATE):
            session.add(Template(**tpl))

    if session.scalar(select(UserProfile).limit(1)) is None:
        session.add(
            UserProfile(
                full_name="",
                short_bio="",
                research_interests="",
                signature_default="",
                facts_allowed=[],
                facts_forbidden=["GPA", "CGPA", "grade point"],
            )
        )


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """Provide a transactional scope around a series of operations."""
    factory = SessionLocal
    if engine is not None:
        factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    if factory is None:
        get_engine()
        factory = SessionLocal
    assert factory is not None
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a DB session."""
    if SessionLocal is None:
        get_engine()
        init_db()
    assert SessionLocal is not None
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_setting(session: Session, key: str, default: Any = None) -> Any:
    """Read a setting value."""
    row = session.get(Setting, key)
    if row is None:
        return DEFAULT_SETTINGS.get(key, default)
    return row.value if row.value is not None else default


def set_setting(session: Session, key: str, value: Any) -> None:
    """Upsert a setting value."""
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value


def reset_engine() -> None:
    """Dispose global engine (tests)."""
    global _engine, SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    SessionLocal = None
