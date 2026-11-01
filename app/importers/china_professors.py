"""Normalize China-professor outreach CSVs into MailPilot contact rows."""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

from app.importers.tabular import normalize_email, suggest_timezone

_NOT_STATED = re.compile(r"^\s*(not\s*stated|n/?a|none|unknown|-)\s*$", re.I)


def _clean(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    if _NOT_STATED.match(text):
        return ""
    return text


def looks_like_china_professor_csv(headers: list[str]) -> bool:
    """Detect the verified China professors export schema."""
    norms = {re.sub(r"\s+", " ", h.strip().lower()) for h in headers}
    markers = {"university_en", "university_cn", "research_direction", "email_source_url"}
    return len(norms & markers) >= 2 or (
        "email" in norms and ("university_en" in norms or "research_direction" in norms)
    )


def preferred_display_name(row: dict[str, Any]) -> str:
    """Prefer English name for EN outreach drafts; fall back to CN / full_name."""
    for key in ("professor_name_en", "full_name_en", "name_en", "english_name"):
        val = _clean(row.get(key))
        if val and re.search(r"[A-Za-z]", val):
            return val
    for key in ("full_name", "professor_name_cn", "name"):
        val = _clean(row.get(key))
        if val:
            return val
    return ""


def normalize_professor_dataframe(
    df: pd.DataFrame,
    *,
    split_batches: bool = True,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """
    Build a MailPilot-ready dataframe + QA summary.
    Adds country/China defaults, preferred EN names, groups, batch split.
    """
    records: list[dict[str, Any]] = []
    qa = {
        "input_rows": len(df),
        "missing_english_name": 0,
        "missing_research": 0,
        "missing_title": 0,
        "duplicate_emails": 0,
        "invalid_emails": 0,
    }
    seen: set[str] = set()

    # Stable half/half by university then email
    work = df.copy().fillna("")
    if "university_en" in work.columns:
        work = work.sort_values(by=["university_en", "email"], kind="stable")
    elif "email" in work.columns:
        work = work.sort_values(by=["email"], kind="stable")

    for idx, series in enumerate(work.to_dict(orient="records")):
        email, err = normalize_email(str(series.get("email", "")))
        if err or not email:
            qa["invalid_emails"] += 1
            continue
        if email in seen:
            qa["duplicate_emails"] += 1
            continue
        seen.add(email)

        en = _clean(series.get("professor_name_en"))
        cn = _clean(series.get("professor_name_cn") or series.get("full_name"))
        display = preferred_display_name(series)
        if not en:
            qa["missing_english_name"] += 1

        title = _clean(series.get("title"))
        if not title:
            qa["missing_title"] += 1
            title = "Professor"

        research = _clean(series.get("research_direction") or series.get("research_summary"))
        if not research:
            qa["missing_research"] += 1

        institution = _clean(series.get("university_en") or series.get("institution"))
        department = _clean(series.get("department"))
        profile_url = _clean(series.get("email_source_url") or series.get("profile_url"))
        verification = _clean(series.get("verification_status"))
        notes_bits = [_clean(series.get("notes"))]
        if cn and en and cn != en:
            notes_bits.append(f"CN name: {cn}")
        if verification:
            notes_bits.append(f"verification: {verification}")
        notes = " | ".join(b for b in notes_bits if b)

        groups = ["professor", "china", "botany"]
        if not research:
            groups.append("needs_research")
        if not en:
            groups.append("needs_english_name")
        if split_batches:
            groups.append("usama_batch" if idx % 2 == 0 else "faizan_batch")

        records.append(
            {
                "email": email,
                "full_name": display,
                "title": title,
                "institution": institution,
                "department": department,
                "country": "China",
                "timezone": suggest_timezone("China") or "Asia/Shanghai",
                "research_summary": research,
                "recent_papers": "",
                "profile_url": profile_url,
                "groups": ", ".join(groups),
                "region_type": "china",
                "notes": notes,
                "verification_status": verification,
                "professor_name_cn": cn,
                "professor_name_en": en,
                "university_cn": _clean(series.get("university_cn")),
            }
        )

    qa["output_rows"] = len(records)
    qa["usama_batch"] = sum(1 for r in records if "usama_batch" in r["groups"])
    qa["faizan_batch"] = sum(1 for r in records if "faizan_batch" in r["groups"])
    out = pd.DataFrame(records)
    return out, qa


def mailpilot_import_columns() -> list[str]:
    """Columns the Contacts importer maps cleanly."""
    return [
        "email",
        "full_name",
        "title",
        "institution",
        "department",
        "country",
        "timezone",
        "research_summary",
        "profile_url",
        "groups",
        "region_type",
        "notes",
    ]
