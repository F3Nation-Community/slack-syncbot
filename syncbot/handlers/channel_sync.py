"""Channel sync handlers — create, join, edit, pause, resume, leave."""

import contextlib
from datetime import UTC, datetime
from logging import Logger

from slack_sdk.web import WebClient

import builders
import constants
import helpers
from builders._common import _format_channel_ref
from db import DbManager, schemas
from handlers._common import (
    _close_modal_done,
    _ensure_membership_or_rollback,
    _get_authorized_workspace,
    _get_selected_conversation_or_option,
    _get_selected_option_value,
    _parse_private_metadata,
    _sanitize_text,
)
from helpers.channel_state import get_slack_channel_private, set_is_private
from helpers.conversations import inspect_bot_channel_access
from helpers.export_import import invalidate_group_member_homes
from helpers.leave_sync import (
    group_name_for_sync,
    soft_delete_sync_channels,
    with_group,
)
from helpers.settings import soft_delete_retention_days
from helpers.sync_lifecycle import (
    deliver_action_notice,
    pause_sync_channel,
    rejoin_sync_channel,
    resume_sync_channel,
    revive_sync_channel,
)
from helpers.synced_by import (
    can_manage_sync,
    set_channel_private,
    sync_privacy_conflict,
    viewer_context,
)
from logger import log_error, log_info, log_warning
from slack import actions, orm
from slack.blocks import context as block_context
from slack.blocks import section

_PARTICIPATION_ACTIONS = (actions.CONFIG_SYNC_PARTICIPATION,)
_CREATE_CHANNEL_ACTIONS = (actions.CONFIG_CREATE_SYNC_SELECT,)
_JOIN_CHANNEL_ACTIONS = (actions.CONFIG_JOIN_SYNC_SELECT,)
_REACTION_STYLE_ACTIONS = (actions.CONFIG_SYNC_REACTION_STYLE,)


def _participation_block(
    *,
    mode: str,
    publishes: bool = True,
    subscribes: bool = True,
) -> orm.InputBlock:
    """Choose this channel's source/target participation in a sync."""
    create_options = [
        orm.SelectorOption(name="Publish and Subscribe", value="publish_and_subscribe"),
        orm.SelectorOption(name="Publish only", value="publish_only"),
    ]
    all_options = [
        orm.SelectorOption(name="Publish and Subscribe", value="publish_and_subscribe"),
        orm.SelectorOption(name="Publish only", value="publish_only"),
        orm.SelectorOption(name="Subscribe only", value="subscribe_only"),
    ]
    options = create_options if mode == "create" else all_options
    if publishes and not subscribes:
        initial = "publish_only"
    elif subscribes and not publishes:
        initial = "subscribe_only"
    else:
        initial = "publish_and_subscribe"
    if mode == "create" and initial == "subscribe_only":
        initial = "publish_and_subscribe"
    return orm.InputBlock(
        label="Participation",
        action=actions.CONFIG_SYNC_PARTICIPATION,
        element=orm.RadioButtonsElement(
            initial_value=initial,
            options=options,
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
                    name="Hybrid — direct when possible, otherwise a thread notice",
                    value=constants.REACTION_STYLE_THREADED_AND_DIRECT,
                ),
                orm.SelectorOption(
                    name="Direct — native emoji on the synced message",
                    value=constants.REACTION_STYLE_DIRECT_ONLY,
                ),
                orm.SelectorOption(
                    name="Off — do not apply incoming reactions",
                    value=constants.REACTION_STYLE_OFF,
                ),
            ],
        ),
        optional=False,
    )


def _valid_reaction_style(value: str | None) -> str | None:
    raw = (value or "").strip()
    if raw in (
        constants.REACTION_STYLE_DIRECT_ONLY,
        constants.REACTION_STYLE_THREADED_AND_DIRECT,
        constants.REACTION_STYLE_OFF,
    ):
        return raw
    return None


def _participation_from_body(body: dict) -> tuple[bool, bool]:
    for action in _PARTICIPATION_ACTIONS:
        value = _get_selected_option_value(body, action)
        if value:
            return helpers.parse_participation_flags(value)
    return True, True


def _selected_channel(body: dict, action_ids: tuple[str, ...]) -> tuple[str | None, str]:
    for action_id in action_ids:
        channel_id = _get_selected_conversation_or_option(body, action_id)
        if channel_id:
            return channel_id, action_id
    return None, action_ids[0]


def _parse_reaction_fields(body: dict, *, style_action: str | None = None) -> str:
    """Reaction type from the form. Always shown; unused until the Channel subscribes."""
    from helpers.reaction import default_reaction_style_for_new_channel

    actions_to_try = (style_action,) if style_action else _REACTION_STYLE_ACTIONS
    for action in actions_to_try:
        if not action:
            continue
        style = _valid_reaction_style(_get_selected_option_value(body, action))
        if style:
            return style
    return default_reaction_style_for_new_channel(subscribes=True)


def _group_name(group_id: int | None) -> str | None:
    if not group_id:
        return None
    group = DbManager.get_record(schemas.WorkspaceGroup, group_id)
    name = getattr(group, "name", None) if group else None
    return str(name).strip() if name else None


def _publishing_sync_channels(sync_id: int) -> list[schemas.SyncChannel]:
    """Rows of *sync_id* that publish and have ``deleted_at`` unset."""
    rows = DbManager.find_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.sync_id == int(sync_id), schemas.SyncChannel.deleted_at.is_(None)],
    )
    return [row for row in rows if helpers.channel_publishes(row)]


def _sync_refusal(row, workspace, user_id: str | None, client, *, action: str) -> str | None:
    """The sentence when this person may not run *row*, else None."""
    viewer = viewer_context(workspace, user_id, client)
    allowed, reason = can_manage_sync(row, viewer, action=action)
    return None if allowed else reason


