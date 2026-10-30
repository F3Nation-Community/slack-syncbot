"""Channel sync form builders."""

from sqlalchemy import or_

import helpers
from builders._common import _format_channel_ref
from db import DbManager
from db.schemas import PostMeta, Sync, SyncChannel, Workspace, WorkspaceGroup, WorkspaceGroupMember
from helpers.synced_by import Viewer, can_manage_sync, viewer_context
from slack import actions, orm
from slack.blocks import header, section


def _build_row_action_buttons(sync: Sync, my_ch: SyncChannel, *, include_pause: bool) -> list[orm.ButtonElement]:
    """Edit, Pause or Resume, and Leave for one local Channel; Leave only while it waits for migration."""
    leave = orm.ButtonElement(
        label=":wave: Leave Sync",
        action=f"{actions.CONFIG_LEAVE_SYNC}_{sync.id}",
        value=str(sync.id),
        style="danger",
    )
    if getattr(my_ch, "status", None) == "pending":
        return [leave]
    buttons: list[orm.ButtonElement] = [
        orm.ButtonElement(
            label=":pencil2: Edit Sync",
            action=f"{actions.CONFIG_EDIT_SYNC}_c_{my_ch.id}",
            value=f"c:{my_ch.id}",
        )
    ]
    if include_pause:
        if getattr(my_ch, "status", None) == "paused":
            buttons.append(
                orm.ButtonElement(
                    label=":arrow_forward: Resume Sync",
                    action=f"{actions.CONFIG_RESUME_SYNC}_{sync.id}",
                    value=str(sync.id),
                )
            )
        else:
            buttons.append(
                orm.ButtonElement(
                    label=":double_vertical_bar: Pause Sync",
                    action=f"{actions.CONFIG_PAUSE_SYNC}_{sync.id}",
                    value=str(sync.id),
                )
            )
    buttons.append(leave)
    return buttons


