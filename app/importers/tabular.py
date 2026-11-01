"""CSV / XLSX / TXT contact import with column mapping."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd
from email_validator import EmailNotValidError, validate_email

HEADER_ALIASES: dict[str, list[str]] = {
    "email": ["email", "e-mail", "mail", "email address", "e mail"],
    "full_name": ["name", "full name", "full_name", "professor", "contact"],
    "title": ["title", "position", "rank"],
    "institution": ["institution", "university", "university/institution", "affiliation", "org", "organization"],
    "department": ["department", "dept", "faculty"],
    "country": ["country", "nation", "region"],
    "research_summary": ["research", "interests", "research interests", "summary", "research_summary"],
    "recent_papers": ["papers", "recent papers", "publications", "recent_papers"],
    "profile_url": ["url", "homepage", "profile", "website", "profile_url"],
    "groups": ["group", "groups", "tag", "tags"],
}

COUNTRY_TZ: dict[str, str] = {
    "china": "Asia/Shanghai",
    "cn": "Asia/Shanghai",
    "prc": "Asia/Shanghai",
    "united states": "America/New_York",
    "usa": "America/New_York",
    "us": "America/New_York",
    "united kingdom": "Europe/London",
    "uk": "Europe/London",
    "germany": "Europe/Berlin",
    "france": "Europe/Paris",
    "japan": "Asia/Tokyo",
    "korea": "Asia/Seoul",
    "south korea": "Asia/Seoul",
    "india": "Asia/Kolkata",
    "australia": "Australia/Sydney",
    "canada": "America/Toronto",
    "singapore": "Asia/Singapore",
    "hong kong": "Asia/Hong_Kong",
}


@dataclass
class ImportReport:
    imported: int = 0
    updated: int = 0
    skipped_duplicate: int = 0
    invalid: int = 0
    rejects: list[dict[str, Any]] = field(default_factory=list)


def suggest_timezone(country: str) -> Optional[str]:
    """Map country name to a common IANA timezone."""
    if not country:
        return None
    return COUNTRY_TZ.get(country.strip().lower())


def guess_mapping(headers: list[str]) -> dict[str, str]:
    """Auto-guess column mapping from header names → field keys."""
    mapping: dict[str, str] = {}
    used: set[str] = set()
    normalized = [(h, re.sub(r"\s+", " ", str(h or "").strip().lower())) for h in headers]
    for field_key, aliases in HEADER_ALIASES.items():
        for orig, norm in normalized:
            if orig in used:
                continue
            if norm in aliases or any(a in norm for a in aliases):
                mapping[field_key] = orig
                used.add(orig)
                break
    return mapping


def _read_txt(raw: bytes) -> pd.DataFrame:
    text = raw.decode("utf-8-sig", errors="replace")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    rows: list[dict[str, str]] = []
    for ln in lines:
        # name <email>
        m = re.match(r"^(.+?)\s*<([^>]+)>$", ln)
        if m:
            rows.append({"full_name": m.group(1).strip(), "email": m.group(2).strip()})
            continue
        if "\t" in ln:
            parts = ln.split("\t")
            if len(parts) >= 2:
                rows.append({"full_name": parts[0].strip(), "email": parts[1].strip()})
            else:
                rows.append({"email": parts[0].strip()})
            continue
        if "@" in ln:
            rows.append({"email": ln.strip()})
    return pd.DataFrame(rows)


def _detect_header_row(df_raw: pd.DataFrame) -> int:
    """Find a header row within the first 5 rows (handles header on row 2)."""
    for i in range(min(5, len(df_raw))):
        vals = [str(v).strip().lower() for v in list(df_raw.iloc[i].values)]
        joined = " ".join(vals)
        if any(a in joined for a in ("email", "e-mail", "mail", "name")):
            return i
    return 0


def load_tabular(
    data: bytes,
    filename: str,
    sheet: str | int | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """
    Load CSV/XLSX/TXT into a DataFrame.
    Returns (df, sheet_names) — sheet_names empty unless XLSX.
    """
    name = filename.lower()
    if name.endswith(".txt"):
        return _read_txt(data), []
    if name.endswith((".xlsx", ".xls")):
        xl = pd.ExcelFile(io.BytesIO(data))
        sheets = list(xl.sheet_names)
        chosen = sheet if sheet is not None else sheets[0]
        raw = pd.read_excel(xl, sheet_name=chosen, header=None, dtype=str)
        raw = raw.fillna("")
        hdr = _detect_header_row(raw)
        headers = [str(c).strip() for c in list(raw.iloc[hdr].values)]
        df = raw.iloc[hdr + 1 :].copy()
        df.columns = headers
        df = df.reset_index(drop=True)
        return df, sheets
    # CSV — utf-8-sig, detect delimiter, pad ragged rows (header may be on row 2)
    sample = data[:4096].decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        sep = dialect.delimiter
    except csv.Error:
        sep = ";" if sample.count(";") > sample.count(",") else ","
    text = data.decode("utf-8-sig", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    split_rows = [next(csv.reader([ln], delimiter=sep)) for ln in lines]
    width = max((len(r) for r in split_rows), default=0)
    padded = [r + [""] * (width - len(r)) for r in split_rows]
    df_raw = pd.DataFrame(padded).fillna("")
    hdr = _detect_header_row(df_raw)
    headers = [str(c).strip() for c in list(df_raw.iloc[hdr].values)]
    df = df_raw.iloc[hdr + 1 :].copy()
    df.columns = headers
    df = df.reset_index(drop=True)
    return df, []


def normalize_email(value: str) -> tuple[Optional[str], Optional[str]]:
    """Validate syntax only; return (email, error)."""
    raw = (value or "").strip().lower()
    if not raw:
        return None, "empty email"
    # extract from Name <email>
    m = re.match(r"^.*<([^>]+)>$", raw)
    if m:
        raw = m.group(1).strip().lower()
    try:
        info = validate_email(raw, check_deliverability=False)
        return info.normalized, None
    except EmailNotValidError as exc:
        return None, str(exc)


def apply_mapping(df: pd.DataFrame, mapping: dict[str, str]) -> list[dict[str, Any]]:
    """Map dataframe rows to contact dicts using column mapping."""
    rows: list[dict[str, Any]] = []
    for _, series in df.iterrows():
        item: dict[str, Any] = {}
        for field_key, col in mapping.items():
            if col not in df.columns:
                continue
            val = series.get(col, "")
            item[field_key] = "" if pd.isna(val) else str(val).strip()
        rows.append(item)
    return rows


def merge_contact(
    existing: dict[str, Any],
    incoming: dict[str, Any],
    policy: str,
) -> tuple[dict[str, Any], str]:
    """
    Apply duplicate policy: skip | fill-empty | overwrite.
    Returns (merged_or_existing, action).
    """
    if policy == "skip":
        return existing, "skipped_duplicate"
    if policy == "overwrite":
        merged = {**existing, **{k: v for k, v in incoming.items() if v not in (None, "")}}
        return merged, "updated"
    # fill-empty
    merged = dict(existing)
    changed = False
    for k, v in incoming.items():
        if v in (None, ""):
            continue
        if not merged.get(k):
            merged[k] = v
            changed = True
    return merged, ("updated" if changed else "skipped_duplicate")


def prepare_contacts(
    rows: list[dict[str, Any]],
    *,
    groups: list[str] | None = None,
    region_type: str = "other",
    source_file: str = "",
    existing_by_email: dict[str, dict[str, Any]] | None = None,
    policy: str = "fill-empty",
) -> tuple[list[dict[str, Any]], ImportReport]:
    """Normalise, validate, dedupe; return (upserts, report)."""
    existing_by_email = existing_by_email or {}
    report = ImportReport()
    seen_in_file: set[str] = set()
    upserts: list[dict[str, Any]] = []

    for row in rows:
        email, err = normalize_email(str(row.get("email", "")))
        if err or not email:
            report.invalid += 1
            report.rejects.append({**row, "error": err or "invalid"})
            continue
        if email in seen_in_file:
            report.skipped_duplicate += 1
            report.rejects.append({**row, "email": email, "error": "duplicate_in_file"})
            continue
        seen_in_file.add(email)
        item = {
            "email": email,
            "full_name": row.get("full_name", ""),
            "title": row.get("title", ""),
            "institution": row.get("institution", ""),
            "department": row.get("department", ""),
            "country": row.get("country", ""),
            "timezone": suggest_timezone(str(row.get("country", ""))) or row.get("timezone"),
            "research_summary": row.get("research_summary", ""),
            "recent_papers": row.get("recent_papers", ""),
            "profile_url": row.get("profile_url", ""),
            "groups": groups or _parse_groups(row.get("groups")),
            "region_type": region_type,
            "source_file": source_file,
        }
        if email in existing_by_email:
            merged, action = merge_contact(existing_by_email[email], item, policy)
            if action == "skipped_duplicate":
                report.skipped_duplicate += 1
            elif action == "updated":
                report.updated += 1
                upserts.append(merged)
            continue
        report.imported += 1
        upserts.append(item)
    return upserts, report


def _parse_groups(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [p.strip() for p in re.split(r"[,;|]", str(value)) if p.strip()]


def rejects_to_csv(rejects: list[dict[str, Any]]) -> str:
    """Serialize rejects to CSV string."""
    if not rejects:
        return "email,error\n"
    fieldnames = sorted({k for r in rejects for k in r.keys()})
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rejects)
    return buf.getvalue()
