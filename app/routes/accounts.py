"""Accounts management routes."""

from __future__ import annotations

import threading

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import HARD_DAILY_CAP, HARD_MIN_DELAY_S, MAX_ACCOUNTS
from app.db import SessionLocal, get_db, get_setting
from app.deps import base_context, flash, redirect, require_csrf
from app.mail.imap_sync import (
    get_sync_status,
    start_account_worker,
    sync_account,
    test_imap_login,
    test_smtp_login,
)
from app.models import Account, Message
from app.security import encrypt_secret, normalize_app_password

router = APIRouter(prefix="/accounts")
templates = Jinja2Templates(directory="app/templates")


@router.get("", response_class=HTMLResponse)
def list_accounts(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    from app.models import CvVariant
    from app.scheduling.limits import remaining_capacity

    accounts = db.scalars(select(Account).order_by(Account.email)).all()
    counts = {
        a.id: db.scalar(select(func.count()).select_from(Message).where(Message.account_id == a.id))
        or 0
        for a in accounts
    }
    remaining = {a.id: remaining_capacity(db, a) for a in accounts}
    cvs = db.scalars(select(CvVariant).order_by(CvVariant.key)).all()
    ctx = base_context(
        request,
        db,
        active="accounts",
        accounts=accounts,
        counts=counts,
        remaining=remaining,
        cvs=cvs,
        max_accounts=MAX_ACCOUNTS,
        statuses=get_sync_status(),
    )
    return templates.TemplateResponse(request, "accounts.html", ctx)


@router.post("/add", dependencies=[Depends(require_csrf)])
def add_account(
    request: Request,
    db: Session = Depends(get_db),
    email: str = Form(...),
    display_name: str = Form(""),
    app_password: str = Form(...),
    daily_cap: int = Form(30),
    owner_key: str = Form(""),
) -> HTMLResponse:
    count = db.scalar(select(func.count()).select_from(Account)) or 0
    if count >= MAX_ACCOUNTS:
        flash(request, f"Maximum of {MAX_ACCOUNTS} accounts reached.", "error")
        return redirect("/accounts")
    email_n = email.strip().lower()
    if db.scalar(select(Account).where(Account.email == email_n)):
        flash(request, "Account already exists.", "error")
        return redirect("/accounts")
    pw = normalize_app_password(app_password)
    ok_imap, err_imap = test_imap_login(email_n, pw)
    if not ok_imap:
        flash(request, err_imap, "error")
        return redirect("/accounts")
    ok_smtp, err_smtp = test_smtp_login(email_n, pw)
    if not ok_smtp:
        flash(request, err_smtp, "error")
        return redirect("/accounts")
    cap = max(1, min(int(daily_cap), HARD_DAILY_CAP))
    acc = Account(
        email=email_n,
        display_name=display_name.strip() or email_n,
        enc_app_password=encrypt_secret(pw),
        daily_cap=cap,
        active=True,
        status="ok",
        owner_key=owner_key.strip().lower(),
    )
    db.add(acc)
    db.commit()
    db.refresh(acc)
    flash(request, f"Account {email_n} added. Sync started.", "success")

    def _factory() -> Session:
        assert SessionLocal is not None
        return SessionLocal()

    interval = int(get_setting(db, "sync_interval_minutes", 5) or 5)
    start_account_worker(_factory, acc.id, interval_minutes=interval)
    threading.Thread(
        target=sync_account, args=(_factory, acc.id), daemon=True, name=f"sync-once-{acc.id}"
    ).start()
    return redirect("/accounts")


@router.post("/{account_id}/toggle", dependencies=[Depends(require_csrf)])
def toggle_account(
    account_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    acc = db.get(Account, account_id)
    if acc:
        acc.active = not acc.active
        flash(request, f"{acc.email} {'activated' if acc.active else 'paused'}.", "success")
    return redirect("/accounts")


@router.post("/{account_id}/update", dependencies=[Depends(require_csrf)])
def update_account(
    account_id: int,
    request: Request,
    db: Session = Depends(get_db),
    daily_cap: int = Form(30),
    min_delay_s: int = Form(90),
    max_delay_s: int = Form(240),
    display_name: str = Form(""),
    owner_key: str = Form(""),
    default_cv_variant_id: int = Form(0),
) -> HTMLResponse:
    acc = db.get(Account, account_id)
    if acc:
        acc.daily_cap = max(1, min(int(daily_cap), HARD_DAILY_CAP))
        acc.min_delay_s = max(HARD_MIN_DELAY_S, int(min_delay_s))
        acc.max_delay_s = max(acc.min_delay_s, int(max_delay_s))
        if display_name.strip():
            acc.display_name = display_name.strip()
        acc.owner_key = owner_key.strip().lower()
        acc.default_cv_variant_id = default_cv_variant_id or None
        flash(request, "Account updated.", "success")
    return redirect("/accounts")


@router.post("/{account_id}/password", dependencies=[Depends(require_csrf)])
def reenter_password(
    account_id: int,
    request: Request,
    db: Session = Depends(get_db),
    app_password: str = Form(...),
) -> HTMLResponse:
    acc = db.get(Account, account_id)
    if not acc:
        return redirect("/accounts")
    pw = normalize_app_password(app_password)
    ok_imap, err = test_imap_login(acc.email, pw)
    if not ok_imap:
        flash(request, err, "error")
        return redirect("/accounts")
    ok_smtp, err = test_smtp_login(acc.email, pw)
    if not ok_smtp:
        flash(request, err, "error")
        return redirect("/accounts")
    acc.enc_app_password = encrypt_secret(pw)
    acc.status = "ok"
    acc.status_msg = ""
    flash(request, "App password updated.", "success")
    return redirect("/accounts")


@router.post("/{account_id}/sync", dependencies=[Depends(require_csrf)])
def trigger_sync(
    account_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    acc = db.get(Account, account_id)
    if not acc:
        return redirect("/accounts")

    def _factory() -> Session:
        assert SessionLocal is not None
        return SessionLocal()

    threading.Thread(
        target=sync_account, args=(_factory, account_id), daemon=True, name=f"sync-once-{account_id}"
    ).start()
    flash(request, "Sync started.", "success")
    return redirect("/accounts")


@router.get("/sync/status", response_class=HTMLResponse)
def sync_status_partial(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    statuses = get_sync_status()
    accounts = db.scalars(select(Account)).all()
    return templates.TemplateResponse(
        request,
        "partials/sync_status.html",
        {"request": request, "statuses": statuses, "accounts": accounts},
    )
