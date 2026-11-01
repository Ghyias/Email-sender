"""Draft generation queue + review UI."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal, get_db, get_setting
from app.deps import base_context, flash, redirect, require_csrf
from app.llm.cv_matcher import (
    CvCandidate,
    accuracy_report,
    decide_match,
    match_cv,
    unpack_embedding,
)
from app.llm.drafting import generate_draft
from app.llm.ollama_client import get_ollama_client
from app.models import (
    Account,
    Contact,
    CvLabel,
    CvVariant,
    Draft,
    GenerationJob,
    Template,
    UserProfile,
)

router = APIRouter(prefix="/drafts")
templates = Jinja2Templates(directory="app/templates")

_gen_lock = threading.Lock()
_gen_stop = threading.Event()
_gen_thread: threading.Thread | None = None


def enqueue_generation(
    db: Session,
    campaign_id: int | None,
    contact_ids: list[int],
    assignment: dict[int, int] | None = None,
) -> GenerationJob:
    """Persist a generation job and kick the worker."""
    job = GenerationJob(
        campaign_id=campaign_id,
        contact_ids=contact_ids,
        status="pending",
        progress=0,
        total=len(contact_ids),
    )
    # stash assignment in error field as JSON sidecar? better: settings-like
    db.add(job)
    db.commit()
    db.refresh(job)
    if assignment:
        # store alongside via a Setting key
        from app.db import set_setting

        set_setting(db, f"gen_assign_{job.id}", assignment)
        db.commit()
    _ensure_worker()
    return job


def _ensure_worker() -> None:
    global _gen_thread
    with _gen_lock:
        if _gen_thread and _gen_thread.is_alive():
            return
        _gen_stop.clear()
        _gen_thread = threading.Thread(target=_generation_loop, daemon=True, name="draft-gen")
        _gen_thread.start()


def _generation_loop() -> None:
    assert SessionLocal is not None
    while not _gen_stop.is_set():
        session = SessionLocal()
        job_id: int | None = None
        try:
            job = session.scalar(
                select(GenerationJob)
                .where(GenerationJob.status.in_(["pending", "running"]))
                .order_by(GenerationJob.id.asc())
                .limit(1)
            )
            if not job:
                session.close()
                _gen_stop.wait(2)
                continue
            if job.status == "paused":
                session.close()
                _gen_stop.wait(2)
                continue
            job_id = job.id
            job.status = "running"
            session.commit()
            _process_job(session, job)
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            if job_id is not None:
                try:
                    failed = session.get(GenerationJob, job_id)
                    if failed and failed.status in {"pending", "running"}:
                        failed.status = "error"
                        failed.error = str(exc)[:2000]
                        session.commit()
                except Exception:  # noqa: BLE001
                    session.rollback()
        finally:
            session.close()
        _gen_stop.wait(0.5)


def _process_job(session: Session, job: GenerationJob) -> None:
    from app.db import get_setting
    from app.scheduling.senders import pick_cv_for_account, profile_for_owner

    assignment = get_setting(session, f"gen_assign_{job.id}", {}) or {}
    # keys may be strings from JSON
    assignment = {int(k): int(v) for k, v in assignment.items()}
    model = str(get_setting(session, "drafting_model", "qwen3.5:4b"))
    emb_model = str(get_setting(session, "embedding_model", "qwen3-embedding:0.6b"))
    temperature = float(get_setting(session, "temperature", 0.4) or 0.4)
    client = get_ollama_client(get_setting(session, "ollama_url"))

    camp_tpl = None
    if job.campaign_id:
        from app.models import Campaign

        camp = session.get(Campaign, job.campaign_id)
        if camp and camp.template_id:
            camp_tpl = session.get(Template, camp.template_id)

    rules = get_setting(session, "cv_rules", {}) or {}

    ids = list(job.contact_ids or [])
    start = job.progress or 0
    for i, cid in enumerate(ids[start:], start=start):
        session.refresh(job)
        if job.status == "paused" or job.status == "cancelled":
            return
        contact = session.get(Contact, cid)
        if not contact:
            job.progress = i + 1
            session.commit()
            continue
        tpl = camp_tpl or session.scalar(select(Template).limit(1))
        if not tpl:
            job.error = "No template"
            job.status = "done"
            session.commit()
            return
        account_id = assignment.get(cid)
        account = session.get(Account, account_id) if account_id else None
        profile = profile_for_owner(session, account.owner_key if account else "")
        allowed = list((profile.facts_allowed if profile else None) or [])
        forbidden_default = list(
            (profile.facts_forbidden if profile else None) or ["GPA", "CGPA", "grade point"]
        )
        forbidden = list(tpl.forbidden_phrases or []) + forbidden_default
        forbidden = list(dict.fromkeys(forbidden))
        contact_dict = {
            "full_name": contact.full_name,
            "title": contact.title,
            "institution": contact.institution,
            "research_summary": contact.research_summary,
            "recent_papers": contact.recent_papers,
        }
        try:
            result = generate_draft(
                contact=contact_dict,
                llm_instructions=tpl.llm_instructions or "",
                allowed_facts=allowed,
                forbidden_phrases=forbidden,
                region_type=contact.region_type or tpl.region_type,
                language=tpl.language,
                max_words=tpl.max_words or 250,
                model=model,
                temperature=temperature,
                client=client,
            )
            research = f"{contact.research_summary}\n{contact.recent_papers}"
            cv_row, match = pick_cv_for_account(
                session,
                account,
                research_text=research,
                contact_groups=list(contact.groups or []),
                embedding_model=emb_model,
                rules=rules if isinstance(rules, dict) else {},
                client=client,
            )
        except Exception as exc:  # noqa: BLE001
            job.status = "error"
            job.error = (
                f"Failed on contact {cid} ({contact.email}): {exc}. "
                "Check Ollama is running and Settings → drafting model matches `ollama list` "
                "(e.g. ollama pull qwen3.5:4b)."
            )[:2000]
            session.commit()
            return
        draft = Draft(
            campaign_id=job.campaign_id,
            contact_id=cid,
            account_id=account_id,
            subject=result.subject,
            body_text=result.body,
            body_html="",
            cv_variant_id=cv_row.id if cv_row else None,
            cv_reason=match.reason,
            cv_confidence=match.confidence,
            cv_needs_review=match.needs_review,
            cv_scores=match.scores,
            llm_model=result.model,
            prompt_hash=result.prompt_hash,
            validation_flags=result.flags,
            status="draft",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add(draft)
        job.progress = i + 1
        session.commit()
    job.status = "done"
    if not ids:
        job.error = (
            "No eligible contacts for this campaign. Import contacts and/or clear the "
            "group filter, then click Generate drafts again."
        )
    session.commit()


@router.get("", response_class=HTMLResponse)
def list_drafts(
    request: Request,
    db: Session = Depends(get_db),
    campaign_id: int | None = None,
    status: str = "",
) -> HTMLResponse:
    stmt = select(Draft).order_by(Draft.created_at.desc())
    if campaign_id:
        stmt = stmt.where(Draft.campaign_id == campaign_id)
    if status:
        stmt = stmt.where(Draft.status == status)
    drafts = db.scalars(stmt.limit(500)).all()
    jobs = db.scalars(select(GenerationJob).order_by(GenerationJob.id.desc()).limit(5)).all()
    rows = []
    for d in drafts:
        rows.append(
            {
                "draft": d,
                "contact": db.get(Contact, d.contact_id),
                "account": db.get(Account, d.account_id) if d.account_id else None,
                "cv": db.get(CvVariant, d.cv_variant_id) if d.cv_variant_id else None,
            }
        )
    ctx = base_context(request, db, active="drafts", rows=rows, jobs=jobs)
    return templates.TemplateResponse(request, "drafts.html", ctx)


@router.get("/{draft_id}", response_class=HTMLResponse)
def draft_detail(draft_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    d = db.get(Draft, draft_id)
    if not d:
        return redirect("/drafts")
    ctx = base_context(
        request,
        db,
        active="drafts",
        draft=d,
        contact=db.get(Contact, d.contact_id),
        account=db.get(Account, d.account_id) if d.account_id else None,
        cv=db.get(CvVariant, d.cv_variant_id) if d.cv_variant_id else None,
        cvs=db.scalars(select(CvVariant)).all(),
    )
    return templates.TemplateResponse(request, "draft_detail.html", ctx)


@router.post("/{draft_id}/save", dependencies=[Depends(require_csrf)])
def save_draft(
    draft_id: int,
    request: Request,
    db: Session = Depends(get_db),
    subject: str = Form(""),
    body_text: str = Form(""),
    cv_variant_id: int = Form(0),
) -> HTMLResponse:
    d = db.get(Draft, draft_id)
    if d:
        d.subject = subject
        d.body_text = body_text
        if cv_variant_id:
            d.cv_variant_id = cv_variant_id
        d.updated_at = datetime.now(timezone.utc)
        flash(request, "Draft saved.", "success")
    return redirect(f"/drafts/{draft_id}")


@router.post("/{draft_id}/regenerate", dependencies=[Depends(require_csrf)])
def regenerate(
    draft_id: int,
    request: Request,
    db: Session = Depends(get_db),
    mode: str = Form("regenerate"),
) -> HTMLResponse:
    d = db.get(Draft, draft_id)
    if not d:
        return redirect("/drafts")
    contact = db.get(Contact, d.contact_id)
    tpl = None
    if d.campaign_id:
        from app.models import Campaign

        camp = db.get(Campaign, d.campaign_id)
        if camp:
            tpl = db.get(Template, camp.template_id) if camp.template_id else None
    tpl = tpl or db.scalar(select(Template).limit(1))
    profile = db.scalar(select(UserProfile).limit(1))
    rewrite = None
    if mode == "shorter":
        rewrite = "Make the email shorter."
    elif mode == "formal":
        rewrite = "Make the tone more formal."
    result = generate_draft(
        contact={
            "full_name": contact.full_name if contact else "",
            "title": contact.title if contact else "",
            "institution": contact.institution if contact else "",
            "research_summary": contact.research_summary if contact else "",
            "recent_papers": contact.recent_papers if contact else "",
        },
        llm_instructions=(tpl.llm_instructions if tpl else ""),
        allowed_facts=list((profile.facts_allowed if profile else None) or []),
        forbidden_phrases=list((tpl.forbidden_phrases if tpl else None) or [])
        + list((profile.facts_forbidden if profile else None) or []),
        region_type=(contact.region_type if contact else "western"),
        language=(tpl.language if tpl else "en"),
        max_words=(tpl.max_words if tpl else 250),
        model=str(get_setting(db, "drafting_model", "qwen3.5:4b")),
        temperature=float(get_setting(db, "temperature", 0.4) or 0.4),
        client=get_ollama_client(get_setting(db, "ollama_url")),
        rewrite_instruction=rewrite,
    )
    d.subject = result.subject
    d.body_text = result.body
    d.validation_flags = result.flags
    d.llm_model = result.model
    d.prompt_hash = result.prompt_hash
    d.updated_at = datetime.now(timezone.utc)
    db.commit()
    flash(request, "Draft regenerated.", "success")
    return redirect(f"/drafts/{draft_id}")


@router.post("/{draft_id}/approve", dependencies=[Depends(require_csrf)])
def approve_one(draft_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    d = db.get(Draft, draft_id)
    if d and d.status == "draft":
        d.status = "approved"
        flash(request, "Approved.", "success")
    return redirect(f"/drafts/{draft_id}")


@router.post("/{draft_id}/reject", dependencies=[Depends(require_csrf)])
def reject_one(draft_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    d = db.get(Draft, draft_id)
    if d:
        d.status = "cancelled"
        flash(request, "Rejected.", "success")
    return redirect("/drafts")


@router.post("/bulk-approve", dependencies=[Depends(require_csrf)])
def bulk_approve(
    request: Request,
    db: Session = Depends(get_db),
    draft_ids: str = Form(""),
    reviewed_flagged: int = Form(0),
) -> HTMLResponse:
    ids = [int(x) for x in draft_ids.split(",") if x.strip().isdigit()]
    approved = 0
    blocked = 0
    for did in ids:
        d = db.get(Draft, did)
        if not d or d.status != "draft":
            continue
        flags = d.validation_flags or []
        if flags and not reviewed_flagged:
            blocked += 1
            continue
        d.status = "approved"
        approved += 1
    flash(
        request,
        f"Approved {approved}." + (f" Blocked {blocked} flagged (tick reviewed)." if blocked else ""),
        "success" if approved else "error",
    )
    return redirect("/drafts")


@router.post("/jobs/{job_id}/pause", dependencies=[Depends(require_csrf)])
def pause_job(job_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    job = db.get(GenerationJob, job_id)
    if job and job.status in ("pending", "running"):
        job.status = "paused"
        flash(request, "Generation paused.", "info")
    elif job and job.status == "paused":
        job.status = "pending"
        _ensure_worker()
        flash(request, "Generation resumed.", "success")
    return redirect("/drafts")


@router.get("/cv-accuracy", response_class=HTMLResponse)
def cv_accuracy_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    contacts = db.scalars(select(Contact).limit(50)).all()
    labels = db.scalars(select(CvLabel)).all()
    cvs = db.scalars(select(CvVariant)).all()
    label_map = {lab.contact_id: lab.cv_key for lab in labels}
    # compute accuracies if we have labels
    report = {}
    if labels and cvs:
        client = get_ollama_client(get_setting(db, "ollama_url"))
        emb_model = str(get_setting(db, "embedding_model", "qwen3-embedding:0.6b"))
        candidates = [
            CvCandidate(
                key=cv.key,
                description=cv.description or "",
                keywords=list(cv.keywords or []),
                embedding=unpack_embedding(cv.embedding),
            )
            for cv in cvs
        ]
        preds: dict[str, dict[str, str]] = {"embedding": {}, "llm": {}, "hybrid": {}}
        label_pairs: list[tuple[str, str]] = []
        for lab in labels:
            c = db.get(Contact, lab.contact_id)
            if not c:
                continue
            key = str(c.id)
            label_pairs.append((key, lab.cv_key))
            research = f"{c.research_summary}\n{c.recent_papers}"
            vecs = client.embed(emb_model, [research])
            qv = vecs[0] if vecs else []
            emb_only = decide_match(
                research_text=research,
                contact_groups=list(c.groups or []),
                candidates=candidates,
                query_embedding=qv,
                llm_pick=None,
            )
            hybrid = match_cv(
                research_text=research,
                contact_groups=list(c.groups or []),
                candidates=candidates,
                embedding_model=emb_model,
                client=client,
            )
            preds["embedding"][key] = emb_only.cv_key
            preds["llm"][key] = hybrid.cv_key  # approximate
            preds["hybrid"][key] = hybrid.cv_key
        report = accuracy_report(label_pairs, preds)
    ctx = base_context(
        request, db, active="cvs", contacts=contacts, cvs=cvs, label_map=label_map, report=report
    )
    return templates.TemplateResponse(request, "cv_accuracy.html", ctx)


@router.post("/cv-accuracy/label", dependencies=[Depends(require_csrf)])
def save_cv_label(
    request: Request,
    db: Session = Depends(get_db),
    contact_id: int = Form(...),
    cv_key: str = Form(...),
) -> HTMLResponse:
    existing = db.scalar(select(CvLabel).where(CvLabel.contact_id == contact_id))
    if existing:
        existing.cv_key = cv_key
    else:
        db.add(CvLabel(contact_id=contact_id, cv_key=cv_key))
    flash(request, "Label saved.", "success")
    return redirect("/drafts/cv-accuracy")