def _sync_phrase(sync, *, capital: bool = False) -> str:
    """``the Sync in this Channel with `Partner Org` ``."""
    phrase = f"the Sync in this Channel{with_group(group_name_for_sync(sync))}"
    if capital:
        return phrase[:1].upper() + phrase[1:]
    return phrase


def _sync_id_from_action(body: dict, prefixes: tuple[str, ...]) -> int | None:
    raw = helpers.safe_get(body, "actions", 0, "value")
    try:
        if raw is not None and str(raw).strip() != "":
            return int(raw)
    except (TypeError, ValueError):
        pass
    action_id = helpers.safe_get(body, "actions", 0, "action_id") or ""
    for prefix in prefixes:
        if action_id.startswith(prefix + "_"):
            try:
                return int(action_id[len(prefix) + 1 :])
            except (TypeError, ValueError):
                continue
    return None


def _relationship_context(
    *,
    group_id: int | None = None,
    local_channel_id: str | None = None,
    local_workspace=None,
    published_channel: schemas.SyncChannel | None = None,
    published_workspace=None,
) -> orm.ContextBlock | None:
    """Local Channel, published source, and group — whichever this modal knows."""
    lines: list[str] = []
    if local_channel_id:
        lines.append(f"Channel: {_format_channel_ref(local_channel_id, local_workspace, is_local=True)}")
    if published_channel is not None:
        lines.append(
            "Published Channel: "
            + _format_channel_ref(published_channel.channel_id, published_workspace, is_local=False)
        )
    name = _group_name(group_id)
    if name:
        lines.append(f"Group: `{name}`")
    if not lines:
        return None
    return block_context("\n".join(lines))


def _participation_help(*, mode: str) -> str:
    if mode == "create":
        return (
            "Publish and Subscribe sends and receives with Workspaces that join. "
            "Publish only sends this Channel's messages without receiving."
        )
    return (
        "Publish and Subscribe exchanges messages both ways. Publish only sends this Channel's "
        "messages without receiving. Subscribe only receives messages from this Sync."
    )


def _reaction_style_help(*, first_time: bool) -> str:
    base = (
        "Reaction type applies while this Channel subscribes. "
        "Off does not apply incoming reactions here, including later unreacts."
    )
    if not first_time:
        return base
    return (
        f"{base} Authorize SyncBot in each Workspace where you want reactions to appear as you. "
        "Custom emoji the other Workspace does not have will not appear as a native reaction there."
    )


def _reaction_style_for_edit(
    sync_channel: schemas.SyncChannel,
    body: dict | None = None,
    *,
    subscribes: bool | None = None,
) -> str | None:
    """Keep a saved type even when the channel does not subscribe.

    Edit always shows the type radios. Turning subscribe off should not
    reset Hybrid, Direct, or Off to the new-channel default.
    """
    from helpers.reaction import default_reaction_style_for_new_channel, get_reaction_style

    submitted = None
    if body is not None:
        for action in _REACTION_STYLE_ACTIONS:
            submitted = _valid_reaction_style(_get_selected_option_value(body, action))
            if submitted:
                break
    if submitted:
        return submitted
    stored = _valid_reaction_style(getattr(sync_channel, "reaction_style", None))
    if stored:
        return stored
    resolved = _valid_reaction_style(get_reaction_style(sync_channel))
    if resolved:
        return resolved
    receives = helpers.channel_subscribes(sync_channel) if subscribes is None else subscribes
    if receives:
        return default_reaction_style_for_new_channel(subscribes=True)
    return None


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
        base = "Search for a Channel in your Workspace to join this Sync."
    else:
        base = "Search for a Channel in your Workspace to create a Sync."
    if helpers.allow_private_channels(team_id):
        if subscribe:
            return (
                f"{base} :warning: Private Channels are currently allowed. Messages from this Sync "
                "will be copied into it, so anyone who can see your Channel "
                "will be able to read them. If you pick a private Channel, SyncBot is added to it "
                "for you, using your permission to invite it."
            )
        return (
            f"{base} :warning: Private Channels are currently allowed. If you create a Sync on a "
            "private Channel, its messages will be copied into the other Workspaces in this Group, "
            "where anyone who can see the synced Channel will be able to read them. If you pick a "
            "private Channel, SyncBot is added to it for you, using your permission to invite it."
        )
    return f"{base} Only public Channels can be synced."


