"""Pause, resume, rejoin, and the other SyncChannel transitions.

``deleted_at`` unset means the row has not been removed. It may be ``active``,
``paused``, or ``pending``. Who may see a row stays in :mod:`helpers.synced_by`.

Imports submodules only. ``leave_sync`` is imported inside the functions that
need it, because that module imports :func:`get_sync_channels` from here.
"""

from datetime import datetime

from slack_sdk.web import WebClient

from db import DbManager, schemas
from helpers.channel_state import (
    get_slack_channel_private,
    refresh_channel_privacy,
    set_is_private,
    update_channel_privacy,
)
from helpers.conversations import (
    ConversationAccessError,
    clear_user_authorization,
    ensure_bot_in_conversation,
    inspect_bot_channel_access,
)
from helpers.core import format_admin_label, format_error_dm, is_workspace_manager
from helpers.notifications import (
    notify_channel_or_managers,
    notify_managers,
    notify_synced_channels,
    notify_user_dm,
)
from helpers.settings import soft_delete_retention_days
from helpers.slack_api import slack_error_code
from helpers.sync_participation import (
    format_action_notice,
    invalidate_channel_memberships,
    invalidate_sync_fanout_for_syncs,
)
from helpers.synced_by import (
    _MANAGER_ONLY,
    can_manage_sync,
    set_synced_by,
    sync_privacy_conflict,
    viewer_context,
)
from helpers.workspace import (
    get_bot_token,
    get_workspace_by_id,
    lookup_channel_name,
    resolve_workspace_name,
)
from helpers.workspace_kind import is_local_workspace
from logger import log_error, log_info, log_warning

PRIVACY_CHANGED_ENDED = ":warning: This Channel's privacy changed, so that Sync ended."


def _set_status(sync_channel: schemas.SyncChannel, status: str) -> None:
    DbManager.update_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.id == sync_channel.id],
        {schemas.SyncChannel.status: status},
    )
    sync_channel.status = status


def get_sync_channels(workspace_id: int, channel_id: str | None = None) -> list:
    """This workspace's rows with ``deleted_at`` unset, optionally one Channel.

    Paused and pending count.
    """
    filters = [
        schemas.SyncChannel.workspace_id == workspace_id,
        schemas.SyncChannel.deleted_at.is_(None),
    ]
    if channel_id:
        filters.append(schemas.SyncChannel.channel_id == channel_id)
    return DbManager.find_records(schemas.SyncChannel, filters)


def revive_sync_channel(
    row: schemas.SyncChannel,
    *,
    client: WebClient,
    workspace,
    acting_user_id: str,
    publishes: bool,
    subscribes: bool,
    reaction_style: str | None,
    context: dict | None = None,
    via: str = "join",
) -> bool | None:
    """Bring a soft-deleted row back to ``active``. PostMeta stays on this id.

    Returns True when SyncBot is in the Channel again, or None when the invite
    failed and the row is ``deleted`` once more. The caller posts the Join or
    Create notice and replicates that upsert, so the peer hears one sentence.
    """
    if row is None or not getattr(row, "id", None) or workspace is None:
        return None
    prior_deleted_at = row.deleted_at
    DbManager.update_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.id == row.id],
        {
            schemas.SyncChannel.deleted_at: None,
            schemas.SyncChannel.status: "active",
            schemas.SyncChannel.publishes: publishes,
            schemas.SyncChannel.subscribes: subscribes,
            schemas.SyncChannel.reaction_style: reaction_style,
            schemas.SyncChannel.synced_by_user_id: None,
        },
    )
    row.deleted_at = None
    row.status = "active"
    row.publishes = publishes
    row.subscribes = subscribes
    row.reaction_style = reaction_style
    row.synced_by_user_id = None
    if row.channel_id:
        invalidate_channel_memberships(row.channel_id)
    if row.sync_id:
        invalidate_sync_fanout_for_syncs([row.sync_id])

    def _rollback() -> None:
        DbManager.update_records(
            schemas.SyncChannel,
            [schemas.SyncChannel.id == row.id],
            {
                schemas.SyncChannel.deleted_at: prior_deleted_at,
                schemas.SyncChannel.status: "deleted",
                schemas.SyncChannel.synced_by_user_id: None,
            },
        )
        row.deleted_at = prior_deleted_at
        row.status = "deleted"
        row.synced_by_user_id = None
        if row.channel_id:
            invalidate_channel_memberships(row.channel_id)
        if row.sync_id:
            invalidate_sync_fanout_for_syncs([row.sync_id])

    try:
        is_private = ensure_bot_in_conversation(
            client,
            row.channel_id,
            team_id=getattr(workspace, "team_id", None),
            acting_user_id=acting_user_id,
            context=context,
        )
    except ConversationAccessError as exc:
        _rollback()
        notify_user_dm(
            client,
            acting_user_id,
            format_error_dm(
                f":warning: {exc}",
                {"event": "sync_channel_revive_failed", "channel": row.channel_id, "via": via},
            ),
        )
        return None
    except Exception as exc:
        _rollback()
        log_error("sync_channel_revive_failed", sync_channel_id=row.id, via=via, error=str(exc))
        notify_user_dm(
            client,
            acting_user_id,
            format_error_dm(
                ":warning: SyncBot could not be added to that Channel. Please try again.",
                {"event": "sync_channel_revive_failed", "channel": row.channel_id, "via": via},
            ),
        )
        return None

    set_is_private(row, is_private=is_private)
    if is_private:
        set_synced_by(row, acting_user_id)
    log_info(
        "sync_channel_revived",
        sync_channel_id=row.id,
        sync_id=row.sync_id,
        channel_id=row.channel_id,
        via=via,
    )
    return True


