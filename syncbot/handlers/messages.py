"""Message sync handlers — new posts, replies, edits, deletes, reactions."""

import logging
import re
import uuid
from logging import Logger

from slack_sdk.web import WebClient

import constants
import federation
import helpers
from db import DbManager, schemas
from db.event_claims import run_claimed
from handlers._common import EventContext
from logger import emit_metric
from slack import orm


def _find_source_workspace_id(records: list[tuple], channel_id: str, ws_index: int = 1) -> int | None:
    """Return the workspace ID from the record whose channel matches *channel_id*."""
    for record in records:
        sync_channel = record[ws_index - 1] if ws_index > 1 else record[0]
        workspace = record[ws_index]
        if sync_channel.channel_id == channel_id:
            return workspace.id
    return None


_logger = logging.getLogger(__name__)


def _event_is_bot_post(event: dict) -> bool:
    """True for ``bot_message`` and for ``message_changed`` of a bot post."""
    if event.get("subtype") == "bot_message" or event.get("bot_id"):
        return True
    nested = event.get("message") if isinstance(event.get("message"), dict) else {}
    return bool(nested.get("bot_id") or nested.get("subtype") == "bot_message")


def _parse_event_fields(body: dict, client: WebClient) -> EventContext:
    """Extract the common fields every message handler needs."""
    event: dict = body.get("event", {})
    layout_blocks = helpers.content_blocks_for_sync(helpers.event_layout_blocks(event))
    if not layout_blocks and _event_is_bot_post(event):
        layout_blocks = helpers.content_blocks_for_sync(helpers.fetch_message_layout_blocks(client, event))
    event_text = helpers.safe_get(event, "text") or helpers.safe_get(event, "message", "text")
    msg_text = helpers.choose_message_text(event_text, layout_blocks)
    mentioned_users = helpers.parse_mentioned_users(msg_text, client)
    extra_ids = [
        uid
        for uid in helpers.collect_user_ids_from_blocks(layout_blocks)
        if uid not in {u.get("user_id") for u in mentioned_users}
    ]
    if extra_ids:
        mentioned_users.extend(helpers.parse_mentioned_users("".join(f"<@{uid}>" for uid in extra_ids), client))

    return EventContext(
        team_id=helpers.safe_get(body, "team_id"),
        channel_id=helpers.safe_get(event, "channel"),
        user_id=(helpers.safe_get(event, "user") or helpers.safe_get(event, "message", "user")),
        msg_text=msg_text,
        mentioned_users=mentioned_users,
        thread_ts=helpers.safe_get(event, "thread_ts"),
        ts=(
            helpers.safe_get(event, "message", "ts")
            or helpers.safe_get(event, "previous_message", "ts")
            or helpers.safe_get(event, "ts")
        ),
        event_subtype=helpers.safe_get(event, "subtype"),
        reply_broadcast=helpers.safe_get(event, "subtype") in ("thread_broadcast", "reply_broadcast"),
        content_blocks=layout_blocks,
    )


def _dest_layout_blocks(
    *,
    ctx: EventContext,
    photo_blocks: list[dict],
    client: WebClient,
    target_client: WebClient,
    source_workspace_id: int,
    target_workspace_id: int,
    source_ws,
    source_workspace_name: str | None,
) -> list[dict]:
    """Content blocks rewritten for dest, plus GIF image blocks."""
    content = ctx.get("content_blocks") or []
    if not content:
        return photo_blocks or []

    def rewrite_mrkdwn(text: str) -> str:
        adapted = helpers.resolve_channel_references(text, client, source_ws, target_workspace_id=target_workspace_id)

        def repl(match: re.Match) -> str:
            return helpers.resolve_mention_for_workspace(
                client,
                match.group(1),
                source_workspace_id,
                target_client,
                target_workspace_id,
            )

        return re.sub(r"<@(\w+)>", repl, adapted or "")

    def map_user_id(uid: str) -> str | None:
        return helpers.get_mapped_target_user_id(uid, source_workspace_id, target_workspace_id)

    names = {u.get("user_id"): u.get("user_name") for u in ctx.get("mentioned_users") or []}

    def unmapped_label(uid: str) -> str:
        return helpers.unmapped_author_label(names.get(uid) or uid, source_workspace_name)

    rewritten = helpers.rewrite_content_blocks(content, rewrite_mrkdwn, map_user_id, unmapped_label)
    return rewritten + (photo_blocks or [])


