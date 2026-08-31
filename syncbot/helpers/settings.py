"""Database-backed instance settings with environment fallback.

Resolution precedence for every setting is **database, then environment, then a
hardcoded default**. Environment variables become the seed and fallback: they
still work for a fresh deployment, and once the operator saves a value in the
Settings modal the database value is authoritative.

Only operational policy belongs here — things that change over a deployment's
life and benefit from a UI. Secrets, connection details, and break-glass
switches stay in environment variables, because a UI toggle would either leak
them or defeat the guard they provide.

Imports submodules only (``db``, ``db.schemas``, ``helpers._cache``), per the
import-direction constraint documented in ``helpers/sync_cleanup.py``.
"""

import logging
import os
from datetime import UTC, datetime

import constants
from db import DbManager, schemas
from helpers._cache import _cache_delete, _cache_get, _cache_set

_logger = logging.getLogger(__name__)

_TRUTHY = ("true", "1", "yes", "on")
_FALSY = ("false", "0", "no", "off")

_SENTINEL_MISSING = "\x00__missing__"


def _cache_key(key: str) -> str:
    return f"setting:{key}"


def get_raw_setting(key: str) -> str | None:
    """Return the stored database value for *key*, or None if there is no row.

    Cached per process, like ``sync_list``. On Lambda each warm container holds
    its own copy, so the TTL bounds staleness rather than removing it; the
    Settings modal invalidates on save within its own container.
    """
    cached = _cache_get(_cache_key(key))
    if cached is not None:
        return None if cached == _SENTINEL_MISSING else cached

    rows = DbManager.find_records(schemas.InstanceSetting, [schemas.InstanceSetting.key == key])
    value = rows[0].value if rows else None
    _cache_set(_cache_key(key), _SENTINEL_MISSING if value is None else value)
    return value


def set_setting(key: str, value: str | None) -> None:
    """Write *key* and invalidate its cache entry."""
    now = datetime.now(UTC)
    existing = DbManager.find_records(schemas.InstanceSetting, [schemas.InstanceSetting.key == key])
    if existing:
        DbManager.update_records(
            schemas.InstanceSetting,
            [schemas.InstanceSetting.key == key],
            {schemas.InstanceSetting.value: value, schemas.InstanceSetting.updated_at: now},
        )
    else:
        DbManager.create_record(schemas.InstanceSetting(key=key, value=value, updated_at=now))

    _cache_delete(_cache_key(key))
    _logger.info("instance_setting_saved", extra={"setting_key": key})


def _resolve(key: str, env_var: str | None) -> str | None:
    """Return the effective raw string for *key*: database, else environment."""
    stored = get_raw_setting(key)
    if stored is not None:
        return stored
    if env_var:
        env_value = os.environ.get(env_var)
        if env_value is not None and env_value.strip() != "":
            return env_value
    return None


def get_bool_setting(key: str, env_var: str | None, default: bool) -> bool:
    """Resolve *key* as a boolean."""
    raw = _resolve(key, env_var)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUTHY:
        return True
    if normalized in _FALSY:
        return False
    _logger.warning("instance_setting_unparseable", extra={"setting_key": key, "expected": "bool"})
    return default


def get_int_setting(key: str, env_var: str | None, default: int) -> int:
    """Resolve *key* as an integer."""
    raw = _resolve(key, env_var)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        _logger.warning("instance_setting_unparseable", extra={"setting_key": key, "expected": "int"})
        return default


def get_list_setting(key: str, env_var: str | None, default: list[str] | None = None) -> list[str]:
    """Resolve *key* as a comma-separated list, matching the SLACK_BOT_SCOPES idiom."""
    raw = _resolve(key, env_var)
    if raw is None:
        return list(default or [])
    return [item.strip() for item in raw.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Typed accessors for the settings that exist today
# ---------------------------------------------------------------------------


def allow_private_channels() -> bool:
    """Whether private channels may be selected for a normal (non-broadcast) sync.

    Defaults to false. Broadcasts are always public-only and ignore this.
    """
    return get_bool_setting(
        constants.SETTING_ALLOW_PRIVATE_CHANNELS,
        constants.ALLOW_PRIVATE_CHANNELS,
        constants.DEFAULT_ALLOW_PRIVATE_CHANNELS,
    )


def broadcast_allowed_workspaces() -> list[str]:
    """Slack team IDs permitted to publish a broadcast. Empty means any installed workspace."""
    return get_list_setting(
        constants.SETTING_BROADCAST_ALLOWED_WORKSPACES,
        constants.BROADCAST_ALLOWED_WORKSPACES,
        constants.DEFAULT_BROADCAST_ALLOWED_WORKSPACES,
    )


def soft_delete_retention_days() -> int:
    """Days a soft-deleted workspace is retained before the purge removes it permanently."""
    return get_int_setting(
        constants.SETTING_SOFT_DELETE_RETENTION_DAYS,
        constants.SOFT_DELETE_RETENTION_DAYS_VAR,
        constants.DEFAULT_SOFT_DELETE_RETENTION_DAYS,
    )


def may_publish_broadcast(team_id: str | None) -> bool:
    """Whether *team_id* may publish a broadcast under the current allow-list."""
    allowed = broadcast_allowed_workspaces()
    if not allowed:
        return True
    return (team_id or "") in allowed
