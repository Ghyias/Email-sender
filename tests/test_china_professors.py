"""China professors CSV normalizer + import detection."""

from __future__ import annotations

import io

import pandas as pd

from app.importers.china_professors import (
    looks_like_china_professor_csv,
    normalize_professor_dataframe,
    preferred_display_name,
)
from app.importers.tabular import clean_field, guess_mapping, prepare_contacts
from app.llm.workflow_coach import heuristic_advice

SAMPLE_HEADERS = [
    "full_name",
    "university_en",
    "university_cn",
    "department",
    "professor_name_cn",
    "professor_name_en",
    "title",
    "email",
    "email_source_url",
    "research_direction",
    "verification_status",
    "notes",
]


def test_detect_china_schema() -> None:
    assert looks_like_china_professor_csv(SAMPLE_HEADERS)
    assert not looks_like_china_professor_csv(["email", "name", "company"])


def test_preferred_english_name() -> None:
    assert preferred_display_name(
        {"professor_name_en": "Li Ming", "professor_name_cn": "李明", "full_name": "李明"}
    ) == "Li Ming"
    assert preferred_display_name(
        {"professor_name_en": "", "full_name": "贾黎明"}
    ) == "贾黎明"


def test_normalize_sets_china_defaults_and_batches() -> None:
    df = pd.DataFrame(
        [
            {
                "full_name": "贾黎明",
                "university_en": "Beijing Forestry University",
                "university_cn": "北京林业大学",
                "department": "Forestry",
                "professor_name_cn": "贾黎明",
                "professor_name_en": "",
                "title": "not stated",
                "email": "jlm@bjfu.edu.cn",
                "email_source_url": "https://example.com",
                "research_direction": "not stated",
                "verification_status": "email_verified",
                "notes": "ok",
            },
            {
                "full_name": "Ada",
                "university_en": "Tsinghua University",
                "university_cn": "清华",
                "department": "CS",
                "professor_name_cn": "艾达",
                "professor_name_en": "Ada Lovelace",
                "title": "Professor",
                "email": "ada@tsinghua.edu.cn",
                "email_source_url": "https://example.com/ada",
                "research_direction": "plant genomics",
                "verification_status": "email_verified",
                "notes": "",
            },
        ]
    )
    ready, qa = normalize_professor_dataframe(df, split_batches=True)
    assert qa["output_rows"] == 2
    assert qa["missing_english_name"] == 1
    assert qa["missing_research"] == 1
    assert qa["missing_title"] == 1
    row0 = ready.iloc[0].to_dict()
    assert row0["country"] == "China"
    assert row0["region_type"] == "china"
    assert row0["title"] == "Professor"
    assert "needs_english_name" in row0["groups"]
    assert "needs_research" in row0["groups"]
    assert "usama_batch" in row0["groups"] or "faizan_batch" in row0["groups"]
    assert ready.iloc[1]["full_name"] == "Ada Lovelace"


def test_guess_mapping_prefers_en_columns() -> None:
    mapping = guess_mapping(SAMPLE_HEADERS)
    assert mapping["email"] == "email"
    assert mapping["full_name"] == "professor_name_en"
    assert mapping["institution"] == "university_en"
    assert mapping["research_summary"] == "research_direction"
    assert mapping["profile_url"] == "email_source_url"


def test_prepare_contacts_merges_groups_and_cleans() -> None:
    rows = [
        {
            "email": "a@x.edu.cn",
            "full_name": "A",
            "title": "not stated",
            "country": "",
            "research_summary": "N/A",
            "groups": "usama_batch",
            "region_type": "china",
            "notes": "CN name: 甲",
        }
    ]
    upserts, report = prepare_contacts(rows, groups=["professor", "china"], region_type="other")
    assert report.imported == 1
    item = upserts[0]
    assert item["region_type"] == "china"
    assert item["country"] == "China"
    assert item["title"] == "Professor"
    assert item["research_summary"] == ""
    assert item["groups"] == ["professor", "china", "usama_batch"]
    assert item["notes"] == "CN name: 甲"
    assert clean_field("not stated") == ""


def test_heuristic_coach_prioritizes_gaps() -> None:
    state = {
        "checklist": [
            {"id": "ollama", "label": "Ollama", "done": False, "href": "/settings"},
            {"id": "accounts", "label": "Accounts", "done": True, "href": "/accounts"},
            {"id": "contacts", "label": "Contacts", "done": False, "href": "/contacts"},
        ],
        "ollama": {"ok": False, "error": "connection refused", "pull_commands": ["ollama pull qwen3.5:4b"]},
        "settings": {"dry_run": True},
    }
    advice = heuristic_advice(state)
    assert advice["source"] == "heuristic"
    assert any("settings" in s.lower() or "Ollama" in s for s in advice["next_steps"])
    assert advice["warnings"]


def test_normalized_csv_roundtrip_bytes() -> None:
    """Prepared CSV loads and maps like a normal MailPilot import file."""
    df = pd.DataFrame(
        [
            {
                "full_name": "X",
                "university_en": "U",
                "university_cn": "U",
                "department": "D",
                "professor_name_cn": "X",
                "professor_name_en": "Prof X",
                "title": "Professor",
                "email": "x@u.edu.cn",
                "email_source_url": "https://u.edu.cn/x",
                "research_direction": "ecology",
                "verification_status": "email_verified",
                "notes": "",
            }
        ]
    )
    ready, _ = normalize_professor_dataframe(df)
    buf = io.StringIO()
    ready.to_csv(buf, index=False)
    mapping = guess_mapping(list(ready.columns))
    assert mapping["region_type"] == "region_type"
    assert mapping["groups"] == "groups"
