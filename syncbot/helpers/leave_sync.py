"""Soft-delete one workspace's Channel Sync without a Slack modal.

The database pass is :func:`soft_delete_sync_channel`. The Slack pass is
:func:`_announce_soft_delete`. :func:`soft_delete_sync_channels` does both
for one Channel that may sit in several Syncs.

Imports submodules only.
"""

from datetime import UTC, datetime

from slack_sdk.web import WebClient

from db import DbManager, schemas
from helpers.export_import import invalidate_group_member_homes, invalidate_home_tab_caches_for_team
from helpers.notifications import notify_channel_or_managers
from helpers.settings import soft_delete_retention_days
from helpers.slack_api import get_user_info, slack_error_code
from helpers.sync_lifecycle import deliver_action_notice, get_sync_channels, post_to_other_sync_channels
from helpers.sync_participation import invalidate_sync_fanout_for_syncs
from helpers.workspace import get_bot_token
from helpers.workspace_kind import is_local_workspace
from logger import log_info, log_warning


def _now():
    return datetime.now(UTC).replace(tzinfo=None)


_LEAVE_OK = frozenset({"is_archived", "channel_not_found", "not_in_channel"})
_REASON_ACTION = {
    "became_private": "Privacy",
    "became_public": "Privacy",
    "private_syncing_off": "Privacy",
    "removed_from_group": "Remove",
    "group_disbanded": "Disband",
    "left_group": "Leave",
}
_REASON_NOTE = {
    "became_private": "The Channel is now private.",
    "became_public": "The Channel is now public.",
    "private_syncing_off": "Private Channel syncing was turned off.",
    "authorization_ended": "SyncBot is no longer authorized for this Channel.",
    "manager_removed": "The manager who runs this Channel was removed.",
}


def group_name_for_sync(sync) -> str | None:
    """The Workspace Group name for this Sync, or None when it has none."""
    group_id = getattr(sync, "group_id", None) if sync is not None else None
    if not group_id:
        return None
    try:
        group = DbManager.get_record(schemas.WorkspaceGroup, id=group_id)
    except Exception:
        return None
    name = (getattr(group, "name", None) or "").strip() if group else ""
    return name or None


def group_name_for_sync_id(sync_id: int | None) -> str | None:
    """The Workspace Group name for this Sync id, or None when the row is gone."""
    if not sync_id:
        return None
    try:
        sync = DbManager.get_record(schemas.Sync, id=sync_id)
    except Exception:
        return None
    return group_name_for_sync(sync) if sync else None


def with_group(group_name: str | None) -> str:
    """`` with `Partner Org` ``, or nothing when the group name is unknown."""
    name = (group_name or "").strip()
    return f" with `{name}`" if name else ""


def retention_sentence(workspace) -> str:
    """How long this install keeps a local Workspace's removed Sync data."""
    if not is_local_workspace(workspace):
        return ""
    return f"Sync data will be retained for {soft_delete_retention_days()} days."


def soft_delete_sync_channel(
    row: schemas.SyncChannel,
    reason: str,
    *,
    is_private: bool | None = None,
) -> bool:
    """Mark one row deleted. No Slack call and no replication. Returns False when already deleted.

    :func:`soft_delete_sync_channels` replicates a local row after the Slack pass.
    A stub row is replicated by the caller that ended it.
    """
    if row is None or not getattr(row, "id", None):
        return False
    if getattr(row, "deleted_at", None) is not None:
        return False
    ended = _now()
    fields = {
        schemas.SyncChannel.deleted_at: ended,
        schemas.SyncChannel.status: "deleted",
        schemas.SyncChannel.synced_by_user_id: None,
    }
    if is_private is not None:
        fields[schemas.SyncChannel.is_private] = is_private
    DbManager.update_records(schemas.SyncChannel, [schemas.SyncChannel.id == row.id], fields)
    row.deleted_at = ended
    row.status = "deleted"
    row.synced_by_user_id = None
    if is_private is not None:
        row.is_private = is_private
    if row.sync_id:
        invalidate_sync_fanout_for_syncs([row.sync_id])
    log_info(
        "sync_channel_soft_deleted",
        sync_channel_id=row.id,
        sync_id=row.sync_id,
        channel_id=row.channel_id,
        reason=reason,
    )
    return True


