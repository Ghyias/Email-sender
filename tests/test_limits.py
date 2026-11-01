"""Rate limiter rolling window."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import Account, SendLog
from app.scheduling.limits import can_send_now, remaining_capacity, sent_count_24h
from app.security import encrypt_secret


def test_rolling_cap(db) -> None:
    acc = Account(
        email="a@gmail.com",
        enc_app_password=encrypt_secret("abcdefghijklmnop"),
        daily_cap=2,
        min_delay_s=90,
        max_delay_s=240,
    )
    db.add(acc)
    db.commit()
    db.refresh(acc)
    now = datetime.now(timezone.utc)
    for i in range(2):
        db.add(
            SendLog(
                ts_utc=now - timedelta(hours=1),
                account_id=acc.id,
                to_addr=f"t{i}@x.com",
                subject="s",
                result="sent",
            )
        )
    db.commit()
    assert sent_count_24h(db, acc.id, now=now) == 2
    assert remaining_capacity(db, acc, now=now) == 0
    ok, reason = can_send_now(db, acc, now=now)
    assert not ok and reason == "cap"
