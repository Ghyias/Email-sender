"""Reply and bounce detection."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.mail.parse import refs_message_ids
from app.models import Contact, Message, SendQueue


def collect_sent_message_ids(session: Session) -> set[str]:
    """Message-IDs from app-sent queue + is_from_me messages."""
    ids: set[str] = set()
    for mid in session.scalars(
        select(SendQueue.smtp_message_id).where(
            SendQueue.smtp_message_id.is_not(None),
            SendQueue.status == "sent",
        )
    ):
        if mid:
            ids.add(mid.strip())
    for mid in session.scalars(
        select(Message.message_id_hdr).where(
            Message.is_from_me.is_(True),
            Message.message_id_hdr.is_not(None),
        )
    ):
        if mid:
            ids.add(mid.strip())
    return ids


def collect_sent_thrids(session: Session) -> set[str]:
    """gm_thrid values for app-sent / from-me messages."""
    thrids: set[str] = set()
    # Threads of from-me messages
    for thrid in session.scalars(
        select(Message.gm_thrid).where(
            Message.is_from_me.is_(True),
            Message.gm_thrid.is_not(None),
        )
    ):
        if thrid:
            thrids.add(str(thrid))
    return thrids


def is_reply_to_outreach(
    message: Message,
    sent_ids: set[str],
    sent_thrids: set[str],
) -> bool:
    """True if message replies to outreach (In-Reply-To/References/thread)."""
    if message.is_from_me:
        return False
    ref_ids = refs_message_ids(message.references_hdr, message.in_reply_to)
    if ref_ids & sent_ids:
        return True
    if message.gm_thrid and str(message.gm_thrid) in sent_thrids:
        # Only count as reply if not from me and thread has our send
        return True
    return False


def is_bounce(message: Message) -> bool:
    """Detect delivery-status bounce messages."""
    from_l = (message.from_addr or "").lower()
    subj = (message.subject or "").lower()
    if "mailer-daemon@googlemail.com" in from_l or from_l.startswith("mailer-daemon@"):
        return True
    if "delivery status notification" in subj:
        return True
    return False


def mark_replies_and_bounces(session: Session) -> dict[str, int]:
    """Scan messages and update contacts for replies/bounces."""
    sent_ids = collect_sent_message_ids(session)
    sent_thrids = collect_sent_thrids(session)
    replied = 0
    bounced = 0
    messages = session.scalars(select(Message).where(Message.is_from_me.is_(False))).all()
    for msg in messages:
        if is_bounce(msg):
            # find referenced sent id in body
            body = msg.body_text or ""
            for mid in sent_ids:
                if mid and mid in body:
                    # cancel is handled by setting bounced on matching contact via to/from
                    pass
            # Heuristic: bounce often references original recipient in body
            for contact in session.scalars(select(Contact)).all():
                if contact.email and contact.email in body:
                    if not contact.bounced:
                        contact.bounced = True
                        bounced += 1
            continue
        if is_reply_to_outreach(msg, sent_ids, sent_thrids):
            contact = session.scalar(select(Contact).where(Contact.email == msg.from_addr))
            if contact:
                contact.last_reply_utc = msg.date_utc or datetime.now(timezone.utc)
                replied += 1
    return {"replied": replied, "bounced": bounced}


def awaiting_reply_contact_ids(
    session: Session,
    min_days: int | None = None,
) -> set[int]:
    """Contacts we emailed who have not replied (and not bounced)."""
    from app.models import Draft

    sent_drafts = session.scalars(
        select(Draft).where(Draft.status == "sent", Draft.is_followup.is_(False))
    ).all()
    result: set[int] = set()
    now = datetime.now(timezone.utc)
    for d in sent_drafts:
        contact = session.get(Contact, d.contact_id)
        if not contact or contact.bounced or contact.last_reply_utc:
            continue
        if min_days is not None and d.updated_at:
            age = (now - d.updated_at.replace(tzinfo=timezone.utc)).days
            if age < min_days:
                continue
            result.add(contact.id)
        else:
            result.add(contact.id)
    return result


def filter_reply_status(
    session: Session,
    messages: Iterable[Message],
    status: str,
    awaiting_days: int | None = None,
) -> list[Message]:
    """Filter messages by reply-status key."""
    sent_ids = collect_sent_message_ids(session)
    sent_thrids = collect_sent_thrids(session)
    out: list[Message] = []
    awaiting = awaiting_reply_contact_ids(session, awaiting_days) if status.startswith("awaiting") else set()
    contact_emails = {
        c.id: c.email
        for c in session.scalars(select(Contact)).all()
    }
    email_to_id = {v: k for k, v in contact_emails.items()}
    for msg in messages:
        if status == "replies":
            if is_reply_to_outreach(msg, sent_ids, sent_thrids):
                out.append(msg)
        elif status == "awaiting":
            to_emails = [a.get("email", "") for a in (msg.to_addrs or [])]
            if msg.is_from_me and any(email_to_id.get(e) in awaiting for e in to_emails):
                out.append(msg)
        elif status == "awaiting_days":
            to_emails = [a.get("email", "") for a in (msg.to_addrs or [])]
            if msg.is_from_me and any(email_to_id.get(e) in awaiting for e in to_emails):
                out.append(msg)
        else:
            out.append(msg)
    return out