def _build_file_context(body: dict, client: WebClient, logger: Logger) -> tuple[list[dict], list[dict]]:
    """Process files attached to a message event.

    Returns ``(photo_blocks, direct_files)`` where:

    * *photo_blocks* — Slack Block Kit ``image`` blocks for inline images
      (e.g. GIF picker URLs), ready for ``chat.postMessage``.
    * *direct_files* — files downloaded to ``/tmp`` for direct upload to
      each target channel via ``files_upload_v2``.
    """
    event = body.get("event", {})
    files = (helpers.safe_get(event, "files") or helpers.safe_get(event, "message", "files") or [])[:20]
    event_subtype = helpers.safe_get(event, "subtype")

    photo_blocks: list[dict] = []
    direct_files: list[dict] = []
    is_edit = event_subtype in ("message_changed", "message_deleted")

    if not is_edit:
        direct_files = helpers.download_slack_files(files, client, logger)

    # Public GIF/image URLs (GIPHY, Slack GIF picker). Include edits so federation
    # thread/edit payloads can carry the same image blocks as new posts.
    if not files:
        attachments = event.get("attachments") or helpers.safe_get(event, "message", "attachments") or []
        for att in attachments:
            img_url = att.get("image_url") or att.get("thumb_url")

            # Slack's built-in GIF picker nests the image inside blocks
            if not img_url:
                for blk in att.get("blocks") or []:
                    if blk.get("type") == "image" and blk.get("image_url"):
                        img_url = blk["image_url"]
                        break

            # Also check top-level event blocks for image blocks
            if not img_url:
                for blk in event.get("blocks") or []:
                    if blk.get("type") == "image" and blk.get("image_url"):
                        img_url = blk["image_url"]
                        break

            if not img_url:
                _logger.info(
                    "attachment_no_image_url", extra={"att_keys": list(att.keys()), "fallback": att.get("fallback")}
                )
                continue

            name = att.get("fallback") or "attachment.gif"
            photo_blocks.append(orm.ImageBlock(image_url=img_url, alt_text=name).as_form_field())

    return photo_blocks, direct_files


def _get_workspace_name(records: list, channel_id: str, workspace_index: int) -> str | None:
    """Pull the workspace name for the originating channel from a record list."""
    return helpers.safe_get(
        [r[workspace_index].workspace_name for r in records if r[workspace_index - 1].channel_id == channel_id],
        0,
    )


def _image_payloads_from_blocks(photo_blocks: list[dict] | None) -> list[dict]:
    """Public image blocks as federation JSON (GIF picker / GIPHY URLs)."""
    payloads: list[dict] = []
    for block in photo_blocks or []:
        if block.get("type") == "image":
            payloads.append(
                {
                    "url": block.get("image_url", ""),
                    "alt_text": block.get("alt_text", "Shared image"),
                }
            )
    return payloads


def _leave_unconfigured_channel(client: WebClient, channel_id: str, user_id: str | None, logger: Logger) -> None:
    """Tell the channel SyncBot is leaving, then leave."""
    if not user_id:
        return
    try:
        client.chat_postMessage(
            channel=channel_id,
            text=":wave: Hello! I'm SyncBot. I was added to this Channel, but this Channel "
            "doesn't seem to be part of a Channel Sync. I'm leaving now. Please open the SyncBot Home "
            "tab to Publish or Subscribe.",
        )
        client.conversations_leave(channel=channel_id)
    except Exception as e:
        logger.error(f"Failed to notify and leave unconfigured channel {channel_id}: {e}")


