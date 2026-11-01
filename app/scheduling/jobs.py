"""APScheduler send/sync jobs, misfire handling, follow-ups."""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone
from email.utils import make_msgid
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_setting
from app.mail.smtp_send import build_message, ensure_re_subject, send_message
from app.models import Account, Contact, CvVariant, Draft, SendLog, SendQueue
from app.scheduling.limits import can_send_now, next_available_slot

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
_session_factory: Callable[[], Session] | None = None


def get_scheduler() -> BackgroundScheduler | None:
    return _scheduler


def start_scheduler(engine_url: str, session_factory: Callable[[], Session]) -> BackgroundScheduler:
    """Boot APScheduler with SQLAlchemy job store on the same SQLite DB."""
    global _scheduler, _session_factory
    _session_factory = session_factory
    if _scheduler and _scheduler.running:
        return _scheduler
    jobstores = {"default": SQLAlchemyJobStore(url=engine_url)}
    sched = BackgroundScheduler(jobstores=jobstores, timezone="UTC")
    sched.start(paused=False)
    _scheduler = sched
    # Process due queue items every 30s
    sched.add_job(
        process_send_queue,
        "interval",
        seconds=30,
        id="process_send_queue",
        replace_existing=True,
        max_instances=1,
    )
    handle_misfires(session_factory())
    return sched


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def _weekday(dt: datetime) -> bool:
    return dt.weekday() < 5


def next_recipient_local_window(
    tz_name: str,
    start: str = "09:00",
    end: str = "11:30",
    after: Optional[datetime] = None,
) -> datetime:
    """Next weekday random time within local window, returned as UTC."""
    after = after or datetime.now(timezone.utc)
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
    local = after.astimezone(tz)
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    candidate = local.replace(hour=sh, minute=sm, second=0, microsecond=0)
    if local > candidate.replace(hour=eh, minute=em):
        candidate = candidate + timedelta(days=1)
        candidate = candidate.replace(hour=sh, minute=sm)
    while not _weekday(candidate):
        candidate += timedelta(days=1)
    window_seconds = (eh * 60 + em) - (sh * 60 + sm)
    window_seconds = max(window_seconds, 1)
    jitter = random.randint(0, window_seconds * 60)
    chosen = candidate + timedelta(seconds=jitter)
    # ensure still in window / weekday
    while not _weekday(chosen) or chosen.hour > eh or (chosen.hour == eh and chosen.minute > em):
        candidate += timedelta(days=1)
        while not _weekday(candidate):
            candidate += timedelta(days=1)
        candidate = candidate.replace(hour=sh, minute=sm, second=0, microsecond=0)
        jitter = random.randint(0, window_seconds * 60)
        chosen = candidate + timedelta(seconds=jitter)
    return chosen.astimezone(timezone.utc)


def schedule_drafts(
    session: Session,
    draft_ids: list[int],
    mode: str,
    *,
    when_utc: Optional[datetime] = None,
    window: Optional[dict[str, Any]] = None,
) -> list[SendQueue]:
    """Enqueue approved drafts into send_queue under a scheduling mode."""
    items: list[SendQueue] = []
    # group by account for delay spreading
    per_account_cursor: dict[int, datetime] = {}
    now = datetime.now(timezone.utc)
    for did in draft_ids:
        draft = session.get(Draft, did)
        if not draft or draft.status not in ("approved", "queued"):
            continue
        if not draft.account_id:
            continue
        account = session.get(Account, draft.account_id)
        if not account:
            continue
        contact = session.get(Contact, draft.contact_id)
        if mode == "now":
            cursor = per_account_cursor.get(account.id, now)
            delay = random.randint(account.min_delay_s or 90, account.max_delay_s or 240)
            scheduled = cursor if cursor == now else cursor + timedelta(seconds=delay)
            if cursor == now:
                # first one soon, subsequent staggered
                per_account_cursor[account.id] = now + timedelta(seconds=delay)
            else:
                per_account_cursor[account.id] = scheduled + timedelta(
                    seconds=random.randint(account.min_delay_s or 90, account.max_delay_s or 240)
                )
        elif mode == "at":
            scheduled = when_utc or now
        elif mode == "recipient_local":
            win = window or {"start": "09:00", "end": "11:30"}
            tz = (contact.timezone if contact else None) or "UTC"
            scheduled = next_recipient_local_window(tz, win.get("start", "09:00"), win.get("end", "11:30"))
        else:
            scheduled = now
        mid = make_msgid(domain="gmail.com")
        item = SendQueue(
            draft_id=draft.id,
            account_id=account.id,
            scheduled_utc=scheduled,
            status="pending",
            smtp_message_id=mid,
        )
        draft.status = "queued"
        session.add(item)
        items.append(item)
    session.flush()
    return items


