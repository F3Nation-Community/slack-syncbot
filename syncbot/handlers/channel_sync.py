"""Channel sync handlers — publish, unpublish, subscribe, pause, resume, stop."""

import contextlib
import logging
from datetime import UTC, datetime
from logging import Logger

from slack_sdk.web import WebClient

import builders
import constants
import helpers
from builders._common import _format_channel_ref, _get_group_members
from db import DbManager, schemas
from handlers._common import (
    _close_modal_done,
    _ensure_membership_or_rollback,
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


def _reaction_direction_block(action_id: str, *, initial: str = constants.REACTION_DIRECTION_BOTH) -> orm.InputBlock:
    return orm.InputBlock(
        label="Reaction direction",
        action=action_id,
        element=orm.RadioButtonsElement(
            initial_value=initial,
            options=[
                orm.SelectorOption(name="Send and receive", value=constants.REACTION_DIRECTION_BOTH),
                orm.SelectorOption(name="Send only", value=constants.REACTION_DIRECTION_SEND),
                orm.SelectorOption(name="Receive only", value=constants.REACTION_DIRECTION_RECEIVE),
                orm.SelectorOption(name="No reactions", value=constants.REACTION_DIRECTION_OFF),
            ],
        ),
        optional=False,
    )


def _reaction_style_block(
    action_id: str,
    *,
    initial: str = constants.DEFAULT_REACTION_STYLE_NEW_RECEIVE,
) -> orm.InputBlock:
    return orm.InputBlock(
        label="Reaction type in this Workspace",
        action=action_id,
        element=orm.RadioButtonsElement(
            initial_value=initial,
            options=[
                orm.SelectorOption(
                    name="Direct — native emoji on the synced message",
                    value=constants.REACTION_STYLE_DIRECT_ONLY,
                ),
                orm.SelectorOption(
                    name="Hybrid — direct when possible, otherwise a thread notice",
                    value=constants.REACTION_STYLE_THREADED_AND_DIRECT,
                ),
            ],
        ),
        optional=False,
    )


def _parse_reaction_fields(body: dict, metadata: dict, *, style_action: str) -> tuple[str, str | None]:
    from helpers.reactions import default_reaction_style_for_new_channel, direction_receives

    direction = metadata.get("reaction_direction") or constants.REACTION_DIRECTION_BOTH
    if not direction_receives(direction):
        return direction, None
    style = _get_selected_option_value(body, style_action)
    if style not in (constants.REACTION_STYLE_DIRECT_ONLY, constants.REACTION_STYLE_THREADED_AND_DIRECT):
        style = default_reaction_style_for_new_channel(direction)
    return direction, style


def _channel_picker_block(label: str, action_id: str, *, team_id: str | None) -> orm.InputBlock:
    """Build a native channel picker, honoring the private-channel policy.

    Slack renders ``conversations_select`` as a searchable list over all of the
    user's conversations with no app-side enumeration, so workspaces with more
    than ~100 channels can reach all of them. It is scoped to the viewer, which
    means the private channels it offers are exactly the ones that person belongs
    to — they cannot pick a private channel they are not in.

    That is a client-side guarantee, so it is not relied on alone. The submitted
    payload could still name any channel, and the answer to that is not a second
    membership lookup but the token used to act on it: a private channel is
    reached only by inviting the bot as the acting user, so a channel that person
    is not in fails at Slack. See :func:`helpers.ensure_bot_in_conversation`.
    Already-synced channels, which the picker also cannot pre-exclude, are
    rejected in :func:`_validate_channel_selection`.
    """
    return orm.InputBlock(
        label=label,
        action=action_id,
        element=orm.ConversationsSelectElement(
            placeholder="Search for a Channel",
            include_private=helpers.allow_private_channels(team_id),
        ),
        optional=False,
    )


def _channel_picker_help_text(*, team_id: str | None, subscribe: bool = False) -> str:
    """Explain what may be selected, including the private-channel warning when relevant."""
    if subscribe:
        base = "Search for a Channel in your Workspace to receive the published Channel."
    else:
        base = "Search for a Channel in your Workspace to publish."
    if helpers.allow_private_channels(team_id):
        if subscribe:
            return (
                f"{base} :warning: Private Channels are currently allowed. Messages from the "
                "published Channel will be copied into it, so anyone who can see your Channel "
                "will be able to read them. If you pick a private Channel, SyncBot is added to it "
                "for you, using your permission to invite it."
            )
        return (
            f"{base} :warning: Private Channels are currently allowed. If you publish one, its "
            "messages will be copied into the other Workspaces in this Group, where anyone who can "
            "see the synced Channel will be able to read them. If you pick a private Channel, "
            "SyncBot is added to it for you, using your permission to invite it."
        )
    return f"{base} Only public Channels can be synced."


def _looks_private(client: WebClient, channel_id: str) -> bool:
    """Whether a channel is private as far as the bot token can tell.

    A private channel SyncBot has never been in is invisible to the bot token, so
    an unreadable channel is treated as private rather than as a lookup failure.
    """
    try:
        conv_info = client.conversations_info(channel=channel_id)
    except Exception as exc:
        _logger.debug(f"_looks_private: conversations_info failed for {channel_id}: {exc}")
        return True
    return bool(helpers.safe_get(conv_info, "channel", "is_private"))


def _validate_channel_selection(
    client: WebClient,
    channel_id: str | None,
    action_id: str,
    *,
    team_id: str | None = None,
    acting_user_id: str | None = None,
) -> dict | None:
    """Validate a selected channel on submit, returning a Slack errors response or None.

    Three rules, all enforced here rather than only in the picker filter, which is
    advisory and bypassable:

    * A channel already in an active sync is rejected. This is a **global** rule,
      not per-group: ``get_sync_list`` resolves a channel to the first matching
      sync, so a channel in two syncs has undefined send fan-out.
    * Private channels are rejected unless the ``allow_private_channels`` setting
      is on.
    * A private channel is also rejected when SyncBot has no user token it could
      invite itself with, because only a member of that channel can add an app.
      The check is a local installation-store lookup, cheap enough for the ack
      phase, and it fails here as a field error instead of failing after the
      modal has closed.
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

    if not helpers.allow_private_channels(team_id or ""):
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

    # Private channels are allowed. Adding the bot to one needs a user token, so
    # when there is none, only a public pick can succeed.
    if helpers.has_user_token(team_id, acting_user_id):
        return None
    if _looks_private(client, channel_id):
        return {
            "response_action": "errors",
            "errors": {action_id: helpers.AUTHORIZE_HINT},
        }

    return None


def _build_publish_step2(
    sync_mode: str,
    other_members: list,
    *,
    team_id: str | None,
    reaction_direction: str,
) -> orm.BlockView:
    """Build the step-2 modal blocks: native channel picker + optional target workspace."""
    from helpers.reactions import direction_receives

    modal_blocks: list[orm.BaseBlock] = []

    modal_blocks.append(
        _channel_picker_block("Channel to Publish", actions.CONFIG_PUBLISH_CHANNEL_SELECT, team_id=team_id)
    )
    modal_blocks.append(block_context(_channel_picker_help_text(team_id=team_id)))

    if direction_receives(reaction_direction):
        modal_blocks.append(_reaction_style_block(actions.CONFIG_PUBLISH_REACTION_STYLE))
        modal_blocks.append(
            block_context(
                "Authorize SyncBot in each Workspace where you want reactions to appear as you. "
                "Custom emoji the other Workspace does not have will not appear as a native reaction there."
            )
        )

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
        _reaction_direction_block(actions.CONFIG_PUBLISH_REACTION_DIRECTION),
        block_context(
            "Reactions only show in a Workspace that chose to receive them. Custom emoji the other "
            "Workspace does not have will not appear as a reaction there."
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
        body=body,
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
    reaction_direction = (
        _get_selected_option_value(body, actions.CONFIG_PUBLISH_REACTION_DIRECTION) or constants.REACTION_DIRECTION_BOTH
    )

    group_members = _get_group_members(group_id)
    other_members = [
        member for member in group_members if member.workspace_id != workspace_record.id and member.workspace_id
    ]
    step2 = _build_publish_step2(
        sync_mode,
        other_members,
        team_id=workspace_record.team_id,
        reaction_direction=reaction_direction,
    )
    updated_view = step2.as_ack_update(
        callback_id=actions.CONFIG_PUBLISH_CHANNEL_SUBMIT,
        title_text="Publish Channel",
        submit_button_text="Publish",
        parent_metadata={
            "group_id": group_id,
            "sync_mode": sync_mode,
            "reaction_direction": reaction_direction,
        },
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

    return _validate_channel_selection(
        client,
        channel_id,
        actions.CONFIG_PUBLISH_CHANNEL_SELECT,
        team_id=_extract_team_id(body) or workspace_record.team_id,
        acting_user_id=helpers.safe_get(body, "user", "id"),
    )


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
    user_id, workspace_record = auth_result

    metadata = _parse_private_metadata(body)
    group_id = metadata.get("group_id")

    if not group_id:
        return

    sync_mode = metadata.get("sync_mode", "group")
    reaction_direction, reaction_style = _parse_reaction_fields(
        body,
        metadata,
        style_action=actions.CONFIG_PUBLISH_REACTION_STYLE,
    )
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
    if _validate_channel_selection(
        client,
        channel_id,
        actions.CONFIG_PUBLISH_CHANNEL_SELECT,
        team_id=_extract_team_id(body) or workspace_record.team_id,
        acting_user_id=helpers.safe_get(body, "user", "id") or user_id,
    ):
        return

    acting_user_id = helpers.safe_get(body, "user", "id") or user_id
    team_id = _extract_team_id(body) or workspace_record.team_id
    channel_name, _is_private = helpers.lookup_channel_meta(
        channel_id,
        workspace_record,
        user_token=helpers.get_user_token(team_id, acting_user_id),
        client=client,
    )

    try:
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
            reaction_direction=reaction_direction,
            reaction_style=reaction_style,
        )
        DbManager.create_record(sync_channel_record)
    except Exception as e:
        _logger.error(f"Failed to publish channel {channel_id}: {e}")
        return

    # Membership comes after the rows exist: Slack fires ``member_joined_channel``
    # as soon as the bot is added, and that handler leaves any channel with no
    # SyncChannel. Writing first is what lets a private channel work at all.
    if not _ensure_membership_or_rollback(
        client,
        channel_id,
        team_id=team_id,
        acting_user_id=acting_user_id,
        rollback=lambda: helpers.purge_sync(sync_record.id),
        log_event="publish_channel_membership_failed",
        log_extra={"workspace_id": workspace_record.id, "channel_id": channel_id, "sync_id": sync_record.id},
        context=context,
    ):
        return

    refreshed_name, _is_private = helpers.lookup_channel_meta(channel_id, workspace_record)
    if refreshed_name and refreshed_name != channel_id and refreshed_name != sync_record.title:
        DbManager.update_records(
            schemas.Sync,
            [schemas.Sync.id == sync_record.id],
            {schemas.Sync.title: _sanitize_text(refreshed_name)},
        )
        sync_record.title = refreshed_name

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
                team_id=workspace_record.team_id if workspace_record else None,
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
                    # Resume re-adds the bot if it was removed while paused. The
                    # row already exists, so nothing to roll back — a private
                    # channel simply needs the invite path instead of join.
                    try:
                        # Resume only touches this workspace's channel, so Bolt's
                        # request-scoped bot_user_id is the right invitee. Do not
                        # pass context if that ever changes — another workspace's
                        # bot member ID is ``user_not_found`` here.
                        helpers.ensure_bot_in_conversation(
                            ws_client,
                            sync_channel.channel_id,
                            team_id=channel_ws.team_id,
                            acting_user_id=user_id,
                            context=context if channel_ws.id == workspace_record.id else None,
                        )
                    except Exception as exc:
                        _logger.warning(
                            "resume_sync_membership_failed",
                            extra={"channel_id": sync_channel.channel_id, "error": str(exc)},
                        )
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
        body=body,
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
    """Open subscribe step 1: published-channel context and reaction direction."""
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

    blocks.extend(
        [
            _reaction_direction_block(actions.CONFIG_SUBSCRIBE_REACTION_DIRECTION),
            block_context(
                "Reactions only show in a Workspace that chose to receive them. Pick how this "
                "Workspace participates before choosing your local Channel."
            ),
        ]
    )

    orm.BlockView(blocks=blocks).post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_SUBSCRIBE_DIRECTION_SUBMIT,
        title_text="Subscribe",
        submit_button_text="Next",
        parent_metadata={"sync_id": int(sync_id)} if sync_id else None,
        new_or_add="new",
        body=body,
    )


def handle_subscribe_direction_submit_ack(
    body: dict,
    client: WebClient,
    context: dict,
) -> dict | None:
    """Ack phase for subscribe step 1: channel picker (+ type when receiving)."""
    auth_result = _get_authorized_workspace(body, client, context, "subscribe_direction_submit")
    if not auth_result:
        return None
    _, workspace_record = auth_result

    metadata = _parse_private_metadata(body)
    sync_id = metadata.get("sync_id")
    if not sync_id:
        _logger.warning("subscribe_direction_submit: missing sync_id")
        return None

    reaction_direction = (
        _get_selected_option_value(body, actions.CONFIG_SUBSCRIBE_REACTION_DIRECTION)
        or constants.REACTION_DIRECTION_BOTH
    )
    from helpers.reactions import direction_receives

    modal_blocks: list[orm.BaseBlock] = [
        _channel_picker_block(
            "Channel to Subscribe",
            actions.CONFIG_SUBSCRIBE_CHANNEL_SELECT,
            team_id=workspace_record.team_id,
        ),
        block_context(_channel_picker_help_text(team_id=workspace_record.team_id, subscribe=True)),
    ]
    if direction_receives(reaction_direction):
        modal_blocks.append(_reaction_style_block(actions.CONFIG_SUBSCRIBE_REACTION_STYLE))

    step2 = orm.BlockView(blocks=modal_blocks)
    updated_view = step2.as_ack_update(
        callback_id=actions.CONFIG_SUBSCRIBE_CHANNEL_SUBMIT,
        title_text="Subscribe",
        submit_button_text="Subscribe",
        parent_metadata={"sync_id": sync_id, "reaction_direction": reaction_direction},
    )
    return {"response_action": "update", "view": updated_view}


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
    user_id, workspace_record = auth_result

    metadata = _parse_private_metadata(body)
    if not metadata.get("sync_id"):
        _logger.warning("subscribe_channel_submit: missing sync_id")
        return None

    channel_id = _get_selected_conversation_or_option(body, actions.CONFIG_SUBSCRIBE_CHANNEL_SELECT)

    return _validate_channel_selection(
        client,
        channel_id,
        actions.CONFIG_SUBSCRIBE_CHANNEL_SELECT,
        team_id=_extract_team_id(body) or workspace_record.team_id,
        acting_user_id=helpers.safe_get(body, "user", "id") or user_id,
    )


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
    if _validate_channel_selection(
        client,
        channel_id,
        actions.CONFIG_SUBSCRIBE_CHANNEL_SELECT,
        team_id=_extract_team_id(body) or workspace_record.team_id,
        acting_user_id=helpers.safe_get(body, "user", "id") or user_id,
    ):
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

    team_id = _extract_team_id(body) or workspace_record.team_id

    reaction_direction, reaction_style = _parse_reaction_fields(
        body,
        metadata,
        style_action=actions.CONFIG_SUBSCRIBE_REACTION_STYLE,
    )

    try:
        sync_channel_record = schemas.SyncChannel(
            sync_id=sync_id,
            channel_id=channel_id,
            workspace_id=workspace_record.id,
            created_at=datetime.now(UTC),
            reaction_direction=reaction_direction,
            reaction_style=reaction_style,
        )
        DbManager.create_record(sync_channel_record)
    except Exception as e:
        _logger.error(f"Failed to subscribe to channel sync {sync_id}: {e}")
        return

    # Same ordering as publish: the row has to exist before Slack announces the
    # bot joined, or the unconfigured-channel handler shows it the door.
    if not _ensure_membership_or_rollback(
        client,
        channel_id,
        team_id=team_id,
        acting_user_id=acting_user_id,
        rollback=lambda: helpers.purge_sync_channels([sync_channel_record]),
        log_event="subscribe_channel_membership_failed",
        log_extra={"workspace_id": workspace_record.id, "channel_id": channel_id, "sync_id": sync_id},
        context=context,
    ):
        return

    publisher_channels: list = []
    try:
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


def _parse_edit_sync_ref(body: dict) -> tuple[str | None, int | None]:
    """Parse Edit button value ``c:{sync_channel_id}`` or ``s:{sync_id}``.

    Channel and Sync PKs both autoincrement from 1, so a bare integer is unsafe.
    """
    action_data = helpers.safe_get(body, "actions", 0) or {}
    raw = (action_data.get("value") or "").strip()
    if not raw and action_data.get("action_id"):
        # Fallback from action_id edit_sync_c_12 / edit_sync_s_12
        aid = action_data.get("action_id") or ""
        prefix = f"{actions.CONFIG_EDIT_SYNC}_"
        if aid.startswith(prefix):
            rest = aid[len(prefix) :]
            if rest.startswith("c_"):
                raw = f"c:{rest[2:]}"
            elif rest.startswith("s_"):
                raw = f"s:{rest[2:]}"
    if raw.startswith("c:"):
        try:
            return "channel", int(raw[2:])
        except (TypeError, ValueError):
            return None, None
    if raw.startswith("s:"):
        try:
            return "sync", int(raw[2:])
        except (TypeError, ValueError):
            return None, None
    return None, None


def _sync_channel_by_pk(sync_channel_id: int) -> schemas.SyncChannel | None:
    """Look up SyncChannel by integer PK (``get_record`` uses Slack ``channel_id``)."""
    rows = DbManager.find_records(schemas.SyncChannel, [schemas.SyncChannel.id == sync_channel_id])
    return rows[0] if rows else None


def _can_edit_sync_policy(sync: schemas.Sync, workspace_record: schemas.Workspace) -> bool:
    """Publisher or group owner may change Any vs Specific."""
    if sync.publisher_workspace_id == workspace_record.id:
        return True
    return bool(sync.group_id and helpers.is_workspace_owner(sync.group_id, workspace_record.id))


def _who_can_subscribe_blocks(sync: schemas.Sync) -> list[orm.BaseBlock]:
    """Any vs Specific radios plus optional single-target picker."""
    mode = sync.sync_mode or "group"
    if mode not in ("group", "direct"):
        mode = "group"
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
    blocks: list[orm.BaseBlock] = [
        orm.InputBlock(
            label="Who can subscribe",
            action=actions.CONFIG_PUBLISH_SYNC_MODE,
            element=orm.RadioButtonsElement(
                initial_value=mode,
                options=orm.as_selector_options(
                    [o.name for o in mode_options],
                    [o.value for o in mode_options],
                ),
            ),
            optional=False,
        ),
        block_context("Any vs Specific can be changed without republishing."),
    ]

    if not sync.group_id:
        return blocks

    # Potential Specific targets are group members other than the publisher.
    publisher_id = sync.publisher_workspace_id
    group_members = _get_group_members(sync.group_id)
    ws_options: list[orm.SelectorOption] = []
    seen: set[str] = set()
    for member in group_members:
        if not member.workspace_id or member.workspace_id == publisher_id:
            continue
        value = str(member.workspace_id)
        if value in seen:
            continue
        seen.add(value)
        other_workspace = helpers.get_workspace_by_id(member.workspace_id)
        name = (
            helpers.resolve_workspace_name(other_workspace) if other_workspace else f"Workspace {member.workspace_id}"
        )
        ws_options.append(orm.SelectorOption(name=name, value=value))

    if ws_options:
        initial_target = None
        if sync.target_workspace_id and str(sync.target_workspace_id) in seen:
            initial_target = str(sync.target_workspace_id)
        blocks.append(
            orm.InputBlock(
                label="Target Workspace",
                action=actions.CONFIG_PUBLISH_DIRECT_TARGET,
                element=orm.StaticSelectElement(
                    placeholder="Select target Workspace",
                    options=ws_options,
                    initial_value=initial_target,
                ),
                optional=True,
            )
        )
        blocks.append(block_context("Required when who can subscribe is Specific. Leave blank when Available to All."))
    return blocks


def _live_subscriber_workspace_ids(sync_id: int, publisher_workspace_id: int | None) -> set[int]:
    """Workspace IDs of live SyncChannels that are not the publisher."""
    channels = DbManager.find_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.sync_id == sync_id, schemas.SyncChannel.deleted_at.is_(None)],
    )
    return {c.workspace_id for c in channels if c.workspace_id and c.workspace_id != publisher_workspace_id}


def _specific_would_drop_subscribers(
    sync: schemas.Sync,
    *,
    target_workspace_id: int | None,
) -> bool:
    """True when switching to Specific would leave other live subscribers stranded."""
    live = _live_subscriber_workspace_ids(sync.id, sync.publisher_workspace_id)
    if not live:
        return False
    if target_workspace_id is None:
        return True
    return bool(live - {target_workspace_id})


def handle_edit_sync(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open the one-step Edit modal for policy and/or this channel's reactions."""
    auth_result = _get_authorized_workspace(body, client, context, "edit_sync")
    if not auth_result:
        return
    _, workspace_record = auth_result

    kind, ref_id = _parse_edit_sync_ref(body)
    if not kind or not ref_id:
        _logger.warning("edit_sync: invalid action value")
        return

    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    sync_channel: schemas.SyncChannel | None = None
    sync_record: schemas.Sync | None = None

    if kind == "channel":
        sync_channel = _sync_channel_by_pk(ref_id)
        if not sync_channel or sync_channel.deleted_at:
            return
        if sync_channel.workspace_id != workspace_record.id:
            _logger.warning("edit_sync: channel not in acting workspace")
            return
        sync_record = DbManager.get_record(schemas.Sync, id=sync_channel.sync_id)
    else:
        sync_record = DbManager.get_record(schemas.Sync, id=ref_id)

    if not sync_record:
        return

    can_policy = _can_edit_sync_policy(sync_record, workspace_record)
    if kind == "sync" and not can_policy:
        _logger.warning("edit_sync: available-row edit denied")
        return

    from helpers.reactions import reaction_direction, reaction_style

    blocks: list[orm.BaseBlock] = []
    metadata: dict = {"sync_id": sync_record.id}
    if sync_channel:
        metadata["sync_channel_id"] = sync_channel.id

    if can_policy:
        blocks.extend(_who_can_subscribe_blocks(sync_record))

    if sync_channel:
        direction = reaction_direction(sync_channel)
        blocks.append(_reaction_direction_block(actions.CONFIG_PUBLISH_REACTION_DIRECTION, initial=direction))
        blocks.append(
            _reaction_style_block(
                actions.CONFIG_PUBLISH_REACTION_STYLE,
                initial=reaction_style(sync_channel) or constants.DEFAULT_REACTION_STYLE_NEW_RECEIVE,
            )
        )
        blocks.append(block_context("Reaction type is used only when this Workspace receives reactions."))

    if not blocks:
        return

    orm.BlockView(blocks=blocks).post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_EDIT_SYNC_SUBMIT,
        title_text="Edit",
        submit_button_text="Save",
        parent_metadata=metadata,
        new_or_add="new",
        body=body,
    )


