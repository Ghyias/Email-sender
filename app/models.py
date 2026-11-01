"""SQLAlchemy models for MailPilot (EMAIL_HUB_SPEC §5)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


def utcnow() -> datetime:
    """Return timezone-aware UTC now."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Declarative base."""

    type_annotation_map = {
        dict[str, Any]: JSON,
        list[Any]: JSON,
    }


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), default="")
    enc_app_password: Mapped[str] = mapped_column(Text, nullable=False)
    daily_cap: Mapped[int] = mapped_column(Integer, default=30)
    min_delay_s: Mapped[int] = mapped_column(Integer, default=90)
    max_delay_s: Mapped[int] = mapped_column(Integer, default=240)
    signature_html: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    all_mail_uidvalidity: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    all_mail_last_uid: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    sync_window_days: Mapped[int] = mapped_column(Integer, default=365)
    status: Mapped[str] = mapped_column(String(32), default="ok")  # ok|auth_failed|error
    status_msg: Mapped[str] = mapped_column(Text, default="")

    messages: Mapped[list[Message]] = relationship(back_populates="account")


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("account_id", "gm_msgid", name="uq_account_gm_msgid"),
        Index("ix_messages_date_utc", "date_utc"),
        Index("ix_messages_from_addr", "from_addr"),
        Index("ix_messages_gm_thrid", "gm_thrid"),
        Index("ix_messages_message_id_hdr", "message_id_hdr"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    gm_msgid: Mapped[str] = mapped_column(String(64), nullable=False)
    gm_thrid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    uid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id_hdr: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    in_reply_to: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    references_hdr: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    from_addr: Mapped[str] = mapped_column(String(320), default="")
    from_name: Mapped[str] = mapped_column(String(255), default="")
    to_addrs: Mapped[Optional[list[Any]]] = mapped_column(JSON, default=list)
    cc_addrs: Mapped[Optional[list[Any]]] = mapped_column(JSON, default=list)
    subject: Mapped[str] = mapped_column(Text, default="")
    date_utc: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    snippet: Mapped[str] = mapped_column(Text, default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    body_html: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    labels: Mapped[Optional[list[Any]]] = mapped_column(JSON, default=list)
    has_attachments: Mapped[bool] = mapped_column(Boolean, default=False)
    is_unread: Mapped[bool] = mapped_column(Boolean, default=False)
    is_from_me: Mapped[bool] = mapped_column(Boolean, default=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    attachments_meta: Mapped[Optional[list[Any]]] = mapped_column(JSON, default=list)

    account: Mapped[Account] = relationship(back_populates="messages")


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), default="")
    title: Mapped[str] = mapped_column(String(255), default="")
    institution: Mapped[str] = mapped_column(String(255), default="")
    department: Mapped[str] = mapped_column(String(255), default="")
    country: Mapped[str] = mapped_column(String(128), default="")
    timezone: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    research_summary: Mapped[str] = mapped_column(Text, default="")
    recent_papers: Mapped[str] = mapped_column(Text, default="")
    profile_url: Mapped[str] = mapped_column(String(512), default="")
    groups: Mapped[Optional[list[Any]]] = mapped_column(JSON, default=list)
    region_type: Mapped[str] = mapped_column(String(32), default="other")  # china|western|other
    source_file: Mapped[str] = mapped_column(String(512), default="")
    do_not_contact: Mapped[bool] = mapped_column(Boolean, default=False)
    bounced: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_reply_utc: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    sticky_account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("accounts.id"), nullable=True)


class Template(Base):
    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    language: Mapped[str] = mapped_column(String(8), default="en")
    region_type: Mapped[str] = mapped_column(String(32), default="western")
    subject_template: Mapped[str] = mapped_column(Text, default="")
    body_template: Mapped[str] = mapped_column(Text, default="")
    llm_instructions: Mapped[str] = mapped_column(Text, default="")
    forbidden_phrases: Mapped[Optional[list[Any]]] = mapped_column(JSON, default=list)
    max_words: Mapped[int] = mapped_column(Integer, default=250)


class CvVariant(Base):
    __tablename__ = "cv_variants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    label: Mapped[str] = mapped_column(String(255), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    keywords: Mapped[Optional[list[Any]]] = mapped_column(JSON, default=list)
    embedding: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    embedding_model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserProfile(Base):
    __tablename__ = "user_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    full_name: Mapped[str] = mapped_column(String(255), default="")
    short_bio: Mapped[str] = mapped_column(Text, default="")
    research_interests: Mapped[str] = mapped_column(Text, default="")
    signature_default: Mapped[str] = mapped_column(Text, default="")
    facts_allowed: Mapped[Optional[list[Any]]] = mapped_column(JSON, default=list)
    facts_forbidden: Mapped[Optional[list[Any]]] = mapped_column(JSON, default=list)


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    template_id: Mapped[Optional[int]] = mapped_column(ForeignKey("templates.id"), nullable=True)
    account_ids: Mapped[Optional[list[Any]]] = mapped_column(JSON, default=list)
    contact_filter: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, default=dict)
    followup_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    send_window: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    paused: Mapped[bool] = mapped_column(Boolean, default=False)


class Draft(Base):
    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[Optional[int]] = mapped_column(ForeignKey("campaigns.id"), nullable=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), nullable=False)
    account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    subject: Mapped[str] = mapped_column(Text, default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    body_html: Mapped[str] = mapped_column(Text, default="")
    cv_variant_id: Mapped[Optional[int]] = mapped_column(ForeignKey("cv_variants.id"), nullable=True)
    cv_reason: Mapped[str] = mapped_column(Text, default="")
    cv_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    cv_needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    cv_scores: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, default=dict)
    llm_model: Mapped[str] = mapped_column(String(128), default="")
    prompt_hash: Mapped[str] = mapped_column(String(64), default="")
    validation_flags: Mapped[Optional[list[Any]]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    is_followup: Mapped[bool] = mapped_column(Boolean, default=False)
    parent_draft_id: Mapped[Optional[int]] = mapped_column(ForeignKey("drafts.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SendQueue(Base):
    __tablename__ = "send_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("drafts.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    scheduled_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    smtp_message_id: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    sent_utc: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class SendLog(Base):
    __tablename__ = "send_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    to_addr: Mapped[str] = mapped_column(String(320), default="")
    subject: Mapped[str] = mapped_column(Text, default="")
    draft_id: Mapped[Optional[int]] = mapped_column(ForeignKey("drafts.id"), nullable=True)
    result: Mapped[str] = mapped_column(String(64), default="")
    error: Mapped[str] = mapped_column(Text, default="")


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)


class GenerationJob(Base):
    """Persistent draft-generation queue (concurrency 1)."""

    __tablename__ = "generation_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[Optional[int]] = mapped_column(ForeignKey("campaigns.id"), nullable=True)
    contact_ids: Mapped[Optional[list[Any]]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending|running|paused|done|cancelled
    progress: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CvLabel(Base):
    """Ground-truth labels for CV matcher accuracy harness."""

    __tablename__ = "cv_labels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), nullable=False)
    cv_key: Mapped[str] = mapped_column(String(64), nullable=False)


# Ensure FTS triggers are documented; actual FTS setup is in db.py
@event.listens_for(Message, "after_insert")
def _noop_after_insert(_mapper, _connection, _target) -> None:  # noqa: ANN001
    pass
