"""MIME parsing, address normalisation, snippets."""

from __future__ import annotations

import email
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from typing import Optional


@dataclass
class ParsedAttachment:
    filename: str
    content_type: str
    size: int
    payload: bytes | None = None


@dataclass
class ParsedMessage:
    message_id: str = ""
    in_reply_to: str = ""
    references: str = ""
    from_addr: str = ""
    from_name: str = ""
    to_addrs: list[dict[str, str]] = field(default_factory=list)
    cc_addrs: list[dict[str, str]] = field(default_factory=list)
    subject: str = ""
    date_utc: Optional[datetime] = None
    body_text: str = ""
    body_html: str = ""
    snippet: str = ""
    has_attachments: bool = False
    attachments: list[ParsedAttachment] = field(default_factory=list)
    size_bytes: int = 0


def decode_mime_header(value: str | None) -> str:
    """Decode RFC2047 encoded header value to str."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001
        return value


def normalize_addr(addr: str) -> str:
    """Lowercase and strip an email address."""
    return addr.strip().lower()


def parse_address_list(header_value: str | None) -> list[dict[str, str]]:
    """Parse To/Cc headers into [{name, email}]."""
    if not header_value:
        return []
    # Decode RFC2047 first so getaddresses sees plain text.
    decoded = decode_mime_header(header_value)
    result: list[dict[str, str]] = []
    for name, addr in getaddresses([decoded]):
        if not addr:
            continue
        # If the whole header was a single encoded blob, recover email via regex
        if "@" not in addr:
            import re as _re

            m = _re.search(r"[\w.+-]+@[\w.-]+\.\w+", decoded)
            if m:
                addr = m.group(0)
                name = decoded.replace(f"<{addr}>", "").strip() or name
            else:
                continue
        result.append({"name": decode_mime_header(name), "email": normalize_addr(addr)})
    return result


def _walk_bodies(msg: Message) -> tuple[str, str]:
    text_parts: list[str] = []
    html_parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition", "")).lower()
            if "attachment" in disp:
                continue
            try:
                payload = part.get_payload(decode=True)
            except Exception:  # noqa: BLE001
                continue
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except Exception:  # noqa: BLE001
                decoded = payload.decode("utf-8", errors="replace")
            if ctype == "text/plain":
                text_parts.append(decoded)
            elif ctype == "text/html":
                html_parts.append(decoded)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                html_parts.append(decoded)
            else:
                text_parts.append(decoded)
    return "\n".join(text_parts).strip(), "\n".join(html_parts).strip()


def _collect_attachments(msg: Message, include_payload: bool = False) -> list[ParsedAttachment]:
    attachments: list[ParsedAttachment] = []
    if not msg.is_multipart():
        return attachments
    for part in msg.walk():
        disp = str(part.get("Content-Disposition", ""))
        filename = part.get_filename()
        if not filename and "attachment" not in disp.lower():
            continue
        if not filename:
            continue
        filename = decode_mime_header(filename)
        payload = part.get_payload(decode=True) or b""
        attachments.append(
            ParsedAttachment(
                filename=filename,
                content_type=part.get_content_type(),
                size=len(payload),
                payload=payload if include_payload else None,
            )
        )
    return attachments


def make_snippet(text: str, limit: int = 200) -> str:
    """First N chars of collapsed whitespace text."""
    collapsed = re.sub(r"\s+", " ", text or "").strip()
    return collapsed[:limit]


def parse_raw_email(raw: bytes | str, include_attachment_payload: bool = False) -> ParsedMessage:
    """Parse raw RFC822 bytes/string into ParsedMessage."""
    if isinstance(raw, str):
        raw_bytes = raw.encode("utf-8", errors="replace")
    else:
        raw_bytes = raw
    msg = email.message_from_bytes(raw_bytes)
    from_list = parse_address_list(msg.get("From"))
    from_name = from_list[0]["name"] if from_list else ""
    from_addr = from_list[0]["email"] if from_list else ""
    date_utc: Optional[datetime] = None
    date_hdr = msg.get("Date")
    if date_hdr:
        try:
            dt = parsedate_to_datetime(date_hdr)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            date_utc = dt.astimezone(timezone.utc)
        except Exception:  # noqa: BLE001
            date_utc = None
    body_text, body_html = _walk_bodies(msg)
    if not body_text and body_html:
        body_text = re.sub(r"<[^>]+>", " ", body_html)
        body_text = re.sub(r"\s+", " ", body_text).strip()
    attachments = _collect_attachments(msg, include_payload=include_attachment_payload)
    return ParsedMessage(
        message_id=(msg.get("Message-ID") or "").strip(),
        in_reply_to=(msg.get("In-Reply-To") or "").strip(),
        references=(msg.get("References") or "").strip(),
        from_addr=from_addr,
        from_name=from_name,
        to_addrs=parse_address_list(msg.get("To")),
        cc_addrs=parse_address_list(msg.get("Cc")),
        subject=decode_mime_header(msg.get("Subject")),
        date_utc=date_utc,
        body_text=body_text,
        body_html=body_html,
        snippet=make_snippet(body_text),
        has_attachments=bool(attachments),
        attachments=attachments,
        size_bytes=len(raw_bytes),
    )


def parse_header_and_text(header_bytes: bytes, text_bytes: bytes) -> ParsedMessage:
    """Parse BODY.PEEK[HEADER] + BODY.PEEK[TEXT] fragments from IMAP."""
    combined = header_bytes + b"\r\n" + text_bytes
    return parse_raw_email(combined)


def refs_message_ids(references_hdr: str | None, in_reply_to: str | None = None) -> set[str]:
    """Extract Message-ID tokens from References / In-Reply-To."""
    ids: set[str] = set()
    for blob in (references_hdr or "", in_reply_to or ""):
        for match in re.findall(r"<[^>]+>", blob):
            ids.add(match.strip())
        # bare ids
        for token in blob.split():
            token = token.strip()
            if token.startswith("<") and token.endswith(">"):
                ids.add(token)
    return ids
