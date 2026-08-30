"""Ordered hard-delete helpers for syncs.

``syncs`` -> ``sync_channels`` -> ``post_meta`` are plain foreign keys with no
``ON DELETE CASCADE``, so MySQL rejects a parent-first delete with error 1451.
Every hard delete in this module therefore removes children before parents.

Two properties matter to callers:

* **Not atomic.** Each :class:`~db.DbManager` call opens and commits its own
  session, so a failure part-way through leaves earlier deletes committed.
  Child-first ordering is what makes that safe — the worst case is a parent row
  with no children, which is harmless and cleaned up by re-running.
* **Idempotent.** Calling either function twice for the same id is a no-op the
  second time, so a retry after a partial failure always converges.

This module imports **submodules only** (``db``, ``db.schemas``,
``helpers._cache``) and never the ``helpers`` package, to avoid a circular
import at Lambda cold start.
"""

import logging

from db import DbManager, schemas
from helpers._cache import _cache_delete

_logger = logging.getLogger(__name__)


def _invalidate_sync_list(channel_ids) -> None:
    """Drop cached ``get_sync_list`` entries for *channel_ids*.

    ``get_sync_list`` caches under ``sync_list:{channel_id}`` for 60 seconds, so
    without this a warm container keeps fanning messages out to channels whose
    rows were just deleted.
    """
    for channel_id in channel_ids:
        if channel_id:
            _cache_delete(f"sync_list:{channel_id}")


def purge_sync(sync_id: int) -> None:
    """Hard-delete a sync and everything beneath it, children first.

    ``syncs`` -> ``sync_channels`` -> ``post_meta`` have no ``ON DELETE
    CASCADE``, so MySQL rejects a parent-first delete. Soft-deleted channels are
    included on purpose: ``deleted_at`` only hides a row from the UI, it still
    references ``syncs`` and would block the parent delete.
    """
    if not sync_id:
        return

    channels = DbManager.find_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.sync_id == sync_id],
    )

    for channel in channels:
        DbManager.delete_records(
            schemas.PostMeta,
            [schemas.PostMeta.sync_channel_id == channel.id],
        )

    if channels:
        DbManager.delete_records(
            schemas.SyncChannel,
            [schemas.SyncChannel.sync_id == sync_id],
        )

    DbManager.delete_records(schemas.Sync, [schemas.Sync.id == sync_id])

    _invalidate_sync_list(channel.channel_id for channel in channels)

    _logger.info(
        "sync_purged",
        extra={"sync_id": sync_id, "sync_channels": len(channels)},
    )


def purge_sync_channels(channels) -> None:
    """Hard-delete specific ``SyncChannel`` rows and their ``post_meta``.

    Used when a workspace leaves a sync that other workspaces still use, so the
    parent ``Sync`` must survive. Also invalidates the fan-out cache, which the
    call sites previously forgot.
    """
    for channel in channels:
        DbManager.delete_records(
            schemas.PostMeta,
            [schemas.PostMeta.sync_channel_id == channel.id],
        )
        DbManager.delete_records(
            schemas.SyncChannel,
            [schemas.SyncChannel.id == channel.id],
        )

    _invalidate_sync_list(channel.channel_id for channel in channels)