def pause_sync_channel(
    sync_channel: schemas.SyncChannel,
    *,
    via: str = "home",
    leave: bool = False,
    who: str | None = None,
) -> bool:
    """Pause one row, then post the action notice and DM this Workspace's managers.

    ``leave`` leaves the Channel only when no other active row of this workspace
    still uses it. Returns False when the row was already paused.
    """
    from helpers.leave_sync import group_name_for_sync_id

    if sync_channel is None or getattr(sync_channel, "status", None) != "active":
        return False
    won = DbManager.update_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.id == sync_channel.id, schemas.SyncChannel.status == "active"],
        {schemas.SyncChannel.status: "paused"},
    )
    if not won:
        return False
    sync_channel.status = "paused"
    workspace = get_workspace_by_id(sync_channel.workspace_id)
    if sync_channel.sync_id:
        invalidate_sync_fanout_for_syncs([sync_channel.sync_id])
    group_name = group_name_for_sync_id(sync_channel.sync_id)
    action = "Removed" if via == "kick" else "Archive" if via == "archive" else "Pause"
    note = None
    if via == "archive":
        note = "Syncing resumes when the Channel is unarchived."
    elif via == "kick":
        note = "SyncBot is not in the Channel."
    deliver_action_notice(
        workspace,
        sync_channel,
        action=action,
        manager=who if via == "home" else None,
        group_name=group_name,
        note=note,
        post_in_channel=via == "home",
    )
    if leave and workspace:
        _leave_unless_active(workspace, sync_channel)
    log_info("sync_paused", sync_channel_id=sync_channel.id, channel_id=sync_channel.channel_id, via=via)
    return True


def pause_archived_channel(workspace, channel_id: str) -> None:
    """Pause every row with ``deleted_at`` unset that is not already paused. The bot stays."""
    rows = [row for row in get_sync_channels(workspace.id, channel_id) if getattr(row, "status", None) != "paused"]
    if not rows:
        return
    for row in rows:
        pause_sync_channel(row, via="archive", leave=False)


