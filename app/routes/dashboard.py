"""Dashboard route."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db, get_setting
from app.deps import base_context
from app.llm.ollama_client import get_ollama_client
from app.models import Account, Campaign, Contact, Draft, Message
from app.scheduling.limits import sent_count_24h

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    accounts = db.scalars(select(Account).order_by(Account.email)).all()
    account_rows = []
    for a in accounts:
        synced = db.scalar(
            select(func.count()).select_from(Message).where(Message.account_id == a.id)
        )
        account_rows.append(
            {
                "account": a,
                "synced": synced or 0,
                "sent_today": sent_count_24h(db, a.id),
            }
        )
    campaigns = db.scalars(select(Campaign).order_by(Campaign.created_at.desc()).limit(20)).all()
    funnel = []
    for c in campaigns:
        drafts = db.scalars(select(Draft).where(Draft.campaign_id == c.id)).all()
        counts = {
            "drafted": sum(1 for d in drafts if d.status == "draft"),
            "approved": sum(1 for d in drafts if d.status == "approved"),
            "sent": sum(1 for d in drafts if d.status == "sent"),
            "failed": sum(1 for d in drafts if d.status == "failed"),
        }
        replied = 0
        bounced = 0
        for d in drafts:
            contact = db.get(Contact, d.contact_id)
            if contact and contact.last_reply_utc:
                replied += 1
            if contact and contact.bounced:
                bounced += 1
        funnel.append({"campaign": c, **counts, "replied": replied, "bounced": bounced})

    ollama = get_ollama_client(get_setting(db, "ollama_url"))
    health = ollama.health(
        [
            str(get_setting(db, "drafting_model", "qwen3.5:4b")),
            str(get_setting(db, "embedding_model", "qwen3-embedding:0.6b")),
        ]
    )
    ctx = base_context(
        request,
        db,
        active="dashboard",
        title="Dashboard",
        account_rows=account_rows,
        funnel=funnel,
        health=health,
    )
    return templates.TemplateResponse(request, "dashboard.html", ctx)
