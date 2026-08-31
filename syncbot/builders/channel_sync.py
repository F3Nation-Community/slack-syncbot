"""Channel sync form builders."""

import logging

import helpers
from builders._common import (
    _format_channel_ref,
)
from db import DbManager
from db.schemas import PostMeta, Sync, SyncChannel, Workspace, WorkspaceGroup, WorkspaceGroupMember
from slack import actions, orm
from slack.blocks import (
    context as block_context,
)
from slack.blocks import (
    section,
)

_logger = logging.getLogger(__name__)


def _build_inline_channel_sync(
    blocks: list,
    group: WorkspaceGroup,
    workspace_record: Workspace,
    other_members: list[WorkspaceGroupMember],
    context: dict | None = None,
) -> None:
    """Append channel-sync blocks inline under a group on the Home tab.

    Shows:
    - Active synced channels with Pause/Stop buttons
    - Paused synced channels with Resume/Stop buttons
    - Channels waiting for a subscriber with Stop Syncing button
    - Available channels from other members with Subscribe button
    """
    syncs_for_group = DbManager.find_records(
        Sync,
        [Sync.group_id == group.id],
    )

    published_syncs: list[tuple[Sync, SyncChannel, list[SyncChannel], bool]] = []
    waiting_syncs: list[tuple[Sync, SyncChannel]] = []
    available_syncs: list[tuple[Sync, list[SyncChannel]]] = []

    for sync in syncs_for_group:
        channels = DbManager.find_records(
            SyncChannel,
            [SyncChannel.sync_id == sync.id, SyncChannel.deleted_at.is_(None)],
        )
        my_channel = next((c for c in channels if c.workspace_id == workspace_record.id), None)
        other_channels = [c for c in channels if c.workspace_id != workspace_record.id]

        if my_channel and other_channels:
            is_paused = my_channel.status == "paused"
            published_syncs.append((sync, my_channel, other_channels, is_paused))
        elif my_channel and not other_channels:
            waiting_syncs.append((sync, my_channel))
        elif not my_channel and other_channels:
            if sync.sync_mode == "direct" and sync.target_workspace_id != workspace_record.id:
                continue
            # A sync whose publisher has left has no source to subscribe to, so
            # don't advertise it as available. It is cleaned up when its last
            # remaining member stops (see handle_stop_sync_confirm).
            if not any(c.workspace_id == sync.publisher_workspace_id for c in other_channels):
                continue
            available_syncs.append((sync, other_channels))

    published_syncs.sort(key=lambda t: (t[0].title or "").lower())
    waiting_syncs.sort(key=lambda t: (t[0].title or "").lower())
    available_syncs.sort(key=lambda t: (t[0].title or "").lower())

    if not published_syncs and not waiting_syncs and not available_syncs:
        return

    blocks.append(section("*Synced Channels*"))

    for sync, my_ch, other_chs, is_paused in published_syncs:
        my_ref = _format_channel_ref(my_ch.channel_id, workspace_record, is_local=True)

        # Workspace names for bracket: local first, then others; append (Paused) per workspace that paused
        local_name = helpers.resolve_workspace_name(workspace_record) or f"Workspace {workspace_record.id}"
        if my_ch.status == "paused":
            local_name = f"{local_name} (Paused)"
        other_names: list[str] = []
        for other_channel in other_chs:
            other_ws = helpers.get_workspace_by_id(other_channel.workspace_id, context=context)
            name = helpers.resolve_workspace_name(other_ws) if other_ws else f"Workspace {other_channel.workspace_id}"
            if other_channel.status == "paused":
                name = f"{name} (Paused)"
            other_names.append(name)
        all_ws_names = [local_name] + other_names

        if is_paused:
            icon = ":double_vertical_bar:"
            toggle_btn = orm.ButtonElement(
                label="Resume Syncing",
                action=f"{actions.CONFIG_RESUME_SYNC}_{sync.id}",
                value=str(sync.id),
            )
        else:
            icon = ":arrows_counterclockwise:"
            toggle_btn = orm.ButtonElement(
                label="Pause Syncing",
                action=f"{actions.CONFIG_PAUSE_SYNC}_{sync.id}",
                value=str(sync.id),
            )

        blocks.append(section(f"{icon} {my_ref}"))

        context_parts: list[str] = []
        if is_paused:
            status_tag = "Paused"
        else:
            status_tag = "Active"

        context_parts.append(f"Status: `{status_tag}`")

        if sync.sync_mode == "direct":
            mode_tag = "1-to-1"
        else:
            mode_tag = "Available to Any"

        context_parts.append(f"Type: `{mode_tag}`")

        if all_ws_names:
            context_parts.append(f"Members: `{', '.join(all_ws_names)}`")

        if getattr(my_ch, "created_at", None):
            context_parts.append(f"Synced Since: `{my_ch.created_at:%B %d, %Y}`")

        msg_count = DbManager.count_records(
            PostMeta,
            [PostMeta.sync_channel_id == my_ch.id],
        )
        context_parts.append(f"Messages Tracked: `{msg_count}`")

        if context_parts:
            blocks.append(block_context("\n".join(context_parts)))
        if sync.publisher_workspace_id == workspace_record.id:
            # The publisher is the source of the channel: their teardown removes
            # the sync for everyone (Unpublish). "Stop Syncing" here would delete
            # only the publisher's own channel and strand the sync with a
            # publisher that no longer has one.
            teardown_btn = orm.ButtonElement(
                label="Unpublish",
                action=f"{actions.CONFIG_UNPUBLISH_CHANNEL}_{sync.id}",
                value=str(sync.id),
                style="danger",
            )
        else:
            teardown_btn = orm.ButtonElement(
                label="Stop Syncing",
                action=f"{actions.CONFIG_STOP_SYNC}_{sync.id}",
                value=str(sync.id),
                style="danger",
            )
        blocks.append(orm.ActionsBlock(elements=[toggle_btn, teardown_btn]))

    for sync, my_ch in waiting_syncs:
        if sync.publisher_workspace_id == workspace_record.id:
            blocks.append(section(f":outbox_tray: <#{my_ch.channel_id}> — _waiting for subscribers_"))
            teardown_btn = orm.ButtonElement(
                label="Unpublish",
                action=f"{actions.CONFIG_UNPUBLISH_CHANNEL}_{sync.id}",
                value=str(sync.id),
                style="danger",
            )
        else:
            # The publisher has left, so there is nothing to sync with anymore.
            # Let the stranded member remove their channel; stopping the last one
            # purges the empty sync (see handle_stop_sync_confirm).
            blocks.append(section(f":outbox_tray: <#{my_ch.channel_id}> — _publisher left; no longer syncing_"))
            teardown_btn = orm.ButtonElement(
                label="Stop Syncing",
                action=f"{actions.CONFIG_STOP_SYNC}_{sync.id}",
                value=str(sync.id),
                style="danger",
            )
        blocks.append(orm.ActionsBlock(elements=[teardown_btn]))

    for sync, _other_chs in available_syncs:
        publisher_ws = helpers.get_workspace_by_id(sync.publisher_workspace_id, context=context)
        publisher_name = helpers.resolve_workspace_name(publisher_ws) if publisher_ws else "another Workspace"
        if sync.sync_mode == "direct":
            mode_tag = "1-to-1"
        else:
            mode_tag = "Available to Any"

        blocks.append(section(":inbox_tray: Published Channel Available"))
        blocks.append(block_context(f"Type: `{mode_tag}`\nPublisher: `{publisher_name}`\nChannel Name: `{sync.title}`"))
        blocks.append(
            orm.ActionsBlock(
                elements=[
                    orm.ButtonElement(
                        label="Subscribe",
                        action=f"{actions.CONFIG_SUBSCRIBE_CHANNEL}_{sync.id}",
                        value=str(sync.id),
                    ),
                ]
            )
        )
