"""Local-LLM workflow coach — tells the user what to do next in MailPilot."""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_setting
from app.llm.ollama_client import OllamaClientProtocol, get_ollama_client
from app.models import Account, Campaign, Contact, CvVariant, Draft, Template, UserProfile

logger = logging.getLogger(__name__)

PLAYBOOK = """You are the MailPilot Workflow Coach for a local Gmail + Ollama outreach app.
You help the operator send professor PhD enquiry emails safely and smoothly.

App facts (always true):
- UI at http://127.0.0.1:8765 only. No cloud LLM — drafting uses Ollama on localhost.
- Dry-run defaults ON: outbound mail becomes .eml under data/outbox_dryrun/ until Settings turns dry-run off.
- App passwords are Fernet-encrypted; sync uses IMAP BODY.PEEK (never marks read).
- China professor CSVs (columns like university_en, research_direction, professor_name_en) are
  auto-detected on Contacts → Upload: region=china, country=China, English name preferred,
  groups professor/china/botany + usama_batch|faizan_batch, needs_research / needs_english_name tags.
- Multi-sender: set owner_key on Accounts, CVs, and Profile (e.g. usama / faizan). Campaigns can
  filter by owner_key so each person only uses their mailboxes and CVs.
- Seeded templates: "Western PhD enquiry (EN)" and "China / CSC enquiry (EN, simpler wording)".
- Recommended Ollama models: drafting qwen3.5:4b, embedding qwen3-embedding:0.6b
  (Settings must match `ollama list`; missing model → `ollama pull <name>`).

Smooth path (order matters):
1) Settings: confirm Ollama URL + models healthy; leave dry-run ON for first campaign.
2) Accounts: add Gmail + app password; set owner_key; optional default CV.
3) Profile: allowed facts + forbidden phrases per owner_key.
4) CVs: upload PDFs, set owner_key + description (LLM can suggest description).
5) Contacts: import professors CSV (China file auto-normalized) → check groups/region.
6) Templates: pick China template for china region_type contacts.
7) Campaigns: create with China template + group filter (e.g. usama_batch) + owner_key.
8) Generate drafts → review flags → approve → Queue. Inspect dry-run .eml before real send.
9) Inbox: watch replies/bounces; DNC / bounced contacts are skipped next campaign.

When advising:
- Be concrete and short (bullet steps). Name the exact MailPilot page.
- Prefer the next 1–3 actions based on STATE_JSON gaps — do not dump the whole playbook.
- If Ollama is down or models missing, say so first.
- Never invent Gmail passwords or ask the user to disable security.
- If asked about CSV gaps: missing English names → needs_english_name group; missing research →
  needs_research; titles "not stated" become Professor; no country column was the main reason
  region/timezone options looked missing — fixed by China auto-normalize.
"""

ADVICE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "next_steps": {"type": "array", "items": {"type": "string"}},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "next_steps", "warnings"],
}


