"""Misfire handling."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import Account, Contact, Draft, SendQueue
from app.scheduling.jobs import handle_misfires
from app.security import encrypt_secret


def test_misfire_marks_missed(db) -> None:
    acc = Account(email="a@gmail.com", enc_app_password=encrypt_secret("abcdefghijklmnop"))
    db.add(acc)
    db.flush()
    c = Contact(email="p@edu")
    db.add(c)
    db.flush()
    d = Draft(contact_id=c.id, account_id=acc.id, subject="s", body_text="b", status="queued")
    db.add(d)
    db.flush()
    now = datetime.now(timezone.utc)
    recent = SendQueue(
        draft_id=d.id,
        account_id=acc.id,
        scheduled_utc=now - timedelta(minutes=30),
        status="pending",
    )
    old = SendQueue(
        draft_id=d.id,
        account_id=acc.id,
        scheduled_utc=now - timedelta(hours=5),
        status="pending",
    )
    db.add_all([recent, old])
    db.commit()
    missed = handle_misfires(db)
    db.refresh(recent)
    db.refresh(old)
    assert missed == 1
    assert recent.status == "pending"
    assert old.status == "missed"