def _announce_soft_delete(
    workspace,
    channel_id: str,
    reason: str,
    *,
    rows: list,
    syncs: dict,
    group_id: int | None,
    stays: bool,
    person: str | None = None,
    acting_workspace: str | None = None,
    channel_notice: str | None = None,
    purge: bool = False,
    dm: bool = True,
    hold_channels: set[tuple[int, str]] | None = None,
) -> list[tuple[int, str]]:
    """The Slack pass for one Channel, run once after every row for it is marked."""
    history = purge
    days = None
    if not history and is_local_workspace(workspace):
        days = soft_delete_retention_days()
    action = _REASON_ACTION.get(reason, "Leave")
    note = _REASON_NOTE.get(reason)
    manager = (person or "").strip() or None
    if acting_workspace and reason in {"left_group", "removed_from_group", "group_disbanded"} and not manager:
        manager = acting_workspace
    texts: list[tuple[int, str]] = []
    seen: set[int] = set()
    if channel_notice:
        texts = [(row.sync_id, channel_notice) for row in rows if row.sync_id]
        if is_local_workspace(workspace):
            notify_channel_or_managers(workspace, channel_id, channel_notice, fallback_dm=False)
        for sync_id, text in texts:
            if sync_id:
                post_to_other_sync_channels(workspace, sync_id, text, hold_channels=hold_channels)
    else:
        for row in rows:
            if not row.sync_id or row.sync_id in seen:
                continue
            seen.add(row.sync_id)
            text = deliver_action_notice(
                workspace,
                row,
                action=action,
                manager=manager,
                group_name=group_name_for_sync(syncs.get(row.sync_id)),
                retention_days=days,
                history_deleted=history,
                note=note,
                replicate=False,
                attach_workspace=manager != acting_workspace,
                dm=dm,
                sync=syncs.get(row.sync_id),
                hold_channels=hold_channels,
            )
            texts.append((row.sync_id, text))
    local = is_local_workspace(workspace)
    token = get_bot_token(workspace) if local else None
    if local and not stays:
        _leave_channel_id(workspace, channel_id, token=token)
        log_info("sync_channel_left_channel", channel_id=channel_id, workspace_id=workspace.id)
    if getattr(workspace, "team_id", None):
        invalidate_home_tab_caches_for_team(workspace.team_id)
    invalidate_group_member_homes(group_id, exclude_workspace_id=workspace.id)
    return texts


def soft_delete_sync_channels(
    workspace,
    channel_id: str,
    reason: str,
    *,
    synced_by_user_id: str | None = None,
    sync_ids: set[int] | None = None,
    person: str | None = None,
    acting_workspace: str | None = None,
    is_private: bool | None = None,
    replicate: bool = True,
    channel_notice: str | None = None,
    purge: bool = False,
    dm: bool = True,
    hold_channels: set[tuple[int, str]] | None = None,
) -> int:
    """Soft-delete every row with ``deleted_at`` unset for this workspace and Channel, then announce once.

    ``synced_by_user_id`` keeps only the rows that person runs.
    Returns the number of rows marked. ``0`` means none.
    """
    if workspace is None or not channel_id:
        return 0
    channel_rows = get_sync_channels(workspace.id, channel_id)
    rows = channel_rows
    if synced_by_user_id:
        rows = [row for row in rows if getattr(row, "synced_by_user_id", None) == synced_by_user_id]
    if sync_ids is not None:
        rows = [row for row in rows if row.sync_id in sync_ids]
    if not rows:
        return 0
    token = get_bot_token(workspace)
    if person is None and synced_by_user_id and token:
        person, _image = get_user_info(WebClient(token=token), synced_by_user_id)
    marked_ids = {row.id for row in rows}
    # A paused sibling already has SyncBot out, so only another active row keeps it here.
    stays = any(row.id not in marked_ids and getattr(row, "status", None) == "active" for row in channel_rows)
    syncs: dict[int, schemas.Sync | None] = {}
    for row in rows:
        if not row.sync_id or row.sync_id in syncs:
            continue
        syncs[row.sync_id] = DbManager.get_record(schemas.Sync, id=row.sync_id)
    marked = 0
    for row in rows:
        if soft_delete_sync_channel(row, reason, is_private=is_private):
            marked += 1
    if marked:
        first_sync = next((sync for sync in syncs.values() if sync), None)
        texts = _announce_soft_delete(
            workspace,
            channel_id,
            reason,
            rows=rows,
            syncs=syncs,
            group_id=getattr(first_sync, "group_id", None),
            stays=stays,
            person=person,
            acting_workspace=acting_workspace,
            channel_notice=(channel_notice or "").strip() or None,
            purge=purge,
            dm=dm,
            hold_channels=hold_channels,
        )
        if replicate and is_local_workspace(workspace):
            for row in rows:
                _replicate_one_remove(
                    syncs.get(row.sync_id),
                    getattr(syncs.get(row.sync_id), "group_id", None),
                    workspace,
                    row,
                    reason=reason,
                    notice=dict(texts).get(row.sync_id),
                    purge=purge,
                )
    return marked


def _leave_channel_id(workspace, channel_id: str, *, token: str | None = None) -> None:
    token = token or get_bot_token(workspace)
    if not token or not channel_id:
        return
    try:
        WebClient(token=token).conversations_leave(channel=channel_id)
    except Exception as exc:
        level = log_info if slack_error_code(exc) in _LEAVE_OK else log_warning
        level("channel_leave_failed", channel_id=channel_id, error=str(exc))


def _replicate_one_remove(
    sync, group_id, workspace, row, reason: str | None = None, notice: str | None = None, purge: bool = False
) -> None:
    if not sync or not group_id or not workspace or not row:
        return
    try:
        from federation.replicate import replicate_sync_channel_remove

        group = DbManager.get_record(schemas.WorkspaceGroup, id=group_id)
        if group:
            replicate_sync_channel_remove(
                sync,
                group,
                team_id=workspace.team_id,
                channel_id=row.channel_id,
                reason=reason,
                notice=notice,
                purge=purge,
            )
    except Exception:
        log_warning("federation_replicate_sync_leave_failed", sync_id=getattr(sync, "id", None))