def _same_instance_dest_post(
    *,
    body: dict,
    client: WebClient,
    ctx: EventContext,
    photo_blocks: list[dict],
    direct_files: list[dict] | None,
    sync_channel: schemas.SyncChannel,
    workspace: schemas.Workspace,
    source_workspace_id: int | None,
    user_name: str | None,
    user_profile_url: str | None,
    workspace_name: str | None,
    thread_ts: str | None = None,
) -> tuple[str | None, str | None]:
    """Post to a same-instance dest channel. Author map runs before mention rewrite.

    Returns ``(message_ts, split_file_ts)``.
    """
    msg_text = ctx["msg_text"]
    mentioned_users = ctx["mentioned_users"]
    user_id = ctx["user_id"]
    reply_broadcast = ctx.get("reply_broadcast") or False

    bot_token = helpers.decrypt_bot_token(workspace.bot_token)
    target_client = WebClient(token=bot_token)
    target_display_name, target_icon_url, author_is_mapped, _ = helpers.get_display_name_and_icon_for_synced_message(
        user_id or "",
        source_workspace_id or 0,
        user_name,
        user_profile_url,
        target_client,
        workspace.id,
        source_client=client,
    )
    name_for_target = target_display_name or user_name or "Someone"
    remote_workspace_label = None if author_is_mapped else workspace_name
    file_notice = helpers.format_file_share_notice(name_for_target, remote_workspace_label)

    adapted_text = helpers.apply_mentioned_users(
        msg_text,
        client,
        target_client,
        mentioned_users,
        source_workspace_id=source_workspace_id or 0,
        target_workspace_id=workspace.id,
    )
    source_ws = helpers.get_workspace_by_id(source_workspace_id) if source_workspace_id else None
    adapted_text = helpers.resolve_channel_references(adapted_text, client, source_ws, target_workspace_id=workspace.id)
    dest_blocks = _dest_layout_blocks(
        ctx=ctx,
        photo_blocks=photo_blocks,
        client=client,
        target_client=target_client,
        source_workspace_id=source_workspace_id or 0,
        target_workspace_id=workspace.id,
        source_ws=source_ws,
        source_workspace_name=workspace_name,
    )

    split_file_ts: str | None = None
    files = direct_files or []

    if files and not msg_text.strip():
        _, file_ts = helpers.upload_files_to_slack(
            bot_token=bot_token,
            channel_id=sync_channel.channel_id,
            files=files,
            initial_comment=file_notice,
            thread_ts=thread_ts,
            reply_broadcast=reply_broadcast,
        )
        ts = file_ts or helpers.safe_get(body, "event", "ts")
        return ts, None

    res = helpers.post_message(
        bot_token=bot_token,
        channel_id=sync_channel.channel_id,
        msg_text=adapted_text,
        user_name=name_for_target,
        user_profile_url=target_icon_url or user_profile_url,
        workspace_name=remote_workspace_label,
        blocks=dest_blocks,
        thread_ts=thread_ts,
        reply_broadcast=reply_broadcast,
    )
    ts = helpers.safe_get(res, "ts") or helpers.safe_get(body, "event", "ts")

    if files:
        # Nest under the text post when top-level; also send that share to the channel.
        file_thread_ts = thread_ts or ts
        file_broadcast = reply_broadcast or thread_ts is None
        _, split_file_ts = helpers.upload_files_to_slack(
            bot_token=bot_token,
            channel_id=sync_channel.channel_id,
            files=files,
            initial_comment=file_notice,
            thread_ts=file_thread_ts,
            reply_broadcast=file_broadcast,
        )
    return ts, split_file_ts


