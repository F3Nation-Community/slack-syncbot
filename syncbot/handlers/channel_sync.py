"""Channel sync handlers — publish, unpublish, subscribe, pause, resume, stop."""

import contextlib
import logging
from datetime import UTC, datetime
from logging import Logger

from slack_sdk.web import WebClient

import builders
import helpers
from builders._common import _format_channel_ref, _get_group_members
from db import DbManager, schemas
from handlers._common import (
    _close_modal_done,
    _extract_team_id,
    _get_authorized_workspace,
    _get_selected_conversation_or_option,
    _get_selected_option_value,
    _parse_private_metadata,
    _sanitize_text,
)
from slack import actions, orm
from slack.blocks import context as block_context
from slack.blocks import section

_logger = logging.getLogger(__name__)


def _channel_picker_block(label: str, action_id: str) -> orm.InputBlock:
    """Build a native channel picker, honoring the private-channel policy.

    Slack renders ``conversations_select`` as a searchable list over all of the
    user's conversations with no app-side enumeration, so workspaces with more
    than ~100 channels can reach all of them. Because the native picker cannot
    pre-exclude already-synced channels, that check moves to submit-time
    validation in :func:`_validate_channel_selection`.
    """
    return orm.InputBlock(
        label=label,
        action=action_id,
        element=orm.ConversationsSelectElement(
            placeholder="Search for a Channel",
            include_private=helpers.allow_private_channels(),
        ),
        optional=False,
    )


def _channel_picker_help_text(*, subscribe: bool = False) -> str:
    """Explain what may be selected, including the private-channel warning when relevant."""
    if subscribe:
        base = "Search for a Channel in your Workspace to receive the published Channel."
    else:
        base = "Search for a Channel in your Workspace to publish."
    if helpers.allow_private_channels():
        if subscribe:
            return (
                f"{base} :warning: Private Channels are currently allowed. Messages from the "
                "published Channel will be copied into it, so anyone who can see your Channel "
                "will be able to read them. SyncBot must already be a member of a private Channel, "
                "because it cannot add itself to one."
            )
        return (
            f"{base} :warning: Private Channels are currently allowed. If you publish one, its "
            "messages will be copied into the other Workspaces in this Group, where anyone who can "
            "see the synced Channel will be able to read them. SyncBot must already be a member of "
            "a private Channel, because it cannot add itself to one."
        )
    return f"{base} Only public Channels can be synced."


def _validate_channel_selection(
    client: WebClient,
    channel_id: str | None,
    action_id: str,
) -> dict | None:
    """Validate a selected channel on submit, returning a Slack errors response or None.

    Two rules, both enforced here rather than only in the picker filter, which is
    advisory and bypassable:

    * Private channels are rejected unless the ``allow_private_channels`` setting
      is on. ``conversations.join`` only works on public channels anyway — a bot
      has to be invited to a private one manually.
    * A channel already in an active sync is rejected. This is a **global** rule,
      not per-group: ``get_sync_list`` resolves a channel to the first matching
      sync, so a channel in two syncs has undefined send fan-out.
    """
    if not channel_id or channel_id == "__none__":
        return {
            "response_action": "errors",
            "errors": {action_id: "Select a Channel."},
        }

    existing = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.channel_id == channel_id,
            schemas.SyncChannel.deleted_at.is_(None),
        ],
    )
    if existing:
        return {
            "response_action": "errors",
            "errors": {action_id: "That Channel is already part of a Channel Sync. Pick a different Channel."},
        }

    if not helpers.allow_private_channels():
        try:
            conv_info = client.conversations_info(channel=channel_id)
            is_private = bool(helpers.safe_get(conv_info, "channel", "is_private"))
        except Exception as e:
            # Fail closed: an unreadable channel is one the bot cannot join either.
            _logger.warning(f"_validate_channel_selection: conversations_info failed for {channel_id}: {e}")
            return {
                "response_action": "errors",
                "errors": {action_id: "SyncBot could not read that Channel. Pick a public Channel it can join."},
            }
        if is_private:
            return {
                "response_action": "errors",
                "errors": {action_id: "Private Channels cannot be synced. Pick a public Channel."},
            }

    return None


