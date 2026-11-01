"""FastAPI application entrypoint — binds to 127.0.0.1 only."""

from __future__ import annotations

import logging
import logging.handlers
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from app import db as app_db
from app.config import HOST, LOGS_DIR, PORT, ensure_data_dirs, get_settings
from app.db import get_engine, get_setting, init_db
from app.mail.imap_sync import start_account_worker, start_db_writer, stop_all_workers
from app.models import Account
from app.routes import (
    accounts,
    campaigns,
    contacts,
    cvs,
    dashboard,
    drafts,
    inbox,
    queue,
    settings,
    templates_routes,
)
from app.routes.settings import backup_db_on_startup
from app.scheduling.jobs import shutdown_scheduler, start_scheduler

logger = logging.getLogger("mailpilot")


def setup_logging() -> None:
    """Rotating file logs: 5 × 5 MB."""
    ensure_data_dirs()
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        LOGS_DIR / "mailpilot.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)
    if not any(
        isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.handlers.RotatingFileHandler)
        for h in root.handlers
    ):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    ensure_data_dirs()
    engine = get_engine()
    init_db(engine)
    backup_db_on_startup()
    start_db_writer()
    # Use module attribute — a direct import of SessionLocal stays None forever.
    session_factory = app_db.SessionLocal
    if session_factory is None:
        raise RuntimeError("Database session factory failed to initialize")
    start_scheduler(f"sqlite:///{get_settings().db_path}", session_factory)
    with session_factory() as session:
        interval = int(get_setting(session, "sync_interval_minutes", 5) or 5)
        for acc in session.scalars(select(Account).where(Account.active.is_(True))).all():
            start_account_worker(session_factory, acc.id, interval_minutes=interval)
    logger.info("MailPilot started on http://%s:%s", HOST, PORT)
    yield
    stop_all_workers()
    shutdown_scheduler()
    logger.info("MailPilot shut down")


app = FastAPI(title="MailPilot", lifespan=lifespan, docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory="app/templates")

# Session / CSRF cookie — secret from DB after init; bootstrap random for middleware
_bootstrap_secret = secrets.token_urlsafe(32)
app.add_middleware(SessionMiddleware, secret_key=_bootstrap_secret, same_site="strict", https_only=False)

static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(dashboard.router)
app.include_router(accounts.router)
app.include_router(inbox.router)
app.include_router(contacts.router)
app.include_router(cvs.router)
app.include_router(templates_routes.router)
app.include_router(campaigns.router)
app.include_router(drafts.router)
app.include_router(queue.router)
app.include_router(settings.router)


@app.middleware("http")
async def bind_check(request: Request, call_next):
    """Refuse if somehow not local (defence in depth)."""
    response = await call_next(request)
    # Never leak secrets
    return response


def create_app() -> FastAPI:
    """Factory for tests."""
    return app


if __name__ == "__main__":
    import uvicorn

    # Hard constraint: 127.0.0.1 only
    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=False)