def _handle_new_post(
    body: dict,
    client: WebClient,
    logger: Logger,
    ctx: EventContext,
    photo_blocks: list[dict],
    direct_files: list[dict] | None = None,
) -> None:
    """Sync a brand-new top-level message to all linked channels."""
    team_id = ctx["team_id"]
    channel_id = ctx["channel_id"]
    msg_text = ctx["msg_text"]
    user_id = ctx["user_id"]

    sync_records = helpers.get_sync_list(team_id, channel_id)
    if not sync_records:
        any_sync_channel = DbManager.find_records(
            schemas.SyncChannel,
            [
                schemas.SyncChannel.channel_id == channel_id,
                schemas.SyncChannel.deleted_at.is_(None),
            ],
        )
        if any_sync_channel:
            return
        _leave_unconfigured_channel(client, channel_id, user_id, logger)
        return

    if user_id:
        user_name, user_profile_url = helpers.get_user_info(client, user_id)
    else:
        user_name, user_profile_url = helpers.get_bot_info_from_event(body)

    workspace_name = _get_workspace_name(sync_records, channel_id, workspace_index=1)

    post_uuid = uuid.uuid4().hex
    post_list: list[schemas.PostMeta] = []
    channels_synced = 0

    source_workspace_id = _find_source_workspace_id(sync_records, channel_id)

    fed_ws = None
    if sync_records:
        fed_ws = helpers.get_federated_workspace_for_sync(sync_records[0][0].sync_id)

    source_ws_fed = helpers.get_workspace_by_id(source_workspace_id) if source_workspace_id else None
    fed_adapted_text = helpers.resolve_channel_references(msg_text, client, source_ws_fed)

    for sync_channel, workspace in sync_records:
        try:
            split_file_ts: str | None = None
            if sync_channel.channel_id == channel_id:
                ts = helpers.safe_get(body, "event", "ts")
            elif fed_ws and workspace.id != source_workspace_id:
                payload = federation.build_message_payload(
                    sync_id=sync_channel.sync_id,
                    post_id=post_uuid,
                    channel_id=sync_channel.channel_id,
                    user_name=user_name,
                    user_avatar_url=user_profile_url,
                    workspace_name=workspace_name,
                    text=fed_adapted_text,
                    images=_image_payloads_from_blocks(photo_blocks),
                    timestamp=helpers.safe_get(body, "event", "ts"),
                    user_id=user_id,
                    reply_broadcast=ctx.get("reply_broadcast") or False,
                )
                result = federation.push_message(fed_ws, payload)
                ts = helpers.safe_get(result, "ts") if result else helpers.safe_get(body, "event", "ts")
                if not ts:
                    ts = helpers.safe_get(body, "event", "ts")
            else:
                ts, split_file_ts = _same_instance_dest_post(
                    body=body,
                    client=client,
                    ctx=ctx,
                    photo_blocks=photo_blocks,
                    direct_files=direct_files,
                    sync_channel=sync_channel,
                    workspace=workspace,
                    source_workspace_id=source_workspace_id,
                    user_name=user_name,
                    user_profile_url=user_profile_url,
                    workspace_name=workspace_name,
                )

            if ts:
                post_list.append(schemas.PostMeta(post_id=post_uuid, sync_channel_id=sync_channel.id, ts=float(ts)))
            if split_file_ts:
                post_list.append(
                    schemas.PostMeta(post_id=post_uuid, sync_channel_id=sync_channel.id, ts=float(split_file_ts))
                )
            if ts or split_file_ts:
                channels_synced += 1
        except Exception as exc:
            _logger.warning(f"Failed to sync new post to channel {sync_channel.channel_id}: {exc}")

    synced = channels_synced
    failed = len(sync_records) - synced
    emit_metric("messages_synced", value=synced, sync_type="new_post")
    if failed:
        emit_metric("sync_failures", value=failed, sync_type="new_post")

    helpers.cleanup_temp_files(None, direct_files)

    if post_list:
        DbManager.create_records(post_list)