def handle_edit_sync_submit_ack(
    body: dict,
    client: WebClient,
    context: dict,
) -> dict | None:
    """Ack phase: refuse Specific when it would drop live subscribers."""
    auth_result = _get_authorized_workspace(body, client, context, "edit_sync_submit_ack")
    if not auth_result:
        return None
    _, workspace_record = auth_result

    metadata = _parse_private_metadata(body)
    sync_id = metadata.get("sync_id")
    if not sync_id:
        return None

    sync_record = DbManager.get_record(schemas.Sync, id=int(sync_id))
    if not sync_record:
        return None

    if not _can_edit_sync_policy(sync_record, workspace_record):
        return None

    # Only validate when the mode field is present (policy editors see it).
    values = helpers.safe_get(body, "view", "state", "values") or {}
    has_mode = any(actions.CONFIG_PUBLISH_SYNC_MODE in block for block in values.values())
    if not has_mode:
        return None

    sync_mode = _get_selected_option_value(body, actions.CONFIG_PUBLISH_SYNC_MODE) or "group"
    if sync_mode != "direct":
        return None

    target_raw = _get_selected_option_value(body, actions.CONFIG_PUBLISH_DIRECT_TARGET)
    target_workspace_id: int | None = None
    if target_raw:
        try:
            target_workspace_id = int(target_raw)
        except (TypeError, ValueError):
            target_workspace_id = None

    if target_workspace_id is None:
        return {
            "response_action": "errors",
            "errors": {
                actions.CONFIG_PUBLISH_DIRECT_TARGET: (
                    "Pick a specific Workspace, or switch Who can subscribe back to Available to All."
                ),
            },
        }

    if _specific_would_drop_subscribers(sync_record, target_workspace_id=target_workspace_id):
        return {
            "response_action": "errors",
            "errors": {
                actions.CONFIG_PUBLISH_SYNC_MODE: (
                    "Other Workspaces are already subscribed. They must Stop Syncing first, or keep Available to All."
                ),
            },
        }
    return None


