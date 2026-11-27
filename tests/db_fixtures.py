"""Database setup and reads shared by tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch


@contextmanager
def temp_sqlite_database(path: Path, *, with_instance: bool = False) -> Iterator:
    """A fresh SQLite database for one test. Yields its engine.

    Puts back the process's engine, session, and schema, and the federation
    instance id and cached keys, even when setup fails. *with_instance* makes
    this database's instance keypair.
    """
    import db as db_mod
    from federation import core as federation_core
    from helpers._cache import clear_all_caches

    saved_db = (db_mod.GLOBAL_ENGINE, db_mod.GLOBAL_SESSION, db_mod.GLOBAL_SCHEMA)
    saved_instance = (
        federation_core._INSTANCE_ID,
        federation_core._cached_private_key,
        federation_core._cached_public_pem,
    )
    env = {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": f"sqlite:///{path}"}
    with patch.dict(os.environ, env, clear=False):
        try:
            db_mod.GLOBAL_ENGINE = None
            db_mod.GLOBAL_SESSION = None
            db_mod.GLOBAL_SCHEMA = None
            db_mod.initialize_database()
            if with_instance:
                federation_core._INSTANCE_ID = None
                federation_core._cached_private_key = None
                federation_core._cached_public_pem = None
                federation_core.get_or_create_instance_keypair()
            clear_all_caches()
            yield db_mod.get_engine()
        finally:
            clear_all_caches()
            if db_mod.GLOBAL_ENGINE:
                db_mod.GLOBAL_ENGINE.dispose()
            db_mod.GLOBAL_ENGINE, db_mod.GLOBAL_SESSION, db_mod.GLOBAL_SCHEMA = saved_db
            (
                federation_core._INSTANCE_ID,
                federation_core._cached_private_key,
                federation_core._cached_public_pem,
            ) = saved_instance


def lock_is_held(name: str, ttl: timedelta) -> bool:
    """True when the run lock row ``lock:{name}`` exists and is younger than *ttl*."""
    from db import DbManager, schemas
    from db.run_lock import LOCK_PREFIX

    rows = DbManager.find_records(schemas.InstanceSetting, [schemas.InstanceSetting.key == f"{LOCK_PREFIX}{name}"])
    if not rows or rows[0].updated_at is None:
        return False
    updated = rows[0].updated_at.replace(tzinfo=None)
    return datetime.now(UTC).replace(tzinfo=None) - updated < ttl