def _handle_thread_reply(
    body: dict,
    client: WebClient,
    logger: Logger,
    ctx: EventContext,
    photo_blocks: list[dict],
    direct_files: list[dict] | None = None,
) -> None:
    """Sync a threaded reply to all linked channels."""
    channel_id = ctx["channel_id"]
    msg_text = ctx["msg_text"]
    user_id = ctx["user_id"]
    thread_ts = ctx["thread_ts"]

    post_records = helpers.get_post_records(thread_ts)
    if not post_records:
        return

    workspace_name = _get_workspace_name(post_records, channel_id, workspace_index=2)

    if user_id:
        user_name, user_profile_url = helpers.get_user_info(client, user_id)
    else:
        user_name, user_profile_url = helpers.get_bot_info_from_event(body)

    post_uuid = uuid.uuid4().hex
    post_list: list[schemas.PostMeta] = []
    channels_synced = 0

    source_workspace_id = _find_source_workspace_id(post_records, channel_id, ws_index=2)

    fed_ws = None
    if post_records:
        fed_ws = helpers.get_federated_workspace_for_sync(post_records[0][1].sync_id)

    thread_post_id = post_records[0][0].post_id if post_records else None

    source_ws_fed = helpers.get_workspace_by_id(source_workspace_id) if source_workspace_id else None
    fed_adapted_text = helpers.resolve_channel_references(msg_text, client, source_ws_fed)

    for post_meta, sync_channel, workspace in post_records:
        try:
            split_file_ts: str | None = None
            if sync_channel.channel_id == channel_id:
                ts = helpers.safe_get(body, "event", "ts")
            elif fed_ws and workspace.id != source_workspace_id:
                payload = federation.build_message_payload(
                    sync_id=sync_channel.sync_id,
                    post_id=post_uuid,
                    channel_id=sync_channel.channel_id,
                    user_name=user_name,
                    user_avatar_url=user_profile_url,
                    workspace_name=workspace_name,
                    text=fed_adapted_text,
                    thread_post_id=str(thread_post_id) if thread_post_id else None,
                    images=_image_payloads_from_blocks(photo_blocks),
                    timestamp=helpers.safe_get(body, "event", "ts"),
                    user_id=user_id,
                    reply_broadcast=ctx.get("reply_broadcast") or False,
                )
                result = federation.push_message(fed_ws, payload)
                ts = helpers.safe_get(result, "ts") if result else helpers.safe_get(body, "event", "ts")
                if not ts:
                    ts = helpers.safe_get(body, "event", "ts")
            else:
                parent_ts = f"{post_meta.ts:.6f}"
                ts, split_file_ts = _same_instance_dest_post(
                    body=body,
                    client=client,
                    ctx=ctx,
                    photo_blocks=photo_blocks,
                    direct_files=direct_files,
                    sync_channel=sync_channel,
                    workspace=workspace,
                    source_workspace_id=source_workspace_id,
                    user_name=user_name,
                    user_profile_url=user_profile_url,
                    workspace_name=workspace_name,
                    thread_ts=parent_ts,
                )

            if ts:
                post_list.append(schemas.PostMeta(post_id=post_uuid, sync_channel_id=sync_channel.id, ts=float(ts)))
            if split_file_ts:
                post_list.append(
                    schemas.PostMeta(post_id=post_uuid, sync_channel_id=sync_channel.id, ts=float(split_file_ts))
                )
            if ts or split_file_ts:
                channels_synced += 1
        except Exception as exc:
            _logger.warning(f"Failed to sync thread reply to channel {sync_channel.channel_id}: {exc}")

    synced = channels_synced
    failed = len(post_records) - synced
    emit_metric("messages_synced", value=synced, sync_type="thread_reply")
    if failed:
        emit_metric("sync_failures", value=failed, sync_type="thread_reply")

    helpers.cleanup_temp_files(None, direct_files)

    if post_list:
        DbManager.create_records(post_list)


def _handle_message_edit(
    client: WebClient,
    logger: Logger,
    ctx: EventContext,
    photo_blocks: list[dict],
) -> None:
    """Propagate an edited message to all linked channels."""
    channel_id = ctx["channel_id"]
    msg_text = ctx["msg_text"]
    mentioned_users = ctx["mentioned_users"]
    ts = ctx["ts"]

    post_records = helpers.get_post_records(ts)
    if not post_records:
        return

    workspace_name = _get_workspace_name(post_records, channel_id, workspace_index=2)

    source_workspace_id = _find_source_workspace_id(post_records, channel_id, ws_index=2)

    fed_ws = None
    if post_records:
        fed_ws = helpers.get_federated_workspace_for_sync(post_records[0][1].sync_id)

    source_ws_fed = helpers.get_workspace_by_id(source_workspace_id) if source_workspace_id else None
    fed_adapted_text = helpers.resolve_channel_references(msg_text, client, source_ws_fed)

    synced = 0
    failed = 0
    for post_meta, sync_channel, workspace in post_records:
        if sync_channel.channel_id == channel_id:
            continue
        try:
            if fed_ws and workspace.id != source_workspace_id:
                payload = federation.build_edit_payload(
                    post_id=post_meta.post_id.hex() if isinstance(post_meta.post_id, bytes) else str(post_meta.post_id),
                    channel_id=sync_channel.channel_id,
                    text=fed_adapted_text,
                    timestamp=f"{post_meta.ts:.6f}",
                    images=_image_payloads_from_blocks(photo_blocks),
                )
                federation.push_edit(fed_ws, payload)
            else:
                bot_token = helpers.decrypt_bot_token(workspace.bot_token)
                target_client = WebClient(token=bot_token)
                adapted_text = helpers.apply_mentioned_users(
                    msg_text,
                    client,
                    target_client,
                    mentioned_users,
                    source_workspace_id=source_workspace_id or 0,
                    target_workspace_id=workspace.id,
                )
                source_ws = helpers.get_workspace_by_id(source_workspace_id) if source_workspace_id else None
                adapted_text = helpers.resolve_channel_references(
                    adapted_text, client, source_ws, target_workspace_id=workspace.id
                )
                dest_blocks = _dest_layout_blocks(
                    ctx=ctx,
                    photo_blocks=photo_blocks,
                    client=client,
                    target_client=target_client,
                    source_workspace_id=source_workspace_id or 0,
                    target_workspace_id=workspace.id,
                    source_ws=source_ws,
                    source_workspace_name=workspace_name,
                )
                helpers.post_message(
                    bot_token=bot_token,
                    channel_id=sync_channel.channel_id,
                    msg_text=adapted_text,
                    update_ts=f"{post_meta.ts:.6f}",
                    workspace_name=workspace_name,
                    blocks=dest_blocks,
                )
            synced += 1
        except Exception as exc:
            failed += 1
            _logger.warning(f"Failed to sync message edit to channel {sync_channel.channel_id}: {exc}")

    emit_metric("messages_synced", value=synced, sync_type="message_edit")
    if failed:
        emit_metric("sync_failures", value=failed, sync_type="message_edit")


