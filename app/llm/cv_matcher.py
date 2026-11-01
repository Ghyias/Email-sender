"""Hybrid CV selection: rules → embedding top-3 → LLM rerank → uncertainty check."""

from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass, field
from typing import Any, Optional

from app.llm.ollama_client import OllamaClientProtocol, get_ollama_client
from app.llm.prompts import CV_RERANK_SCHEMA, build_cv_rerank_messages


def pack_embedding(vec: list[float]) -> bytes:
    """Pack float list to bytes."""
    return struct.pack(f"{len(vec)}f", *vec)


def unpack_embedding(blob: bytes | None) -> list[float]:
    """Unpack bytes to float list."""
    if not blob:
        return []
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


@dataclass
class CvCandidate:
    key: str
    description: str
    keywords: list[str] = field(default_factory=list)
    embedding: list[float] = field(default_factory=list)


@dataclass
class MatchResult:
    cv_key: str
    reason: str
    confidence: float
    needs_review: bool
    scores: dict[str, float] = field(default_factory=dict)
    method: str = "hybrid"


def apply_rule_override(
    contact_groups: list[str],
    contact_text: str,
    rules: dict[str, str],
) -> Optional[MatchResult]:
    """
    Rule override: mapping from group/keyword → cv_key.
    rules e.g. {"medical imaging": "medical", "professor:ml": "ml"}
    """
    lowered_text = (contact_text or "").lower()
    groups = [g.lower() for g in (contact_groups or [])]
    for needle, cv_key in (rules or {}).items():
        n = needle.lower()
        if n in groups or n in lowered_text:
            return MatchResult(
                cv_key=cv_key,
                reason="rule",
                confidence=1.0,
                needs_review=False,
                scores={cv_key: 1.0},
                method="rule",
            )
    return None


def embedding_topk(
    query_vec: list[float],
    candidates: list[CvCandidate],
    k: int = 3,
) -> list[tuple[CvCandidate, float]]:
    """Return top-k candidates by cosine similarity."""
    scored = [(c, cosine(query_vec, c.embedding)) for c in candidates]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:k]


def decide_match(
    *,
    research_text: str,
    contact_groups: list[str],
    candidates: list[CvCandidate],
    query_embedding: list[float],
    llm_pick: Optional[dict[str, Any]] = None,
    rules: Optional[dict[str, str]] = None,
    general_key: str = "general",
) -> MatchResult:
    """
    Pure decision logic (testable with stubbed scores / llm_pick).
    """
    rule = apply_rule_override(contact_groups, research_text, rules or {})
    if rule:
        return rule

    top = embedding_topk(query_embedding, candidates, k=3)
    scores = {c.key: float(s) for c, s in top}
    if not top:
        return MatchResult(
            cv_key=general_key,
            reason="no candidates",
            confidence=0.0,
            needs_review=True,
            scores={},
            method="fallback",
        )

    emb_top_key, emb_top_score = top[0][0].key, top[0][1]
    short_evidence = len((research_text or "").split()) < 30

    if llm_pick is None:
        needs = short_evidence or emb_top_score < 0.6
        key = general_key if short_evidence else emb_top_key
        return MatchResult(
            cv_key=key,
            reason="embedding-only",
            confidence=float(emb_top_score),
            needs_review=needs,
            scores=scores,
            method="embedding",
        )

    llm_key = str(llm_pick.get("cv_key") or emb_top_key)
    confidence = float(llm_pick.get("confidence") or 0.0)
    reason = str(llm_pick.get("reason") or "")

    # margin of emb top-1 over the LLM pick
    llm_emb_score = scores.get(llm_key, 0.0)
    margin = emb_top_score - llm_emb_score
    disagree = llm_key != emb_top_key and margin > 0.05
    needs_review = disagree or confidence < 0.6 or short_evidence

    if short_evidence:
        return MatchResult(
            cv_key=general_key,
            reason="insufficient research text; fell back to general",
            confidence=confidence,
            needs_review=True,
            scores=scores,
            method="hybrid",
        )

    return MatchResult(
        cv_key=llm_key if llm_key in {c.key for c in candidates} else emb_top_key,
        reason=reason,
        confidence=confidence,
        needs_review=needs_review,
        scores=scores,
        method="hybrid",
    )


def match_cv(
    *,
    research_text: str,
    contact_groups: list[str],
    candidates: list[CvCandidate],
    embedding_model: str,
    drafting_ignored: str | None = None,
    rules: Optional[dict[str, str]] = None,
    client: OllamaClientProtocol | None = None,
    general_key: str = "general",
) -> MatchResult:
    """Full hybrid pipeline with live/fake Ollama client."""
    client = client or get_ollama_client()
    rule = apply_rule_override(contact_groups, research_text, rules or {})
    if rule:
        return rule

    vectors = client.embed(embedding_model, [research_text or ""])
    query_vec = vectors[0] if vectors else []
    # ensure candidates have embeddings; compute missing
    missing = [c for c in candidates if not c.embedding]
    if missing:
        texts = [f"{c.key} {c.description} {' '.join(c.keywords)}" for c in missing]
        emb = client.embed(embedding_model, texts)
        for c, v in zip(missing, emb):
            c.embedding = v

    top = embedding_topk(query_vec, candidates, k=3)
    llm_pick = None
    if top:
        cand_payload = [{"key": c.key, "description": c.description} for c, _ in top]
        messages = build_cv_rerank_messages(research_text, cand_payload)
        # reuse a drafting-capable model name from caller via health — pass as embedding for tests
        # Prefer chat model; callers should set client appropriately.
        data = client.chat(
            embedding_model if False else (getattr(client, "chat_model", None) or "draft-model"),
            messages,
            format_schema=CV_RERANK_SCHEMA,
            temperature=0.2,
        )
        # Fake client ignores model name
        content = data.get("message", {}).get("content", "{}")
        if isinstance(content, dict):
            llm_pick = content
        else:
            try:
                llm_pick = json.loads(content)
            except json.JSONDecodeError:
                llm_pick = None

    return decide_match(
        research_text=research_text,
        contact_groups=contact_groups,
        candidates=candidates,
        query_embedding=query_vec,
        llm_pick=llm_pick,
        rules=None,  # already applied
        general_key=general_key,
    )


def accuracy_report(
    labels: list[tuple[str, str]],
    predictions: dict[str, dict[str, str]],
) -> dict[str, float]:
    """
    labels: list of (contact_key, true_cv_key)
    predictions: {"embedding": {contact_key: cv_key}, "llm": ..., "hybrid": ...}
    """
    out: dict[str, float] = {}
    if not labels:
        return {k: 0.0 for k in predictions}
    for method, preds in predictions.items():
        correct = sum(1 for ck, truth in labels if preds.get(ck) == truth)
        out[method] = correct / len(labels)
    return out