def _validate_channel_selection(
    client: WebClient,
    channel_id: str | None,
    action_id: str,
    *,
    team_id: str | None = None,
    acting_user_id: str | None = None,
    workspace_id: int | None = None,
    source_sync_id: int | None = None,
) -> tuple[dict | None, bool | None]:
    """Validate a selected channel on submit.

    Returns ``(error response or None, is_private or None)``.

    Three rules, all enforced here rather than only in the picker filter, which is
    advisory and bypassable:

    * A Workspace cannot subscribe to the same published source twice.
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
        }, None

    if workspace_id and source_sync_id:
        source_rows = DbManager.find_records(
            schemas.SyncChannel,
            [
                schemas.SyncChannel.sync_id == int(source_sync_id),
                schemas.SyncChannel.deleted_at.is_(None),
            ],
        )
        for source in source_rows:
            if not helpers.channel_publishes(source):
                continue
            if helpers.already_subscribed_to_source(
                workspace_id=workspace_id,
                source_workspace_id=source.workspace_id,
                source_channel_id=source.channel_id,
            ):
                return {
                    "response_action": "errors",
                    "errors": {
                        action_id: (
                            "This Workspace already subscribes to that published Channel through another Channel Sync."
                        )
                    },
                }, None

    if not helpers.allow_private_channels(team_id or ""):
        is_private = get_slack_channel_private(client, channel_id)
        if is_private is None:
            # Fail closed: an unreadable channel is one the bot cannot join either.
            return {
                "response_action": "errors",
                "errors": {action_id: "SyncBot could not read that Channel. Pick a public Channel it can join."},
            }, None
        if is_private:
            return {
                "response_action": "errors",
                "errors": {action_id: "Private Channels cannot be synced. Pick a public Channel."},
            }, True
        return None, False

    # Private channels are allowed; a Channel the bot cannot see is private.
    picked_private = inspect_bot_channel_access(client, channel_id)[0]
    if helpers.has_user_token(team_id, acting_user_id):
        return None, picked_private
    if picked_private:
        return {
            "response_action": "errors",
            "errors": {action_id: helpers.AUTHORIZE_HINT},
        }, True

    return None, False


def _privacy_mismatch(sync_id: int, picked_private: bool | None) -> str | None:
    """Field error when the pick disagrees with a live row that already has a privacy flag."""
    conflict = sync_privacy_conflict(sync_id, picked_private)
    if conflict is True:
        return "This Sync is private, so pick a private Channel."
    if conflict is False:
        return "This Sync is public, so pick a public Channel."
    return None


def _build_create_sync_blocks(*, team_id: str | None, group_id: int | None) -> list[orm.BaseBlock]:
    """One-screen Create Sync: group context, participation, picker, reaction type."""
    blocks: list[orm.BaseBlock] = []
    ctx = _relationship_context(group_id=group_id)
    if ctx:
        blocks.append(ctx)
    blocks.extend(
        [
            _participation_block(mode="create"),
            block_context(_participation_help(mode="create")),
            _channel_picker_block("Channel", actions.CONFIG_CREATE_SYNC_SELECT, team_id=team_id),
            block_context(_channel_picker_help_text(team_id=team_id)),
            _reaction_style_block(actions.CONFIG_SYNC_REACTION_STYLE),
            block_context(_reaction_style_help(first_time=True)),
        ]
    )
    return blocks


def handle_create_sync(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open the one-step Create Sync modal."""
    auth_result = _get_authorized_workspace(body, client, context, "create_sync")
    if not auth_result:
        return
    _, workspace_record = auth_result

    trigger_id = helpers.safe_get(body, "trigger_id")
    raw_group_id = helpers.safe_get(body, "actions", 0, "value")
    try:
        group_id = int(raw_group_id)
    except (TypeError, ValueError):
        log_warning("create_sync", raw_group_id=raw_group_id)
        return

    orm.BlockView(blocks=_build_create_sync_blocks(team_id=workspace_record.team_id, group_id=group_id)).post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_CREATE_SYNC_SUBMIT,
        title_text="Create Sync",
        submit_button_text="Create Sync",
        parent_metadata={"group_id": group_id, "workspace_id": workspace_record.id},
        body=body,
    )


def handle_create_sync_submit_ack(
    body: dict,
    client: WebClient,
    context: dict,
) -> dict | None:
    """Ack phase for Create Sync: validate and close modal (errors) or empty ack (success)."""
    auth_result = _get_authorized_workspace(body, client, context, "create_sync_submit")
    if not auth_result:
        return None
    _, workspace_record = auth_result

    metadata = _parse_private_metadata(body)
    group_id = metadata.get("group_id")

    if not group_id:
        log_warning("create_sync_submit", reason="missing group_id in metadata")
        return None

    channel_id, picker_action = _selected_channel(body, _CREATE_CHANNEL_ACTIONS)

    error, _picked = _validate_channel_selection(
        client,
        channel_id,
        picker_action,
        team_id=helpers.get_team_id_from_body(body) or workspace_record.team_id,
        acting_user_id=helpers.get_user_id_from_body(body),
    )
    return error


def _revive_deleted_create(
    client,
    workspace,
    acting_user_id: str,
    group_id: int,
    channel_id: str,
    publishes: bool,
    subscribes: bool,
    reaction_style: str | None,
    context: dict | None,
):
    """Turn this workspace's removed row back on, or None when Create should insert.

    Returns that row when it is active again, and False when the invite failed.
    """
    row = rejoin_sync_channel(group_id, workspace.id, channel_id)
    if row is None:
        return None
    revived = revive_sync_channel(
        row,
        client=client,
        workspace=workspace,
        acting_user_id=acting_user_id,
        publishes=publishes,
        subscribes=subscribes,
        reaction_style=reaction_style,
        context=context,
        via="create",
    )
    if revived is None:
        return False
    return row