def collect_state(db: Session) -> dict[str, Any]:
    """Snapshot of what is configured so the coach can prioritize."""
    accounts = list(db.scalars(select(Account)).all())
    contacts = list(db.scalars(select(Contact)).all())
    cvs = list(db.scalars(select(CvVariant)).all())
    profiles = list(db.scalars(select(UserProfile)).all())
    drafts = list(db.scalars(select(Draft)).all())
    campaigns = list(db.scalars(select(Campaign)).all())
    templates = list(db.scalars(select(Template)).all())

    groups: dict[str, int] = {}
    for c in contacts:
        for g in c.groups or []:
            groups[str(g)] = groups.get(str(g), 0) + 1

    owners = sorted(
        {
            (a.owner_key or "").strip().lower()
            for a in accounts
            if (a.owner_key or "").strip()
        }
        | {
            (cv.owner_key or "").strip().lower()
            for cv in cvs
            if (cv.owner_key or "").strip()
        }
    )

    ollama = get_ollama_client(get_setting(db, "ollama_url"))
    drafting = str(get_setting(db, "drafting_model", "qwen3.5:4b"))
    embedding = str(get_setting(db, "embedding_model", "qwen3-embedding:0.6b"))
    health = ollama.health([drafting, embedding])

    return {
        "accounts": {
            "total": len(accounts),
            "active": sum(1 for a in accounts if a.active),
            "with_owner": sum(1 for a in accounts if (a.owner_key or "").strip()),
            "emails": [a.email for a in accounts[:12]],
        },
        "contacts": {
            "total": len(contacts),
            "china_region": sum(1 for c in contacts if (c.region_type or "") == "china"),
            "dnc_or_bounced": sum(1 for c in contacts if c.do_not_contact or c.bounced),
            "groups": groups,
        },
        "cvs": {
            "total": len(cvs),
            "with_owner": sum(1 for c in cvs if (c.owner_key or "").strip()),
            "keys": [c.key for c in cvs[:20]],
        },
        "profiles": {
            "total": len(profiles),
            "owner_keys": [p.owner_key for p in profiles if getattr(p, "owner_key", None)],
        },
        "templates": [{"id": t.id, "name": t.name, "region_type": t.region_type} for t in templates],
        "campaigns": {
            "total": len(campaigns),
            "statuses": {
                s: sum(1 for c in campaigns if c.status == s)
                for s in sorted({c.status for c in campaigns})
            },
        },
        "drafts": {
            "total": len(drafts),
            "by_status": {
                s: sum(1 for d in drafts if d.status == s)
                for s in sorted({d.status for d in drafts})
            },
            "flagged": sum(1 for d in drafts if d.validation_flags),
        },
        "settings": {
            "dry_run": bool(get_setting(db, "dry_run", True)),
            "pause_all_sending": bool(get_setting(db, "pause_all_sending", False)),
            "drafting_model": drafting,
            "embedding_model": embedding,
            "timezone": get_setting(db, "timezone", "Asia/Shanghai"),
        },
        "ollama": {
            "ok": health.get("ok"),
            "models": health.get("models", [])[:30],
            "missing": health.get("missing", []),
            "pull_commands": health.get("pull_commands", []),
            "error": health.get("error"),
        },
        "owners": owners,
        "checklist": _checklist(
            accounts=accounts,
            contacts=contacts,
            cvs=cvs,
            profiles=profiles,
            health=health,
            dry_run=bool(get_setting(db, "dry_run", True)),
            campaigns=campaigns,
            drafts=drafts,
        ),
    }


def _checklist(
    *,
    accounts: list[Account],
    contacts: list[Contact],
    cvs: list[CvVariant],
    profiles: list[UserProfile],
    health: dict[str, Any],
    dry_run: bool,
    campaigns: list[Campaign],
    drafts: list[Draft],
) -> list[dict[str, Any]]:
    return [
        {
            "id": "ollama",
            "label": "Ollama reachable with drafting + embedding models",
            "done": bool(health.get("ok")) and not health.get("missing"),
            "href": "/settings",
        },
        {
            "id": "accounts",
            "label": "At least one Gmail account connected",
            "done": any(a.active for a in accounts),
            "href": "/accounts",
        },
        {
            "id": "owner_keys",
            "label": "Accounts tagged with owner_key (usama/faizan)",
            "done": any((a.owner_key or "").strip() for a in accounts),
            "href": "/accounts",
        },
        {
            "id": "profile",
            "label": "User profile with allowed facts",
            "done": any((getattr(p, "facts_allowed", None) or []) for p in profiles)
            or len(profiles) > 0,
            "href": "/profile",
        },
        {
            "id": "cvs",
            "label": "CV PDFs uploaded",
            "done": len(cvs) > 0,
            "href": "/cvs",
        },
        {
            "id": "contacts",
            "label": "Contacts imported (professors CSV)",
            "done": len(contacts) > 0,
            "href": "/contacts",
        },
        {
            "id": "campaign",
            "label": "Campaign created",
            "done": len(campaigns) > 0,
            "href": "/campaigns",
        },
        {
            "id": "drafts",
            "label": "Drafts generated and reviewed",
            "done": any(d.status in {"draft", "approved", "sent"} for d in drafts),
            "href": "/drafts",
        },
        {
            "id": "dry_run",
            "label": "Dry-run still ON (safe until you verify .eml)",
            "done": dry_run,
            "href": "/settings",
        },
    ]


