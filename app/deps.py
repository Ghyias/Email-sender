"""Shared FastAPI dependencies and template context."""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import Form, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from app.db import get_setting
from app.models import SendQueue


def get_csrf_token(request: Request) -> str:
    """Return or create CSRF token stored in session cookie jar."""
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(24)
        request.session["csrf_token"] = token
    return token


def require_csrf(request: Request, csrf_token: str = Form(...)) -> None:
    """Validate CSRF token from form posts."""
    expected = request.session.get("csrf_token")
    if not expected or not secrets.compare_digest(expected, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def pop_flashes(request: Request) -> list[dict[str, str]]:
    """Pop flash messages from session."""
    flashes = request.session.pop("flashes", [])
    return flashes


def flash(request: Request, message: str, level: str = "info") -> None:
    """Append a flash message."""
    flashes = request.session.setdefault("flashes", [])
    flashes.append({"message": message, "level": level})


def base_context(
    request: Request,
    db: Session,
    active: str = "",
    **extra: Any,
) -> dict[str, Any]:
    """Common Jinja context for all pages."""
    missed = db.scalar(
        select(func.count()).select_from(SendQueue).where(SendQueue.status == "missed")
    )
    missed_banner = None
    if missed and missed > 0:
        missed_banner = f"{missed} scheduled emails were missed — reschedule?"
    return {
        "request": request,
        "active": active,
        "csrf_token": get_csrf_token(request),
        "flashes": pop_flashes(request),
        "dry_run": bool(get_setting(db, "dry_run", True)),
        "pause_all": bool(get_setting(db, "pause_all_sending", False)),
        "missed_banner": missed_banner,
        **extra,
    }


def redirect(url: str) -> RedirectResponse:
    """303 redirect helper."""
    return RedirectResponse(url=url, status_code=303)
