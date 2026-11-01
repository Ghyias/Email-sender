"""Sync logic with mocked IMAPClient."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import sessionmaker

from app.mail.imap_sync import find_all_mail_folder, sync_account
from app.models import Account
from app.security import encrypt_secret


class FakeIMAP:
    def __init__(self) -> None:
        self._logged_in = False

    def login(self, email: str, password: str) -> None:
        self._logged_in = True

    def list_folders(self):
        return [((b"\\HasNoChildren", b"\\All"), b"/", "[Gmail]/All Mail")]

    def select_folder(self, name: str, readonly: bool = False):
        assert readonly is True
        return {b"UIDVALIDITY": 42}

    def search(self, criteria):
        return [101, 102]

    def fetch(self, uids, items):
        out = {}
        for uid in uids:
            header = (
                b"From: Prof <prof@mit.edu>\r\n"
                b"To: me@gmail.com\r\n"
                b"Subject: Hello\r\n"
                b"Message-ID: <m" + str(uid).encode() + b"@x>\r\n"
                b"Date: Sat, 01 Nov 2025 10:00:00 +0000\r\n"
                b"\r\n"
            )
            out[uid] = {
                b"X-GM-MSGID": uid * 1000,
                b"X-GM-THRID": 999,
                b"X-GM-LABELS": [b"INBOX"],
                b"FLAGS": (),
                b"RFC822.SIZE": 100,
                b"BODY[HEADER]": header,
                b"BODY[TEXT]": b"Body text here about research.",
            }
        return out

    def logout(self) -> None:
        pass


def test_find_all_mail_folder() -> None:
    client = FakeIMAP()
    assert find_all_mail_folder(client) == "[Gmail]/All Mail"


def test_sync_account_with_mock(db, db_engine) -> None:
    acc = Account(
        email="me@gmail.com",
        display_name="Me",
        enc_app_password=encrypt_secret("x" * 16),
        active=True,
        sync_window_days=365,
    )
    db.add(acc)
    db.commit()
    db.refresh(acc)
    factory = sessionmaker(bind=db_engine, autoflush=False, autocommit=False, future=True)

    def imap_factory() -> Any:
        return FakeIMAP()

    result = sync_account(factory, acc.id, imap_factory=imap_factory)
    assert result.fetched == 2
    from sqlalchemy import func, select

    from app.models import Message

    s = factory()
    try:
        count = s.scalar(select(func.count()).select_from(Message))
        assert count == 2
        msg = s.scalar(select(Message).limit(1))
        assert msg is not None
        assert msg.body_html is None  # lazy
        assert "Body text" in msg.body_text
    finally:
        s.close()