def _handle_message_delete(
    ctx: EventContext,
    logger: Logger,
) -> None:
    """Propagate a deleted message to all linked channels."""
    channel_id = ctx["channel_id"]
    ts = ctx["ts"]

    post_records = helpers.get_post_records(ts)
    if not post_records:
        return

    fed_ws = None
    if post_records:
        fed_ws = helpers.get_federated_workspace_for_sync(post_records[0][1].sync_id)

    source_workspace_id = _find_source_workspace_id(post_records, channel_id, ws_index=2)

    synced = 0
    failed = 0
    for post_meta, sync_channel, workspace in post_records:
        if sync_channel.channel_id == channel_id:
            continue
        try:
            if fed_ws and workspace.id != source_workspace_id:
                payload = federation.build_delete_payload(
                    post_id=post_meta.post_id.hex() if isinstance(post_meta.post_id, bytes) else str(post_meta.post_id),
                    channel_id=sync_channel.channel_id,
                    timestamp=f"{post_meta.ts:.6f}",
                )
                federation.push_delete(fed_ws, payload)
            else:
                helpers.delete_message(
                    bot_token=helpers.decrypt_bot_token(workspace.bot_token),
                    channel_id=sync_channel.channel_id,
                    ts=f"{post_meta.ts:.6f}",
                )
            synced += 1
        except Exception as exc:
            failed += 1
            _logger.warning(f"Failed to sync message delete to channel {sync_channel.channel_id}: {exc}")

    emit_metric("messages_synced", value=synced, sync_type="message_delete")
    if failed:
        emit_metric("sync_failures", value=failed, sync_type="message_delete")


