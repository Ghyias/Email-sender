"""Settings, export, wipe, pause-all."""

from __future__ import annotations

import io
import shutil
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from app.config import BACKUP_DIR, DATA_DIR, DB_PATH, HOST, ensure_data_dirs
from app.db import DEFAULT_SETTINGS, get_db, get_setting, set_setting
from app.deps import base_context, flash, redirect, require_csrf
from app.models import Contact, Draft, Message, SendLog

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    values = {k: get_setting(db, k, v) for k, v in DEFAULT_SETTINGS.items()}
    ctx = base_context(
        request,
        db,
        active="settings",
        values=values,
        host=HOST,
        data_dir=str(DATA_DIR),
    )
    return templates.TemplateResponse(request, "settings.html", ctx)


@router.post("/settings", dependencies=[Depends(require_csrf)])
async def save_settings(
    request: Request,
    db: Session = Depends(get_db),
    timezone_name: str = Form("Asia/Shanghai"),
    sync_interval_minutes: int = Form(5),
    sync_window_days: int = Form(365),
    ollama_url: str = Form("http://127.0.0.1:11434"),
    drafting_model: str = Form("qwen3.5:4b"),
    embedding_model: str = Form("qwen3-embedding:0.6b"),
    temperature: float = Form(0.4),
    default_daily_cap: int = Form(30),
    default_min_delay_s: int = Form(90),
    default_max_delay_s: int = Form(240),
    attachment_filename_pattern: str = Form("{name}_CV.pdf"),
) -> HTMLResponse:
    form = await request.form()
    dry_vals = form.getlist("dry_run")
    dry_on = any(str(v) == "1" for v in dry_vals)
    set_setting(db, "timezone", timezone_name)
    set_setting(db, "sync_interval_minutes", sync_interval_minutes)
    set_setting(db, "sync_window_days", sync_window_days)
    set_setting(db, "ollama_url", ollama_url)
    set_setting(db, "drafting_model", drafting_model)
    set_setting(db, "embedding_model", embedding_model)
    set_setting(db, "temperature", temperature)
    set_setting(db, "dry_run", dry_on)
    set_setting(db, "default_daily_cap", default_daily_cap)
    set_setting(db, "default_min_delay_s", default_min_delay_s)
    set_setting(db, "default_max_delay_s", default_max_delay_s)
    set_setting(db, "attachment_filename_pattern", attachment_filename_pattern)
    db.commit()
    flash(request, "Settings saved.", "success")
    return redirect("/settings")


@router.post("/settings/pause-all", dependencies=[Depends(require_csrf)])
def pause_all(
    request: Request,
    db: Session = Depends(get_db),
    pause: int = Form(1),
) -> HTMLResponse:
    set_setting(db, "pause_all_sending", bool(int(pause)))
    db.commit()
    flash(request, "All sending paused." if pause else "Sending resumed.", "success")
    return redirect(request.headers.get("referer") or "/settings")


@router.post("/settings/wipe-mail", dependencies=[Depends(require_csrf)])
def wipe_mail(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    db.execute(delete(Message))
    # rebuild FTS
    try:
        db.execute(text("DELETE FROM messages_fts"))
    except Exception:  # noqa: BLE001
        pass
    db.commit()
    flash(request, "Local mail cache wiped.", "success")
    return redirect("/settings")


@router.get("/settings/export.xlsx")
def export_xlsx(db: Session = Depends(get_db)) -> Response:
    import pandas as pd

    contacts = [
        {
            "email": c.email,
            "full_name": c.full_name,
            "institution": c.institution,
            "groups": ",".join(c.groups or []),
            "do_not_contact": c.do_not_contact,
            "bounced": c.bounced,
        }
        for c in db.scalars(select(Contact)).all()
    ]
    drafts = [
        {
            "id": d.id,
            "contact_id": d.contact_id,
            "subject": d.subject,
            "status": d.status,
            "flags": ",".join(d.validation_flags or []),
        }
        for d in db.scalars(select(Draft)).all()
    ]
    logs = [
        {
            "ts": str(row.ts_utc),
            "account_id": row.account_id,
            "to": row.to_addr,
            "subject": row.subject,
            "result": row.result,
            "error": row.error,
        }
        for row in db.scalars(select(SendLog)).all()
    ]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame(contacts).to_excel(writer, sheet_name="contacts", index=False)
        pd.DataFrame(drafts).to_excel(writer, sheet_name="drafts", index=False)
        pd.DataFrame(logs).to_excel(writer, sheet_name="send_log", index=False)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=mailpilot_export.xlsx"},
    )


def backup_db_on_startup() -> None:
    """Copy DB to backups/, keep last 5."""
    ensure_data_dirs()
    if not DB_PATH.exists():
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = BACKUP_DIR / f"mailpilot-{stamp}.db"
    shutil.copy2(DB_PATH, dest)
    backups = sorted(BACKUP_DIR.glob("mailpilot-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[5:]:
        try:
            old.unlink()
        except OSError:
            pass