def _build_inline_channel_sync(
    blocks: list,
    group: WorkspaceGroup,
    workspace_record: Workspace,
    other_members: list[WorkspaceGroupMember],
    context: dict | None = None,
    *,
    user_id: str | None = None,
    client=None,
    viewer: Viewer | None = None,
) -> None:
    """Append channel-sync blocks inline under a group on the Home tab.

    Shows:
    - Active synced channels with Pause/Resume and Leave Sync
    - Paused synced channels with Resume/Leave Sync
    - Channels waiting for others to join with Leave Sync
    - Available syncs from other members with Join Sync

    A row this workspace already has is shown when :func:`can_manage_sync` allows
    it. Otherwise the whole Sync is skipped, so it never renders as Available.
    An available card is shown when every publisher row passes the same check.
    """
    if viewer is None:
        viewer = viewer_context(workspace_record, user_id, client)
    syncs_for_group = DbManager.find_records(
        Sync,
        [Sync.group_id == group.id],
    )

    published_syncs: list[tuple[Sync, SyncChannel, list[SyncChannel], list[SyncChannel]]] = []
    waiting_syncs: list[tuple[Sync, SyncChannel]] = []
    available_syncs: list[tuple[Sync, list[SyncChannel]]] = []

    for sync in syncs_for_group:
        # Live rows, plus uninstalled ones for the Members line only.
        rows = DbManager.find_records(
            SyncChannel,
            [
                SyncChannel.sync_id == sync.id,
                or_(SyncChannel.deleted_at.is_(None), SyncChannel.status == "uninstalled"),
            ],
        )
        uninstalled = [row for row in rows if getattr(row, "status", None) == "uninstalled"]
        live = [row for row in rows if getattr(row, "status", None) != "uninstalled"]
        my_channel = next((row for row in live if row.workspace_id == workspace_record.id), None)
        if my_channel and not can_manage_sync(my_channel, viewer)[0]:
            continue
        other_channels = [row for row in live if row.workspace_id != workspace_record.id]
        member_channels = other_channels + [row for row in uninstalled if row.workspace_id != workspace_record.id]

        if my_channel and member_channels:
            published_syncs.append((sync, my_channel, member_channels, live))
        elif my_channel:
            waiting_syncs.append((sync, my_channel))
        elif other_channels:
            publishers = [row for row in other_channels if helpers.channel_publishes(row)]
            if not publishers:
                continue
            if not all(can_manage_sync(row, viewer, action="join it")[0] for row in publishers):
                continue
            available_syncs.append((sync, other_channels))

    published_syncs.sort(key=lambda t: (t[0].title or "").lower())
    waiting_syncs.sort(key=lambda t: (t[0].title or "").lower())
    available_syncs.sort(key=lambda t: (t[0].title or "").lower())

    if not published_syncs and not waiting_syncs and not available_syncs:
        return

    blocks.append(header("Synced Channels"))

    for sync, my_ch, member_chs, _live_rows in published_syncs:
        is_paused = my_ch.status == "paused"
        my_ref = _format_channel_ref(my_ch.channel_id, workspace_record, is_local=True)

        other_refs: list[str] = []
        for other_channel in member_chs:
            other_ws = helpers.get_workspace_by_id(other_channel.workspace_id, context=context)
            other_refs.append(helpers.format_sync_member(other_channel, other_ws))

        icon = ":double_vertical_bar:" if is_paused else ":arrows_counterclockwise:"
        blocks.append(section(f"{icon} {my_ref}"))

        context_parts: list[str] = [f"Status: `{helpers.sync_state_label(my_ch)}`"]
        from helpers.workspace_settings import allow_private_channels

        if allow_private_channels(getattr(workspace_record, "team_id", None)):
            flag = getattr(my_ch, "is_private", None)
            if flag is True:
                context_parts.append("Privacy: `Private`")
            elif flag is False:
                context_parts.append("Privacy: `Public`")
        context_parts.append(f"Members: {', '.join(other_refs) if other_refs else '`no other Channels`'}")

        if getattr(my_ch, "created_at", None):
            context_parts.append(f"Synced Since: `{my_ch.created_at:%B %d, %Y}`")

        msg_count = DbManager.count_records(
            PostMeta,
            [PostMeta.sync_channel_id == my_ch.id],
        )
        context_parts.append(f"Messages Tracked: `{msg_count}`")

        blocks.append(section("\n".join(context_parts)))
        blocks.append(orm.ActionsBlock(elements=_build_row_action_buttons(sync, my_ch, include_pause=True)))

    for sync, my_ch in waiting_syncs:
        blocks.append(section(f":outbox_tray: <#{my_ch.channel_id}>"))
        blocks.append(section(f"Status: `{helpers.sync_state_label(my_ch)}`"))
        blocks.append(orm.ActionsBlock(elements=_build_row_action_buttons(sync, my_ch, include_pause=False)))

    if available_syncs:
        blocks.append(section(":inbox_tray: Available Sync Relationships"))
    for sync, other_chs in available_syncs:
        publishers = [c for c in other_chs if helpers.channel_publishes(c)]
        pub_ch = publishers[0] if publishers else None
        publisher_ws = helpers.get_workspace_by_id(pub_ch.workspace_id, context=context) if pub_ch else None
        publisher_name = helpers.resolve_workspace_name(publisher_ws) if publisher_ws else "another Workspace"
        if pub_ch:
            stored = (getattr(pub_ch, "channel_name", None) or "").strip()
            looked = stored or helpers.lookup_channel_name(pub_ch.channel_id, publisher_ws) or pub_ch.channel_id
            channel_label = f"#{str(looked).removeprefix('#')} [{helpers.sync_state_label(pub_ch)}]"
        else:
            channel_label = sync.title or "Unknown"
        lines = [f"From: `{publisher_name}`", f"Channel: `{channel_label}`"]
        from helpers.workspace_settings import allow_private_channels

        if pub_ch and allow_private_channels(getattr(workspace_record, "team_id", None)):
            flag = getattr(pub_ch, "is_private", None)
            if flag is True:
                lines.append("Privacy: `Private`")
            elif flag is False:
                lines.append("Privacy: `Public`")
        blocks.append(section("\n".join(lines)))
        duplicate_source = bool(
            pub_ch
            and helpers.already_subscribed_to_source(
                workspace_id=workspace_record.id,
                source_workspace_id=pub_ch.workspace_id,
                source_channel_id=pub_ch.channel_id,
            )
        )
        if duplicate_source:
            blocks.append(section("_Already subscribed to this published Channel through another sync._"))
        else:
            blocks.append(
                orm.ActionsBlock(
                    elements=[
                        orm.ButtonElement(
                            label=":inbox_tray: Join Sync",
                            action=f"{actions.CONFIG_JOIN_SYNC}_{sync.id}",
                            value=str(sync.id),
                        )
                    ]
                )
            )