def handle_misfires(session: Session) -> int:
    """On startup: overdue < 2h keep pending; else mark missed."""
    now = datetime.now(timezone.utc)
    missed = 0
    rows = session.scalars(
        select(SendQueue).where(SendQueue.status == "pending")
    ).all()
    for row in rows:
        sched = row.scheduled_utc
        if sched.tzinfo is None:
            sched = sched.replace(tzinfo=timezone.utc)
        if sched > now:
            continue
        overdue = (now - sched).total_seconds()
        if overdue >= 2 * 3600:
            row.status = "missed"
            missed += 1
    session.commit()
    return missed


def process_send_queue() -> None:
    """Dequeue due pending items, enforce caps, send or dry-run."""
    if _session_factory is None:
        return
    session = _session_factory()
    try:
        if get_setting(session, "pause_all_sending", False):
            return
        dry_run = bool(get_setting(session, "dry_run", True))
        now = datetime.now(timezone.utc)
        rows = session.scalars(
            select(SendQueue)
            .where(SendQueue.status == "pending", SendQueue.scheduled_utc <= now)
            .order_by(SendQueue.scheduled_utc.asc())
            .limit(20)
        ).all()
        for row in rows:
            account = session.get(Account, row.account_id)
            draft = session.get(Draft, row.draft_id)
            if not account or not draft:
                row.status = "failed"
                row.last_error = "missing account/draft"
                continue
            # campaign pause
            if draft.campaign_id:
                from app.models import Campaign

                camp = session.get(Campaign, draft.campaign_id)
                if camp and camp.paused:
                    continue
            ok, reason = can_send_now(session, account, now=now)
            if not ok:
                row.scheduled_utc = next_available_slot(session, account, now=now)
                row.last_error = f"deferred ({reason})"
                session.commit()
                continue
            contact = session.get(Contact, draft.contact_id)
            if not contact:
                row.status = "failed"
                row.last_error = "missing contact"
                continue
            cv_path = None
            cv_name = None
            if draft.cv_variant_id:
                cv = session.get(CvVariant, draft.cv_variant_id)
                if cv:
                    cv_path = Path(cv.file_path)
                    pattern = get_setting(session, "attachment_filename_pattern", "{name}_CV.pdf")
                    name = (get_setting(session, "user_display_name", None)
                            or (session.scalar(select(Account.display_name).limit(1)) or "CV"))
                    # prefer profile name
                    from app.models import UserProfile

                    profile = session.scalar(select(UserProfile).limit(1))
                    if profile and profile.full_name:
                        name = profile.full_name.replace(" ", "_")
                    cv_name = pattern.format(name=name, key=cv.key)
            body = draft.body_text
            subject = draft.subject
            in_reply_to = None
            references = None
            if draft.is_followup and draft.parent_draft_id:
                parent_q = session.scalar(
                    select(SendQueue).where(
                        SendQueue.draft_id == draft.parent_draft_id,
                        SendQueue.status == "sent",
                    )
                )
                if parent_q and parent_q.smtp_message_id:
                    in_reply_to = parent_q.smtp_message_id
                    references = parent_q.smtp_message_id
                subject = ensure_re_subject(subject)

            mid = row.smtp_message_id or make_msgid(domain="gmail.com")
            row.smtp_message_id = mid
            row.status = "sending"
            row.attempts += 1
            session.commit()

            try:
                msg = build_message(
                    from_name=account.display_name or account.email,
                    from_addr=account.email,
                    to_addr=contact.email,
                    subject=subject,
                    body_text=body,
                    signature_html=account.signature_html or "",
                    attachment_path=cv_path if cv_path and cv_path.exists() else None,
                    attachment_filename=cv_name,
                    in_reply_to=in_reply_to,
                    references=references,
                    message_id=mid,
                )
                result = send_message(
                    account_email=account.email,
                    enc_app_password=account.enc_app_password,
                    msg=msg,
                    dry_run=dry_run,
                )
            except Exception as exc:  # noqa: BLE001
                result = type("R", (), {"ok": False, "error": str(exc), "error_class": "permanent", "message_id": mid})()

            if result.ok:
                row.status = "sent"
                row.sent_utc = datetime.now(timezone.utc)
                row.last_error = ""
                draft.status = "sent"
                draft.updated_at = datetime.now(timezone.utc)
                session.add(
                    SendLog(
                        account_id=account.id,
                        to_addr=contact.email,
                        subject=subject,
                        draft_id=draft.id,
                        result="sent",
                        error="",
                    )
                )
            else:
                if result.error_class == "auth":
                    account.status = "auth_failed"
                    account.status_msg = (
                        "Wrong or revoked app password. Check 2-Step Verification "
                        "is on and generate a new one."
                    )
                    row.status = "failed"
                    row.last_error = result.error
                elif result.error_class == "daily_limit":
                    account.status = "error"
                    account.status_msg = "Daily SMTP limit hit; paused 24h"
                    row.scheduled_utc = datetime.now(timezone.utc) + timedelta(hours=24)
                    row.status = "pending"
                    row.last_error = "deferred (daily_limit)"
                elif result.error_class == "transient" and row.attempts < 3:
                    row.status = "pending"
                    row.scheduled_utc = datetime.now(timezone.utc) + timedelta(minutes=2**row.attempts)
                    row.last_error = result.error
                else:
                    row.status = "failed"
                    row.last_error = result.error
                    draft.status = "failed"
                session.add(
                    SendLog(
                        account_id=account.id,
                        to_addr=contact.email,
                        subject=subject,
                        draft_id=draft.id,
                        result="failed",
                        error=result.error,
                    )
                )
            session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("process_send_queue failed")
        session.rollback()
    finally:
        session.close()


