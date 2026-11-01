"""Email templates + user profile."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import BaseLoader, Environment, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import base_context, flash, redirect, require_csrf
from app.models import Contact, Template, UserProfile

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
_jinja = Environment(loader=BaseLoader(), autoescape=select_autoescape(enabled_extensions=()))


@router.get("/templates", response_class=HTMLResponse)
def list_templates(
    request: Request,
    db: Session = Depends(get_db),
    edit: int | None = None,
    preview_contact: int | None = None,
) -> HTMLResponse:
    tpls = db.scalars(select(Template).order_by(Template.name)).all()
    current = db.get(Template, edit) if edit else (tpls[0] if tpls else None)
    contacts = db.scalars(select(Contact).order_by(Contact.full_name).limit(200)).all()
    preview_subject = preview_body = ""
    if current:
        contact = db.get(Contact, preview_contact) if preview_contact else (contacts[0] if contacts else None)
        profile = db.scalar(select(UserProfile).limit(1))
        ctx_vars = {
            "contact": contact or Contact(email="example@edu", full_name="Dr. Example", institution="Example U"),
            "user": profile or UserProfile(full_name="You"),
            "llm": type("L", (), {"personal_paragraph": "[personalised paragraph]"})(),
        }
        try:
            preview_subject = _jinja.from_string(current.subject_template or "").render(**ctx_vars)
            preview_body = _jinja.from_string(current.body_template or "").render(**ctx_vars)
        except Exception as exc:  # noqa: BLE001
            preview_body = f"Preview error: {exc}"
    ctx = base_context(
        request,
        db,
        active="templates",
        tpls=tpls,
        current=current,
        contacts=contacts,
        preview_contact=preview_contact,
        preview_subject=preview_subject,
        preview_body=preview_body,
    )
    return templates.TemplateResponse(request, "templates.html", ctx)


@router.post("/templates/save", dependencies=[Depends(require_csrf)])
def save_template(
    request: Request,
    db: Session = Depends(get_db),
    template_id: int = Form(0),
    name: str = Form(...),
    language: str = Form("en"),
    region_type: str = Form("western"),
    subject_template: str = Form(""),
    body_template: str = Form(""),
    llm_instructions: str = Form(""),
    forbidden_phrases: str = Form(""),
    max_words: int = Form(250),
) -> HTMLResponse:
    phrases = [p.strip() for p in forbidden_phrases.split(",") if p.strip()]
    if template_id:
        tpl = db.get(Template, template_id)
        if not tpl:
            flash(request, "Template not found.", "error")
            return redirect("/templates")
    else:
        tpl = Template()
        db.add(tpl)
    tpl.name = name
    tpl.language = language
    tpl.region_type = region_type
    tpl.subject_template = subject_template
    tpl.body_template = body_template
    tpl.llm_instructions = llm_instructions
    tpl.forbidden_phrases = phrases
    tpl.max_words = max_words
    db.commit()
    db.refresh(tpl)
    flash(request, "Template saved.", "success")
    return redirect(f"/templates?edit={tpl.id}")


@router.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    profile = db.scalar(select(UserProfile).limit(1))
    ctx = base_context(request, db, active="settings", profile=profile)
    return templates.TemplateResponse(request, "profile.html", ctx)


@router.post("/profile", dependencies=[Depends(require_csrf)])
def save_profile(
    request: Request,
    db: Session = Depends(get_db),
    full_name: str = Form(""),
    short_bio: str = Form(""),
    research_interests: str = Form(""),
    signature_default: str = Form(""),
    facts_allowed: str = Form(""),
    facts_forbidden: str = Form(""),
) -> HTMLResponse:
    profile = db.scalar(select(UserProfile).limit(1))
    if not profile:
        profile = UserProfile()
        db.add(profile)
    profile.full_name = full_name
    profile.short_bio = short_bio
    profile.research_interests = research_interests
    profile.signature_default = signature_default
    profile.facts_allowed = [f.strip() for f in facts_allowed.split("\n") if f.strip()]
    profile.facts_forbidden = [f.strip() for f in facts_forbidden.split("\n") if f.strip()]
    db.commit()
    flash(request, "Profile saved.", "success")
    return redirect("/profile")
