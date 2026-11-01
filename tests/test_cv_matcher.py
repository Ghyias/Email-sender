"""CV matcher decision logic with stubbed scores."""

from __future__ import annotations

from app.llm.cv_matcher import CvCandidate, accuracy_report, decide_match


def _cands() -> list[CvCandidate]:
    return [
        CvCandidate(key="ml", description="machine learning", embedding=[1.0, 0.0, 0.0]),
        CvCandidate(key="medical", description="medical imaging", embedding=[0.0, 1.0, 0.0]),
        CvCandidate(key="general", description="general CV", embedding=[0.2, 0.2, 0.2]),
    ]


def test_rule_override() -> None:
    r = decide_match(
        research_text="anything",
        contact_groups=["medical imaging"],
        candidates=_cands(),
        query_embedding=[0, 1, 0],
        rules={"medical imaging": "medical"},
    )
    assert r.cv_key == "medical"
    assert r.confidence == 1.0
    assert r.method == "rule"


def test_embedding_only() -> None:
    r = decide_match(
        research_text="lots of words about medical imaging MRI CT scan radiology hospital clinic research methods and datasets " * 2,
        contact_groups=[],
        candidates=_cands(),
        query_embedding=[0.0, 1.0, 0.0],
        llm_pick=None,
    )
    assert r.cv_key == "medical"
    assert r.method == "embedding"


def test_disagreement_needs_review() -> None:
    r = decide_match(
        research_text="lots of words about machine learning neural networks deep learning transformers attention " * 3,
        contact_groups=[],
        candidates=_cands(),
        query_embedding=[1.0, 0.0, 0.0],  # emb top = ml
        llm_pick={"cv_key": "medical", "reason": "maybe medical", "confidence": 0.9},
    )
    assert r.needs_review is True


def test_short_evidence_falls_back_to_general() -> None:
    r = decide_match(
        research_text="AI stuff",
        contact_groups=[],
        candidates=_cands(),
        query_embedding=[1.0, 0.0, 0.0],
        llm_pick={"cv_key": "ml", "reason": "x", "confidence": 0.9},
    )
    assert r.cv_key == "general"
    assert r.needs_review is True


def test_accuracy_report() -> None:
    labels = [("1", "ml"), ("2", "medical")]
    preds = {
        "embedding": {"1": "ml", "2": "ml"},
        "hybrid": {"1": "ml", "2": "medical"},
    }
    rep = accuracy_report(labels, preds)
    assert rep["embedding"] == 0.5
    assert rep["hybrid"] == 1.0
