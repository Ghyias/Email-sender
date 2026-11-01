"""Inbox filter helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import Account, Contact, Message
from app.routes.inbox import build_inbox_query, domain_match_sql
from app.security import encrypt_secret


def test_domain_match_sql() -> None:
    assert domain_match_sql("*.edu.cn") == "%.edu.cn"
    assert domain_match_sql("@mit.edu") == "%@mit.edu"


def test_domain_and_group_filters(db) -> None:
    acc = Account(email="me@gmail.com", enc_app_password=encrypt_secret("abcdefghijklmnop"))
    db.add(acc)
    db.flush()
    db.add(Contact(email="p@tsinghua.edu.cn", full_name="P", groups=["professor"]))
    now = datetime.now(timezone.utc)
    db.add(
        Message(
            account_id=acc.id,
            gm_msgid="1",
            uid=1,
            from_addr="p@tsinghua.edu.cn",
            subject="hi",
            date_utc=now - timedelta(days=2),
            body_text="x",
        )
    )
    db.add(
        Message(
            account_id=acc.id,
            gm_msgid="2",
            uid=2,
            from_addr="other@gmail.com",
            subject="other",
            date_utc=now - timedelta(days=2),
            body_text="y",
        )
    )
    db.commit()
    stmt, _, _ = build_inbox_query(
        db,
        domain="*.edu.cn",
        date_from=now - timedelta(days=30),
        group="professor",
    )
    rows = db.scalars(stmt).all()
    assert len(rows) == 1
    assert rows[0].from_addr.endswith(".edu.cn")
