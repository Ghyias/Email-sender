"""Contacts list + import wizard."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import base_context, flash, redirect, require_csrf
from app.importers.tabular import (
    apply_mapping,
    guess_mapping,
    load_tabular,
    prepare_contacts,
    rejects_to_csv,
)
from app.models import Contact

router = APIRouter(prefix="/contacts")
templates = Jinja2Templates(directory="app/templates")

# In-memory staging for import preview (single-user local app)
_import_staging: dict[str, Any] = {}


@router.get("", response_class=HTMLResponse)
def list_contacts(
    request: Request,
    db: Session = Depends(get_db),
    q: str = "",
    group: str = "",
) -> HTMLResponse:
    stmt = select(Contact).order_by(Contact.full_name.asc(), Contact.email.asc())
    contacts = list(db.scalars(stmt).all())
    if q:
        ql = q.lower()
        contacts = [
            c
            for c in contacts
            if ql in (c.email or "").lower()
            or ql in (c.full_name or "").lower()
            or ql in (c.institution or "").lower()
        ]
    if group:
        contacts = [
            c for c in contacts if group.lower() in [str(g).lower() for g in (c.groups or [])]
        ]
    all_groups: set[str] = set()
    for c in db.scalars(select(Contact)).all():
        for g in c.groups or []:
            all_groups.add(str(g))
    ctx = base_context(
        request,
        db,
        active="contacts",
        contacts=contacts,
        q=q,
        group=group,
        all_groups=sorted(all_groups),
        staging=_import_staging.get("preview"),
    )
    return templates.TemplateResponse(request, "contacts.html", ctx)


@router.post("/import/upload", dependencies=[Depends(require_csrf)])
async def import_upload(
    request: Request,
    db: Session = Depends(get_db),
    file: UploadFile = File(...),
    sheet: str = Form(""),
) -> HTMLResponse:
    data = await file.read()
    filename = file.filename or "upload.csv"
    try:
        df, sheets = load_tabular(data, filename, sheet=sheet or None)
    except Exception as exc:  # noqa: BLE001
        flash(request, f"Failed to read file: {exc}", "error")
        return redirect("/contacts")
    headers = [str(c) for c in df.columns]
    mapping = guess_mapping(headers)
    preview = df.head(20).fillna("").to_dict(orient="records")
    _import_staging.clear()
    _import_staging.update(
        {
            "filename": filename,
            "data": data,
            "sheet": sheet or (sheets[0] if sheets else ""),
            "sheets": sheets,
            "headers": headers,
            "mapping": mapping,
            "preview": preview,
        }
    )
    return redirect("/contacts/import/map")


@router.get("/import/map", response_class=HTMLResponse)
def import_map(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    if not _import_staging:
        flash(request, "Upload a file first.", "error")
        return redirect("/contacts")
    ctx = base_context(request, db, active="contacts", staging=_import_staging)
    return templates.TemplateResponse(request, "contacts_import.html", ctx)


@router.post("/import/commit", dependencies=[Depends(require_csrf)])
async def import_commit(
    request: Request,
    db: Session = Depends(get_db),
    policy: str = Form("fill-empty"),
    region_type: str = Form("other"),
    groups: str = Form(""),
) -> HTMLResponse:
    if not _import_staging:
        flash(request, "No import in progress.", "error")
        return redirect("/contacts")
    form = await request.form()
    mapping: dict[str, str] = {}
    for key in (
        "email",
        "full_name",
        "title",
        "institution",
        "department",
        "country",
        "research_summary",
        "recent_papers",
        "profile_url",
        "groups",
    ):
        col = str(form.get(f"map_{key}", "") or "")
        if col:
            mapping[key] = col
    from app.importers.tabular import load_tabular

    df, _ = load_tabular(
        _import_staging["data"],
        _import_staging["filename"],
        sheet=_import_staging.get("sheet") or None,
    )
    rows = apply_mapping(df, mapping)
    existing = {
        c.email: {
            "email": c.email,
            "full_name": c.full_name,
            "title": c.title,
            "institution": c.institution,
            "department": c.department,
            "country": c.country,
            "timezone": c.timezone,
            "research_summary": c.research_summary,
            "recent_papers": c.recent_papers,
            "profile_url": c.profile_url,
            "groups": c.groups or [],
            "region_type": c.region_type,
            "id": c.id,
        }
        for c in db.scalars(select(Contact)).all()
    }
    group_list = [g.strip() for g in groups.split(",") if g.strip()]
    upserts, report = prepare_contacts(
        rows,
        groups=group_list or None,
        region_type=region_type,
        source_file=_import_staging["filename"],
        existing_by_email=existing,
        policy=policy,
    )
    for item in upserts:
        email = item["email"]
        if email in existing and existing[email].get("id"):
            contact = db.get(Contact, existing[email]["id"])
            if contact:
                for k, v in item.items():
                    if k == "email":
                        continue
                    setattr(contact, k, v)
        else:
            db.add(Contact(**{k: v for k, v in item.items() if k != "id"}))
    db.commit()
    _import_staging["report"] = {
        "imported": report.imported,
        "updated": report.updated,
        "skipped_duplicate": report.skipped_duplicate,
        "invalid": report.invalid,
    }
    _import_staging["rejects_csv"] = rejects_to_csv(report.rejects)
    flash(
        request,
        (
            f"Import done: {report.imported} new, {report.updated} updated, "
            f"{report.skipped_duplicate} skipped, {report.invalid} invalid."
        ),
        "success",
    )
    return redirect("/contacts/import/report")


@router.get("/import/report", response_class=HTMLResponse)
def import_report(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    ctx = base_context(
        request,
        db,
        active="contacts",
        report=_import_staging.get("report"),
    )
    return templates.TemplateResponse(request, "contacts_report.html", ctx)


@router.get("/import/rejects.csv")
def download_rejects() -> Response:
    csv_data = _import_staging.get("rejects_csv") or "email,error\n"
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=rejects.csv"},
    )


@router.post("/{contact_id}/toggle-dnc", dependencies=[Depends(require_csrf)])
def toggle_dnc(contact_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    c = db.get(Contact, contact_id)
    if c:
        c.do_not_contact = not c.do_not_contact
    return redirect("/contacts")


@router.post("/bulk-groups", dependencies=[Depends(require_csrf)])
def bulk_groups(
    request: Request,
    db: Session = Depends(get_db),
    contact_ids: str = Form(""),
    groups: str = Form(""),
) -> HTMLResponse:
    ids = [int(x) for x in contact_ids.split(",") if x.strip().isdigit()]
    group_list = [g.strip() for g in groups.split(",") if g.strip()]
    for cid in ids:
        c = db.get(Contact, cid)
        if c:
            existing = list(c.groups or [])
            for g in group_list:
                if g not in existing:
                    existing.append(g)
            c.groups = existing
    flash(request, "Groups updated.", "success")
    return redirect("/contacts")