def _build_publish_step2(
    sync_mode: str,
    other_members: list,
) -> orm.BlockView:
    """Build the step-2 modal blocks: native channel picker + optional target workspace."""
    modal_blocks: list[orm.BaseBlock] = []

    modal_blocks.append(_channel_picker_block("Channel to Publish", actions.CONFIG_PUBLISH_CHANNEL_SELECT))
    modal_blocks.append(block_context(_channel_picker_help_text()))

    if sync_mode == "direct" and other_members:
        ws_options: list[orm.SelectorOption] = []
        for other_member in other_members:
            other_workspace = helpers.get_workspace_by_id(other_member.workspace_id)
            name = (
                helpers.resolve_workspace_name(other_workspace)
                if other_workspace
                else f"Workspace {other_member.workspace_id}"
            )
            ws_options.append(orm.SelectorOption(name=name, value=str(other_member.workspace_id)))

        if ws_options:
            modal_blocks.append(
                orm.InputBlock(
                    label="Target Workspace",
                    action=actions.CONFIG_PUBLISH_DIRECT_TARGET,
                    element=orm.StaticSelectElement(
                        placeholder="Select target Workspace",
                        options=ws_options,
                    ),
                    optional=False,
                )
            )

    return orm.BlockView(blocks=modal_blocks)