def _sync_reaction_records(body: dict, client: WebClient, reacted_records: list[tuple]) -> None:
    """Apply reaction add/remove to linked channels that receive from the source."""
    from helpers.reactions import (
        apply_reaction_to_target,
        channel_sends_reactions,
        find_source_sync_channel,
    )

    event = body.get("event", {})
    reaction = event.get("reaction")
    user_id = event.get("user")
    item = event.get("item", {})
    channel_id = item.get("channel")
    event_type = event.get("type")
    action = "add" if event_type == "reaction_added" else "remove"

    source_sync_channel = find_source_sync_channel(reacted_records, channel_id)
    if not source_sync_channel or not channel_sends_reactions(source_sync_channel):
        return

    sync_id = source_sync_channel.sync_id

    fed_ws = helpers.get_federated_workspace_for_sync(sync_id) if sync_id else None
    source_workspace_id = _find_source_workspace_id(reacted_records, channel_id, ws_index=2)
    user_name, user_profile_url = helpers.get_user_info(client, user_id) if user_id else (None, None)
    source_ws = helpers.get_workspace_by_id(source_workspace_id) if source_workspace_id else None
    ws_name = helpers.resolve_workspace_name(source_ws) if source_ws else None
    posted_from = f"({ws_name})" if ws_name else "(via SyncBot)"

    post_list: list[schemas.PostMeta] = []
    synced = 0
    failed = 0
    name_probe_cache: dict[tuple[str, str], bool] = {}

    for post_meta, sync_channel, workspace in reacted_records:
        if sync_channel.channel_id == channel_id:
            continue

        try:
            if fed_ws and workspace.id != source_workspace_id:
                from helpers.reactions import should_sync_reaction_between

                if not should_sync_reaction_between(source_sync_channel, sync_channel):
                    continue
                payload = federation.build_reaction_payload(
                    post_id=str(post_meta.post_id),
                    channel_id=sync_channel.channel_id,
                    reaction=reaction,
                    action=action,
                    user_name=user_name or user_id or "Someone",
                    user_avatar_url=user_profile_url,
                    workspace_name=ws_name,
                    timestamp=f"{post_meta.ts:.6f}",
                    user_id=user_id,
                )
                federation.push_reaction(fed_ws, payload)
                synced += 1
                continue

            target_client = WebClient(token=helpers.decrypt_bot_token(workspace.bot_token))
            target_display_name, target_icon_url, author_is_mapped, mapped_user_id = (
                helpers.get_display_name_and_icon_for_synced_message(
                    user_id or "",
                    source_workspace_id or 0,
                    user_name,
                    user_profile_url,
                    target_client,
                    workspace.id,
                    source_client=client,
                )
            )
            display_name = target_display_name or user_name or user_id or "Someone"

            result, notice = apply_reaction_to_target(
                action=action,
                reaction=reaction,
                source_user_id=user_id,
                source_workspace_id=source_workspace_id,
                source_sync_channel=source_sync_channel,
                target_post_meta=post_meta,
                target_sync_channel=sync_channel,
                target_workspace=workspace,
                display_name=display_name,
                icon_url=target_icon_url or user_profile_url,
                posted_from=posted_from,
                author_is_mapped=author_is_mapped,
                mapped_user_id=mapped_user_id,
                name_probe_cache=name_probe_cache,
                event_workspace_id=source_workspace_id,
            )
            if notice:
                post_list.append(notice)
            if result in ("direct", "thread"):
                synced += 1
            elif result == "failed":
                failed += 1
        except Exception as exc:
            failed += 1
            _logger.warning(f"Failed to sync reaction to channel {sync_channel.channel_id}: {exc}")

    if post_list:
        DbManager.create_records(post_list)

    emit_metric("messages_synced", value=synced, sync_type=f"reaction_{action}")
    if failed:
        emit_metric("sync_failures", value=failed, sync_type=f"reaction_{action}")