def handle_create_sync_submit_work(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Lazy work phase: create Sync + SyncChannel after modal closed."""
    auth_result = _get_authorized_workspace(body, client, context, "create_sync_submit")
    if not auth_result:
        return
    user_id, workspace_record = auth_result

    metadata = _parse_private_metadata(body)
    group_id = metadata.get("group_id")

    if not group_id:
        return

    publishes, subscribes = _participation_from_body(body)
    reaction_style = _parse_reaction_fields(body)
    channel_id, picker_action = _selected_channel(body, _CREATE_CHANNEL_ACTIONS)

    # The ack phase already surfaced any error; this keeps the work phase from
    # writing on a payload it should reject.
    error, _picked = _validate_channel_selection(
        client,
        channel_id,
        picker_action,
        team_id=helpers.get_team_id_from_body(body) or workspace_record.team_id,
        acting_user_id=user_id,
    )
    if error:
        return

    acting_user_id = user_id
    team_id = helpers.get_team_id_from_body(body) or workspace_record.team_id
    revived = _revive_deleted_create(
        client,
        workspace_record,
        acting_user_id,
        int(group_id),
        channel_id,
        publishes,
        subscribes,
        reaction_style,
        context,
    )
    if revived is not None:
        if revived is False:
            return
        sync_record = DbManager.get_record(schemas.Sync, id=revived.sync_id)
        admin_name = helpers.format_admin_label(client, acting_user_id, workspace_record)
        deliver_action_notice(
            workspace_record,
            revived,
            action="Create",
            manager=admin_name,
            group_name=group_name_for_sync(sync_record),
            sync=sync_record,
        )
        builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)
        invalidate_group_member_homes(group_id, exclude_workspace_id=workspace_record.id)
        return

    channel_name = helpers.lookup_channel_name(
        channel_id,
        workspace_record,
        user_token=helpers.get_user_token(team_id, acting_user_id),
        client=client,
    )

    try:
        from federation.replicate import mint_uid

        sync_record = schemas.Sync(
            title=_sanitize_text(channel_name),
            description=None,
            group_id=group_id,
            sync_mode="group",
            uid=mint_uid(),
        )
        DbManager.create_record(sync_record)

        sync_channel_record = schemas.SyncChannel(
            sync_id=sync_record.id,
            channel_id=channel_id,
            channel_name=channel_name if channel_name and channel_name != channel_id else None,
            workspace_id=workspace_record.id,
            created_at=datetime.now(UTC),
            reaction_style=reaction_style,
            publishes=publishes,
            subscribes=subscribes,
        )
        DbManager.create_record(sync_channel_record)
    except Exception as e:
        log_error("failed_to_create_sync_for_channel", channel_id=channel_id, error=str(e))
        return

    helpers.invalidate_channel_memberships(channel_id)

    # Membership comes after the rows exist: Slack fires ``member_joined_channel``
    # as soon as the bot is added, and that handler leaves any channel with no
    # SyncChannel. Writing first is what lets a private channel work at all.
    is_private = _ensure_membership_or_rollback(
        client,
        channel_id,
        team_id=team_id,
        acting_user_id=acting_user_id,
        rollback=lambda: helpers.purge_sync(sync_record.id),
        log_event="create_sync_membership_failed",
        log_extra={"workspace_id": workspace_record.id, "channel_id": channel_id, "sync_id": sync_record.id},
        context=context,
    )
    if is_private is None:
        return

    set_is_private(sync_channel_record, is_private=is_private)
    if is_private:
        set_channel_private(sync_channel_record, acting_user_id)
    refreshed_name = helpers.lookup_channel_name(channel_id, workspace_record)
    if refreshed_name and refreshed_name != channel_id and refreshed_name != sync_record.title:
        DbManager.update_records(
            schemas.Sync,
            [schemas.Sync.id == sync_record.id],
            {schemas.Sync.title: _sanitize_text(refreshed_name)},
        )
        sync_record.title = refreshed_name

    admin_name = helpers.format_admin_label(client, acting_user_id, workspace_record)
    deliver_action_notice(
        workspace_record,
        sync_channel_record,
        action="Create",
        manager=admin_name,
        group_name=group_name_for_sync(sync_record),
        sync=sync_record,
    )

    log_info(
        "sync_created",
        workspace_id=workspace_record.id,
        channel_id=channel_id,
        group_id=group_id,
        sync_id=sync_record.id,
        publishes=publishes,
        subscribes=subscribes,
    )

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)
    invalidate_group_member_homes(group_id, exclude_workspace_id=workspace_record.id)


def handle_leave_sync(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Show a confirmation modal before leaving a channel sync."""
    sync_id = _sync_id_from_action(body, (actions.CONFIG_LEAVE_SYNC,))
    if not sync_id:
        log_warning("leave_sync_invalid_id", action_id=helpers.safe_get(body, "actions", 0, "action_id"))
        return

    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    confirm_blocks: list[orm.BaseBlock] = []
    auth_result = _get_authorized_workspace(body, client, context, "leave_sync")
    if auth_result:
        user_id, workspace_record = auth_result
        all_channels = DbManager.find_records(
            schemas.SyncChannel,
            [schemas.SyncChannel.sync_id == sync_id, schemas.SyncChannel.deleted_at.is_(None)],
        )
        my_channel = next((row for row in all_channels if row.workspace_id == workspace_record.id), None)
        if my_channel:
            denied = _sync_refusal(my_channel, workspace_record, user_id, client, action="leave it")
            if denied:
                _close_modal_done(client, body, denied)
                return
        sync_record = DbManager.get_record(schemas.Sync, sync_id)
        ctx = _relationship_context(
            group_id=getattr(sync_record, "group_id", None) if sync_record else None,
            local_channel_id=my_channel.channel_id if my_channel else None,
            local_workspace=workspace_record,
        )
        if ctx:
            confirm_blocks.append(ctx)

    days = soft_delete_retention_days()
    which = _sync_phrase(sync_record) if sync_record else "the Sync in this Channel"
    warning = (
        f":warning: *Leave {which}?*\n\n"
        "This will:\n"
        "\u2022 Remove this Channel from that Sync; SyncBot leaves unless another Sync still uses it\n"
        f"\u2022 Keep its history for {days} days, so Joining again within that time restores it\n"
        "\u2022 Other Workspaces in that Sync continue\n"
        "\u2022 The notice lists who is still in the Sync\n"
    )
    warning += "\n_No messages are deleted from any Channel._"
    confirm_blocks.append(section(warning))
    confirm_form = orm.BlockView(
        blocks=confirm_blocks
        + [
            orm.ActionsBlock(
                elements=[
                    orm.ButtonElement(
                        label=":wave: Leave Sync",
                        action=actions.CONFIG_LEAVE_SYNC_CONFIRM,
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
        callback_id=actions.CONFIG_LEAVE_SYNC_CONFIRM,
        title_text="Leave Sync",
        submit_button_text=None,
        close_button_text="Cancel",
        parent_metadata={"sync_id": sync_id},
        body=body,
    )


def handle_leave_sync_confirm(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Soft-delete this workspace's row; SyncBot leaves the Channel unless another Sync still uses it."""
    auth_result = _get_authorized_workspace(body, client, context, "leave_sync_confirm")
    if not auth_result:
        return
    user_id, workspace_record = auth_result

    meta = _parse_private_metadata(body)
    sync_id = meta.get("sync_id")
    if not sync_id:
        log_warning("leave_sync_confirm", reason="missing sync_id in metadata")
        return

    my_channel = next(
        (
            row
            for row in DbManager.find_records(
                schemas.SyncChannel,
                [
                    schemas.SyncChannel.sync_id == int(sync_id),
                    schemas.SyncChannel.workspace_id == workspace_record.id,
                    schemas.SyncChannel.deleted_at.is_(None),
                ],
            )
        ),
        None,
    )
    if not my_channel:
        log_warning("leave_sync_confirm", reason="no channel", sync_id=sync_id)
        return
    denied = _sync_refusal(my_channel, workspace_record, user_id, client, action="leave it")
    if denied:
        _close_modal_done(client, body, denied)
        return

    admin_name = helpers.format_admin_label(client, user_id, workspace_record)
    if not soft_delete_sync_channels(
        workspace_record,
        my_channel.channel_id,
        "left_sync",
        sync_ids={int(sync_id)},
        person=admin_name,
    ):
        log_error("leave_sync_failed", sync_id=sync_id, channel_id=my_channel.channel_id, reason="no_live_row")
        with contextlib.suppress(Exception):
            helpers.notify_admins_dm(
                client,
                helpers.format_error_dm(
                    ":warning: Leaving that Sync failed, so it is still active. "
                    "Please try again, and let your SyncBot operator know if it keeps failing.",
                    {"event": "leave_sync_failed", "sync": sync_id},
                ),
                team_id=workspace_record.team_id,
            )
        return
    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)
    left_sync = DbManager.get_record(schemas.Sync, id=int(sync_id)) if sync_id else None
    left = _sync_phrase(left_sync) if left_sync else "the Sync in this Channel"
    _close_modal_done(client, body, f":wave: You left {left}. You can close this now.")


def _open_pause_resume_confirm(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
    *,
    prefixes: tuple[str, ...],
    confirm_action: str,
    title: str,
    button_label: str,
    warning: str,
    log_event: str,
) -> None:
    sync_id = _sync_id_from_action(body, prefixes)
    if not sync_id:
        log_warning(f"{log_event}_invalid_id", action_id=helpers.safe_get(body, "actions", 0, "action_id"))
        return
    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    confirm_blocks: list[orm.BaseBlock] = []
    auth_result = _get_authorized_workspace(body, client, context, log_event)
    if auth_result:
        user_id, workspace_record = auth_result
        sync_record = DbManager.get_record(schemas.Sync, sync_id)
        my_channel = next(
            (
                row
                for row in DbManager.find_records(
                    schemas.SyncChannel,
                    [
                        schemas.SyncChannel.sync_id == sync_id,
                        schemas.SyncChannel.workspace_id == workspace_record.id,
                        schemas.SyncChannel.deleted_at.is_(None),
                    ],
                )
            ),
            None,
        )
        ctx = _relationship_context(
            group_id=getattr(sync_record, "group_id", None) if sync_record else None,
            local_channel_id=my_channel.channel_id if my_channel else None,
            local_workspace=workspace_record,
        )
        if ctx:
            confirm_blocks.append(ctx)
        if "{sync}" in warning:
            warning = warning.format(sync=_sync_phrase(sync_record) if sync_record else "the Sync in this Channel")
        if my_channel:
            action = "resume it" if log_event == "resume_sync" else "pause it"
            denied = _sync_refusal(my_channel, workspace_record, user_id, client, action=action)
            if denied:
                orm.BlockView(blocks=[*confirm_blocks, section(denied)]).post_modal(
                    client=client,
                    trigger_id=trigger_id,
                    callback_id=confirm_action,
                    title_text=title,
                    submit_button_text=None,
                    close_button_text="Close",
                    parent_metadata={"sync_id": sync_id},
                    body=body,
                )
                return
    if "{sync}" in warning:
        warning = warning.format(sync="the Sync in this Channel")
    confirm_blocks.append(section(warning))
    confirm_form = orm.BlockView(
        blocks=confirm_blocks
        + [
            orm.ActionsBlock(
                elements=[
                    orm.ButtonElement(
                        label=button_label,
                        action=confirm_action,
                        value=str(sync_id),
                    ),
                ]
            ),
        ]
    )
    confirm_form.post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=confirm_action,
        title_text=title,
        submit_button_text=None,
        close_button_text="Cancel",
        parent_metadata={"sync_id": sync_id},
        body=body,
    )


def _confirmed_sync_row(
    body: dict, client: WebClient, context: dict, log_event: str
) -> tuple[int, str, schemas.Workspace, schemas.SyncChannel] | None:
    """Shared parsing for the Pause and Resume confirms: the Sync id, person, workspace, and their row."""
    meta = _parse_private_metadata(body)
    sync_id = meta.get("sync_id") or _sync_id_from_action(body, (actions.CONFIG_PAUSE_SYNC, actions.CONFIG_RESUME_SYNC))
    if not sync_id:
        log_warning(f"{log_event}_invalid_id")
        return None
    auth_result = _get_authorized_workspace(body, client, context, log_event)
    if not auth_result:
        return None
    user_id, workspace_record = auth_result
    row = next(
        (
            candidate
            for candidate in DbManager.find_records(
                schemas.SyncChannel,
                [
                    schemas.SyncChannel.sync_id == int(sync_id),
                    schemas.SyncChannel.workspace_id == workspace_record.id,
                    schemas.SyncChannel.deleted_at.is_(None),
                ],
            )
        ),
        None,
    )
    if not row:
        log_warning(f"{log_event}_no_channel_for_workspace", sync_id=sync_id, workspace_id=workspace_record.id)
        return None
    return int(sync_id), user_id, workspace_record, row


def _refresh_homes_after_status_change(
    workspace_record: schemas.Workspace, sync_id: int, user_id: str, logger: Logger, context: dict
) -> None:
    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)
    sync_record = DbManager.get_record(schemas.Sync, id=sync_id)
    if sync_record and sync_record.group_id:
        invalidate_group_member_homes(sync_record.group_id, exclude_workspace_id=workspace_record.id)


def handle_pause_sync(body: dict, client: WebClient, logger: Logger, context: dict) -> None:
    """Show a confirmation modal before pausing."""
    _open_pause_resume_confirm(
        body,
        client,
        logger,
        context,
        prefixes=(actions.CONFIG_PAUSE_SYNC,),
        confirm_action=actions.CONFIG_PAUSE_SYNC_CONFIRM,
        title="Pause Sync",
        button_label=":double_vertical_bar: Pause Sync",
        warning=(
            ":double_vertical_bar: *Pause {sync}?*\n\n"
            "Messages stop here until a manager resumes the Sync. "
            "SyncBot leaves unless another Sync on this Channel is still running."
        ),
        log_event="pause_sync",
    )


def handle_pause_sync_confirm(body: dict, client: WebClient, logger: Logger, context: dict) -> None:
    """Pause this workspace's row: SyncBot leaves the Channel and the row stays on Home as Paused."""
    found = _confirmed_sync_row(body, client, context, "pause_sync_confirm")
    if not found:
        return
    sync_id, user_id, workspace_record, row = found
    denied = _sync_refusal(row, workspace_record, user_id, client, action="pause it")
    if denied:
        _close_modal_done(client, body, denied)
        return
    admin_name = helpers.format_admin_label(client, user_id, workspace_record)
    paused_sync = DbManager.get_record(schemas.Sync, id=sync_id)
    which = _sync_phrase(paused_sync, capital=True) if paused_sync else "The Sync in this Channel"
    paused = pause_sync_channel(row, who=admin_name, via="home", leave=True)
    if not paused:
        _close_modal_done(client, body, f":double_vertical_bar: {which} is already Paused. You can close this now.")
        return
    _refresh_homes_after_status_change(workspace_record, sync_id, user_id, logger, context)
    _close_modal_done(client, body, f":double_vertical_bar: {which} is paused. You can close this now.")


def handle_resume_sync(body: dict, client: WebClient, logger: Logger, context: dict) -> None:
    """Show a confirmation modal before resuming."""
    _open_pause_resume_confirm(
        body,
        client,
        logger,
        context,
        prefixes=(actions.CONFIG_RESUME_SYNC,),
        confirm_action=actions.CONFIG_RESUME_SYNC_CONFIRM,
        title="Resume Sync",
        button_label=":arrow_forward: Resume Sync",
        warning=(
            ":arrow_forward: *Resume {sync}?*\n\n"
            "SyncBot rejoins this Channel, and messages, threads, and reactions start Syncing again."
        ),
        log_event="resume_sync",
    )


def handle_resume_sync_confirm(body: dict, client: WebClient, logger: Logger, context: dict) -> None:
    """Resume this workspace's paused row: SyncBot rejoins the Channel and the Sync runs again."""
    found = _confirmed_sync_row(body, client, context, "resume_sync_confirm")
    if not found:
        return
    sync_id, user_id, workspace_record, row = found
    resumed_sync = DbManager.get_record(schemas.Sync, id=sync_id)
    which = _sync_phrase(resumed_sync, capital=True) if resumed_sync else "The Sync in this Channel"
    if getattr(row, "status", None) != "paused":
        _close_modal_done(client, body, f":warning: {which} is not Paused, so there is nothing to Resume.")
        return
    denied = resume_sync_channel(row, workspace_record, user_id, client=client, join=True, context=context)
    if denied:
        _close_modal_done(client, body, denied)
        return
    _refresh_homes_after_status_change(workspace_record, sync_id, user_id, logger, context)
    _close_modal_done(client, body, f":arrow_forward: {which} is Resumed. You can close this now.")


def handle_join_sync(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open the one-step Join Sync modal."""
    auth_result = _get_authorized_workspace(body, client, context, "join_sync")
    if not auth_result:
        return
    user_id, workspace_record = auth_result

    trigger_id = helpers.safe_get(body, "trigger_id")
    sync_id = helpers.safe_get(body, "actions", 0, "value")
    if not sync_id:
        log_warning("join_sync", reason="missing sync_id")
        return

    viewer = viewer_context(workspace_record, user_id, client)
    publishers = _publishing_sync_channels(int(sync_id))
    for publisher in publishers:
        allowed, reason = can_manage_sync(publisher, viewer, action="join it")
        if not allowed:
            orm.BlockView(blocks=[section(reason)]).post_modal(
                client=client,
                trigger_id=trigger_id,
                callback_id=actions.CONFIG_JOIN_SYNC_SUBMIT,
                title_text="Join Sync",
                submit_button_text=None,
                close_button_text="Close",
                parent_metadata={"sync_id": int(sync_id)},
                body=body,
            )
            return

    sync_record = DbManager.get_record(schemas.Sync, int(sync_id))
    pub_ch = publishers[0] if publishers else None
    pub_ws = helpers.get_workspace_by_id(pub_ch.workspace_id) if pub_ch else None

    blocks: list[orm.BaseBlock] = []
    ctx = _relationship_context(
        group_id=getattr(sync_record, "group_id", None) if sync_record else None,
        published_channel=pub_ch,
        published_workspace=pub_ws,
    )
    if ctx:
        blocks.append(ctx)
    blocks.extend(
        [
            _participation_block(mode="join"),
            block_context(_participation_help(mode="join")),
            _channel_picker_block(
                "Channel",
                actions.CONFIG_JOIN_SYNC_SELECT,
                team_id=workspace_record.team_id,
            ),
            block_context(_channel_picker_help_text(team_id=workspace_record.team_id, subscribe=True)),
            _reaction_style_block(actions.CONFIG_SYNC_REACTION_STYLE),
            block_context(_reaction_style_help(first_time=True)),
        ]
    )

    orm.BlockView(blocks=blocks).post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_JOIN_SYNC_SUBMIT,
        title_text="Join Sync",
        submit_button_text="Join Sync",
        parent_metadata={"sync_id": int(sync_id)},
        body=body,
    )


def handle_join_sync_submit_ack(
    body: dict,
    client: WebClient,
    context: dict,
) -> dict | None:
    """Ack phase for Join Sync: surface a visible error, or ack empty on success.

    The native picker cannot pre-exclude ineligible channels, so an invalid
    choice has to be reported here rather than returning silently and leaving the
    user with a modal that appeared to work.
    """
    auth_result = _get_authorized_workspace(body, client, context, "join_sync_submit")
    if not auth_result:
        return None
    user_id, workspace_record = auth_result

    metadata = _parse_private_metadata(body)
    if not metadata.get("sync_id"):
        log_warning("join_sync_submit", reason="missing sync_id")
        return None

    channel_id, picker_action = _selected_channel(body, _JOIN_CHANNEL_ACTIONS)

    error, picked_private = _validate_channel_selection(
        client,
        channel_id,
        picker_action,
        team_id=helpers.get_team_id_from_body(body) or workspace_record.team_id,
        acting_user_id=user_id,
        workspace_id=workspace_record.id,
        source_sync_id=int(metadata["sync_id"]),
    )
    if error:
        return error
    mismatch = _privacy_mismatch(int(metadata["sync_id"]), picked_private)
    if mismatch:
        return {"response_action": "errors", "errors": {picker_action: mismatch}}
    return None


def handle_join_sync_submit(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Join an available channel sync: create SyncChannel for this workspace."""
    auth_result = _get_authorized_workspace(body, client, context, "join_sync_submit")
    if not auth_result:
        return
    user_id, workspace_record = auth_result

    metadata = _parse_private_metadata(body)
    sync_id = metadata.get("sync_id")

    if not sync_id:
        log_warning("join_sync_submit", reason="missing sync_id")
        return

    channel_id, picker_action = _selected_channel(body, _JOIN_CHANNEL_ACTIONS)

    # The ack phase already surfaced any error; this keeps the work phase from
    # writing on a payload it should reject.
    error, picked_private = _validate_channel_selection(
        client,
        channel_id,
        picker_action,
        team_id=helpers.get_team_id_from_body(body) or workspace_record.team_id,
        acting_user_id=user_id,
        workspace_id=workspace_record.id,
        source_sync_id=int(sync_id),
    )
    if error or _privacy_mismatch(int(sync_id), picked_private):
        return

    sync_record = DbManager.get_record(schemas.Sync, id=sync_id)
    if not sync_record:
        return

    group_id = sync_record.group_id
    acting_user_id = user_id
    matches = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.sync_id == sync_id,
            schemas.SyncChannel.workspace_id == workspace_record.id,
            schemas.SyncChannel.channel_id == channel_id,
        ],
    )
    live = [row for row in matches if getattr(row, "id", None) and getattr(row, "deleted_at", None) is None]
    if live:
        log_info("join_sync_duplicate_skip", sync_id=sync_id, channel_id=channel_id, workspace_id=workspace_record.id)
        builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)
        if group_id:
            invalidate_group_member_homes(group_id, exclude_workspace_id=workspace_record.id)
        return
    rejoin = rejoin_sync_channel(group_id, workspace_record.id, channel_id) if group_id else None
    if rejoin and rejoin.sync_id == int(sync_id):
        revived_row = rejoin
    elif rejoin:
        revived_row = None
    else:
        deleted = [
            row
            for row in matches
            if getattr(row, "status", None) == "deleted" and getattr(row, "deleted_at", None) is not None
        ]
        deleted.sort(key=lambda row: row.deleted_at, reverse=True)
        revived_row = deleted[0] if deleted else None

    admin_name = helpers.format_admin_label(client, acting_user_id, workspace_record)

    team_id = helpers.get_team_id_from_body(body) or workspace_record.team_id

    reaction_style = _parse_reaction_fields(body)
    publishes, subscribes = _participation_from_body(body)
    if revived_row is None:
        channel_name = helpers.lookup_channel_name(
            channel_id,
            workspace_record,
            user_token=helpers.get_user_token(team_id, acting_user_id),
            client=client,
        )

        try:
            sync_channel_record = schemas.SyncChannel(
                sync_id=sync_id,
                channel_id=channel_id,
                channel_name=channel_name if channel_name and channel_name != channel_id else None,
                workspace_id=workspace_record.id,
                created_at=datetime.now(UTC),
                reaction_style=reaction_style,
                publishes=publishes,
                subscribes=subscribes,
            )
            DbManager.create_record(sync_channel_record)
        except Exception as e:
            log_error("failed_to_join_channel_sync", sync_id=sync_id, error=str(e))
            return

        helpers.invalidate_sync_fanout_for_syncs([sync_id])

        # Same ordering as publish: the row has to exist before Slack announces the
        # bot joined, or leave_channel_without_sync shows it the door.
        is_private = _ensure_membership_or_rollback(
            client,
            channel_id,
            team_id=team_id,
            acting_user_id=acting_user_id,
            rollback=lambda: helpers.purge_sync_channels([sync_channel_record]),
            log_event="join_sync_membership_failed",
            log_extra={"workspace_id": workspace_record.id, "channel_id": channel_id, "sync_id": sync_id},
            context=context,
        )
        if is_private is None:
            return

        set_is_private(sync_channel_record, is_private=is_private)
        if is_private:
            set_channel_private(sync_channel_record, acting_user_id)
    else:
        if (
            revive_sync_channel(
                revived_row,
                client=client,
                workspace=workspace_record,
                acting_user_id=acting_user_id,
                publishes=publishes,
                subscribes=subscribes,
                reaction_style=reaction_style,
                context=context,
                via="join",
            )
            is None
        ):
            return
        sync_channel_record = revived_row

    try:
        group_name = group_name_for_sync(sync_record)
        deliver_action_notice(
            workspace_record,
            sync_channel_record,
            action="Join",
            manager=admin_name,
            group_name=group_name,
            sync=sync_record,
        )

        log_info(
            "sync_joined", workspace_id=workspace_record.id, channel_id=channel_id, sync_id=sync_id, group_id=group_id
        )
    except Exception as e:
        log_error("failed_to_join_channel_sync", sync_id=sync_id, error=str(e))

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)
    if group_id:
        invalidate_group_member_homes(group_id, exclude_workspace_id=workspace_record.id)


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


