"""Per-channel reaction direction, style, and apply helpers."""

from __future__ import annotations

import logging
import uuid
from typing import Literal

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

import constants
from db import DbManager, schemas
from helpers.conversations import get_user_token
from helpers.core import safe_get
from helpers.encryption import decrypt_bot_token
from helpers.user_action_echo import reaction_echo_fingerprint, remember_user_action, slack_message_ts

_logger = logging.getLogger(__name__)

ApplyResult = Literal["direct", "thread", "skipped", "failed"]


def reaction_direction(sync_channel: schemas.SyncChannel | None) -> str:
    raw = (getattr(sync_channel, "reaction_direction", None) or constants.DEFAULT_REACTION_DIRECTION).strip()
    if raw in (
        constants.REACTION_DIRECTION_BOTH,
        constants.REACTION_DIRECTION_SEND,
        constants.REACTION_DIRECTION_RECEIVE,
        constants.REACTION_DIRECTION_OFF,
    ):
        return raw
    return constants.DEFAULT_REACTION_DIRECTION


def reaction_style(sync_channel: schemas.SyncChannel | None) -> str | None:
    raw = getattr(sync_channel, "reaction_style", None)
    if raw is None or str(raw).strip() == "":
        if channel_receives_reactions(sync_channel):
            return constants.DEFAULT_REACTION_STYLE_EXISTING
        return None
    return str(raw).strip()


def channel_sends_reactions(sync_channel: schemas.SyncChannel | None) -> bool:
    return reaction_direction(sync_channel) in (
        constants.REACTION_DIRECTION_BOTH,
        constants.REACTION_DIRECTION_SEND,
    )


def channel_receives_reactions(sync_channel: schemas.SyncChannel | None) -> bool:
    return reaction_direction(sync_channel) in (
        constants.REACTION_DIRECTION_BOTH,
        constants.REACTION_DIRECTION_RECEIVE,
    )


def should_sync_reaction_between(source: schemas.SyncChannel, target: schemas.SyncChannel) -> bool:
    return channel_sends_reactions(source) and channel_receives_reactions(target)


def default_reaction_style_for_new_channel(direction: str) -> str | None:
    if direction in (constants.REACTION_DIRECTION_BOTH, constants.REACTION_DIRECTION_RECEIVE):
        return constants.DEFAULT_REACTION_STYLE_NEW_RECEIVE
    return None


def direction_receives(direction: str) -> bool:
    return direction in (constants.REACTION_DIRECTION_BOTH, constants.REACTION_DIRECTION_RECEIVE)


def _mapped_user_for_target(
    source_user_id: str | None,
    source_workspace_id: int | None,
    target_workspace_id: int,
) -> str | None:
    from helpers.user_matching import get_mapped_target_user_id

    if not source_user_id or not source_workspace_id:
        return None
    return get_mapped_target_user_id(source_user_id, source_workspace_id, target_workspace_id)


def _post_threaded_reaction_notice(
    *,
    target_client: WebClient,
    sync_channel: schemas.SyncChannel,
    post_meta: schemas.PostMeta,
    reaction: str,
    display_name: str,
    icon_url: str | None,
    posted_from: str,
    author_is_mapped: bool,
) -> schemas.PostMeta | None:
    target_msg_ts = f"{post_meta.ts:.6f}"
    reaction_username_suffix = "" if author_is_mapped else posted_from
    permalink = None
    try:
        plink_resp = target_client.chat_getPermalink(
            channel=sync_channel.channel_id,
            message_ts=target_msg_ts,
        )
        permalink = safe_get(plink_resp, "permalink")
    except Exception as exc:
        _logger.debug(
            "reaction_permalink_lookup_failed",
            extra={"channel_id": sync_channel.channel_id, "message_ts": target_msg_ts, "error": str(exc)},
        )

    if permalink:
        msg_text = f"reacted with :{reaction}: to <{permalink}|this message>"
    else:
        msg_text = f"reacted with :{reaction}:"

    resp = target_client.chat_postMessage(
        channel=sync_channel.channel_id,
        text=msg_text,
        username=f"{display_name} {reaction_username_suffix}".strip(),
        icon_url=icon_url,
        thread_ts=target_msg_ts,
        unfurl_links=False,
        unfurl_media=False,
    )
    ts = safe_get(resp, "ts")
    if not ts:
        return None
    return schemas.PostMeta(post_id=uuid.uuid4().hex, sync_channel_id=sync_channel.id, ts=float(ts))


_NO_AUTHORIZE_ERRORS = frozenset({"invalid_auth", "not_authed", "token_revoked", "missing_scope", "account_inactive"})
_IDEMPOTENT_ADD_ERRORS = frozenset({"already_reacted", "already_added"})


def _slack_error_code(exc: SlackApiError) -> str:
    response = exc.response
    if response is None:
        return ""
    if isinstance(response, dict):
        err = response.get("error")
        return str(err) if err else ""
    data = getattr(response, "data", None)
    if isinstance(data, dict):
        err = data.get("error")
        return str(err) if err else ""
    return ""


