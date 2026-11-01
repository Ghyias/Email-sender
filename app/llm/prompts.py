"""All LLM prompt templates in one place."""

from __future__ import annotations

from typing import Any

DRAFT_SYSTEM = """You write email drafts as the user in first person.
Rules:
- Plain professional tone. No flattery clichés ("I am thrilled", "esteemed", "groundbreaking").
- Use ONLY facts from ALLOWED_FACTS about the user. Never invent achievements, grades, or publications.
- Mention 1–2 specific items from the professor's research data; do not invent paper titles.
- If research_summary is empty, keep the personal paragraph short and refer only to the institution/field — do not invent topics.
- Never mention items in FORBIDDEN.
- Respect max_words; one clear ask; say the CV is attached.
- For region_type=china: shorter sentences, simple vocabulary, mention CSC scholarship plan if the template says so.
  Address as Professor + family/full name from Contact name; many contacts are China university faculty.
- Output JSON only matching the schema.
"""

FEW_SHOT_WESTERN = {
    "role": "user",
    "content": (
        "ALLOWED_FACTS: ['MSc Computer Vision', 'worked on medical image segmentation']\n"
        "FORBIDDEN: ['GPA','CGPA']\n"
        "Contact: Dr. Smith, MIT, research: diffusion models for MRI\n"
        "region_type: western, language: en, max_words: 200\n"
        "Write the draft JSON."
    ),
}
FEW_SHOT_WESTERN_ASSISTANT = {
    "role": "assistant",
    "content": (
        '{"subject":"PhD enquiry — medical imaging","body":"Dear Dr. Smith,\\n\\n'
        "I recently completed an MSc focused on computer vision and medical image segmentation. "
        "Your work on diffusion models for MRI aligns closely with my interests, and I would "
        "welcome the chance to discuss PhD opportunities. My CV is attached.\\n\\nKind regards\","
        '"referenced_items":["diffusion models for MRI"]}'
    ),
}

FEW_SHOT_CHINA = {
    "role": "user",
    "content": (
        "ALLOWED_FACTS: ['BEng Electrical Engineering', 'CSC scholarship applicant']\n"
        "FORBIDDEN: ['GPA','CGPA']\n"
        "Contact: Prof. Wang, Tsinghua, research: graph neural networks for molecules\n"
        "region_type: china, language: en, max_words: 150\n"
        "Write the draft JSON."
    ),
}
FEW_SHOT_CHINA_ASSISTANT = {
    "role": "assistant",
    "content": (
        '{"subject":"PhD application enquiry","body":"Dear Professor Wang,\\n\\n'
        "I studied electrical engineering and now focus on machine learning. "
        "I am interested in your research on graph neural networks for molecules. "
        "I plan to apply for a CSC scholarship. My CV is attached.\\n\\nBest regards\","
        '"referenced_items":["graph neural networks for molecules"]}'
    ),
}

DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "subject": {"type": "string"},
        "body": {"type": "string"},
        "referenced_items": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["subject", "body", "referenced_items"],
}

CV_RERANK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cv_key": {"type": "string"},
        "reason": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["cv_key", "reason", "confidence"],
}

CV_SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"description": {"type": "string"}},
    "required": ["description"],
}


def build_draft_messages(
    *,
    llm_instructions: str,
    allowed_facts: list[str],
    forbidden: list[str],
    contact: dict[str, Any],
    region_type: str,
    language: str,
    max_words: int,
    retry_violation: str | None = None,
    rewrite_instruction: str | None = None,
) -> list[dict[str, str]]:
    """Assemble chat messages for draft generation."""
    user_blob = (
        f"Instructions: {llm_instructions}\n"
        f"ALLOWED_FACTS: {allowed_facts}\n"
        f"FORBIDDEN: {forbidden}\n"
        f"Contact name: {contact.get('full_name','')}\n"
        f"Title: {contact.get('title','')}\n"
        f"Institution: {contact.get('institution','')}\n"
        f"Research summary: {contact.get('research_summary','')}\n"
        f"Recent papers: {contact.get('recent_papers','')}\n"
        f"region_type: {region_type}\n"
        f"language: {language}\n"
        f"max_words: {max_words}\n"
    )
    if retry_violation:
        user_blob += f"\nPrevious draft violated: {retry_violation}. Fix it.\n"
    if rewrite_instruction:
        user_blob += f"\nRewrite request: {rewrite_instruction}\n"
    user_blob += "Write the draft JSON."
    return [
        {"role": "system", "content": DRAFT_SYSTEM},
        FEW_SHOT_WESTERN,
        FEW_SHOT_WESTERN_ASSISTANT,
        FEW_SHOT_CHINA,
        FEW_SHOT_CHINA_ASSISTANT,
        {"role": "user", "content": user_blob},
    ]


def build_cv_rerank_messages(research_text: str, candidates: list[dict[str, str]]) -> list[dict[str, str]]:
    """Messages for CV LLM rerank over top-3 candidates only."""
    lines = [f"- {c['key']}: {c['description']}" for c in candidates]
    return [
        {
            "role": "system",
            "content": (
                "Pick the single best CV key for this research profile from the candidates. "
                "Reason in <= 40 words. Output JSON only."
            ),
        },
        {
            "role": "user",
            "content": f"Research:\n{research_text}\n\nCandidates:\n" + "\n".join(lines),
        },
    ]