def handle_publish_channel(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open the publish-channel flow — always starts with step 1 (sync mode selection)."""
    auth_result = _get_authorized_workspace(body, client, context, "publish_channel")
    if not auth_result:
        return
    _, workspace_record = auth_result

    trigger_id = helpers.safe_get(body, "trigger_id")
    raw_group_id = helpers.safe_get(body, "actions", 0, "value")
    try:
        group_id = int(raw_group_id)
    except (TypeError, ValueError):
        _logger.warning(f"publish_channel: invalid group_id: {raw_group_id!r}")
        return

    mode_options = [
        orm.SelectorOption(
            name="Available to All Workspaces\nAny current or future Workspace Group Member can subscribe.",
            value="group",
        ),
        orm.SelectorOption(
            name="Only with Specific Workspace\nChoose a specific Workspace Group Member to allow to subscribe.",
            value="direct",
        ),
    ]
    step1_blocks: list[orm.BaseBlock] = [
        orm.InputBlock(
            label="Who can subscribe",
            action=actions.CONFIG_PUBLISH_SYNC_MODE,
            element=orm.RadioButtonsElement(
                initial_value="group",
                options=orm.as_selector_options(
                    [o.name for o in mode_options],
                    [o.value for o in mode_options],
                ),
            ),
            optional=False,
        ),
    ]
    orm.BlockView(blocks=step1_blocks).post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_PUBLISH_MODE_SUBMIT,
        title_text="Publish Channel",
        submit_button_text="Next",
        parent_metadata={"group_id": group_id, "workspace_id": workspace_record.id},
        new_or_add="new",
    )


def handle_publish_mode_submit_ack(
    body: dict,
    client: WebClient,
    context: dict,
) -> dict | None:
    """Ack phase for step 1: read sync mode and return ``response_action=update`` for step 2."""
    auth_result = _get_authorized_workspace(body, client, context, "publish_mode_submit")
    if not auth_result:
        return None
    _, workspace_record = auth_result

    metadata = _parse_private_metadata(body)
    group_id = metadata.get("group_id")
    if not group_id:
        raw_pm = helpers.safe_get(body, "view", "private_metadata") or ""
        _logger.warning(
            "publish_mode_submit: missing group_id in metadata",
            extra={
                "team_id": _extract_team_id(body),
                "workspace_id": metadata.get("workspace_id"),
                "private_metadata_len": len(raw_pm) if isinstance(raw_pm, str) else None,
            },
        )
        return None

    sync_mode = _get_selected_option_value(body, actions.CONFIG_PUBLISH_SYNC_MODE) or "group"

    group_members = _get_group_members(group_id)
    other_members = [
        member for member in group_members if member.workspace_id != workspace_record.id and member.workspace_id
    ]
    step2 = _build_publish_step2(sync_mode, other_members)
    updated_view = step2.as_ack_update(
        callback_id=actions.CONFIG_PUBLISH_CHANNEL_SUBMIT,
        title_text="Publish Channel",
        submit_button_text="Publish",
        parent_metadata={"group_id": group_id, "sync_mode": sync_mode},
    )
    return {"response_action": "update", "view": updated_view}


def handle_publish_channel_submit_ack(
    body: dict,
    client: WebClient,
    context: dict,
) -> dict | None:
    """Ack phase for publish: validate and close modal (errors) or empty ack (success)."""
    auth_result = _get_authorized_workspace(body, client, context, "publish_channel_submit")
    if not auth_result:
        return None
    _, workspace_record = auth_result

    metadata = _parse_private_metadata(body)
    group_id = metadata.get("group_id")

    if not group_id:
        _logger.warning("publish_channel_submit: missing group_id in metadata")
        return None

    sync_mode = metadata.get("sync_mode", "group")
    target_workspace_id = None
    selected_target = _get_selected_option_value(body, actions.CONFIG_PUBLISH_DIRECT_TARGET)
    if selected_target:
        with contextlib.suppress(TypeError, ValueError):
            target_workspace_id = int(selected_target)

    if sync_mode == "direct" and not target_workspace_id:
        sync_mode = "group"

    channel_id = _get_selected_conversation_or_option(body, actions.CONFIG_PUBLISH_CHANNEL_SELECT)

    return _validate_channel_selection(client, channel_id, actions.CONFIG_PUBLISH_CHANNEL_SELECT)


def handle_publish_channel_submit_work(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Lazy work phase: create Sync + SyncChannel after modal closed."""
    auth_result = _get_authorized_workspace(body, client, context, "publish_channel_submit")
    if not auth_result:
        return
    _, workspace_record = auth_result

    metadata = _parse_private_metadata(body)
    group_id = metadata.get("group_id")

    if not group_id:
        return

    sync_mode = metadata.get("sync_mode", "group")
    target_workspace_id = None
    selected_target = _get_selected_option_value(body, actions.CONFIG_PUBLISH_DIRECT_TARGET)
    if selected_target:
        with contextlib.suppress(TypeError, ValueError):
            target_workspace_id = int(selected_target)

    if sync_mode == "direct" and not target_workspace_id:
        sync_mode = "group"

    channel_id = _get_selected_conversation_or_option(body, actions.CONFIG_PUBLISH_CHANNEL_SELECT)

    # The ack phase already surfaced any error; this keeps the work phase from
    # writing on a payload it should reject.
    if _validate_channel_selection(client, channel_id, actions.CONFIG_PUBLISH_CHANNEL_SELECT):
        return

    try:
        conv_info = client.conversations_info(channel=channel_id)
        channel_name = helpers.safe_get(conv_info, "channel", "name") or channel_id
    except Exception as exc:
        _logger.debug(f"handle_publish_channel_submit_work: conversations_info failed for {channel_id}: {exc}")
        channel_name = channel_id

    try:
        client.conversations_join(channel=channel_id)

        sync_record = schemas.Sync(
            title=_sanitize_text(channel_name),
            description=None,
            group_id=group_id,
            sync_mode=sync_mode,
            target_workspace_id=target_workspace_id if sync_mode == "direct" else None,
            publisher_workspace_id=workspace_record.id,
        )
        DbManager.create_record(sync_record)

        sync_channel_record = schemas.SyncChannel(
            sync_id=sync_record.id,
            channel_id=channel_id,
            workspace_id=workspace_record.id,
            created_at=datetime.now(UTC),
        )
        DbManager.create_record(sync_channel_record)

        _logger.info(
            "channel_published",
            extra={
                "workspace_id": workspace_record.id,
                "channel_id": channel_id,
                "group_id": group_id,
                "sync_id": sync_record.id,
                "sync_mode": sync_mode,
            },
        )
    except Exception as e:
        _logger.error(f"Failed to publish channel {channel_id}: {e}")

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context)
    _refresh_group_member_homes(group_id, workspace_record.id, logger, context=context)


