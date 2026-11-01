"""Send queue + composer."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import make_msgid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db, get_setting
from app.deps import base_context, flash, redirect, require_csrf
from app.mail.smtp_send import build_message, send_message
from app.models import Account, Contact, CvVariant, Draft, SendLog, SendQueue
from app.scheduling.jobs import process_send_queue, schedule_drafts

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/queue", response_class=HTMLResponse)
def queue_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    rows = db.scalars(select(SendQueue).order_by(SendQueue.scheduled_utc.asc()).limit(200)).all()
    enriched = []
    for r in rows:
        draft = db.get(Draft, r.draft_id)
        contact = db.get(Contact, draft.contact_id) if draft else None
        account = db.get(Account, r.account_id)
        enriched.append({"item": r, "draft": draft, "contact": contact, "account": account})
    ctx = base_context(request, db, active="queue", rows=enriched)
    return templates.TemplateResponse(request, "queue.html", ctx)


@router.post("/queue/schedule", dependencies=[Depends(require_csrf)])
def schedule_approved(
    request: Request,
    db: Session = Depends(get_db),
    draft_ids: str = Form(""),
    mode: str = Form("now"),
    when_utc: str = Form(""),
    window_start: str = Form("09:00"),
    window_end: str = Form("11:30"),
    confirm: int = Form(0),
) -> HTMLResponse:
    ids = [int(x) for x in draft_ids.split(",") if x.strip().isdigit()]
    if not confirm:
        # show confirmation counts
        by_account: dict[str, int] = {}
        for did in ids:
            d = db.get(Draft, did)
            if not d or not d.account_id:
                continue
            acc = db.get(Account, d.account_id)
            key = acc.email if acc else str(d.account_id)
            by_account[key] = by_account.get(key, 0) + 1
        ctx = base_context(
            request,
            db,
            active="queue",
            confirm=True,
            draft_ids=draft_ids,
            mode=mode,
            when_utc=when_utc,
            window_start=window_start,
            window_end=window_end,
            by_account=by_account,
            total=len(ids),
        )
        return templates.TemplateResponse(request, "queue_confirm.html", ctx)
    when = None
    if when_utc:
        try:
            when = datetime.fromisoformat(when_utc)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except ValueError:
            when = None
    items = schedule_drafts(
        db,
        ids,
        mode,
        when_utc=when,
        window={"start": window_start, "end": window_end},
    )
    db.commit()
    flash(request, f"Scheduled {len(items)} messages.", "success")
    return redirect("/queue")


@router.post("/queue/process", dependencies=[Depends(require_csrf)])
def process_now(request: Request) -> HTMLResponse:
    process_send_queue()
    flash(request, "Queue processed.", "success")
    return redirect("/queue")


@router.post("/queue/{item_id}/cancel", dependencies=[Depends(require_csrf)])
def cancel_item(item_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    item = db.get(SendQueue, item_id)
    if item and item.status in ("pending", "missed"):
        item.status = "cancelled"
        draft = db.get(Draft, item.draft_id)
        if draft and draft.status == "queued":
            draft.status = "approved"
        flash(request, "Cancelled.", "success")
    return redirect("/queue")


@router.get("/compose", response_class=HTMLResponse)
def compose_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    ctx = base_context(
        request,
        db,
        active="queue",
        accounts=db.scalars(select(Account).where(Account.active.is_(True))).all(),
        cvs=db.scalars(select(CvVariant)).all(),
    )
    return templates.TemplateResponse(request, "compose.html", ctx)


@router.post("/compose", dependencies=[Depends(require_csrf)])
def compose_send(
    request: Request,
    db: Session = Depends(get_db),
    account_id: int = Form(...),
    to_addr: str = Form(...),
    subject: str = Form(...),
    body: str = Form(...),
    cv_id: int = Form(0),
    send_mode: str = Form("now"),
) -> HTMLResponse:
    account = db.get(Account, account_id)
    if not account:
        flash(request, "Account not found.", "error")
        return redirect("/compose")
    dry_run = bool(get_setting(db, "dry_run", True))
    if get_setting(db, "pause_all_sending", False):
        flash(request, "Sending is paused.", "error")
        return redirect("/compose")
    cv_path = None
    cv_name = None
    if cv_id:
        cv = db.get(CvVariant, cv_id)
        if cv:
            cv_path = Path(cv.file_path)
            pattern = get_setting(db, "attachment_filename_pattern", "{name}_CV.pdf")
            cv_name = pattern.format(name=(account.display_name or "CV").replace(" ", "_"), key=cv.key)
    mid = make_msgid(domain="gmail.com")
    msg = build_message(
        from_name=account.display_name or account.email,
        from_addr=account.email,
        to_addr=to_addr.strip(),
        subject=subject,
        body_text=body,
        signature_html=account.signature_html or "",
        attachment_path=cv_path if cv_path and cv_path.exists() else None,
        attachment_filename=cv_name,
        message_id=mid,
    )
    result = send_message(
        account_email=account.email,
        enc_app_password=account.enc_app_password,
        msg=msg,
        dry_run=dry_run,
    )
    db.add(
        SendLog(
            account_id=account.id,
            to_addr=to_addr.strip(),
            subject=subject,
            result="sent" if result.ok else "failed",
            error=result.error,
        )
    )
    db.commit()
    if result.ok:
        flash(
            request,
            f"Sent (dry-run → {result.dry_run_path})." if dry_run else "Sent.",
            "success",
        )
    else:
        flash(request, f"Send failed: {result.error}", "error")
    return redirect("/queue")
