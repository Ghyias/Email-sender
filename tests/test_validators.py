"""Draft validator unit tests."""

from __future__ import annotations

from app.llm.drafting import validate_draft

CONTACT = {
    "full_name": "Dr. Alice Smith",
    "institution": "MIT",
    "research_summary": "diffusion models for MRI reconstruction",
    "recent_papers": "A paper on diffusion models for MRI",
}


def test_forbidden_gpa() -> None:
    flags = validate_draft(
        subject="Hi",
        body="My GPA is 4.0 and I study diffusion models for MRI at MIT with Dr. Alice Smith. "
        + ("word " * 70),
        referenced_items=["diffusion models for MRI"],
        contact=CONTACT,
        forbidden_phrases=["GPA", "CGPA"],
        max_words=250,
        language="en",
    )
    assert any(f.startswith("forbidden:GPA") for f in flags)


def test_invented_paper() -> None:
    flags = validate_draft(
        subject="Hi",
        body="Dear Dr. Alice Smith, I read your work on quantum banana farming at MIT. "
        + ("word " * 70),
        referenced_items=["quantum banana farming"],
        contact=CONTACT,
        forbidden_phrases=[],
        max_words=250,
        language="en",
    )
    assert any(f.startswith("invented_reference:") for f in flags)


def test_placeholder() -> None:
    flags = validate_draft(
        subject="Hi",
        body="Dear [Name], please see my CV. " + ("word " * 70),
        referenced_items=[],
        contact=CONTACT,
        forbidden_phrases=[],
        max_words=250,
        language="en",
    )
    assert "placeholder_leftover" in flags


def test_too_short() -> None:
    flags = validate_draft(
        subject="Hi",
        body="Short.",
        referenced_items=[],
        contact=CONTACT,
        forbidden_phrases=[],
        max_words=250,
        language="en",
    )
    assert any(f.startswith("too_short:") for f in flags)