def handle_unpublish_channel(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Unpublish a channel: hard-delete the Sync and everything beneath it.

    There are no DB cascades on ``sync_channels.sync_id`` or
    ``post_meta.sync_channel_id``, so :func:`helpers.purge_sync` deletes the
    children first. Deleting the ``Sync`` directly fails on MySQL with error
    1451 and makes the button look dead.

    Only the original publisher can unpublish.
    """
    auth_result = _get_authorized_workspace(body, client, context, "unpublish_channel")
    if not auth_result:
        return
    user_id, workspace_record = auth_result

    admin_name, admin_label = helpers.format_admin_label(client, user_id, workspace_record)

    raw_value = helpers.safe_get(body, "actions", 0, "value")
    try:
        sync_id = int(raw_value)
    except (TypeError, ValueError):
        _logger.warning(f"Invalid sync_id for unpublish: {raw_value!r}")
        return

    sync_record = DbManager.get_record(schemas.Sync, id=sync_id)
    if not sync_record:
        return

    if workspace_record and sync_record.publisher_workspace_id != workspace_record.id:
        _logger.warning("unpublish_denied: not the publisher")
        return

    group_id = sync_record.group_id

    all_channels = DbManager.find_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.sync_id == sync_id, schemas.SyncChannel.deleted_at.is_(None)],
    )

    for sync_channel in all_channels:
        try:
            member_ws = helpers.get_workspace_by_id(sync_channel.workspace_id)
            if member_ws and member_ws.bot_token:
                name = (
                    admin_name if workspace_record and sync_channel.workspace_id == workspace_record.id else admin_label
                )
                member_client = WebClient(token=helpers.decrypt_bot_token(member_ws.bot_token))
                helpers.notify_synced_channels(
                    member_client,
                    [sync_channel.channel_id],
                    f":octagonal_sign: *{name}* unpublished this Channel. Syncing is no longer available.",
                )
                member_client.conversations_leave(channel=sync_channel.channel_id)
        except Exception as e:
            _logger.warning(f"Failed to notify/leave channel {sync_channel.channel_id}: {e}")

    try:
        helpers.purge_sync(sync_id)
    except Exception as exc:
        # Previously any failure here was indistinguishable from a dead button.
        _logger.error(
            "unpublish_failed",
            extra={"sync_id": sync_id, "group_id": group_id, "error": str(exc)},
        )
        with contextlib.suppress(Exception):
            helpers.notify_admins_dm(
                client,
                ":warning: Unpublishing that Channel failed, so it is still published. "
                "Please try again, and let your SyncBot operator know if it keeps failing.",
            )
        return

    _logger.info(
        "channel_unpublished",
        extra={"sync_id": sync_id, "group_id": group_id},
    )

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context)
    if group_id:
        _refresh_group_member_homes(group_id, workspace_record.id if workspace_record else 0, logger, context=context)


def _toggle_sync_status(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
    *,
    action_prefix: str,
    target_status: str,
    emoji: str,
    verb: str,
    log_event: str,
) -> None:
    """Shared logic for pausing or resuming a channel sync. Only the current workspace's channel is toggled."""
    action_id = helpers.safe_get(body, "actions", 0, "action_id") or ""
    sync_id_str = action_id.replace(action_prefix + "_", "")

    try:
        sync_id = int(sync_id_str)
    except (TypeError, ValueError):
        _logger.warning(f"{log_event}_invalid_id", extra={"action_id": action_id})
        return

    auth_result = _get_authorized_workspace(body, client, context, log_event)
    if not auth_result:
        return
    user_id, workspace_record = auth_result
    admin_name, admin_label = helpers.format_admin_label(client, user_id, workspace_record)

    all_channels = DbManager.find_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.sync_id == sync_id, schemas.SyncChannel.deleted_at.is_(None)],
    )
    my_sync_channel = next(
        (c for c in all_channels if c.workspace_id == workspace_record.id),
        None,
    )
    if not my_sync_channel:
        _logger.warning(
            f"{log_event}_no_channel_for_workspace", extra={"sync_id": sync_id, "workspace_id": workspace_record.id}
        )
        return

    DbManager.update_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.id == my_sync_channel.id],
        {schemas.SyncChannel.status: target_status},
    )
    helpers._cache_delete(f"sync_list:{my_sync_channel.channel_id}")

    ws_cache: dict[int, schemas.Workspace | None] = {}
    for sync_channel in [my_sync_channel]:
        try:
            channel_ws = ws_cache.get(sync_channel.workspace_id) or helpers.get_workspace_by_id(
                sync_channel.workspace_id
            )
            ws_cache[sync_channel.workspace_id] = channel_ws
            if channel_ws and channel_ws.bot_token:
                ws_client = WebClient(token=helpers.decrypt_bot_token(channel_ws.bot_token))
                if target_status == "active":
                    with contextlib.suppress(Exception):
                        ws_client.conversations_join(channel=sync_channel.channel_id)
                name = (
                    admin_name if workspace_record and sync_channel.workspace_id == workspace_record.id else admin_label
                )
                other_channels = [c for c in all_channels if c.workspace_id != sync_channel.workspace_id]
                if other_channels:
                    other_ws = ws_cache.get(other_channels[0].workspace_id) or helpers.get_workspace_by_id(
                        other_channels[0].workspace_id
                    )
                    ws_cache[other_channels[0].workspace_id] = other_ws
                    channel_ref = helpers.resolve_channel_name(other_channels[0].channel_id, other_ws)
                    msg = f":{emoji}: *{name}* {verb} syncing with *{channel_ref}*."
                else:
                    msg = f":{emoji}: *{name}* {verb} channel syncing."
                helpers.notify_synced_channels(ws_client, [sync_channel.channel_id], msg)
        except Exception as e:
            _logger.warning(f"Failed to notify channel {sync_channel.channel_id} about {verb}: {e}")

    _logger.info(log_event, extra={"sync_id": sync_id, "sync_channel_id": my_sync_channel.id})

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context)
    sync_record = DbManager.get_record(schemas.Sync, id=sync_id)
    if sync_record and sync_record.group_id:
        _refresh_group_member_homes(
            sync_record.group_id, workspace_record.id if workspace_record else 0, logger, context=context
        )