def resume_unarchived_channel(workspace, channel_id: str) -> None:
    """Resume paused rows when Slack unarchives a Channel the bot is still in."""
    from helpers.leave_sync import group_name_for_sync_id

    rows = [row for row in get_sync_channels(workspace.id, channel_id) if getattr(row, "status", None) == "paused"]
    if not rows:
        return
    token = get_bot_token(workspace)
    member = False
    if token:
        _private, member = inspect_bot_channel_access(WebClient(token=token), channel_id)
    if not member:
        for row in rows:
            group_name = group_name_for_sync_id(row.sync_id)
            deliver_action_notice(
                workspace,
                row,
                action="Unarchive",
                group_name=group_name,
                note="SyncBot is not in the Channel.",
                post_in_channel=False,
            )
        return
    for row in rows:
        _set_status(row, "active")
        if row.sync_id:
            invalidate_sync_fanout_for_syncs([row.sync_id])
        group_name = group_name_for_sync_id(row.sync_id)
        deliver_action_notice(workspace, row, action="Unarchive", group_name=group_name)
    log_info("sync_resumed", channel_id=channel_id, via="unarchive")


def purge_slack_deleted_channel(workspace, channel_id: str) -> None:
    """Delete this workspace's history for a Slack Channel that is gone. Other workspaces stay."""
    from helpers.leave_sync import group_name_for_sync
    from helpers.sync_cleanup import purge_sync_channels

    rows = get_sync_channels(workspace.id, channel_id)
    if not rows:
        return
    from helpers.notifications import claim_once

    purge_sync_channels(rows)
    if not claim_once(f"channel_deleted:{workspace.id}:{channel_id}"):
        return
    for row in rows:
        sync = DbManager.get_record(schemas.Sync, id=row.sync_id) if row.sync_id else None
        group_name = group_name_for_sync(sync) if sync else None
        text = deliver_action_notice(
            workspace,
            row,
            action="Channel deleted",
            group_name=group_name,
            history_deleted=True,
            replicate=False,
            post_in_channel=False,
        )
        if sync and getattr(sync, "group_id", None):
            from helpers.leave_sync import _replicate_one_remove

            _replicate_one_remove(
                sync,
                sync.group_id,
                workspace,
                row,
                reason="channel_deleted",
                notice=text,
                purge=True,
            )
    log_info("slack_channel_deleted", channel_id=channel_id, workspace_id=workspace.id, rows=len(rows))


def _leave_unless_active(workspace, sync_channel: schemas.SyncChannel) -> None:
    """Leave the Channel unless another active row of this workspace still uses it."""
    from helpers.leave_sync import _leave_channel_id

    if not sync_channel.channel_id:
        return
    still_active = any(
        row.id != sync_channel.id and getattr(row, "status", None) == "active"
        for row in get_sync_channels(workspace.id, sync_channel.channel_id)
    )
    if not still_active:
        _leave_channel_id(workspace, sync_channel.channel_id)


