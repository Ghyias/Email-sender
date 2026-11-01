"""Unified inbox with filters and reader."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import nh3
from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, func, select, text
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import base_context, flash, redirect, require_csrf
from app.mail import threads as thread_mod
from app.mail.imap_sync import fetch_html_by_uid, gmail_raw_search, mark_flag
from app.models import Account, Contact, Message

router = APIRouter(prefix="/inbox")
templates = Jinja2Templates(directory="app/templates")

PAGE_SIZE = 50


def _parse_date(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def domain_match_sql(pattern: str) -> str:
    """Convert *.edu.cn style pattern to SQL LIKE (matches domain suffix)."""
    p = pattern.strip().lower()
    if p.startswith("*."):
        # *.edu.cn → %@%.edu.cn OR ends with .edu.cn after @
        return f"%.{p[2:]}"
    if p.startswith("@"):
        return f"%{p}"
    if "@" not in p:
        return f"%@{p}"
    return p


def build_inbox_query(
    db: Session,
    *,
    account_ids: list[int] | None = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    sender: str | None = None,
    domain: str | None = None,
    group: str | None = None,
    direction: str | None = None,
    reply_status: str | None = None,
    awaiting_days: int | None = None,
    unread: bool = False,
    has_attachments: bool = False,
    label: str | None = None,
    q: str | None = None,
    sort: str = "date_desc",
) -> Any:
    """Build a filtered SQLAlchemy select for messages."""
    stmt = select(Message)
    conds: list[Any] = []

    if account_ids:
        conds.append(Message.account_id.in_(account_ids))
    if date_from:
        conds.append(Message.date_utc >= date_from)
    if date_to:
        conds.append(Message.date_utc <= date_to)
    if sender:
        conds.append(func.lower(Message.from_addr) == sender.strip().lower())
    if domain:
        like = domain_match_sql(domain)
        conds.append(Message.from_addr.ilike(like))
    if group:
        matched = []
        for c in db.scalars(select(Contact)).all():
            groups = c.groups or []
            if group.lower() in [str(g).lower() for g in groups]:
                matched.append(c.email)
        if matched:
            conds.append(Message.from_addr.in_(matched))
        else:
            conds.append(Message.id == -1)
    if direction == "sent":
        conds.append(Message.is_from_me.is_(True))
    elif direction == "received":
        conds.append(Message.is_from_me.is_(False))
    if unread:
        conds.append(Message.is_unread.is_(True))
    if has_attachments:
        conds.append(Message.has_attachments.is_(True))
    # label filtered in Python after fetch (JSON column portability)
    if q:
        # FTS5
        fts_ids = db.execute(
            text("SELECT rowid FROM messages_fts WHERE messages_fts MATCH :q"),
            {"q": q},
        ).scalars().all()
        if fts_ids:
            conds.append(Message.id.in_(list(fts_ids)))
        else:
            conds.append(Message.id == -1)

    if conds:
        stmt = stmt.where(and_(*conds))

    if sort == "date_asc":
        stmt = stmt.order_by(Message.date_utc.asc())
    elif sort == "sender":
        stmt = stmt.order_by(Message.from_addr.asc(), Message.date_utc.desc())
    else:
        stmt = stmt.order_by(Message.date_utc.desc())

    # reply_status post-filter (needs Python logic)
    return stmt, reply_status, awaiting_days


def sanitize_html(html: str, allow_images: bool = False) -> str:
    """Sanitize HTML with nh3; strip remote images unless allowed."""
    tags = set(nh3.ALLOWED_TAGS)
    attributes = dict(nh3.ALLOWED_ATTRIBUTES)
    if not allow_images:
        tags.discard("img")
    cleaned = nh3.clean(html or "", tags=tags, attributes=attributes)
    if not allow_images:
        # also neutralize remaining remote refs
        cleaned = cleaned.replace("src=", "data-blocked-src=")
    return cleaned


@router.get("", response_class=HTMLResponse)
def inbox(
    request: Request,
    db: Session = Depends(get_db),
    account: list[int] = Query(default=[]),
    date_from: str | None = None,
    date_to: str | None = None,
    preset: str | None = None,
    sender: str | None = None,
    domain: str | None = None,
    group: str | None = None,
    direction: str | None = None,
    reply_status: str | None = None,
    awaiting_days: int | None = None,
    unread: int = 0,
    has_attachments: int = 0,
    label: str | None = None,
    q: str | None = None,
    sort: str = "date_desc",
    page: int = 1,
    msg: int | None = None,
    load_images: int = 0,
    gm_raw: str | None = None,
    gm_account: int | None = None,
) -> HTMLResponse:
    now = datetime.now(timezone.utc)
    df = _parse_date(date_from)
    dt = _parse_date(date_to)
    if preset == "today":
        df = now.replace(hour=0, minute=0, second=0, microsecond=0)
        dt = now
    elif preset == "7d":
        df = now - timedelta(days=7)
        dt = now
    elif preset == "30d":
        df = now - timedelta(days=30)
        dt = now

    stmt, rs, aw = build_inbox_query(
        db,
        account_ids=account or None,
        date_from=df,
        date_to=dt,
        sender=sender,
        domain=domain,
        group=group,
        direction=direction,
        reply_status=reply_status,
        awaiting_days=awaiting_days,
        unread=bool(unread),
        has_attachments=bool(has_attachments),
        label=label,
        q=q,
        sort=sort,
    )

    # advanced X-GM-RAW
    if gm_raw and gm_account:
        acc = db.get(Account, gm_account)
        if acc:
            try:
                uids = gmail_raw_search(acc, gm_raw)
                stmt = stmt.where(Message.account_id == gm_account, Message.uid.in_(uids))
            except Exception as exc:  # noqa: BLE001
                flash(request, f"Gmail search failed: {exc}", "error")

    messages = list(db.scalars(stmt.limit(2000)).all())
    if label:
        lab = label.lower()
        messages = [
            m
            for m in messages
            if any(lab in str(x).lower() for x in (m.labels or []))
        ]
    if rs:
        messages = thread_mod.filter_reply_status(db, messages, rs, awaiting_days)

    total = len(messages)
    page = max(1, page)
    start = (page - 1) * PAGE_SIZE
    page_msgs = messages[start : start + PAGE_SIZE]

    selected = None
    thread_msgs: list[Message] = []
    html_body = ""
    if msg:
        selected = db.get(Message, msg)
        if selected:
            if selected.gm_thrid:
                thread_msgs = list(
                    db.scalars(
                        select(Message)
                        .where(Message.gm_thrid == selected.gm_thrid)
                        .order_by(Message.date_utc.asc())
                    ).all()
                )
            html_src = selected.body_html
            if not html_src:
                # lazy fetch
                acc = db.get(Account, selected.account_id)
                if acc:
                    try:
                        html_src = fetch_html_by_uid(acc, selected.uid)
                        selected.body_html = html_src
                        db.commit()
                    except Exception:  # noqa: BLE001
                        html_src = ""
            if html_src:
                html_body = sanitize_html(html_src, allow_images=bool(load_images))
            elif selected.body_text:
                html_body = sanitize_html(
                    "<pre>" + selected.body_text.replace("<", "&lt;") + "</pre>",
                    allow_images=False,
                )

    accounts = db.scalars(select(Account).order_by(Account.email)).all()
    groups: set[str] = set()
    for c in db.scalars(select(Contact)).all():
        for g in c.groups or []:
            groups.add(str(g))

    query = dict(request.query_params)
    ctx = base_context(
        request,
        db,
        active="inbox",
        messages=page_msgs,
        total=total,
        page=page,
        page_size=PAGE_SIZE,
        pages=max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
        selected=selected,
        thread_msgs=thread_msgs,
        html_body=html_body,
        accounts=accounts,
        groups=sorted(groups),
        filters={
            "account": account,
            "date_from": date_from or "",
            "date_to": date_to or "",
            "preset": preset or "",
            "sender": sender or "",
            "domain": domain or "",
            "group": group or "",
            "direction": direction or "",
            "reply_status": reply_status or "",
            "awaiting_days": awaiting_days or "",
            "unread": unread,
            "has_attachments": has_attachments,
            "label": label or "",
            "q": q or "",
            "sort": sort,
            "gm_raw": gm_raw or "",
            "gm_account": gm_account or "",
            "load_images": load_images,
        },
        query_string=urlencode(query, doseq=True),
    )
    return templates.TemplateResponse(request, "inbox.html", ctx)


@router.post("/{message_id}/read", dependencies=[Depends(require_csrf)])
def mark_read(
    message_id: int,
    request: Request,
    db: Session = Depends(get_db),
    unread: int = Form(0),
) -> HTMLResponse:
    msg = db.get(Message, message_id)
    if msg:
        acc = db.get(Account, msg.account_id)
        try:
            if acc:
                if unread:
                    mark_flag(acc, msg.uid, remove=["\\Seen"])
                    msg.is_unread = True
                else:
                    mark_flag(acc, msg.uid, add=["\\Seen"])
                    msg.is_unread = False
        except Exception as exc:  # noqa: BLE001
            flash(request, f"IMAP flag update failed: {exc}", "error")
    return redirect(f"/inbox?msg={message_id}")


@router.post("/{message_id}/contact", dependencies=[Depends(require_csrf)])
def create_contact_from_sender(
    message_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    msg = db.get(Message, message_id)
    if msg and msg.from_addr:
        existing = db.scalar(select(Contact).where(Contact.email == msg.from_addr))
        if not existing:
            db.add(Contact(email=msg.from_addr, full_name=msg.from_name or ""))
            flash(request, f"Contact {msg.from_addr} created.", "success")
        else:
            flash(request, "Contact already exists.", "info")
    return redirect(f"/inbox?msg={message_id}")
