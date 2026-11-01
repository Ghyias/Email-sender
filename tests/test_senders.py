"""Multi-sender owner → CV binding."""

from __future__ import annotations

from app.llm.ollama_client import FakeOllamaClient
from app.models import Account, CvVariant
from app.scheduling.senders import pick_cv_for_account
from app.security import encrypt_secret


def test_owner_single_cv_always_used(db, fake_ollama: FakeOllamaClient) -> None:
    usama_cv = CvVariant(
        key="usama_general",
        file_path="/tmp/u.pdf",
        label="Usama",
        description="general",
        owner_key="usama",
    )
    faizan_cv = CvVariant(
        key="faizan_general",
        file_path="/tmp/f.pdf",
        label="Faizan",
        description="general",
        owner_key="faizan",
    )
    db.add_all([usama_cv, faizan_cv])
    db.flush()
    acc = Account(
        email="usama@gmail.com",
        enc_app_password=encrypt_secret("abcdefghijklmnop"),
        owner_key="usama",
        default_cv_variant_id=usama_cv.id,
    )
    db.add(acc)
    db.commit()
    db.refresh(acc)

    cv, match = pick_cv_for_account(
        db,
        acc,
        research_text="plant ecology and phylogenetics of orchids in China " * 5,
        contact_groups=["professor"],
        embedding_model="fake",
        rules={},
        client=fake_ollama,
    )
    assert cv is not None
    assert cv.key == "usama_general"
    assert match.method in ("owner_single", "account_default")


def test_does_not_pick_other_owner_cv(db, fake_ollama: FakeOllamaClient) -> None:
    db.add(
        CvVariant(
            key="faizan_only",
            file_path="/tmp/f.pdf",
            label="Faizan",
            description="ecology",
            owner_key="faizan",
        )
    )
    db.flush()
    acc = Account(
        email="usama2@gmail.com",
        enc_app_password=encrypt_secret("abcdefghijklmnop"),
        owner_key="usama",
    )
    db.add(acc)
    db.commit()
    db.refresh(acc)
    cv, match = pick_cv_for_account(
        db,
        acc,
        research_text="anything long enough for embedding path " * 10,
        contact_groups=[],
        embedding_model="fake",
        rules={},
        client=fake_ollama,
    )
    assert cv is None
    assert match.needs_review is True