def resume_sync_channel(
    row: schemas.SyncChannel,
    workspace,
    acting_user_id: str,
    *,
    client: WebClient,
    join: bool,
    context: dict | None = None,
) -> str | None:
    """Resume one paused Channel Sync for Home Resume or a re-add. Returns a denial sentence, or None.

    ``active`` is written before the invite so a concurrent re-add event finds nothing to do.
    Slack's ``is_archived`` error leaves the row paused. There is no ``is_archived`` column.
    A stored-public ``channel_not_found`` deletes this workspace's history for that Channel.
    Every other failure writes ``paused`` back.
    """
    from helpers.leave_sync import group_name_for_sync_id, with_group

    if not is_workspace_manager(client, acting_user_id, workspace.team_id):
        return f"{_MANAGER_ONLY} resume this Sync."
    viewer = viewer_context(workspace, acting_user_id, client)
    allowed, denied = can_manage_sync(row, viewer, action="resume it")
    if not allowed:
        return denied
    token = get_bot_token(workspace)
    if not token:
        return ":warning: SyncBot has no bot token for this Workspace, so it cannot be added to the Channel."
    bot_client = WebClient(token=token)
    _set_status(row, "active")
    if join:
        try:
            is_private: bool | None = ensure_bot_in_conversation(
                bot_client,
                row.channel_id,
                team_id=workspace.team_id,
                acting_user_id=acting_user_id,
                context=context,
            )
        except ConversationAccessError as exc:
            code = slack_error_code(exc.__cause__)
            gone_public = code == "channel_not_found" and getattr(row, "is_private", None) is False
            if code == "is_archived":
                _set_status(row, "paused")
                group_name = group_name_for_sync_id(row.sync_id)
                which = f"the Sync{with_group(group_name)}" if group_name else "this Sync"
                return (
                    f":warning: This Channel is archived, so {which} stays paused. "
                    "It resumes when the Channel is unarchived."
                )
            if gone_public:
                _set_status(row, "paused")
                purge_slack_deleted_channel(workspace, row.channel_id)
                return ":warning: This Channel was deleted, so this Workspace's synced history for it was deleted."
            _set_status(row, "paused")
            log_warning("resume_sync_membership_failed", channel_id=row.channel_id, error=str(exc))
            notify_user_dm(client, acting_user_id, f":warning: {exc}")
            return f":warning: {exc}"
    else:
        is_private = get_slack_channel_private(bot_client, row.channel_id)
    stored = getattr(row, "is_private", None)
    if is_private is not None and stored is not None and stored != is_private:
        update_channel_privacy(row, is_private=is_private)
        return PRIVACY_CHANGED_ENDED
    if sync_privacy_conflict(row.sync_id, is_private, exclude_row_id=row.id) is not None:
        _set_status(row, "paused")
        _leave_unless_active(workspace, row)
        group_name = group_name_for_sync_id(row.sync_id)
        which = f"the Sync{with_group(group_name)}" if group_name else "this Sync"
        if is_private:
            return f":warning: This Channel is private, but {which} has public Channels."
        return f":warning: This Channel is public, but {which} has private Channels."
    set_is_private(row, is_private=is_private)
    if getattr(row, "is_private", None) is True:
        set_synced_by(row, acting_user_id)
    if row.sync_id:
        invalidate_sync_fanout_for_syncs([row.sync_id])
    name = format_admin_label(client, acting_user_id, workspace)
    group_name = group_name_for_sync_id(row.sync_id)
    deliver_action_notice(workspace, row, action="Resume", manager=name, group_name=group_name)
    log_info("sync_resumed", sync_channel_id=row.id, channel_id=row.channel_id, via="home" if join else "member_joined")
    return None


def soft_delete_rows_for_user(workspace, user_id: str, reason: str, *, client: WebClient) -> int:
    """Soft-delete every row with ``deleted_at`` unset that this person runs, one announcement per Channel."""
    from helpers.leave_sync import soft_delete_sync_channels

    if workspace is None or not user_id:
        return 0
    rows = [row for row in get_sync_channels(workspace.id) if getattr(row, "synced_by_user_id", None) == user_id]
    channels = {row.channel_id for row in rows if row.channel_id}
    marked = 0
    for channel_id in channels:
        marked += soft_delete_sync_channels(workspace, channel_id, reason, synced_by_user_id=user_id)
    return marked


def end_user_authorization(team_id: str | None, user_id: str | None, *, client: WebClient) -> int:
    """Forget this person's user token and soft-delete the private rows they run."""
    clear_user_authorization(team_id, user_id)
    workspace = DbManager.get_record(schemas.Workspace, id=team_id) if team_id else None
    if not workspace or not user_id:
        return 0
    return soft_delete_rows_for_user(workspace, user_id, "authorization_ended", client=client)


def soft_delete_private_syncs_for_settings(workspace, client: WebClient) -> None:
    """Soft-delete every private Channel Sync when the workspace turns that setting off."""
    from helpers.leave_sync import soft_delete_sync_channels

    team_id = getattr(workspace, "team_id", None)
    if not workspace or not team_id:
        return
    for row in get_sync_channels(workspace.id):
        if getattr(row, "is_private", None) is None:
            refresh_channel_privacy(row, client)
        if getattr(row, "is_private", None) is not True:
            continue
        soft_delete_sync_channels(workspace, row.channel_id, "private_syncing_off")


