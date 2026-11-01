"""Multi-sender helpers: owner-scoped accounts, profiles, and CVs."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.llm.cv_matcher import CvCandidate, MatchResult, match_cv, unpack_embedding
from app.llm.ollama_client import OllamaClientProtocol
from app.models import Account, CvVariant, UserProfile


def normalize_owner(value: str | None) -> str:
    """Lowercase owner key; strip spaces."""
    return (value or "").strip().lower()


def profile_for_owner(session: Session, owner_key: str | None) -> Optional[UserProfile]:
    """Return the profile for an owner, falling back to the default (empty owner) row."""
    key = normalize_owner(owner_key)
    if key:
        row = session.scalar(select(UserProfile).where(UserProfile.owner_key == key).limit(1))
        if row:
            return row
    return session.scalar(
        select(UserProfile)
        .where(or_(UserProfile.owner_key == "", UserProfile.owner_key.is_(None)))
        .limit(1)
    )


def cvs_for_owner(session: Session, owner_key: str | None) -> list[CvVariant]:
    """CVs belonging to this owner. If owner unset, return all CVs."""
    key = normalize_owner(owner_key)
    cvs = list(session.scalars(select(CvVariant)).all())
    if not key:
        return cvs
    return [c for c in cvs if normalize_owner(c.owner_key) == key]


def pick_cv_for_account(
    session: Session,
    account: Account | None,
    *,
    research_text: str,
    contact_groups: list[str],
    embedding_model: str,
    rules: dict | None,
    client: OllamaClientProtocol,
) -> tuple[Optional[CvVariant], MatchResult]:
    """
    Choose a CV for the sending account:
    1. Restrict candidates to the account's owner_key.
    2. If only one CV for that owner, use it.
    3. Else run hybrid matcher on that set.
    4. Prefer account.default_cv_variant_id when match needs review / missing.
    """
    owner = normalize_owner(account.owner_key if account else "")
    owned_cvs = cvs_for_owner(session, owner) if owner else list(session.scalars(select(CvVariant)).all())

    default_cv = None
    if account and account.default_cv_variant_id:
        default_cv = session.get(CvVariant, account.default_cv_variant_id)

    if not owned_cvs:
        if default_cv:
            return default_cv, MatchResult(
                cv_key=default_cv.key,
                reason="account default CV (no owner-scoped library)",
                confidence=1.0,
                needs_review=False,
                scores={default_cv.key: 1.0},
                method="account_default",
            )
        return None, MatchResult(
            cv_key="",
            reason="no CVs available for this sender",
            confidence=0.0,
            needs_review=True,
            scores={},
            method="fallback",
        )

    if len(owned_cvs) == 1:
        only = owned_cvs[0]
        return only, MatchResult(
            cv_key=only.key,
            reason="only CV for this sender",
            confidence=1.0,
            needs_review=False,
            scores={only.key: 1.0},
            method="owner_single",
        )

    candidates = [
        CvCandidate(
            key=cv.key,
            description=cv.description or cv.label,
            keywords=list(cv.keywords or []),
            embedding=unpack_embedding(cv.embedding),
        )
        for cv in owned_cvs
    ]
    match = match_cv(
        research_text=research_text,
        contact_groups=contact_groups,
        candidates=candidates,
        embedding_model=embedding_model,
        rules=rules or {},
        client=client,
    )
    cv_by_key = {cv.key: cv for cv in owned_cvs}
    chosen = cv_by_key.get(match.cv_key)
    owned_ids = {c.id for c in owned_cvs}

    if (chosen is None or match.needs_review) and default_cv and default_cv.id in owned_ids:
        return default_cv, MatchResult(
            cv_key=default_cv.key,
            reason=f"account default (matcher: {match.reason})",
            confidence=match.confidence,
            needs_review=False,
            scores=match.scores,
            method="account_default",
        )
    return chosen, match