def create_followup_drafts(session: Session) -> int:
    """Create follow-up drafts for campaigns with followup_days and no reply."""
    from app.models import Campaign

    created = 0
    camps = session.scalars(
        select(Campaign).where(Campaign.followup_days.is_not(None), Campaign.paused.is_(False))
    ).all()
    now = datetime.now(timezone.utc)
    for camp in camps:
        days = camp.followup_days or 0
        drafts = session.scalars(
            select(Draft).where(
                Draft.campaign_id == camp.id,
                Draft.status == "sent",
                Draft.is_followup.is_(False),
            )
        ).all()
        for d in drafts:
            contact = session.get(Contact, d.contact_id)
            if not contact or contact.bounced or contact.last_reply_utc or contact.do_not_contact:
                continue
            existing = session.scalar(
                select(Draft).where(
                    Draft.parent_draft_id == d.id,
                    Draft.is_followup.is_(True),
                )
            )
            if existing:
                continue
            if d.updated_at:
                sent_at = d.updated_at if d.updated_at.tzinfo else d.updated_at.replace(tzinfo=timezone.utc)
                if (now - sent_at).days < days:
                    continue
            follow = Draft(
                campaign_id=camp.id,
                contact_id=d.contact_id,
                account_id=d.account_id,
                subject=ensure_re_subject(d.subject),
                body_text=(
                    f"Dear {contact.full_name or 'Professor'},\n\n"
                    "I wanted to follow up on my previous email. "
                    "Please let me know if you need any further information.\n\n"
                    "Kind regards"
                ),
                cv_variant_id=d.cv_variant_id,
                status="draft",
                is_followup=True,
                parent_draft_id=d.id,
            )
            session.add(follow)
            created += 1
    session.commit()
    return created