def rejoin_sync_channel(group_id: int, workspace_id: int, channel_id: str):
    """The removed row Create or Join should turn back on, or None.

    A Sync that already has another member, including a peer stub, is the one
    they came back to. Otherwise it is the Sync this Channel left most recently.
    A row with ``deleted_at`` unset for this workspace and Channel on that Sync is left alone.
    """
    if not group_id or not workspace_id or not channel_id:
        return None
    rows = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.workspace_id == workspace_id,
            schemas.SyncChannel.channel_id == channel_id,
            schemas.SyncChannel.status == "deleted",
        ],
    )
    candidates = []
    for row in rows:
        if not getattr(row, "sync_id", None) or getattr(row, "deleted_at", None) is None:
            continue
        sync = DbManager.get_record(schemas.Sync, id=row.sync_id)
        if not sync or getattr(sync, "group_id", None) != group_id:
            continue
        already = DbManager.find_records(
            schemas.SyncChannel,
            [
                schemas.SyncChannel.sync_id == row.sync_id,
                schemas.SyncChannel.workspace_id == workspace_id,
                schemas.SyncChannel.channel_id == channel_id,
                schemas.SyncChannel.deleted_at.is_(None),
            ],
        )
        if already:
            continue
        others = DbManager.find_records(
            schemas.SyncChannel,
            [
                schemas.SyncChannel.sync_id == row.sync_id,
                schemas.SyncChannel.deleted_at.is_(None),
                schemas.SyncChannel.id != row.id,
            ],
        )
        candidates.append((row, bool(others)))
    if not candidates:
        return None
    with_member = [row for row, member in candidates if member]
    pool = with_member or [row for row, _member in candidates]

    def _removed_at(row):
        stamp = row.deleted_at
        if stamp is not None and getattr(stamp, "tzinfo", None):
            stamp = stamp.replace(tzinfo=None)
        return stamp or datetime.min

    return max(pool, key=_removed_at)


def leave_channel_without_sync(client: WebClient, channel_id: str, *, workspace=None, text: str | None = None) -> str:
    """Tell a Channel with no Sync that SyncBot is leaving, then leave. Returns the outcome.

    When a ``deleted`` row exists for that Channel, the notice says how long its history waits.
    """
    deleted = []
    if workspace is not None and getattr(workspace, "id", None):
        deleted = DbManager.find_records(
            schemas.SyncChannel,
            [
                schemas.SyncChannel.workspace_id == workspace.id,
                schemas.SyncChannel.channel_id == channel_id,
                schemas.SyncChannel.status == "deleted",
            ],
        )
    if text:
        outcome = "left_denied"
    elif deleted:
        outcome = "left_deleted"
        from helpers.leave_sync import retention_sentence

        kept = retention_sentence(workspace) if workspace is not None else ""
        if not kept:
            kept = f"Sync data will be retained for {soft_delete_retention_days()} days."
        text = f":wave: {kept}"
    else:
        outcome = "left_no_sync"
        text = (
            ":wave: Hello! I'm SyncBot. I was added to this Channel, but this Channel doesn't seem to be "
            "part of a Channel Sync. I'm leaving now. Please open the SyncBot Home tab to Create Sync or Join Sync."
        )
    try:
        client.chat_postMessage(channel=channel_id, text=text)
        client.conversations_leave(channel=channel_id)
    except Exception as exc:
        log_warning("leave_channel_without_sync_failed", channel_id=channel_id, error=str(exc))
    return outcome


def post_to_other_sync_channels(
    workspace, sync_id: int, message: str, *, hold_channels: set[tuple[int, str]] | None = None
) -> None:
    """Post *message* in the other active Channels of this Sync. A failed post is logged."""
    if not workspace or not sync_id or not message:
        return
    rows = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.sync_id == sync_id,
            schemas.SyncChannel.workspace_id != workspace.id,
            schemas.SyncChannel.deleted_at.is_(None),
            schemas.SyncChannel.status == "active",
        ],
    )
    posted: set[tuple[int, str]] = set()
    for row in rows:
        member_ws = get_workspace_by_id(row.workspace_id)
        token = get_bot_token(member_ws) if member_ws else None
        if not token or not row.channel_id:
            continue
        key = (row.workspace_id, row.channel_id)
        if key in posted or (hold_channels and key in hold_channels):
            continue
        posted.add(key)
        client = WebClient(token=token)
        if notify_synced_channels(client, [row.channel_id], message):
            continue
        log_warning(
            "notice_not_posted",
            channel_id=row.channel_id,
            workspace_id=row.workspace_id,
        )


