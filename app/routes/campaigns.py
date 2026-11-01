"""Campaign wizard routes."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import base_context, flash, redirect, require_csrf
from app.models import Account, Campaign, Contact, Draft, Template
from app.scheduling.limits import assign_accounts_round_robin

router = APIRouter(prefix="/campaigns")
templates = Jinja2Templates(directory="app/templates")


def _filter_contacts(db: Session, filt: dict) -> list[Contact]:
    contacts = list(db.scalars(select(Contact)).all())
    out = []
    group = (filt.get("group") or "").lower()
    source_file = filt.get("source_file") or ""
    for c in contacts:
        if c.do_not_contact or c.bounced or c.last_reply_utc:
            continue
        # already contacted: has a sent draft
        sent = db.scalar(
            select(Draft).where(Draft.contact_id == c.id, Draft.status == "sent").limit(1)
        )
        if sent and filt.get("exclude_contacted", True):
            continue
        if group and group not in [str(g).lower() for g in (c.groups or [])]:
            continue
        if source_file and source_file not in (c.source_file or ""):
            continue
        out.append(c)
    return out


@router.get("", response_class=HTMLResponse)
def list_campaigns(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    camps = db.scalars(select(Campaign).order_by(Campaign.created_at.desc())).all()
    ctx = base_context(request, db, active="campaigns", campaigns=camps)
    return templates.TemplateResponse(request, "campaigns.html", ctx)


@router.get("/new", response_class=HTMLResponse)
def new_campaign(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    ctx = base_context(
        request,
        db,
        active="campaigns",
        tpls=db.scalars(select(Template)).all(),
        accounts=db.scalars(select(Account).where(Account.active.is_(True))).all(),
        groups=sorted(
            {
                str(g)
                for c in db.scalars(select(Contact)).all()
                for g in (c.groups or [])
            }
        ),
    )
    return templates.TemplateResponse(request, "campaign_new.html", ctx)


@router.post("/create", dependencies=[Depends(require_csrf)])
def create_campaign(
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    template_id: int = Form(...),
    account_ids: str = Form(""),
    owner_key: str = Form(""),
    group: str = Form(""),
    followup_days: str = Form(""),
    send_mode: str = Form("now"),
    window_start: str = Form("09:00"),
    window_end: str = Form("11:30"),
) -> HTMLResponse:
    aids = [int(x) for x in account_ids.split(",") if x.strip().isdigit()]
    owner = owner_key.strip().lower()
    if not aids:
        # Auto: all active accounts, optionally filtered by owner (usama/faizan)
        q = select(Account).where(Account.active.is_(True), Account.status != "auth_failed")
        accounts = list(db.scalars(q).all())
        if owner:
            accounts = [a for a in accounts if (a.owner_key or "").lower() == owner]
        aids = [a.id for a in accounts]
    if not aids:
        flash(
            request,
            "No sending accounts selected. Tag accounts with an owner key or paste account IDs.",
            "error",
        )
        return redirect("/campaigns/new")
    filt = {"group": group, "exclude_contacted": True, "owner_key": owner}
    camp = Campaign(
        name=name,
        template_id=template_id,
        account_ids=aids,
        contact_filter=filt,
        followup_days=int(followup_days) if followup_days.strip().isdigit() else None,
        send_window={"mode": send_mode, "start": window_start, "end": window_end},
        status="ready",
        created_at=datetime.now(timezone.utc),
    )
    db.add(camp)
    db.commit()
    db.refresh(camp)
    flash(
        request,
        f"Campaign created with {len(aids)} account(s). Generate drafts next.",
        "success",
    )
    return redirect(f"/campaigns/{camp.id}")


@router.get("/{campaign_id}", response_class=HTMLResponse)
def campaign_detail(
    campaign_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    camp = db.get(Campaign, campaign_id)
    if not camp:
        return redirect("/campaigns")
    contacts = _filter_contacts(db, camp.contact_filter or {})
    drafts = db.scalars(select(Draft).where(Draft.campaign_id == campaign_id)).all()
    ctx = base_context(
        request,
        db,
        active="campaigns",
        campaign=camp,
        contact_count=len(contacts),
        drafts=drafts,
    )
    return templates.TemplateResponse(request, "campaign_detail.html", ctx)


@router.post("/{campaign_id}/generate", dependencies=[Depends(require_csrf)])
def generate_drafts(
    campaign_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    from app.routes.drafts import enqueue_generation

    camp = db.get(Campaign, campaign_id)
    if not camp:
        return redirect("/campaigns")
    contacts = _filter_contacts(db, camp.contact_filter or {})
    if not contacts:
        flash(
            request,
            "No eligible contacts to draft. Import professors on Contacts, or edit the "
            "campaign group filter (empty group = all). Then Generate again.",
            "error",
        )
        return redirect(f"/campaigns/{campaign_id}")
    if not camp.account_ids:
        flash(
            request,
            "Campaign has no sending accounts. Edit/recreate it with an owner key or account IDs.",
            "error",
        )
        return redirect(f"/campaigns/{campaign_id}")
    assignment = assign_accounts_round_robin(
        db, [c.id for c in contacts], list(camp.account_ids or [])
    )
    enqueue_generation(db, campaign_id, [c.id for c in contacts], assignment)
    flash(
        request,
        f"Queued draft generation for {len(contacts)} contacts — watch progress on Drafts "
        "(needs Ollama + drafting model).",
        "success",
    )
    return redirect(f"/campaigns/{campaign_id}")


@router.post("/{campaign_id}/pause", dependencies=[Depends(require_csrf)])
def pause_campaign(
    campaign_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    camp = db.get(Campaign, campaign_id)
    if camp:
        camp.paused = not camp.paused
        flash(request, "Campaign paused." if camp.paused else "Campaign resumed.", "success")
    return redirect(f"/campaigns/{campaign_id}")
