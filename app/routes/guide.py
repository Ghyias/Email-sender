"""Workflow Guide — checklist + local LLM coach."""

from __future__ import annotations

import html
import re
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import base_context, require_csrf
from app.llm.workflow_coach import ask_coach, collect_state, heuristic_advice

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _md_to_simple_html(text: str) -> str:
    """Tiny markdown subset → HTML for the in-app user guide."""
    lines = text.splitlines()
    out: list[str] = []
    in_ul = False
    in_code = False
    code_buf: list[str] = []

    def close_ul() -> None:
        nonlocal in_ul
        if in_ul:
            out.append("</ul>")
            in_ul = False

    for ln in lines:
        if ln.startswith("```"):
            if in_code:
                out.append(
                    "<pre class='bg-slate-900 text-slate-100 text-xs overflow-x-auto rounded p-3 my-3'><code>"
                    + html.escape("\n".join(code_buf))
                    + "</code></pre>"
                )
                code_buf = []
                in_code = False
            else:
                close_ul()
                in_code = True
            continue
        if in_code:
            code_buf.append(ln)
            continue
        if not ln.strip():
            close_ul()
            continue
        if ln.startswith("# "):
            close_ul()
            out.append(f"<h1 class='text-2xl font-semibold mt-6 mb-3'>{html.escape(ln[2:])}</h1>")
            continue
        if ln.startswith("## "):
            close_ul()
            out.append(f"<h2 class='text-xl font-semibold mt-6 mb-2'>{html.escape(ln[3:])}</h2>")
            continue
        if ln.startswith("### "):
            close_ul()
            out.append(f"<h3 class='text-lg font-medium mt-4 mb-2'>{html.escape(ln[4:])}</h3>")
            continue
        if re.match(r"^[-*] ", ln):
            if not in_ul:
                out.append("<ul class='list-disc pl-5 space-y-1 text-sm mb-3'>")
                in_ul = True
            item = html.escape(ln[2:])
            item = re.sub(r"`([^`]+)`", r"<code class='text-xs'>\1</code>", item)
            item = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", item)
            out.append(f"<li>{item}</li>")
            continue
        close_ul()
        para = html.escape(ln)
        para = re.sub(r"`([^`]+)`", r"<code class='text-xs'>\1</code>", para)
        para = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", para)
        out.append(f"<p class='text-sm mb-2'>{para}</p>")
    close_ul()
    if in_code and code_buf:
        out.append(
            "<pre class='bg-slate-900 text-slate-100 text-xs overflow-x-auto rounded p-3 my-3'><code>"
            + html.escape("\n".join(code_buf))
            + "</code></pre>"
        )
    return "\n".join(out)


@router.get("/guide", response_class=HTMLResponse)
def guide_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    state = collect_state(db)
    advice = heuristic_advice(state)
    ctx = base_context(
        request,
        db,
        active="guide",
        state=state,
        advice=advice,
        question="",
    )
    return templates.TemplateResponse(request, "guide.html", ctx)


@router.post("/guide/ask", dependencies=[Depends(require_csrf)])
def guide_ask(
    request: Request,
    db: Session = Depends(get_db),
    question: str = Form(""),
) -> HTMLResponse:
    advice = ask_coach(db, question)
    state = advice.pop("state", None) or collect_state(db)
    ctx = base_context(
        request,
        db,
        active="guide",
        state=state,
        advice=advice,
        question=question,
    )
    return templates.TemplateResponse(request, "guide.html", ctx)


@router.get("/docs/user-guide", response_class=HTMLResponse)
def user_guide_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    path = _REPO_ROOT / "USER_GUIDE.md"
    raw = path.read_text(encoding="utf-8") if path.exists() else "# USER_GUIDE.md missing"
    ctx = base_context(
        request,
        db,
        active="guide",
        guide_html=_md_to_simple_html(raw),
    )
    return templates.TemplateResponse(request, "user_guide.html", ctx)
