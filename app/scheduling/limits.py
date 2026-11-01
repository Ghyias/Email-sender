"""Per-account rolling 24h rate limiting."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import HARD_DAILY_CAP, HARD_MIN_DELAY_S
from app.models import Account, SendLog


def sent_count_24h(session: Session, account_id: int, now: Optional[datetime] = None) -> int:
    """Count successful sends in the rolling 24h window."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=24)
    return int(
        session.scalar(
            select(func.count())
            .select_from(SendLog)
            .where(
                SendLog.account_id == account_id,
                SendLog.result == "sent",
                SendLog.ts_utc >= since,
            )
        )
        or 0
    )


def remaining_capacity(session: Session, account: Account, now: Optional[datetime] = None) -> int:
    """How many more messages this account may send in the next 24h."""
    cap = min(account.daily_cap or 30, HARD_DAILY_CAP)
    used = sent_count_24h(session, account.id, now=now)
    return max(0, cap - used)


def last_send_utc(session: Session, account_id: int) -> Optional[datetime]:
    """Timestamp of the most recent successful send."""
    return session.scalar(
        select(SendLog.ts_utc)
        .where(SendLog.account_id == account_id, SendLog.result == "sent")
        .order_by(SendLog.ts_utc.desc())
        .limit(1)
    )


def can_send_now(
    session: Session,
    account: Account,
    now: Optional[datetime] = None,
) -> tuple[bool, str]:
    """Check rolling cap and min delay. Returns (ok, reason)."""
    now = now or datetime.now(timezone.utc)
    if remaining_capacity(session, account, now=now) <= 0:
        return False, "cap"
    last = last_send_utc(session, account.id)
    min_delay = max(account.min_delay_s or HARD_MIN_DELAY_S, HARD_MIN_DELAY_S)
    if last is not None:
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if (now - last).total_seconds() < min_delay:
            return False, "delay"
    return True, ""


def next_available_slot(
    session: Session,
    account: Account,
    now: Optional[datetime] = None,
) -> datetime:
    """Earliest UTC datetime when account may send again."""
    now = now or datetime.now(timezone.utc)
    ok, reason = can_send_now(session, account, now=now)
    if ok:
        return now
    if reason == "delay":
        last = last_send_utc(session, account.id) or now
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        min_delay = max(account.min_delay_s or HARD_MIN_DELAY_S, HARD_MIN_DELAY_S)
        return last + timedelta(seconds=min_delay)
    # at cap — push ~24h from oldest send in window
    since = now - timedelta(hours=24)
    oldest = session.scalar(
        select(SendLog.ts_utc)
        .where(
            SendLog.account_id == account.id,
            SendLog.result == "sent",
            SendLog.ts_utc >= since,
        )
        .order_by(SendLog.ts_utc.asc())
        .limit(1)
    )
    if oldest is None:
        return now + timedelta(hours=1)
    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=timezone.utc)
    return oldest + timedelta(hours=24, seconds=1)


def assign_accounts_round_robin(
    session: Session,
    contact_ids: list[int],
    account_ids: list[int],
) -> dict[int, int]:
    """
    Assign each contact one sending account.
    Sticky: reuse contact.sticky_account_id when set and still in account_ids.
    Otherwise weighted round-robin by remaining capacity.
    """
    from app.models import Contact

    accounts = [session.get(Account, aid) for aid in account_ids]
    accounts = [a for a in accounts if a and a.active and a.status != "auth_failed"]
    if not accounts:
        return {}
    capacity = {a.id: remaining_capacity(session, a) for a in accounts}
    assignment: dict[int, int] = {}
    # order accounts by remaining capacity desc for weighting
    rr_order = sorted(accounts, key=lambda a: capacity.get(a.id, 0), reverse=True)
    idx = 0
    for cid in contact_ids:
        contact = session.get(Contact, cid)
        if contact and contact.sticky_account_id and contact.sticky_account_id in {a.id for a in accounts}:
            assignment[cid] = contact.sticky_account_id
            continue
        # pick next with capacity > 0, else any
        chosen = None
        for _ in range(len(rr_order)):
            cand = rr_order[idx % len(rr_order)]
            idx += 1
            if capacity.get(cand.id, 0) > 0:
                chosen = cand
                capacity[cand.id] = capacity.get(cand.id, 0) - 1
                break
        if chosen is None:
            chosen = rr_order[idx % len(rr_order)]
            idx += 1
        assignment[cid] = chosen.id
        if contact:
            contact.sticky_account_id = chosen.id
    return assignment
