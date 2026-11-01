"""Fernet encryption with OS keyring for the master key."""

from __future__ import annotations

import logging
import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

logger = logging.getLogger(__name__)

# Fallback file used only when keyring backend is unavailable (headless CI / Linux).
_FALLBACK_KEY_ENV = "MAILPILOT_FERNET_KEY"


def _load_or_create_key() -> bytes:
    """Load Fernet key from keyring, env, or create and store a new one."""
    settings = get_settings()
    service = settings.keyring_service
    username = "fernet_key"

    # Prefer explicit env (tests / CI).
    env_key = os.environ.get(_FALLBACK_KEY_ENV)
    if env_key:
        return env_key.encode("utf-8") if not env_key.startswith("gAAAA") else env_key.encode("utf-8")

    try:
        import keyring

        stored = keyring.get_password(service, username)
        if stored:
            return stored.encode("utf-8")
        key = Fernet.generate_key()
        keyring.set_password(service, username, key.decode("utf-8"))
        return key
    except Exception as exc:  # noqa: BLE001 — keyring backends vary widely
        logger.warning("keyring unavailable (%s); using file-backed Fernet key", exc)
        from app.config import DATA_DIR

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        key_path = DATA_DIR / ".fernet_key"
        if key_path.exists():
            return key_path.read_bytes().strip()
        key = Fernet.generate_key()
        key_path.write_bytes(key)
        try:
            key_path.chmod(0o600)
        except OSError:
            pass
        return key


@lru_cache
def _fernet() -> Fernet:
    raw = _load_or_create_key()
    # Env may hold the raw Fernet key string.
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    # If someone put a non-Fernet string in env, generate from it improperly —
    # require a valid Fernet key; otherwise regenerate via keyring path.
    try:
        return Fernet(raw)
    except (ValueError, Exception):
        key = Fernet.generate_key()
        return Fernet(key)


def reset_fernet_cache() -> None:
    """Clear cached Fernet instance (for tests)."""
    _fernet.cache_clear()


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret string; returns url-safe base64 ciphertext."""
    if not plaintext:
        raise ValueError("cannot encrypt empty secret")
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a Fernet ciphertext string."""
    if not ciphertext:
        raise ValueError("cannot decrypt empty ciphertext")
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("invalid ciphertext") from exc


def normalize_app_password(raw: str) -> str:
    """Strip spaces from a Gmail app password."""
    return "".join(raw.split())