def handle_pause_sync(body: dict, client: WebClient, logger: Logger, context: dict) -> None:
    """Pause an active channel sync."""
    _toggle_sync_status(
        body,
        client,
        logger,
        context,
        action_prefix=actions.CONFIG_PAUSE_SYNC,
        target_status="paused",
        emoji="double_vertical_bar",
        verb="paused",
        log_event="sync_paused",
    )


def handle_resume_sync(body: dict, client: WebClient, logger: Logger, context: dict) -> None:
    """Resume a paused channel sync."""
    _toggle_sync_status(
        body,
        client,
        logger,
        context,
        action_prefix=actions.CONFIG_RESUME_SYNC,
        target_status="active",
        emoji="arrow_forward",
        verb="resumed",
        log_event="sync_resumed",
    )


def handle_stop_sync(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Show a confirmation modal before stopping a channel sync."""
    action_id = helpers.safe_get(body, "actions", 0, "action_id") or ""
    sync_id_str = action_id.replace(actions.CONFIG_STOP_SYNC + "_", "")

    try:
        sync_id = int(sync_id_str)
    except (TypeError, ValueError):
        _logger.warning("stop_sync_invalid_id", extra={"action_id": action_id})
        return

    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    confirm_form = orm.BlockView(
        blocks=[
            section(
                ":warning: *Are you sure you want to stop syncing this Channel?*\n\n"
                "This will:\n"
                "\u2022 Remove your Workspace's Sync history for this Channel\n"
                "\u2022 Remove this Channel from the active Sync\n"
                "\u2022 Other Workspaces in the Sync will continue uninterrupted\n\n"
                "_No messages will be deleted from any Channel — only SyncBot's tracking history for your Workspace is removed._"
            ),
            orm.ActionsBlock(
                elements=[
                    orm.ButtonElement(
                        label="Stop Syncing",
                        action=actions.CONFIG_STOP_SYNC_CONFIRM,
                        value=str(sync_id),
                        style="danger",
                    ),
                ]
            ),
        ]
    )

    confirm_form.post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_STOP_SYNC_CONFIRM,
        title_text="Stop Syncing",
        submit_button_text=None,
        close_button_text="Cancel",
        parent_metadata={"sync_id": sync_id},
    )


def handle_stop_sync_confirm(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Execute channel sync stop after confirmation.

    Removes only this workspace's ``SyncChannel`` and its ``PostMeta``.
    Other workspaces' data and the Sync record remain intact.
    """
    auth_result = _get_authorized_workspace(body, client, context, "stop_sync_confirm")
    if not auth_result:
        return
    user_id, workspace_record = auth_result

    meta = _parse_private_metadata(body)
    sync_id = meta.get("sync_id")
    if not sync_id:
        _logger.warning("stop_sync_confirm: missing sync_id in metadata")
        return

    sync_record = DbManager.get_record(schemas.Sync, id=sync_id)
    if sync_record and sync_record.publisher_workspace_id == workspace_record.id:
        # The publisher is the channel's source: they tear a sync down with
        # Unpublish, which removes it for everyone. Stopping here would delete
        # only the publisher's own channel and strand the sync with a publisher
        # that no longer has one. The Home tab routes the publisher to Unpublish;
        # this guards forged or stale payloads.
        _logger.warning(
            "stop_sync_denied_publisher",
            extra={"sync_id": sync_id, "workspace_id": workspace_record.id},
        )
        return

    admin_name, admin_label = helpers.format_admin_label(client, user_id, workspace_record)

    all_channels = DbManager.find_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.sync_id == sync_id, schemas.SyncChannel.deleted_at.is_(None)],
    )

    my_channel = next((c for c in all_channels if c.workspace_id == workspace_record.id), None)
    other_channels = [c for c in all_channels if c.workspace_id != workspace_record.id]

    for sync_channel in all_channels:
        try:
            channel_ws = helpers.get_workspace_by_id(sync_channel.workspace_id)
            if channel_ws and channel_ws.bot_token:
                if sync_channel.workspace_id == workspace_record.id and other_channels:
                    other_ws = helpers.get_workspace_by_id(other_channels[0].workspace_id)
                    channel_ref = helpers.resolve_channel_name(other_channels[0].channel_id, other_ws)
                    msg = f":octagonal_sign: *{admin_name}* stopped syncing with *{channel_ref}*."
                elif sync_channel.workspace_id != workspace_record.id:
                    my_ref = (
                        helpers.resolve_channel_name(my_channel.channel_id, workspace_record)
                        if my_channel
                        else "the other Workspace"
                    )
                    msg = f":octagonal_sign: *{admin_label}* stopped syncing with *{my_ref}*."
                else:
                    msg = f":octagonal_sign: *{admin_name}* stopped Channel Syncing."
                ws_client = WebClient(token=helpers.decrypt_bot_token(channel_ws.bot_token))
                helpers.notify_synced_channels(ws_client, [sync_channel.channel_id], msg)
        except Exception as e:
            _logger.warning(f"Failed to notify channel {sync_channel.channel_id}: {e}")

    if my_channel:
        helpers.purge_sync_channels([my_channel])
        try:
            client.conversations_leave(channel=my_channel.channel_id)
        except Exception as e:
            _logger.warning(f"Failed to leave channel {my_channel.channel_id}: {e}")

    if not other_channels:
        # That was the last channel, so the sync is now an empty shell — e.g. a
        # member stranded after the publisher left. Remove it so it stops showing
        # up as "waiting"/"available" forever.
        helpers.purge_sync(sync_id)

    _logger.info(
        "sync_stopped",
        extra={
            "sync_id": sync_id,
            "workspace_id": workspace_record.id,
            "channel_id": my_channel.channel_id if my_channel else None,
        },
    )

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context)
    if sync_record and sync_record.group_id:
        _refresh_group_member_homes(sync_record.group_id, workspace_record.id, logger, context=context)
    _close_modal_done(client, body, ":octagonal_sign: Channel sync stopped. You can close this now.")


