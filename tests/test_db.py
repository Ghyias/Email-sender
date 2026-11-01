"""DB creation + FTS + seeds."""

from __future__ import annotations

from sqlalchemy import select, text

from app.models import Setting, Template, UserProfile


def test_db_creates_tables_and_seeds(db, db_engine) -> None:
    assert db.scalar(select(Setting).limit(1)) is not None
    assert db.scalar(select(Setting).where(Setting.key == "dry_run")).value is True
    templates = db.scalars(select(Template)).all()
    assert len(templates) >= 2
    profile = db.scalar(select(UserProfile).limit(1))
    assert profile is not None
    assert "GPA" in (profile.facts_forbidden or [])


def test_fts5_exists(db_engine) -> None:
    with db_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT name FROM sqlite_master WHERE name='messages_fts'")
        ).fetchall()
    assert rows