def handle_edit_sync(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open the one-step Edit Sync modal for this channel's participation."""
    auth_result = _get_authorized_workspace(body, client, context, "edit_sync")
    if not auth_result:
        return
    user_id, workspace_record = auth_result

    kind, ref_id = _parse_edit_sync_ref(body)
    if not kind or not ref_id:
        log_warning("edit_sync", reason="invalid action value")
        return

    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    sync_channel: schemas.SyncChannel | None = None
    sync_record: schemas.Sync | None = None

    if kind == "channel":
        sync_channel = DbManager.get_record(schemas.SyncChannel, id=ref_id)
        if not sync_channel or sync_channel.deleted_at:
            return
        if sync_channel.workspace_id != workspace_record.id:
            log_warning("edit_sync", reason="channel not in acting workspace")
            return
        denied = _sync_refusal(sync_channel, workspace_record, user_id, client, action="edit it")
        if denied:
            _close_modal_done(client, body, denied)
            return
        sync_record = DbManager.get_record(schemas.Sync, id=sync_channel.sync_id)
    else:
        # Available cards no longer expose sync-wide policy editing.
        return

    if not sync_record:
        return

    blocks: list[orm.BaseBlock] = []
    metadata: dict = {"sync_id": sync_record.id}
    if sync_channel:
        metadata["sync_channel_id"] = sync_channel.id
        publishes = helpers.channel_publishes(sync_channel)
        subscribes = helpers.channel_subscribes(sync_channel)
        ctx = _relationship_context(
            group_id=sync_record.group_id,
            local_channel_id=sync_channel.channel_id,
            local_workspace=workspace_record,
        )
        if ctx:
            blocks.append(ctx)
        blocks.append(
            _participation_block(
                mode="edit",
                publishes=publishes,
                subscribes=subscribes,
            )
        )
        blocks.append(block_context(_participation_help(mode="edit")))
        style_initial = _reaction_style_for_edit(sync_channel) or constants.DEFAULT_REACTION_STYLE_NEW_RECEIVE
        blocks.append(
            _reaction_style_block(
                actions.CONFIG_SYNC_REACTION_STYLE,
                initial=style_initial,
            )
        )
        blocks.append(block_context(_reaction_style_help(first_time=False)))

    if not blocks:
        return

    orm.BlockView(blocks=blocks).post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_EDIT_SYNC_SUBMIT,
        title_text="Edit Sync",
        submit_button_text="Save",
        parent_metadata=metadata,
        body=body,
    )


