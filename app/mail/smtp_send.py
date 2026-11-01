"""Build and send EmailMessage via SMTP_SSL or dry-run .eml files."""

from __future__ import annotations

import logging
import re
import smtplib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path
from typing import Optional

from app.config import DRYRUN_DIR, MAX_ATTACHMENT_BYTES
from app.security import decrypt_secret

logger = logging.getLogger(__name__)


@dataclass
class SendResult:
    ok: bool
    message_id: str = ""
    error: str = ""
    error_class: str = ""  # auth|daily_limit|transient|permanent|""
    dry_run_path: str = ""


def text_to_simple_html(body: str) -> str:
    """Convert plain text paragraphs to simple HTML."""
    parts = []
    for para in re.split(r"\n\s*\n", body.strip()):
        escaped = (
            para.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br>\n")
        )
        parts.append(f"<p>{escaped}</p>")
    return "\n".join(parts)


def build_message(
    *,
    from_name: str,
    from_addr: str,
    to_addr: str,
    subject: str,
    body_text: str,
    signature_html: str = "",
    attachment_path: Optional[Path] = None,
    attachment_filename: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
    message_id: Optional[str] = None,
) -> EmailMessage:
    """Build a multipart EmailMessage ready to send."""
    msg = EmailMessage()
    msg["From"] = f'"{from_name}" <{from_addr}>' if from_name else from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    mid = message_id or make_msgid(domain="gmail.com")
    msg["Message-ID"] = mid
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references

    full_text = body_text
    if signature_html:
        # strip tags for text part signature approx
        sig_text = re.sub(r"<[^>]+>", "", signature_html)
        full_text = f"{body_text}\n\n--\n{sig_text}"
    html = text_to_simple_html(body_text)
    if signature_html:
        html = f"{html}\n<div class=\"signature\">{signature_html}</div>"
    msg.set_content(full_text)
    msg.add_alternative(html, subtype="html")

    if attachment_path:
        path = Path(attachment_path)
        data = path.read_bytes()
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"Attachment exceeds {MAX_ATTACHMENT_BYTES} bytes")
        filename = attachment_filename or path.name
        msg.add_attachment(
            data,
            maintype="application",
            subtype="pdf",
            filename=filename,
        )
    return msg


def send_message(
    *,
    account_email: str,
    enc_app_password: str,
    msg: EmailMessage,
    dry_run: bool = True,
) -> SendResult:
    """Send via SMTP_SSL 465, or write .eml in dry-run mode."""
    message_id = str(msg["Message-ID"])
    if dry_run:
        DRYRUN_DIR.mkdir(parents=True, exist_ok=True)
        safe_to = re.sub(r"[^a-zA-Z0-9_.+-]", "_", str(msg["To"]))
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        out = DRYRUN_DIR / f"{ts}_{safe_to}.eml"
        out.write_bytes(msg.as_bytes())
        return SendResult(ok=True, message_id=message_id, dry_run_path=str(out))

    password = decrypt_secret(enc_app_password)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
            smtp.login(account_email, password)
            smtp.send_message(msg)
        return SendResult(ok=True, message_id=message_id)
    except smtplib.SMTPAuthenticationError as exc:
        return SendResult(ok=False, message_id=message_id, error=str(exc), error_class="auth")
    except smtplib.SMTPDataError as exc:
        code = getattr(exc, "smtp_code", None) or (exc.args[0] if exc.args else None)
        err = str(exc)
        if code in (550, 552) or "5.4.5" in err:
            return SendResult(ok=False, message_id=message_id, error=err, error_class="daily_limit")
        return SendResult(ok=False, message_id=message_id, error=err, error_class="permanent")
    except (smtplib.SMTPServerDisconnected, TimeoutError, OSError) as exc:
        return SendResult(ok=False, message_id=message_id, error=str(exc), error_class="transient")
    except smtplib.SMTPResponseException as exc:
        if 400 <= int(exc.smtp_code) < 500:
            return SendResult(
                ok=False, message_id=message_id, error=str(exc), error_class="transient"
            )
        return SendResult(ok=False, message_id=message_id, error=str(exc), error_class="permanent")
    except Exception as exc:  # noqa: BLE001
        logger.exception("send failed")
        return SendResult(ok=False, message_id=message_id, error=str(exc), error_class="permanent")


def ensure_re_subject(subject: str) -> str:
    """Prefix Re: exactly once."""
    s = subject or ""
    if re.match(r"^re:\s*", s, flags=re.IGNORECASE):
        return s
    return f"Re: {s}"
