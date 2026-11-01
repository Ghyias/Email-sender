"""CV library routes."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import CVS_DIR, MAX_ATTACHMENT_BYTES, WARN_ATTACHMENT_BYTES
from app.db import get_db, get_setting
from app.deps import base_context, flash, redirect, require_csrf
from app.llm.cv_matcher import pack_embedding
from app.llm.ollama_client import get_ollama_client
from app.llm.prompts import CV_SUMMARY_SCHEMA
from app.models import CvVariant

router = APIRouter(prefix="/cvs")
templates = Jinja2Templates(directory="app/templates")


def _key_from_filename(name: str) -> str:
    stem = Path(name).stem
    return re.sub(r"[^a-z0-9_]+", "_", stem.lower()).strip("_") or "cv"


@router.get("", response_class=HTMLResponse)
def list_cvs(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    cvs = db.scalars(select(CvVariant).order_by(CvVariant.key)).all()
    ctx = base_context(request, db, active="cvs", cvs=cvs, warn_mb=WARN_ATTACHMENT_BYTES // (1024 * 1024))
    return templates.TemplateResponse(request, "cvs.html", ctx)


@router.post("/upload", dependencies=[Depends(require_csrf)])
async def upload_cv(
    request: Request,
    db: Session = Depends(get_db),
    file: UploadFile = File(...),
    label: str = Form(""),
    description: str = Form(""),
    keywords: str = Form(""),
) -> HTMLResponse:
    data = await file.read()
    filename = file.filename or "cv.pdf"
    if not filename.lower().endswith(".pdf"):
        flash(request, "Only PDF files are accepted.", "error")
        return redirect("/cvs")
    if len(data) > MAX_ATTACHMENT_BYTES:
        flash(request, "File exceeds 20 MB limit.", "error")
        return redirect("/cvs")
    if len(data) > WARN_ATTACHMENT_BYTES:
        flash(request, "Warning: file is larger than 5 MB.", "info")
    CVS_DIR.mkdir(parents=True, exist_ok=True)
    key = _key_from_filename(filename)
    dest = CVS_DIR / f"{key}.pdf"
    # uniquify
    i = 1
    while dest.exists() and db.scalar(select(CvVariant).where(CvVariant.key == key)):
        key = f"{_key_from_filename(filename)}_{i}"
        dest = CVS_DIR / f"{key}.pdf"
        i += 1
    dest.write_bytes(data)
    kw = [k.strip() for k in keywords.split(",") if k.strip()]
    cv = CvVariant(
        key=key,
        file_path=str(dest),
        label=label or key,
        description=description,
        keywords=kw,
        updated_at=datetime.now(timezone.utc),
    )
    db.add(cv)
    db.commit()
    db.refresh(cv)
    _maybe_embed(db, cv)
    flash(request, f"CV '{key}' added.", "success")
    return redirect("/cvs")


@router.post("/scan-folder", dependencies=[Depends(require_csrf)])
def scan_folder(
    request: Request,
    db: Session = Depends(get_db),
    folder: str = Form(...),
) -> HTMLResponse:
    path = Path(folder)
    if not path.is_dir():
        flash(request, "Folder not found.", "error")
        return redirect("/cvs")
    added = 0
    for pdf in path.glob("*.pdf"):
        key = _key_from_filename(pdf.name)
        if db.scalar(select(CvVariant).where(CvVariant.key == key)):
            continue
        if pdf.stat().st_size > MAX_ATTACHMENT_BYTES:
            continue
        dest = CVS_DIR / f"{key}.pdf"
        dest.write_bytes(pdf.read_bytes())
        cv = CvVariant(key=key, file_path=str(dest), label=key, description="", keywords=[])
        db.add(cv)
        added += 1
    db.commit()
    flash(request, f"Imported {added} PDFs.", "success")
    return redirect("/cvs")


@router.post("/{cv_id}/update", dependencies=[Depends(require_csrf)])
def update_cv(
    cv_id: int,
    request: Request,
    db: Session = Depends(get_db),
    label: str = Form(""),
    description: str = Form(""),
    keywords: str = Form(""),
) -> HTMLResponse:
    cv = db.get(CvVariant, cv_id)
    if cv:
        cv.label = label or cv.label
        cv.description = description
        cv.keywords = [k.strip() for k in keywords.split(",") if k.strip()]
        cv.updated_at = datetime.now(timezone.utc)
        cv.embedding = None
        db.commit()
        _maybe_embed(db, cv)
        flash(request, "CV updated.", "success")
    return redirect("/cvs")


@router.post("/{cv_id}/suggest", dependencies=[Depends(require_csrf)])
def suggest_description(
    cv_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    cv = db.get(CvVariant, cv_id)
    if not cv:
        return redirect("/cvs")
    try:
        from pypdf import PdfReader

        reader = PdfReader(cv.file_path)
        text = "\n".join((p.extract_text() or "") for p in reader.pages[:5])[:4000]
    except Exception as exc:  # noqa: BLE001
        flash(request, f"PDF extract failed: {exc}", "error")
        return redirect("/cvs")
    client = get_ollama_client(get_setting(db, "ollama_url"))
    model = str(get_setting(db, "drafting_model", "qwen3.5:4b"))
    try:
        data = client.chat(
            model,
            [
                {
                    "role": "system",
                    "content": "Summarise what this CV emphasises in exactly 2 sentences. JSON only.",
                },
                {"role": "user", "content": text or cv.label},
            ],
            format_schema=CV_SUMMARY_SCHEMA,
            temperature=0.3,
        )
        content = data.get("message", {}).get("content", "{}")
        parsed = json.loads(content) if isinstance(content, str) else content
        suggestion = parsed.get("description", "")
    except Exception as exc:  # noqa: BLE001
        flash(
            request,
            (
                f"LLM suggest failed: {exc}. "
                f"Confirm Ollama is running and the drafting model is installed "
                f"(Settings shows the model name; run `ollama pull {model}`)."
            ),
            "error",
        )
        return redirect("/cvs")
    ctx = base_context(request, db, active="cvs", cv=cv, suggestion=suggestion)
    return templates.TemplateResponse(request, "cv_suggest.html", ctx)


@router.post("/{cv_id}/confirm-suggest", dependencies=[Depends(require_csrf)])
def confirm_suggest(
    cv_id: int,
    request: Request,
    db: Session = Depends(get_db),
    description: str = Form(...),
) -> HTMLResponse:
    cv = db.get(CvVariant, cv_id)
    if cv:
        cv.description = description
        cv.embedding = None
        cv.updated_at = datetime.now(timezone.utc)
        db.commit()
        _maybe_embed(db, cv)
        flash(request, "Description saved.", "success")
    return redirect("/cvs")


@router.post("/{cv_id}/embed", dependencies=[Depends(require_csrf)])
def recompute_embed(cv_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    cv = db.get(CvVariant, cv_id)
    if cv:
        ok = _maybe_embed(db, cv)
        flash(request, "Embedding computed." if ok else "Embedding failed.", "success" if ok else "error")
    return redirect("/cvs")


def _maybe_embed(db: Session, cv: CvVariant) -> bool:
    model = str(get_setting(db, "embedding_model", "qwen3-embedding:0.6b"))
    client = get_ollama_client(get_setting(db, "ollama_url"))
    text = f"{cv.label} {cv.description} {' '.join(cv.keywords or [])}"
    try:
        vecs = client.embed(model, [text])
        if vecs:
            cv.embedding = pack_embedding(vecs[0])
            cv.embedding_model = model
            cv.updated_at = datetime.now(timezone.utc)
            db.commit()
            return True
    except Exception:  # noqa: BLE001
        return False
    return False
