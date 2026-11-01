"""Draft generation with FakeOllamaClient."""

from __future__ import annotations

import json

from app.llm.drafting import generate_draft
from app.llm.ollama_client import FakeOllamaClient


def test_generate_with_fake(fake_ollama: FakeOllamaClient) -> None:
    fake_ollama.chat_responses = [
        {
            "message": {
                "content": json.dumps(
                    {
                        "subject": "PhD enquiry",
                        "body": (
                            "Dear Dr. Alice Smith,\n\nI am interested in diffusion models for MRI "
                            "at MIT. My CV is attached.\n\n" + ("word " * 70)
                        ),
                        "referenced_items": ["diffusion models for MRI"],
                    }
                )
            },
            "eval_count": 12,
        }
    ]
    result = generate_draft(
        contact={
            "full_name": "Dr. Alice Smith",
            "institution": "MIT",
            "research_summary": "diffusion models for MRI",
            "recent_papers": "",
            "title": "Professor",
        },
        llm_instructions="Write a draft",
        allowed_facts=["MSc CV"],
        forbidden_phrases=["GPA"],
        region_type="western",
        language="en",
        max_words=250,
        model="fake",
        client=fake_ollama,
    )
    assert result.subject
    assert result.body