def deliver_action_notice(
    workspace,
    row,
    *,
    action: str,
    manager: str | None = None,
    group_name: str | None = None,
    retention_days: int | None = None,
    history_deleted: bool = False,
    note: str | None = None,
    announce: bool = True,
    replicate: bool = True,
    attach_workspace: bool = True,
    post_in_channel: bool = True,
    dm: bool = True,
    label_for: dict[int, str] | None = None,
    sync=None,
    hold_channels: set[tuple[int, str]] | None = None,
) -> str:
    """Post the Channel notice where SyncBot is in, and DM only the Workspace that acted.

    Call after the row is saved. ``post_in_channel`` is for the Channel the bot is still in.
    A private Channel's DM reaches managers still in it.
    """
    ws_name = resolve_workspace_name(workspace) if workspace else "Workspace"
    sync_id = getattr(row, "sync_id", None)
    channel_text = format_action_notice(
        action=action,
        workspace_name=ws_name,
        group_name=group_name,
        manager=manager,
        sync_id=sync_id,
        attach_workspace=attach_workspace,
        label_for=label_for,
    )
    private = getattr(row, "is_private", None) is True
    channel_id = getattr(row, "channel_id", None)
    local = bool(workspace and is_local_workspace(workspace) and channel_id)
    if local and post_in_channel:
        notify_channel_or_managers(workspace, channel_id, channel_text, fallback_dm=False, private=private)
    if local and dm:
        stored = (getattr(row, "channel_name", None) or "").strip()
        channel_name = stored or lookup_channel_name(channel_id, workspace) or channel_id
        detail = format_action_notice(
            action=action,
            workspace_name=ws_name,
            group_name=group_name,
            manager=manager,
            sync_id=sync_id,
            channel_name=channel_name,
            retention_days=retention_days,
            history_deleted=history_deleted,
            for_managers=True,
            note=note,
            attach_workspace=attach_workspace,
            label_for=label_for,
        )
        token = get_bot_token(workspace)
        if token:
            notify_managers(
                WebClient(token=token),
                detail,
                team_id=getattr(workspace, "team_id", None),
                channel_id=channel_id,
                private=private,
            )
    if announce and sync_id:
        if replicate:
            announce_sync_channels(workspace, row, channel_text, sync=sync, hold_channels=hold_channels)
        else:
            if hold_channels:
                post_to_other_sync_channels(workspace, sync_id, channel_text, hold_channels=hold_channels)
            else:
                post_to_other_sync_channels(workspace, sync_id, channel_text)
    return channel_text


def announce_sync_channels(
    workspace,
    sync_channel: schemas.SyncChannel,
    message: str,
    *,
    sync=None,
    hold_channels: set[tuple[int, str]] | None = None,
) -> None:
    """Post *message* to the other Channels in this Sync, on this instance and on a peer.

    The peer posts this same string. A snapshot upsert does not use this function.
    """
    if workspace and getattr(sync_channel, "sync_id", None) and message:
        if hold_channels:
            post_to_other_sync_channels(workspace, sync_channel.sync_id, message, hold_channels=hold_channels)
        else:
            post_to_other_sync_channels(workspace, sync_channel.sync_id, message)
    replicate_sync_channel_status(sync_channel, workspace, sync=sync, notice=message or None)


def replicate_sync_channel_status(
    sync_channel: schemas.SyncChannel,
    workspace=None,
    *,
    sync=None,
    notice: str | None = None,
) -> None:
    """Push this row's status to federation peers. A failure is logged, never raised."""
    if not sync_channel.sync_id:
        return
    try:
        from federation.replicate import replicate_sync_channel_upsert

        sync = sync or DbManager.get_record(schemas.Sync, id=sync_channel.sync_id)
        if not sync or not sync.group_id:
            return
        group = DbManager.get_record(schemas.WorkspaceGroup, id=sync.group_id)
        workspace = workspace or get_workspace_by_id(sync_channel.workspace_id)
        if group and workspace:
            replicate_sync_channel_upsert(sync, group, sync_channel, workspace, notice=notice)
    except Exception as exc:
        log_warning("federation_replicate_sync_status_failed", sync_id=sync_channel.sync_id, error=str(exc))
