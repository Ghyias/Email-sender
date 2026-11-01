"""Reply detection tests."""

from __future__ import annotations

from app.mail.threads import is_bounce, is_reply_to_outreach
from app.models import Message


def test_is_reply_via_in_reply_to() -> None:
    msg = Message(
        account_id=1,
        gm_msgid="1",
        uid=1,
        from_addr="prof@edu",
        in_reply_to="<sent@mail.gmail.com>",
        references_hdr="",
        is_from_me=False,
    )
    assert is_reply_to_outreach(msg, {"<sent@mail.gmail.com>"}, set())


def test_is_reply_via_thrid() -> None:
    msg = Message(
        account_id=1,
        gm_msgid="2",
        uid=2,
        from_addr="prof@edu",
        gm_thrid="555",
        is_from_me=False,
    )
    assert is_reply_to_outreach(msg, set(), {"555"})


def test_bounce_detection() -> None:
    msg = Message(
        account_id=1,
        gm_msgid="3",
        uid=3,
        from_addr="mailer-daemon@googlemail.com",
        subject="Delivery Status Notification (Failure)",
        is_from_me=False,
    )
    assert is_bounce(msg)
