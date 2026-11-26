"""Database reads shared by tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def lock_is_held(name: str, ttl: timedelta) -> bool:
    """True when the run lock row ``lock:{name}`` exists and is younger than *ttl*."""
    from db import DbManager, schemas
    from db.run_lock import LOCK_PREFIX

    rows = DbManager.find_records(schemas.InstanceSetting, [schemas.InstanceSetting.key == f"{LOCK_PREFIX}{name}"])
    if not rows or rows[0].updated_at is None:
        return False
    updated = rows[0].updated_at.replace(tzinfo=None)
    return datetime.now(UTC).replace(tzinfo=None) - updated < ttl