def handle_edit_sync_submit_ack(
    body: dict,
    client: WebClient,
    context: dict,
) -> dict | None:
    """Ack phase for the participation-only Edit modal."""
    auth_result = _get_authorized_workspace(body, client, context, "edit_sync_submit_ack")
    if not auth_result:
        return None
    return None


def handle_edit_sync_submit(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Persist this channel's participation and reaction type."""
    auth_result = _get_authorized_workspace(body, client, context, "edit_sync_submit")
    if not auth_result:
        return
    user_id, workspace_record = auth_result

    metadata = _parse_private_metadata(body)
    sync_id = metadata.get("sync_id")
    sync_channel_id = metadata.get("sync_channel_id")
    if not sync_id:
        return

    sync_record = DbManager.get_record(schemas.Sync, id=int(sync_id))
    if not sync_record:
        return

    if sync_channel_id:
        sync_channel = DbManager.get_record(schemas.SyncChannel, id=int(sync_channel_id))
        if sync_channel and sync_channel.workspace_id == workspace_record.id:
            denied = _sync_refusal(sync_channel, workspace_record, user_id, client, action="edit it")
            if denied:
                helpers.notify_user_dm(client, user_id, denied)
                return
            from helpers.reaction import default_reaction_style_for_new_channel, update_sync_channel_reactions

            publishes, subscribes = _participation_from_body(body)
            style = _reaction_style_for_edit(sync_channel, body, subscribes=subscribes)
            if subscribes and style is None:
                style = default_reaction_style_for_new_channel(subscribes=True)
            update_sync_channel_reactions(
                int(sync_channel_id),
                style=style,
                publishes=publishes,
                subscribes=subscribes,
            )
            sync_channel.publishes = publishes
            sync_channel.subscribes = subscribes
            name = helpers.format_admin_label(client, user_id, workspace_record)
            group_name = group_name_for_sync(sync_record)
            deliver_action_notice(
                workspace_record,
                sync_channel,
                action="Edit",
                manager=name,
                group_name=group_name,
                sync=sync_record,
            )

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)
    if sync_record.group_id:
        invalidate_group_member_homes(sync_record.group_id, exclude_workspace_id=workspace_record.id)
