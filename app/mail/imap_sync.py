"""Per-account IMAP sync worker (BODY.PEEK, SPECIAL-USE \\All)."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.mail.parse import parse_header_and_text, parse_raw_email
from app.models import Account, Message
from app.security import decrypt_secret

logger = logging.getLogger(__name__)

_sync_status: dict[int, dict[str, Any]] = {}
_status_lock = threading.Lock()
_db_writer: queue.Queue[Callable[[], None] | None] | None = None
_writer_thread: threading.Thread | None = None
_workers: dict[int, threading.Thread] = {}
_stop_flags: dict[int, threading.Event] = {}


def get_sync_status(account_id: int | None = None) -> dict[str, Any]:
    """Return sync progress for one or all accounts."""
    with _status_lock:
        if account_id is None:
            return dict(_sync_status)
        return dict(_sync_status.get(account_id, {"state": "idle", "progress": 0, "total": 0}))


def _set_status(account_id: int, **kwargs: Any) -> None:
    with _status_lock:
        cur = _sync_status.setdefault(
            account_id, {"state": "idle", "progress": 0, "total": 0, "error": ""}
        )
        cur.update(kwargs)


def start_db_writer() -> None:
    """Start the single DB writer thread if not running."""
    global _db_writer, _writer_thread
    if _writer_thread and _writer_thread.is_alive():
        return
    _db_writer = queue.Queue()

    def _loop() -> None:
        assert _db_writer is not None
        while True:
            job = _db_writer.get()
            if job is None:
                break
            try:
                job()
            except Exception:  # noqa: BLE001
                logger.exception("DB writer job failed")
            finally:
                _db_writer.task_done()

    _writer_thread = threading.Thread(target=_loop, name="mailpilot-db-writer", daemon=True)
    _writer_thread.start()


def enqueue_db(fn: Callable[[], None]) -> None:
    """Enqueue a DB write callable."""
    if _db_writer is None:
        start_db_writer()
    assert _db_writer is not None
    _db_writer.put(fn)


def find_all_mail_folder(client: Any) -> str:
    """Find the \\All SPECIAL-USE folder; never hard-code the name."""
    for flags, _delimiter, name in client.list_folders():
        normalized: set[str] = set()
        for f in flags or []:
            s = f.decode() if isinstance(f, bytes) else str(f)
            normalized.add(s.lower().replace("\\", ""))
        if "all" in normalized or "allmail" in normalized:
            return name if isinstance(name, str) else name.decode()
    for flags, _delimiter, name in client.list_folders():
        n = name if isinstance(name, str) else name.decode()
        if any("all" in (f.decode() if isinstance(f, bytes) else str(f)).lower() for f in (flags or [])):
            return n
    raise RuntimeError("Could not locate Gmail \\All folder via SPECIAL-USE")


def test_imap_login(email_addr: str, app_password: str) -> tuple[bool, str]:
    """Test IMAP login; return (ok, error_message)."""
    try:
        from imapclient import IMAPClient

        with IMAPClient("imap.gmail.com", ssl=True, timeout=30) as client:
            client.login(email_addr, app_password)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "AUTHENTICATIONFAILED" in msg.upper() or "invalid credentials" in msg.lower():
            return False, (
                "Wrong or revoked app password. Check 2-Step Verification is on "
                "and generate a new one."
            )
        if "imap" in msg.lower() and "disabled" in msg.lower():
            return False, (
                "IMAP appears disabled. Enable it in Gmail Settings → "
                "Forwarding and POP/IMAP."
            )
        return False, msg


def test_smtp_login(email_addr: str, app_password: str) -> tuple[bool, str]:
    """Test SMTP login (ehlo + login, no send)."""
    import smtplib

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.ehlo()
            smtp.login(email_addr, app_password)
        return True, ""
    except smtplib.SMTPAuthenticationError:
        return False, (
            "Wrong or revoked app password. Check 2-Step Verification is on "
            "and generate a new one."
        )
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


@dataclass
class SyncResult:
    fetched: int = 0
    errors: list[str] = field(default_factory=list)


def _account_emails(session: Session) -> set[str]:
    rows = session.scalars(select(Account.email)).all()
    return {e.lower() for e in rows}


def sync_account(
    session_factory: Callable[[], Session],
    account_id: int,
    imap_factory: Callable[[], Any] | None = None,
) -> SyncResult:
    """Run one sync cycle for an account. Uses BODY.PEEK only."""
    result = SyncResult()
    _set_status(account_id, state="running", progress=0, total=0, error="")
    start_db_writer()

    session = session_factory()
    try:
        account = session.get(Account, account_id)
        if account is None or not account.active:
            _set_status(account_id, state="idle")
            return result
        email_addr = account.email
        password = decrypt_secret(account.enc_app_password)
        uidvalidity = account.all_mail_uidvalidity
        last_uid = account.all_mail_last_uid or 0
        window_days = account.sync_window_days or 365
        my_emails = _account_emails(session)
    finally:
        session.close()

    try:
        if imap_factory:
            client = imap_factory()
        else:
            from imapclient import IMAPClient

            client = IMAPClient("imap.gmail.com", ssl=True, timeout=120)
        client.login(email_addr, password)

        folder = find_all_mail_folder(client)
        select_info = client.select_folder(folder, readonly=True)
        new_uidvalidity = int(
            select_info.get(b"UIDVALIDITY") or select_info.get("UIDVALIDITY") or 0
        )

        since = (datetime.now(timezone.utc) - timedelta(days=window_days)).date()
        if uidvalidity and uidvalidity == new_uidvalidity and last_uid:
            uids = client.search(["UID", f"{last_uid + 1}:*"])
            uids = [u for u in uids if u > last_uid]
            recent = client.search(["UID", f"{max(1, last_uid - 500)}:{last_uid}"])
            _refresh_flags(client, session_factory, account_id, recent)
        else:
            uids = client.search(["SINCE", since])
            last_uid = 0

        total = len(uids)
        _set_status(account_id, state="running", progress=0, total=total)
        batch_size = 200
        max_uid = last_uid

        for i in range(0, len(uids), batch_size):
            batch = uids[i : i + batch_size]
            if not batch:
                continue
            fetched = client.fetch(
                batch,
                [
                    "X-GM-MSGID",
                    "X-GM-THRID",
                    "X-GM-LABELS",
                    "FLAGS",
                    "RFC822.SIZE",
                    "INTERNALDATE",
                    "BODY.PEEK[HEADER]",
                    "BODY.PEEK[TEXT]<0.20000>",
                ],
            )
            rows: list[dict[str, Any]] = []
            for uid, data in fetched.items():
                parsed_row = _parse_fetch_item(uid, data, my_emails)
                if parsed_row:
                    rows.append(parsed_row)
                    max_uid = max(max_uid, int(uid))
            _persist_messages(session_factory, account_id, rows)
            result.fetched += len(rows)
            _set_status(account_id, progress=min(i + len(batch), total), total=total)

        def _update_account() -> None:
            s = session_factory()
            try:
                acc = s.get(Account, account_id)
                if acc:
                    acc.all_mail_uidvalidity = new_uidvalidity
                    acc.all_mail_last_uid = max_uid
                    acc.last_sync_at = datetime.now(timezone.utc)
                    acc.status = "ok"
                    acc.status_msg = ""
                    s.commit()
            finally:
                s.close()

        _update_account()
        try:
            client.logout()
        except Exception:  # noqa: BLE001
            pass
        _set_status(account_id, state="idle", progress=total, total=total)
    except Exception as exc:  # noqa: BLE001
        logger.exception("sync failed for account %s", account_id)
        msg = str(exc)
        status = "auth_failed" if "AUTHENTICATIONFAILED" in msg.upper() else "error"

        def _mark_error() -> None:
            s = session_factory()
            try:
                acc = s.get(Account, account_id)
                if acc:
                    if status == "auth_failed":
                        acc.status = "auth_failed"
                        acc.status_msg = (
                            "Wrong or revoked app password. Check 2-Step Verification "
                            "is on and generate a new one."
                        )
                    else:
                        acc.status = "error"
                        acc.status_msg = msg[:500]
                    s.commit()
            finally:
                s.close()

        _mark_error()
        _set_status(account_id, state="error", error=msg)
        result.errors.append(msg)
    return result


def _parse_fetch_item(uid: int, data: dict[Any, Any], my_emails: set[str]) -> dict[str, Any] | None:
    def _get(key: str | bytes) -> Any:
        if key in data:
            return data[key]
        if isinstance(key, str) and key.encode() in data:
            return data[key.encode()]
        return None

    gm_msgid = _get("X-GM-MSGID") or _get(b"X-GM-MSGID")
    gm_thrid = _get("X-GM-THRID") or _get(b"X-GM-THRID")
    labels = _get("X-GM-LABELS") or _get(b"X-GM-LABELS") or []
    flags = _get("FLAGS") or _get(b"FLAGS") or ()
    size = _get("RFC822.SIZE") or _get(b"RFC822.SIZE") or 0
    header = _get("BODY[HEADER]") or _get(b"BODY[HEADER]") or b""
    text = _get("BODY[TEXT]<0>") or _get(b"BODY[TEXT]<0>") or _get("BODY[TEXT]") or b""
    for k, v in data.items():
        ks = k.decode() if isinstance(k, bytes) else str(k)
        if "BODY" in ks and "HEADER" in ks and not header:
            header = v or b""
        if "BODY" in ks and "TEXT" in ks and not text:
            text = v or b""

    if isinstance(header, str):
        header = header.encode("utf-8", errors="replace")
    if isinstance(text, str):
        text = text.encode("utf-8", errors="replace")
    if not header:
        return None
    parsed = parse_header_and_text(header or b"", text or b"")
    label_list = [lab.decode() if isinstance(lab, bytes) else str(lab) for lab in labels]
    flag_list = [f.decode() if isinstance(f, bytes) else str(f) for f in flags]
    is_unread = "\\Seen" not in flag_list
    return {
        "uid": int(uid),
        "gm_msgid": str(gm_msgid),
        "gm_thrid": str(gm_thrid) if gm_thrid is not None else None,
        "message_id_hdr": parsed.message_id,
        "in_reply_to": parsed.in_reply_to,
        "references_hdr": parsed.references,
        "from_addr": parsed.from_addr,
        "from_name": parsed.from_name,
        "to_addrs": parsed.to_addrs,
        "cc_addrs": parsed.cc_addrs,
        "subject": parsed.subject,
        "date_utc": parsed.date_utc,
        "snippet": parsed.snippet,
        "body_text": parsed.body_text[:50000],
        "labels": label_list,
        "has_attachments": parsed.has_attachments,
        "is_unread": is_unread,
        "is_from_me": parsed.from_addr in my_emails,
        "size_bytes": int(size) if size else parsed.size_bytes,
        "attachments_meta": [
            {"filename": a.filename, "content_type": a.content_type, "size": a.size}
            for a in parsed.attachments
        ],
    }


def _persist_messages(
    session_factory: Callable[[], Session],
    account_id: int,
    rows: list[dict[str, Any]],
) -> None:
    s = session_factory()
    try:
        for row in rows:
            existing = s.scalar(
                select(Message).where(
                    Message.account_id == account_id,
                    Message.gm_msgid == row["gm_msgid"],
                )
            )
            if existing:
                for k, v in row.items():
                    if k == "gm_msgid":
                        continue
                    setattr(existing, k, v)
            else:
                s.add(Message(account_id=account_id, **row))
        s.commit()
    finally:
        s.close()


def _refresh_flags(
    client: Any,
    session_factory: Callable[[], Session],
    account_id: int,
    uids: list[int],
) -> None:
    if not uids:
        return
    fetched = client.fetch(uids, ["FLAGS", "X-GM-LABELS", "X-GM-MSGID"])
    updates: list[tuple[str, bool, list[str]]] = []
    for _uid, data in fetched.items():
        gm = data.get(b"X-GM-MSGID") or data.get("X-GM-MSGID")
        flags = data.get(b"FLAGS") or data.get("FLAGS") or ()
        labels = data.get(b"X-GM-LABELS") or data.get("X-GM-LABELS") or []
        flag_list = [f.decode() if isinstance(f, bytes) else str(f) for f in flags]
        is_unread = "\\Seen" not in flag_list
        label_list = [lab.decode() if isinstance(lab, bytes) else str(lab) for lab in labels]
        if gm is not None:
            updates.append((str(gm), is_unread, label_list))

    s = session_factory()
    try:
        for gm_msgid, is_unread, labels in updates:
            msg = s.scalar(
                select(Message).where(
                    Message.account_id == account_id,
                    Message.gm_msgid == gm_msgid,
                )
            )
            if msg:
                msg.is_unread = is_unread
                msg.labels = labels
        s.commit()
    finally:
        s.close()


def fetch_html_by_uid(
    account: Account,
    uid: int,
    imap_factory: Callable[[], Any] | None = None,
) -> str:
    """Lazy-fetch full HTML body for a message by UID (BODY.PEEK)."""
    password = decrypt_secret(account.enc_app_password)
    if imap_factory:
        client = imap_factory()
    else:
        from imapclient import IMAPClient

        client = IMAPClient("imap.gmail.com", ssl=True, timeout=60)
    client.login(account.email, password)
    folder = find_all_mail_folder(client)
    client.select_folder(folder, readonly=True)
    data = client.fetch([uid], ["BODY.PEEK[]"])
    raw = None
    for _u, item in data.items():
        raw = item.get(b"BODY[]") or item.get("BODY[]") or item.get(b"RFC822")
    try:
        client.logout()
    except Exception:  # noqa: BLE001
        pass
    if not raw:
        return ""
    parsed = parse_raw_email(raw)
    return parsed.body_html


def mark_flag(
    account: Account,
    uid: int,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> None:
    """STORE flags on a message (mark read/unread etc)."""
    password = decrypt_secret(account.enc_app_password)
    from imapclient import IMAPClient

    with IMAPClient("imap.gmail.com", ssl=True, timeout=60) as client:
        client.login(account.email, password)
        folder = find_all_mail_folder(client)
        client.select_folder(folder, readonly=False)
        if add:
            client.add_flags([uid], add)
        if remove:
            client.remove_flags([uid], remove)


def start_account_worker(
    session_factory: Callable[[], Session],
    account_id: int,
    interval_minutes: int = 5,
) -> None:
    """Start a background polling worker for one account."""
    start_db_writer()
    if account_id in _workers and _workers[account_id].is_alive():
        return
    stop = threading.Event()
    _stop_flags[account_id] = stop

    def _loop() -> None:
        backoff = 5
        while not stop.is_set():
            try:
                sync_account(session_factory, account_id)
                backoff = 5
            except Exception:  # noqa: BLE001
                logger.exception("worker error account=%s", account_id)
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)
            stop.wait(interval_minutes * 60)

    t = threading.Thread(target=_loop, name=f"sync-{account_id}", daemon=True)
    _workers[account_id] = t
    t.start()


def stop_all_workers() -> None:
    """Signal all sync workers to stop."""
    for ev in _stop_flags.values():
        ev.set()


def gmail_raw_search(account: Account, query: str, limit: int = 50) -> list[int]:
    """Run X-GM-RAW search for one account; returns UIDs."""
    password = decrypt_secret(account.enc_app_password)
    from imapclient import IMAPClient

    with IMAPClient("imap.gmail.com", ssl=True, timeout=60) as client:
        client.login(account.email, password)
        folder = find_all_mail_folder(client)
        client.select_folder(folder, readonly=True)
        uids = client.search(["X-GM-RAW", query])
        return list(uids)[:limit]
