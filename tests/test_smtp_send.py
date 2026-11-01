"""SMTP send via aiosmtpd + dry-run."""

from __future__ import annotations

from email import message_from_bytes
from pathlib import Path

import pytest
from aiosmtpd.controller import Controller

from app.mail.smtp_send import build_message, ensure_re_subject, send_message
from app.security import encrypt_secret


class _Handler:
    def __init__(self) -> None:
        self.messages: list[bytes] = []

    async def handle_DATA(self, server, session, envelope):  # noqa: N802, ANN001
        self.messages.append(envelope.content)
        return "250 OK"


@pytest.fixture()
def smtp_server(tmp_path: Path):
    handler = _Handler()
    controller = Controller(handler, hostname="127.0.0.1", port=0)
    controller.start()
    yield controller, handler
    controller.stop()


def test_build_message_headers_and_attachment(tmp_path: Path) -> None:
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    msg = build_message(
        from_name="Ada",
        from_addr="ada@gmail.com",
        to_addr="prof@mit.edu",
        subject="Re: Hello",
        body_text="Hello\n\nWorld",
        attachment_path=pdf,
        attachment_filename="Ada_CV.pdf",
        in_reply_to="<parent@gmail.com>",
        references="<parent@gmail.com>",
        message_id="<child@gmail.com>",
    )
    assert msg["Message-ID"] == "<child@gmail.com>"
    assert msg["In-Reply-To"] == "<parent@gmail.com>"
    assert msg["References"] == "<parent@gmail.com>"
    raw = msg.as_bytes()
    assert b"Ada_CV.pdf" in raw
    assert b"application/pdf" in raw


def test_dry_run_writes_eml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "outbox"
    monkeypatch.setattr("app.mail.smtp_send.DRYRUN_DIR", out)
    msg = build_message(
        from_name="Ada",
        from_addr="ada@gmail.com",
        to_addr="prof@mit.edu",
        subject="Hi",
        body_text="Body",
        message_id="<x@gmail.com>",
    )
    result = send_message(
        account_email="ada@gmail.com",
        enc_app_password=encrypt_secret("abcdefghijklmnop"),
        msg=msg,
        dry_run=True,
    )
    assert result.ok
    assert Path(result.dry_run_path).exists()
    parsed = message_from_bytes(Path(result.dry_run_path).read_bytes())
    assert parsed["To"] == "prof@mit.edu"


def test_ensure_re_once() -> None:
    assert ensure_re_subject("Hello") == "Re: Hello"
    assert ensure_re_subject("Re: Hello") == "Re: Hello"
