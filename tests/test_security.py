"""Tests for Fernet helpers."""

from __future__ import annotations

import pytest

from app.security import decrypt_secret, encrypt_secret, normalize_app_password, reset_fernet_cache


def test_encrypt_decrypt_roundtrip() -> None:
    reset_fernet_cache()
    ct = encrypt_secret("abcd efgh ijkl mnop")
    assert "abcd" not in ct
    assert decrypt_secret(ct) == "abcd efgh ijkl mnop"


def test_normalize_app_password() -> None:
    assert normalize_app_password("abcd efgh ijkl mnop") == "abcdefghijklmnop"


def test_empty_secret_raises() -> None:
    with pytest.raises(ValueError):
        encrypt_secret("")
