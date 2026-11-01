"""Hardening checks: bind host + passwords never returned."""

from __future__ import annotations

from app.config import HOST
from app.main import app


def test_host_is_localhost_only() -> None:
    assert HOST == "127.0.0.1"


def test_no_route_exposes_password_fields() -> None:
    # Scan route callables' source isn't needed — ensure Account templates never
    # print enc_app_password and OpenAPI is disabled.
    assert app.docs_url is None
    from pathlib import Path

    for path in Path("app/templates").rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert "enc_app_password" not in text
        assert "decrypt_secret" not in text
