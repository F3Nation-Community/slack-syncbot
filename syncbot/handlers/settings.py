"""Operator instance-settings modal.

Visible only to ``PRIMARY_WORKSPACE``. Holds operational policy that changes
over a deployment's life; secrets, connection details, and the ``ENABLE_DB_RESET``
break-glass switch stay in environment variables.

Values are seeded from the effective configuration, so the operator sees what is
actually live rather than an empty form: each field shows the database value if
one has been saved, otherwise the environment variable, otherwise the default.
"""

import logging
from logging import Logger

from slack_sdk.web import WebClient

import builders
import constants
import helpers
from db import DbManager, schemas
from slack import actions, orm

_logger = logging.getLogger(__name__)

_BOOL_YES = "true"
_BOOL_NO = "false"


def _team_id_from_body(body: dict) -> str | None:
    """Resolve the acting team from either a block action or a view submission."""
    return (
        helpers.safe_get(body, "view", "team_id")
        or helpers.safe_get(body, "team", "id")
        or helpers.safe_get(body, "team_id")
        or helpers.safe_get(body, "user", "team_id")
    )


def _installed_workspace_options() -> list[orm.SelectorOption]:
    """Every installed workspace, as options keyed by Slack team id."""
    workspaces = DbManager.find_records(schemas.Workspace, [schemas.Workspace.deleted_at.is_(None)])
    options = []
    for workspace in workspaces:
        if not workspace.team_id or not workspace.bot_token:
            continue
        options.append(
            orm.SelectorOption(
                name=helpers.resolve_workspace_name(workspace) or workspace.team_id,
                value=workspace.team_id,
            )
        )
    return sorted(options, key=lambda option: option.name.lower())


def _build_settings_form() -> orm.BlockView:
    """Build the settings modal, seeded from the effective configuration."""
    workspace_options = _installed_workspace_options()

    blocks = [
        orm.InputBlock(
            label="Allow private Channels to be synced",
            action=actions.CONFIG_SETTINGS_ALLOW_PRIVATE_CHANNELS,
            element=orm.RadioButtonsElement(
                initial_value=_BOOL_YES if helpers.allow_private_channels() else _BOOL_NO,
                options=[
                    orm.SelectorOption(name="No — public Channels only (recommended)", value=_BOOL_NO),
                    orm.SelectorOption(name="Yes — allow private Channels", value=_BOOL_YES),
                ],
            ),
            optional=False,
        ),
        orm.ContextBlock(
            element=orm.ContextElement(
                initial_value=(
                    "When this is on, an Admin can publish a private Channel, and its messages will "
                    "then be copied into other Workspaces. Anyone who can see the synced Channel in "
                    "those Workspaces will be able to read that content, so the Channel is no longer "
                    "really private. SyncBot cannot add itself to a private Channel, so invite it "
                    "there first. Broadcasts always require a public Channel regardless of this setting."
                ),
            ),
        ),
        orm.InputBlock(
            label="Workspaces allowed to publish a Broadcast",
            action=actions.CONFIG_SETTINGS_BROADCAST_WORKSPACES,
            element=orm.MultiStaticSelectElement(
                placeholder="Leave empty to allow any Workspace",
                initial_values=helpers.broadcast_allowed_workspaces(),
                options=workspace_options,
            ),
            optional=True,
        ),
        orm.ContextBlock(
            element=orm.ContextElement(
                initial_value=(
                    "Leave this empty and any installed Workspace may publish a Broadcast, which is "
                    "the default. Select one or more Workspaces to restrict it to just those."
                ),
            ),
        ),
        orm.InputBlock(
            label="Days to retain a removed Workspace",
            action=actions.CONFIG_SETTINGS_RETENTION_DAYS,
            element=orm.NumberInputElement(
                initial_value=helpers.soft_delete_retention_days(),
                min_value=1,
                max_value=3650,
                is_decimal_allowed=False,
            ),
            optional=False,
        ),
        orm.ContextBlock(
            element=orm.ContextElement(
                initial_value=(
                    "When a Workspace uninstalls SyncBot, its data is kept for this many days so a "
                    "reinstall picks up where it left off. After that it is permanently deleted."
                ),
            ),
        ),
    ]
    return orm.BlockView(blocks=blocks)


def handle_open_settings(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open the instance settings modal for the primary workspace."""
    user_id = helpers.get_user_id_from_body(body)
    if not user_id or not helpers.is_user_authorized(client, user_id):
        _logger.warning("authorization_denied", extra={"user_id": user_id, "action": "open_settings"})
        return

    team_id = _team_id_from_body(body)
    if not helpers.is_settings_visible_for_workspace(team_id):
        _logger.warning("authorization_denied", extra={"action": "open_settings", "team_id": team_id})
        return

    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    _build_settings_form().post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_SETTINGS_SUBMIT,
        title_text="SyncBot Settings",
        submit_button_text="Save",
        close_button_text="Cancel",
    )


def handle_settings_submit(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Persist the settings modal.

    Re-checks the primary-workspace gate: ``main_response`` has no authorization
    gate of its own, so the button-visibility check alone is bypassable with a
    forged view submission.
    """
    user_id = helpers.get_user_id_from_body(body)
    if not user_id or not helpers.is_user_authorized(client, user_id):
        _logger.warning("authorization_denied", extra={"user_id": user_id, "action": "settings_submit"})
        return

    team_id = _team_id_from_body(body)
    if not helpers.is_settings_visible_for_workspace(team_id):
        _logger.warning("authorization_denied", extra={"action": "settings_submit", "team_id": team_id})
        return

    values = helpers.safe_get(body, "view", "state", "values") or {}
    selected = _build_settings_form().get_selected_values(body)

    allow_private = selected.get(actions.CONFIG_SETTINGS_ALLOW_PRIVATE_CHANNELS)
    if allow_private in (_BOOL_YES, _BOOL_NO):
        helpers.set_setting(constants.SETTING_ALLOW_PRIVATE_CHANNELS, allow_private)

    # An empty multi-select is a meaningful value here ("any Workspace may
    # publish"), so it is written rather than skipped.
    broadcast = selected.get(actions.CONFIG_SETTINGS_BROADCAST_WORKSPACES)
    if actions.CONFIG_SETTINGS_BROADCAST_WORKSPACES in values:
        helpers.set_setting(
            constants.SETTING_BROADCAST_ALLOWED_WORKSPACES,
            ",".join(broadcast) if broadcast else "",
        )

    retention = selected.get(actions.CONFIG_SETTINGS_RETENTION_DAYS)
    if retention:
        try:
            days = int(float(retention))
        except (TypeError, ValueError):
            _logger.warning("settings_retention_unparseable")
        else:
            if days >= 1:
                helpers.set_setting(constants.SETTING_SOFT_DELETE_RETENTION_DAYS, str(days))

    _logger.info("instance_settings_updated", extra={"team_id": team_id})

    workspace_record = helpers.get_workspace_record(team_id, body, context, client)
    if workspace_record:
        builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context)
