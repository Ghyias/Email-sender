"""Application settings and path defaults."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root (parent of app/)
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "mailpilot.db"
CVS_DIR = DATA_DIR / "cvs"
ATTACHMENTS_DIR = DATA_DIR / "attachments_cache"
LOGS_DIR = DATA_DIR / "logs"
DRYRUN_DIR = DATA_DIR / "outbox_dryrun"
BACKUP_DIR = DATA_DIR / "backups"

MAX_ACCOUNTS = 10
HARD_DAILY_CAP = 150
DEFAULT_DAILY_CAP = 30
DEFAULT_MIN_DELAY_S = 90
DEFAULT_MAX_DELAY_S = 240
HARD_MIN_DELAY_S = 30
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
WARN_ATTACHMENT_BYTES = 5 * 1024 * 1024
HOST = "127.0.0.1"
PORT = 8765


class Settings(BaseSettings):
    """Runtime settings loaded from env / defaults."""

    model_config = SettingsConfigDict(env_prefix="MAILPILOT_", extra="ignore")

    host: str = HOST
    port: int = PORT
    data_dir: Path = DATA_DIR
    db_path: Path = DB_PATH
    timezone: str = "Asia/Shanghai"
    sync_interval_minutes: int = 5
    sync_window_days: int = 365
    ollama_url: str = "http://127.0.0.1:11434"
    drafting_model: str = "qwen3.5:4b"
    embedding_model: str = "qwen3-embedding:0.6b"
    temperature: float = 0.4
    dry_run: bool = True
    default_daily_cap: int = DEFAULT_DAILY_CAP
    default_min_delay_s: int = DEFAULT_MIN_DELAY_S
    default_max_delay_s: int = DEFAULT_MAX_DELAY_S
    attachment_filename_pattern: str = "{name}_CV.pdf"
    pause_all_sending: bool = False
    session_secret: str = Field(default="")
    keyring_service: str = "mailpilot"


@lru_cache
def get_settings() -> Settings:
    """Return cached settings singleton."""
    return Settings()


def ensure_data_dirs() -> None:
    """Create runtime data directories if missing."""
    for path in (DATA_DIR, CVS_DIR, ATTACHMENTS_DIR, LOGS_DIR, DRYRUN_DIR, BACKUP_DIR):
        path.mkdir(parents=True, exist_ok=True)
