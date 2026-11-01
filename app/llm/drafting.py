"""Draft generation + post-generation validators."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz

from app.llm.ollama_client import OllamaClientProtocol, get_ollama_client
from app.llm.prompts import DRAFT_SCHEMA, build_draft_messages

logger = logging.getLogger(__name__)

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
PLACEHOLDER_RE = re.compile(r"(\[Name\]|\{\{|<insert)", re.IGNORECASE)
QUOTED_RE = re.compile(r'[""]([^""]{3,})[""]|[""]([^""]{3,})[""]')


@dataclass
class DraftResult:
    subject: str
    body: str
    referenced_items: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    prompt_hash: str = ""
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def _word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text or ""))


def validate_draft(
    *,
    subject: str,
    body: str,
    referenced_items: list[str],
    contact: dict[str, Any],
    forbidden_phrases: list[str],
    max_words: int,
    language: str,
) -> list[str]:
    """Run all 7 validators; return list of flag strings."""
    flags: list[str] = []
    text = f"{subject}\n{body}"

    # 1. Forbidden phrase
    for phrase in forbidden_phrases or []:
        if phrase and re.search(re.escape(phrase), text, flags=re.IGNORECASE):
            flags.append(f"forbidden:{phrase}")

    # 2. Word count
    wc = _word_count(body)
    if wc > max_words * 1.15:
        flags.append(f"too_long:{wc}")
    if wc < 60:
        flags.append(f"too_short:{wc}")

    # 3. referenced_items must appear in research data (fuzzy ≥ 0.85)
    research_blob = f"{contact.get('research_summary', '')}\n{contact.get('recent_papers', '')}"
    for item in referenced_items or []:
        if not item:
            continue
        score = fuzz.partial_ratio(item.lower(), research_blob.lower()) / 100.0
        if score < 0.85:
            flags.append(f"invented_reference:{item}")

    # 4. Quoted titles not in contact data
    for m in QUOTED_RE.finditer(body or ""):
        title = m.group(1) or m.group(2) or ""
        if title and fuzz.partial_ratio(title.lower(), research_blob.lower()) / 100.0 < 0.85:
            flags.append(f"hallucinated_quote:{title}")

    # 5. Name / institution match
    name = (contact.get("full_name") or "").strip()
    institution = (contact.get("institution") or "").strip()
    if name:
        # allow last name only
        parts = name.split()
        if not any(fuzz.partial_ratio(p.lower(), body.lower()) >= 85 for p in parts if len(p) > 1):
            flags.append("name_mismatch")
    if institution and fuzz.partial_ratio(institution.lower(), body.lower()) < 70:
        # soft — only flag if institution is reasonably long
        if len(institution) >= 4 and institution.lower() not in body.lower():
            flags.append("institution_mismatch")

    # 6. Placeholder leftovers
    if PLACEHOLDER_RE.search(text):
        flags.append("placeholder_leftover")

    # 7. Language check
    if body:
        cjk = len(CJK_RE.findall(body))
        ratio = cjk / max(len(body), 1)
        if language == "en" and ratio >= 0.02:
            flags.append("language_cjk_in_en")
        if language == "zh" and ratio < 0.02:
            flags.append("language_not_zh")

    return flags


HARD_FLAGS = ("forbidden:", "placeholder_leftover")


def _is_hard(flags: list[str]) -> bool:
    return any(f.startswith("forbidden:") or f == "placeholder_leftover" for f in flags)


def generate_draft(
    *,
    contact: dict[str, Any],
    llm_instructions: str,
    allowed_facts: list[str],
    forbidden_phrases: list[str],
    region_type: str,
    language: str,
    max_words: int,
    model: str,
    temperature: float = 0.4,
    client: OllamaClientProtocol | None = None,
    rewrite_instruction: str | None = None,
) -> DraftResult:
    """Generate a draft via Ollama and validate; auto-retry once on hard failures."""
    client = client or get_ollama_client()
    messages = build_draft_messages(
        llm_instructions=llm_instructions,
        allowed_facts=allowed_facts,
        forbidden=forbidden_phrases,
        contact=contact,
        region_type=region_type,
        language=language,
        max_words=max_words,
        rewrite_instruction=rewrite_instruction,
    )
    prompt_hash = hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()[:16]

    def _once(msgs: list[dict[str, str]]) -> DraftResult:
        data = client.chat(model, msgs, format_schema=DRAFT_SCHEMA, temperature=temperature)
        content = data.get("message", {}).get("content", "{}")
        if isinstance(content, dict):
            parsed = content
        else:
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                parsed = {"subject": "", "body": content, "referenced_items": []}
        subject = str(parsed.get("subject") or "")
        body = str(parsed.get("body") or "")
        refs = list(parsed.get("referenced_items") or [])
        flags = validate_draft(
            subject=subject,
            body=body,
            referenced_items=refs,
            contact=contact,
            forbidden_phrases=forbidden_phrases,
            max_words=max_words,
            language=language,
        )
        return DraftResult(
            subject=subject,
            body=body,
            referenced_items=refs,
            flags=flags,
            prompt_hash=prompt_hash,
            model=model,
            raw=parsed,
        )

    result = _once(messages)
    if _is_hard(result.flags):
        violation = ", ".join(result.flags)
        retry_msgs = build_draft_messages(
            llm_instructions=llm_instructions,
            allowed_facts=allowed_facts,
            forbidden=forbidden_phrases,
            contact=contact,
            region_type=region_type,
            language=language,
            max_words=max_words,
            retry_violation=violation,
            rewrite_instruction=rewrite_instruction,
        )
        result = _once(retry_msgs)
    return result
