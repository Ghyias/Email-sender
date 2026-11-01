"""Optional pywebview desktop wrapper. Requires: pip install pywebview."""

from __future__ import annotations

import threading

import uvicorn

from app.config import HOST, PORT


def _run_server() -> None:
    uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="info")


def main() -> None:
    """Open MailPilot in a native window (127.0.0.1 only)."""
    try:
        import webview
    except ImportError as exc:
        raise SystemExit("Install pywebview: pip install pywebview") from exc

    threading.Thread(target=_run_server, daemon=True).start()
    webview.create_window("MailPilot", f"http://{HOST}:{PORT}")
    webview.start()


if __name__ == "__main__":
    main()