def heuristic_advice(state: dict[str, Any]) -> dict[str, Any]:
    """Deterministic next steps when Ollama is unavailable."""
    steps: list[str] = []
    warnings: list[str] = []
    for item in state.get("checklist") or []:
        if item.get("id") == "dry_run":
            continue
        if not item.get("done"):
            steps.append(f"Go to {item.get('href')}: {item.get('label')}")
        if len(steps) >= 3:
            break
    ollama = state.get("ollama") or {}
    if not ollama.get("ok"):
        warnings.append(f"Ollama not reachable: {ollama.get('error') or 'unknown error'}")
        for cmd in ollama.get("pull_commands") or []:
            warnings.append(f"After starting Ollama, run: {cmd}")
    elif ollama.get("missing"):
        for cmd in ollama.get("pull_commands") or []:
            warnings.append(f"Missing model — run: {cmd}")
    if state.get("settings", {}).get("dry_run"):
        steps.append("Keep dry-run ON until you open data/outbox_dryrun/ and verify a sample .eml")
    if not steps:
        steps.append("Review Drafts → approve → Queue. Then check Inbox for replies.")
    return {
        "summary": "Next actions based on your current MailPilot setup.",
        "next_steps": steps,
        "warnings": warnings,
        "source": "heuristic",
    }


def ask_coach(
    db: Session,
    question: str = "",
    *,
    client: OllamaClientProtocol | None = None,
) -> dict[str, Any]:
    """Ask the local LLM for next-step advice, with heuristic fallback."""
    state = collect_state(db)
    fallback = heuristic_advice(state)
    drafting = str(get_setting(db, "drafting_model", "qwen3.5:4b"))
    ollama = client or get_ollama_client(get_setting(db, "ollama_url"))
    health = state.get("ollama") or {}
    if not health.get("ok") or health.get("missing"):
        return {**fallback, "state": state}

    user_q = (question or "").strip() or "What should I do next to send China professor outreach safely?"
    messages = [
        {"role": "system", "content": PLAYBOOK},
        {
            "role": "user",
            "content": (
                "STATE_JSON:\n"
                + json.dumps({k: v for k, v in state.items() if k != "checklist"}, ensure_ascii=False)
                + "\nCHECKLIST:\n"
                + json.dumps(state.get("checklist"), ensure_ascii=False)
                + f"\nUSER_QUESTION: {user_q}\n"
                "Reply with JSON: summary, next_steps (array of short imperative strings), warnings."
            ),
        },
    ]
    try:
        raw = ollama.chat(drafting, messages, format_schema=ADVICE_SCHEMA, temperature=0.2)
        content = (raw.get("message") or {}).get("content") or "{}"
        parsed = json.loads(content)
        return {
            "summary": str(parsed.get("summary") or fallback["summary"]),
            "next_steps": [str(x) for x in (parsed.get("next_steps") or [])] or fallback["next_steps"],
            "warnings": [str(x) for x in (parsed.get("warnings") or [])],
            "source": "ollama",
            "state": state,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("workflow coach LLM failed: %s", exc)
        return {
            **fallback,
            "warnings": fallback["warnings"] + [f"LLM coach unavailable: {exc}"],
            "state": state,
        }


def contact_count(db: Session) -> int:
    return int(db.scalar(select(func.count()).select_from(Contact)) or 0)