def handle_subscribe_channel(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Push the channel picker modal for subscribing to an available channel.

    Uses Slack's native searchable picker, so every channel in the workspace is
    reachable. Whether the chosen channel is eligible is validated on submit.
    """
    if not _get_authorized_workspace(body, client, context, "subscribe_channel"):
        return

    trigger_id = helpers.safe_get(body, "trigger_id")
    sync_id = helpers.safe_get(body, "actions", 0, "value")

    blocks: list[orm.BaseBlock] = []

    if sync_id:
        publisher_channels = DbManager.find_records(
            schemas.SyncChannel,
            [schemas.SyncChannel.sync_id == int(sync_id), schemas.SyncChannel.deleted_at.is_(None)],
        )
        if publisher_channels:
            pub_ch = publisher_channels[0]
            pub_ws = helpers.get_workspace_by_id(pub_ch.workspace_id)
            ch_ref = _format_channel_ref(pub_ch.channel_id, pub_ws, is_local=False)
            blocks.append(section(f"Published Channel: {ch_ref}"))

    blocks.append(_channel_picker_block("Channel to Subscribe", actions.CONFIG_SUBSCRIBE_CHANNEL_SELECT))
    blocks.append(block_context(_channel_picker_help_text(subscribe=True)))

    orm.BlockView(blocks=blocks).post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_SUBSCRIBE_CHANNEL_SUBMIT,
        title_text="Subscribe",
        submit_button_text="Subscribe",
        parent_metadata={"sync_id": int(sync_id)} if sync_id else None,
        new_or_add="new",
    )


def handle_subscribe_channel_submit_ack(
    body: dict,
    client: WebClient,
    context: dict,
) -> dict | None:
    """Ack phase for subscribe: surface a visible error, or ack empty on success.

    The native picker cannot pre-exclude ineligible channels, so an invalid
    choice has to be reported here rather than returning silently and leaving the
    user with a modal that appeared to work.
    """
    auth_result = _get_authorized_workspace(body, client, context, "subscribe_channel_submit")
    if not auth_result:
        return None

    metadata = _parse_private_metadata(body)
    if not metadata.get("sync_id"):
        _logger.warning("subscribe_channel_submit: missing sync_id")
        return None

    channel_id = _get_selected_conversation_or_option(body, actions.CONFIG_SUBSCRIBE_CHANNEL_SELECT)

    return _validate_channel_selection(client, channel_id, actions.CONFIG_SUBSCRIBE_CHANNEL_SELECT)


def handle_subscribe_channel_submit(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Subscribe to an available channel sync: create SyncChannel for subscriber."""
    auth_result = _get_authorized_workspace(body, client, context, "subscribe_channel_submit")
    if not auth_result:
        return
    user_id, workspace_record = auth_result

    metadata = _parse_private_metadata(body)
    sync_id = metadata.get("sync_id")

    if not sync_id:
        _logger.warning("subscribe_channel_submit: missing sync_id")
        return

    channel_id = _get_selected_conversation_or_option(body, actions.CONFIG_SUBSCRIBE_CHANNEL_SELECT)

    # The ack phase already surfaced any error; this keeps the work phase from
    # writing on a payload it should reject.
    if _validate_channel_selection(client, channel_id, actions.CONFIG_SUBSCRIBE_CHANNEL_SELECT):
        return

    sync_record = DbManager.get_record(schemas.Sync, id=sync_id)
    if not sync_record:
        return

    group_id = sync_record.group_id

    existing_sub = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.sync_id == sync_id,
            schemas.SyncChannel.workspace_id == workspace_record.id,
            schemas.SyncChannel.channel_id == channel_id,
            schemas.SyncChannel.deleted_at.is_(None),
            schemas.SyncChannel.status == "active",
        ],
    )
    if existing_sub:
        _logger.info(
            "subscribe_channel_duplicate_skip",
            extra={
                "sync_id": sync_id,
                "channel_id": channel_id,
                "workspace_id": workspace_record.id,
            },
        )
        builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context)
        if group_id:
            _refresh_group_member_homes(group_id, workspace_record.id, logger, context=context)
        return

    acting_user_id = helpers.safe_get(body, "user", "id") or user_id
    admin_name, admin_label = helpers.format_admin_label(client, acting_user_id, workspace_record)

    publisher_channels: list = []
    try:
        client.conversations_join(channel=channel_id)

        sync_channel_record = schemas.SyncChannel(
            sync_id=sync_id,
            channel_id=channel_id,
            workspace_id=workspace_record.id,
            created_at=datetime.now(UTC),
        )
        DbManager.create_record(sync_channel_record)

        publisher_channels = DbManager.find_records(
            schemas.SyncChannel,
            [
                schemas.SyncChannel.sync_id == sync_id,
                schemas.SyncChannel.deleted_at.is_(None),
                schemas.SyncChannel.workspace_id != workspace_record.id,
            ],
        )

        try:
            if publisher_channels:
                pub_ch = publisher_channels[0]
                pub_ws = helpers.get_workspace_by_id(pub_ch.workspace_id)
                channel_ref = helpers.resolve_channel_name(pub_ch.channel_id, pub_ws)
            else:
                channel_ref = sync_record.title or "the other Channel"
            client.chat_postMessage(
                channel=channel_id,
                text=f":arrows_counterclockwise: *{admin_name}* subscribed this Channel to *{channel_ref}*. Messages will be shared automatically.",
            )
        except Exception as exc:
            _logger.debug(f"subscribe_channel: failed to notify subscriber channel {channel_id}: {exc}")

        local_ref = helpers.resolve_channel_name(channel_id, workspace_record)
        for pub_ch in publisher_channels:
            try:
                pub_ws = helpers.get_workspace_by_id(pub_ch.workspace_id)
                if pub_ws:
                    pub_client = WebClient(token=helpers.decrypt_bot_token(pub_ws.bot_token))
                    pub_client.chat_postMessage(
                        channel=pub_ch.channel_id,
                        text=f":arrows_counterclockwise: *{admin_label}* subscribed *{local_ref}* to this Channel. Messages will be shared automatically.",
                    )
            except Exception as exc:
                _logger.debug(f"subscribe_channel: failed to notify publisher channel {pub_ch.channel_id}: {exc}")

        _logger.info(
            "channel_subscribed",
            extra={
                "workspace_id": workspace_record.id,
                "channel_id": channel_id,
                "sync_id": sync_id,
                "group_id": group_id,
            },
        )
    except Exception as e:
        _logger.error(f"Failed to subscribe to channel sync {sync_id}: {e}")

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context)
    if group_id:
        _refresh_group_member_homes(group_id, workspace_record.id, logger, context=context)


def _refresh_group_member_homes(
    group_id: int,
    exclude_workspace_id: int,
    logger: Logger,
    context: dict | None = None,
) -> None:
    """Refresh the Home tab for all group members except the acting workspace.

    Uses context=None when refreshing other members so admin lookups are always
    fresh for each workspace (avoids request-scoped cache from the acting ws).
    """
    members = _get_group_members(group_id)
    refreshed: set[int] = set()
    for member in members:
        if not member.workspace_id or member.workspace_id == exclude_workspace_id or member.workspace_id in refreshed:
            continue
        member_ws = helpers.get_workspace_by_id(member.workspace_id, context=context)
        if member_ws:
            builders.refresh_home_tab_for_workspace(member_ws, logger, context=None)
            refreshed.add(member.workspace_id)