def _dest_reaction_name_is_invalid(
    bot_client: WebClient,
    *,
    team_id: str | None,
    channel_id: str,
    target_ts: str,
    reaction: str,
    cache: dict[tuple[str, str], bool] | None = None,
) -> bool | None:
    """Whether dest Slack rejects this emoji name.

    Returns ``True`` for ``invalid_name``, ``False`` when the name exists, and
    ``None`` when the probe did not settle it. Called only on the Hybrid thread
    path (no dest user token, or that token hit an auth error). ``invalid_name``
    is per workspace; pass *cache* so one event does not probe the same dest
    name twice.
    """
    cache_key = (str(team_id or ""), reaction)
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    result: bool | None
    try:
        bot_client.reactions_add(channel=channel_id, timestamp=target_ts, name=reaction)
    except SlackApiError as exc:
        error_code = _slack_error_code(exc)
        if error_code == "invalid_name":
            result = True
        elif error_code in _IDEMPOTENT_ADD_ERRORS:
            result = False
        else:
            _logger.debug(
                "reaction_name_probe_failed",
                extra={"channel_id": channel_id, "error": error_code or str(exc)},
            )
            result = None
        if cache is not None and result is not None:
            cache[cache_key] = result
        return result
    try:
        bot_client.reactions_remove(channel=channel_id, timestamp=target_ts, name=reaction)
    except SlackApiError as exc:
        _logger.debug(
            "reaction_name_probe_remove_failed",
            extra={"channel_id": channel_id, "error": _slack_error_code(exc) or str(exc)},
        )
    if cache is not None:
        cache[cache_key] = False
    return False


def _remember_native_reaction(
    *,
    team_id: str | None,
    user_id: str | None,
    action: str,
    channel_id: str,
    target_ts: str,
    reaction: str,
) -> None:
    if not team_id or not user_id:
        return
    kind = "reaction_added" if action == "add" else "reaction_removed"
    remember_user_action(
        team_id,
        user_id,
        kind,
        reaction_echo_fingerprint(channel_id, target_ts, reaction),
    )


def apply_reaction_to_target(
    *,
    action: str,
    reaction: str,
    source_user_id: str | None,
    source_workspace_id: int | None,
    source_sync_channel: schemas.SyncChannel,
    target_post_meta: schemas.PostMeta,
    target_sync_channel: schemas.SyncChannel,
    target_workspace: schemas.Workspace,
    display_name: str,
    icon_url: str | None,
    posted_from: str,
    author_is_mapped: bool,
    mapped_user_id: str | None = None,
    name_probe_cache: dict[tuple[str, str], bool] | None = None,
) -> tuple[ApplyResult, schemas.PostMeta | None]:
    """Apply a reaction add/remove on a target channel. Never writes to the origin.

    User-token add/remove runs first when dest has an ``xoxp``. Bot name probe
    (`_dest_reaction_name_is_invalid`) runs only on the Hybrid thread path: no
    dest token, or that token hit ``_NO_AUTHORIZE_ERRORS``. Direct-only never
    probes. ``invalid_name`` always skips; it never becomes a thread notice.
    """
    if not should_sync_reaction_between(source_sync_channel, target_sync_channel):
        return "skipped", None

    style = reaction_style(target_sync_channel)
    target_ts = slack_message_ts(target_post_meta.ts)
    channel_id = target_sync_channel.channel_id

    resolved_user = mapped_user_id or _mapped_user_for_target(
        source_user_id,
        source_workspace_id,
        target_workspace.id,
    )
    user_token = get_user_token(target_workspace.team_id, resolved_user)

    if user_token and style in (constants.REACTION_STYLE_DIRECT_ONLY, constants.REACTION_STYLE_THREADED_AND_DIRECT):
        user_client = WebClient(token=user_token)
        try:
            if action == "add":
                user_client.reactions_add(channel=channel_id, timestamp=target_ts, name=reaction)
            else:
                user_client.reactions_remove(channel=channel_id, timestamp=target_ts, name=reaction)
            _remember_native_reaction(
                team_id=target_workspace.team_id,
                user_id=resolved_user,
                action=action,
                channel_id=channel_id,
                target_ts=target_ts,
                reaction=reaction,
            )
            return "direct", None
        except SlackApiError as exc:
            error_code = _slack_error_code(exc)
            if action == "remove":
                return "skipped", None
            if error_code in _IDEMPOTENT_ADD_ERRORS:
                return "direct", None
            if error_code == "invalid_name":
                return "skipped", None
            if error_code in _NO_AUTHORIZE_ERRORS:
                user_token = None
            else:
                _logger.warning(
                    "reaction_direct_failed",
                    extra={"channel_id": channel_id, "error": error_code or str(exc)},
                )
                return "failed", None

    if action != "add":
        return "skipped", None

    if style != constants.REACTION_STYLE_THREADED_AND_DIRECT:
        return "skipped", None

    bot_client = WebClient(token=decrypt_bot_token(target_workspace.bot_token))
    name_invalid = _dest_reaction_name_is_invalid(
        bot_client,
        team_id=getattr(target_workspace, "team_id", None),
        channel_id=channel_id,
        target_ts=target_ts,
        reaction=reaction,
        cache=name_probe_cache,
    )
    if name_invalid is not False:
        return "skipped", None

    notice = _post_threaded_reaction_notice(
        target_client=bot_client,
        sync_channel=target_sync_channel,
        post_meta=target_post_meta,
        reaction=reaction,
        display_name=display_name,
        icon_url=icon_url,
        posted_from=posted_from,
        author_is_mapped=author_is_mapped,
    )
    return ("thread", notice) if notice else ("failed", None)


def find_source_sync_channel(
    records: list[tuple[schemas.PostMeta, schemas.SyncChannel, schemas.Workspace]],
    event_channel_id: str,
) -> schemas.SyncChannel | None:
    for _pm, sync_channel, _ws in records:
        if sync_channel.channel_id == event_channel_id:
            return sync_channel
    return None


def update_sync_channel_reactions(
    sync_channel_id: int,
    *,
    direction: str,
    style: str | None,
) -> None:
    DbManager.update_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.id == sync_channel_id],
        {
            schemas.SyncChannel.reaction_direction: direction,
            schemas.SyncChannel.reaction_style: style,
        },
    )
