"""MIME parsing fixtures."""

from __future__ import annotations

from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.mail.parse import parse_raw_email, refs_message_ids


def _build_chinese_msg() -> bytes:
    msg = MIMEMultipart("alternative")
    msg["From"] = Header("张三", "utf-8").encode() + " <zhang@example.edu.cn>"
    msg["To"] = "me@gmail.com"
    msg["Subject"] = Header("关于博士申请的回复", "utf-8")
    msg["Message-ID"] = "<abc123@mail.gmail.com>"
    msg["In-Reply-To"] = "<orig@mail.gmail.com>"
    msg["References"] = "<orig@mail.gmail.com> <mid2@mail.gmail.com>"
    msg.attach(MIMEText("您好，感谢来信。", "plain", "utf-8"))
    msg.attach(MIMEText("<p>您好，感谢来信。</p>", "html", "utf-8"))
    return msg.as_bytes()


def test_parse_chinese_subject_and_addresses() -> None:
    parsed = parse_raw_email(_build_chinese_msg())
    assert "博士" in parsed.subject
    assert parsed.from_addr == "zhang@example.edu.cn"
    assert parsed.message_id == "<abc123@mail.gmail.com>"
    assert "您好" in parsed.body_text
    assert parsed.body_html


def test_multipart_prefers_text() -> None:
    parsed = parse_raw_email(_build_chinese_msg())
    assert parsed.snippet


def test_refs_message_ids() -> None:
    ids = refs_message_ids("<a@x> <b@y>", "<c@z>")
    assert "<a@x>" in ids
    assert "<c@z>" in ids