def _handle_reaction(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Sync reaction add/remove to linked channels that receive them."""
    event = body.get("event", {})
    reaction = event.get("reaction")
    user_id = event.get("user")
    item = event.get("item", {})
    item_type = item.get("type")
    channel_id = item.get("channel")
    msg_ts = item.get("ts")
    event_type = event.get("type")

    if event_type not in ("reaction_added", "reaction_removed"):
        return

    if not reaction or not channel_id or not msg_ts or item_type != "message":
        return

    own_user_id = helpers.get_own_bot_user_id(client, context)
    if own_user_id and user_id == own_user_id:
        return

    reacted_records = helpers.get_post_records(msg_ts)
    if not reacted_records:
        _logger.debug(
            "reaction_no_post_meta",
            extra={"msg_ts": msg_ts, "channel_id": channel_id, "float_ts": float(msg_ts)},
        )
        return

    team_id = helpers.safe_get(body, "team_id") or helpers.safe_get(body, "team", "id")

    def _sync_reaction() -> None:
        from helpers.user_action_echo import reaction_echo_fingerprint, take_user_action_echo

        if team_id and user_id and reaction and channel_id and msg_ts:
            fingerprint = reaction_echo_fingerprint(channel_id, msg_ts, reaction)
            if take_user_action_echo(str(team_id), user_id, event_type, fingerprint):
                return
        _sync_reaction_records(body, client, reacted_records)

    run_claimed(body, _sync_reaction)


def _is_own_bot_message(body: dict, client: WebClient, context: dict) -> bool:
    """Return *True* if the event was generated by SyncBot itself.

    Compares the ``bot_id`` in the event payload against SyncBot's own
    bot ID.  This replaces the old blanket ``bot_message`` filter so
    that messages from *other* bots are synced normally while SyncBot's
    own re-posts are still ignored (preventing infinite loops).
    """
    event = body.get("event", {})
    event_bot_id = (
        event.get("bot_id")
        or helpers.safe_get(event, "message", "bot_id")
        or helpers.safe_get(event, "previous_message", "bot_id")
    )
    if not event_bot_id:
        return False

    own_bot_id = helpers.get_own_bot_id(client, context)
    return event_bot_id == own_bot_id


def _try_handle_reaction_notice_delete(
    body: dict,
    client: WebClient,
    context: dict,
    ctx: EventContext,
) -> bool:
    """Tombstone a user-deleted Hybrid reaction notice on this channel only."""
    if ctx.get("event_subtype") != "message_deleted":
        return False

    channel_id = ctx.get("channel_id")
    ts = ctx.get("ts")
    if not channel_id or not ts:
        return False

    team_id = helpers.safe_get(body, "team_id") or helpers.safe_get(body, "team", "id")
    workspace = helpers.get_workspace_record(team_id, body, context, client) if team_id else None
    if not workspace:
        return False

    sync_channels = DbManager.find_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.workspace_id == workspace.id, schemas.SyncChannel.channel_id == channel_id],
    )
    if not sync_channels:
        return False

    from helpers.reaction_notices import find_post_meta_by_channel_ts, tombstone_reaction_notice_locally

    notice = find_post_meta_by_channel_ts(sync_channels[0].id, ts)
    if (
        not notice
        or getattr(notice, "kind", constants.POST_META_KIND_MESSAGE) != constants.POST_META_KIND_REACTION_NOTICE
    ):
        return False

    bot_client = WebClient(token=helpers.decrypt_bot_token(workspace.bot_token))
    tombstone_reaction_notice_locally(
        notice=notice,
        sync_channel=sync_channels[0],
        client=bot_client,
    )
    return True


def respond_to_message_event(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Dispatch incoming message events to the appropriate sub-handler."""
    ctx = _parse_event_fields(body, client)
    event_type = helpers.safe_get(body, "event", "type")
    event_subtype = ctx["event_subtype"]

    if event_type != "message":
        return

    if event_subtype == "message_deleted" and _try_handle_reaction_notice_delete(body, client, context, ctx):
        return

    # Skip messages from SyncBot itself to prevent infinite sync loops.
    # Messages from OTHER bots are synced normally.
    if _is_own_bot_message(body, client, context):
        return

    # Slack sends a plain message event and then a file_share for the same post; process only file_share
    # so we do not sync twice (and avoid downloading files twice). thread_broadcast carries files on
    # the same event — do not wait for a second file_share.
    event_has_files = bool(
        helpers.safe_get(body, "event", "files") or helpers.safe_get(body, "event", "message", "files")
    )
    if not event_subtype and event_has_files:
        _logger.debug(
            "skip_message_pending_file_share",
            extra={"channel": helpers.safe_get(body, "event", "channel")},
        )
        return

    _SYNCED_SUBTYPES = frozenset(
        {
            None,
            "bot_message",
            "file_share",
            "thread_broadcast",
            "reply_broadcast",
            "me_message",
            "message_changed",
            "message_deleted",
        }
    )
    if event_subtype not in _SYNCED_SUBTYPES:
        _logger.info(
            "unhandled_message_subtype",
            extra={"subtype": event_subtype, "channel": helpers.safe_get(body, "event", "channel")},
        )
        return

    def _sync_message() -> None:
        photo_blocks, direct_files = _build_file_context(body, client, logger)
        has_files = bool(photo_blocks or direct_files)
        if event_subtype in (
            None,
            "bot_message",
            "file_share",
            "thread_broadcast",
            "reply_broadcast",
            "me_message",
        ) and (event_subtype != "file_share" or ctx["msg_text"] != "" or has_files):
            if not ctx["thread_ts"]:
                _handle_new_post(body, client, logger, ctx, photo_blocks, direct_files)
            else:
                _handle_thread_reply(body, client, logger, ctx, photo_blocks, direct_files)
        elif event_subtype == "message_changed":
            _handle_message_edit(client, logger, ctx, photo_blocks)
        elif event_subtype == "message_deleted":
            _handle_message_delete(ctx, logger)

    run_claimed(body, _sync_message)