def handle_edit_sync_submit(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Persist policy (when allowed) and this channel's reaction settings."""
    auth_result = _get_authorized_workspace(body, client, context, "edit_sync_submit")
    if not auth_result:
        return
    _, workspace_record = auth_result

    metadata = _parse_private_metadata(body)
    sync_id = metadata.get("sync_id")
    sync_channel_id = metadata.get("sync_channel_id")
    if not sync_id:
        return

    sync_record = DbManager.get_record(schemas.Sync, id=int(sync_id))
    if not sync_record:
        return

    mode_changed = False
    if _can_edit_sync_policy(sync_record, workspace_record):
        values = helpers.safe_get(body, "view", "state", "values") or {}
        has_mode = any(actions.CONFIG_PUBLISH_SYNC_MODE in block for block in values.values())
        if has_mode:
            sync_mode = _get_selected_option_value(body, actions.CONFIG_PUBLISH_SYNC_MODE) or "group"
            if sync_mode not in ("group", "direct"):
                sync_mode = "group"
            target_workspace_id: int | None = None
            if sync_mode == "direct":
                target_raw = _get_selected_option_value(body, actions.CONFIG_PUBLISH_DIRECT_TARGET)
                try:
                    target_workspace_id = int(target_raw) if target_raw else None
                except (TypeError, ValueError):
                    target_workspace_id = None
                if target_workspace_id is None or _specific_would_drop_subscribers(
                    sync_record, target_workspace_id=target_workspace_id
                ):
                    # Ack should have blocked; skip policy write.
                    sync_mode = None
            if sync_mode is not None:
                new_target = target_workspace_id if sync_mode == "direct" else None
                if sync_record.sync_mode != sync_mode or sync_record.target_workspace_id != new_target:
                    DbManager.update_records(
                        schemas.Sync,
                        [schemas.Sync.id == sync_record.id],
                        {
                            schemas.Sync.sync_mode: sync_mode,
                            schemas.Sync.target_workspace_id: new_target,
                        },
                    )
                    mode_changed = True

    if sync_channel_id:
        sync_channel = _sync_channel_by_pk(int(sync_channel_id))
        if sync_channel and sync_channel.workspace_id == workspace_record.id:
            direction = (
                _get_selected_option_value(body, actions.CONFIG_PUBLISH_REACTION_DIRECTION)
                or constants.REACTION_DIRECTION_BOTH
            )
            from helpers.reactions import (
                default_reaction_style_for_new_channel,
                direction_receives,
                update_sync_channel_reactions,
            )

            style = None
            if direction_receives(direction):
                style = _get_selected_option_value(body, actions.CONFIG_PUBLISH_REACTION_STYLE)
                if style not in (constants.REACTION_STYLE_DIRECT_ONLY, constants.REACTION_STYLE_THREADED_AND_DIRECT):
                    style = default_reaction_style_for_new_channel(direction)
            update_sync_channel_reactions(int(sync_channel_id), direction=direction, style=style)

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context)
    if mode_changed and sync_record.group_id:
        _refresh_group_member_homes(sync_record.group_id, workspace_record.id, logger, context=context)


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
