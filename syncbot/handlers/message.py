"""Message sync handlers — new posts, replies, edits, deletes, and files."""

import uuid
from logging import Logger

from slack_sdk.web import WebClient

import constants
import helpers
from db import DbManager, schemas
from db.event_claims import run_claimed
from handlers._common import EventContext
from helpers.sync_lifecycle import leave_channel_without_sync
from logger import emit_metric, log_debug, log_error, log_info, log_warning
from slack import orm


def _is_thread_reply(ts: str | None, thread_ts: str | None) -> bool:
    """True when Slack *thread_ts* is a parent, not this message's own ts."""
    if not ts or not thread_ts:
        return False
    return helpers.slack_message_ts(ts) != helpers.slack_message_ts(thread_ts)


def _build_envelope_people(
    ctx: EventContext,
    user_id: str | None,
    user_name: str | None,
    user_profile_url: str | None,
    workspace: schemas.Workspace | None,
) -> list[dict]:
    """Author plus mentioned users for the source-canonical envelope."""
    people = [
        helpers.build_people_entry(
            person["user_id"],
            name=person.get("user_name"),
            email=person.get("email"),
            avatar_url=person.get("user_profile_url"),
        )
        for person in (ctx.get("mentioned_users") or [])
        if person.get("user_id")
    ]
    if user_id:
        email = next(
            (person.get("email") for person in (ctx.get("mentioned_users") or []) if person.get("user_id") == user_id),
            None,
        )
        if not email and workspace is not None:
            rows = DbManager.find_records(
                schemas.UserDirectory,
                [
                    schemas.UserDirectory.workspace_id == workspace.id,
                    schemas.UserDirectory.slack_user_id == user_id,
                    schemas.UserDirectory.deleted_at.is_(None),
                ],
            )
            email = rows[0].email if rows else None
        people.insert(
            0,
            helpers.build_people_entry(user_id, name=user_name, email=email, avatar_url=user_profile_url),
        )
    return people


def _event_is_bot_post(event: dict) -> bool:
    """True for ``bot_message`` and for ``message_changed`` of a bot post."""
    if event.get("subtype") == "bot_message" or event.get("bot_id"):
        return True
    nested = event.get("message") if isinstance(event.get("message"), dict) else {}
    return bool(nested.get("bot_id") or nested.get("subtype") == "bot_message")


def _parse_event_fields_light(body: dict) -> EventContext:
    """Extract message fields without Slack API calls.

    Layout blocks and text come from the event payload only. Mention profiles
    and ``conversations.replies`` are filled later by
    :func:`_enrich_event_fields_for_sync` after membership and publish gates.
    """
    event: dict = body.get("event", {})
    raw_blocks = helpers.get_event_layout_blocks(event)
    layout_blocks = helpers.build_content_blocks_for_sync(raw_blocks)
    event_text = helpers.safe_get(event, "text") or helpers.safe_get(event, "message", "text")
    msg_text = helpers.choose_message_text(event_text, layout_blocks)
    ts = (
        helpers.safe_get(event, "message", "ts")
        or helpers.safe_get(event, "previous_message", "ts")
        or helpers.safe_get(event, "ts")
    )
    raw_thread = helpers.safe_get(event, "thread_ts") or helpers.safe_get(event, "message", "thread_ts")
    return EventContext(
        team_id=helpers.get_team_id_from_body(body),
        channel_id=helpers.safe_get(event, "channel"),
        user_id=(helpers.safe_get(event, "user") or helpers.safe_get(event, "message", "user")),
        msg_text=msg_text,
        mentioned_users=[],
        thread_ts=raw_thread if _is_thread_reply(ts, raw_thread) else None,
        ts=ts,
        event_subtype=helpers.safe_get(event, "subtype"),
        reply_broadcast=helpers.safe_get(event, "subtype") in ("thread_broadcast", "reply_broadcast"),
        content_blocks=layout_blocks,
        block_file_ids=helpers.collect_slack_file_ids_from_blocks(raw_blocks),
    )


def _enrich_event_fields_for_sync(body: dict, client: WebClient, ctx: EventContext) -> EventContext:
    """Resolve mentions and bot layout via Slack after publish gates pass."""
    event: dict = body.get("event", {})
    raw_event_blocks = helpers.get_event_layout_blocks(event)
    layout_blocks = list(ctx.get("content_blocks") or [])
    if not layout_blocks and not raw_event_blocks and _event_is_bot_post(event):
        fetched_blocks = helpers.fetch_message_layout_blocks(client, event)
        ctx["block_file_ids"] = helpers.collect_slack_file_ids_from_blocks(fetched_blocks)
        layout_blocks = helpers.build_content_blocks_for_sync(fetched_blocks)
        event_text = helpers.safe_get(event, "text") or helpers.safe_get(event, "message", "text")
        ctx["msg_text"] = helpers.choose_message_text(event_text, layout_blocks)
        ctx["content_blocks"] = layout_blocks

    _fold_event_attachments(event, ctx)
    mention_text = "\n".join(
        part for part in (ctx.get("msg_text") or "", helpers.text_from_attachments(ctx.get("attachments"))) if part
    )
    mentioned_users = helpers.parse_mentioned_users(mention_text, client)
    extra_ids = [
        uid
        for uid in helpers.collect_user_ids_from_blocks(layout_blocks)
        if uid not in {u.get("user_id") for u in mentioned_users}
    ]
    if extra_ids:
        mentioned_users.extend(helpers.parse_mentioned_users("".join(f"<@{uid}>" for uid in extra_ids), client))
    ctx["mentioned_users"] = mentioned_users
    return ctx


def _fold_event_attachments(event: dict, ctx: EventContext) -> None:
    """Copy forwards and other attachments Slack already delivered on this event.

    A forward and a bare link to a message copy the same way: the author's
    words, the labeled link (``{name} in #channel (Workspace)``), then the card.
    The name is the card's, noted for the label (``note_message_link_names``).
    A link the author wrote their own words for keeps them and gets no card.
    The source Channel's privacy is not a second gate.
    """
    raw = helpers.get_event_attachments(event)
    helpers.note_message_link_names(raw)
    event_text = helpers.safe_get(event, "text") or helpers.safe_get(event, "message", "text")
    raw = helpers.drop_previews_of_labeled_links(raw, event_text, helpers.get_event_layout_blocks(event))
    ctx["attachments"] = helpers.build_attachments_for_sync(raw)
    ctx["share_files"] = helpers.get_share_files(raw)
    forward_links = helpers.get_share_permalinks(raw)
    if forward_links:
        ctx["msg_text"], ctx["content_blocks"] = helpers.append_link_lines(
            ctx.get("msg_text"),
            ctx.get("content_blocks"),
            forward_links,
        )


def _get_source_permalink(client: WebClient, channel_id: str | None, ts: str | None) -> str | None:
    """Permalink for a message SyncBot cannot otherwise copy."""
    if not channel_id or not ts:
        return None
    try:
        res = client.chat_getPermalink(channel=channel_id, message_ts=ts)
    except Exception as exc:
        from helpers.slack_api import slack_error_code

        log_warning("source_permalink_failed", channel=channel_id, ts=ts, error=slack_error_code(exc))
        return None
    permalink = helpers.safe_get(res, "permalink")
    return permalink if isinstance(permalink, str) and permalink.startswith("https://") else None


_NOT_COPIED = {
    "create": "SyncBot could not copy your message to {names}.",
    "edit": "SyncBot could not copy your edit to {names}.",
    "delete": "SyncBot could not delete the copy of your message in {names}.",
}
_NO_REPLY = {
    "create": "SyncBot sent your message to {names}, but {whom} didn't reply.",
    "edit": "SyncBot sent your edit to {names}, but {whom} didn't reply.",
    "delete": "SyncBot asked {names} to delete the copy of your message, but {whom} didn't reply.",
}
_REDUCED = "SyncBot copied your {what} to {names}, but it may not look the same there."
_REDUCED_EVERYWHERE = "SyncBot copied your message, but it may not look the same in the other Channels."
_FILE_NOT_COPIED = "SyncBot could not copy your file to the other Channels."
_STORAGE_FULL = "That workspace's file storage is full."
_REPORT_IT = "Please send this to a workspace admin so it can be fixed."


def _build_target_label(sync_channel, workspace) -> str:
    """``#name (Workspace)``, or only the Workspace when the target Channel may be private."""
    ws_name = helpers.resolve_workspace_name(workspace) or "Workspace"
    if getattr(sync_channel, "is_private", None) is not False:
        return ws_name
    stored = (getattr(sync_channel, "channel_name", None) or "").strip()
    name = stored or helpers.lookup_channel_name(sync_channel.channel_id, workspace) or sync_channel.channel_id
    return f"#{name.lstrip('#')} ({ws_name})"


def _join_names(labels: list[str]) -> str:
    ticked = [f"`{label}`" for label in labels]
    if len(ticked) < 3:
        return " and ".join(ticked)
    return ", ".join(ticked[:-1]) + f", and {ticked[-1]}"


def _notify_author(
    client: WebClient,
    user_id: str | None,
    ctx: EventContext,
    action: str,
    report: list | None,
    *,
    nothing_posted: bool = False,
) -> None:
    """One DM to the author when their message did not copy all the way.

    *report* is ``run_sync_pipeline``'s ``(sync_channel, workspace, error, copied)``.
    A target that never answered (``no_response``) is "sent, but didn't reply", not "could not copy".
    ``ctx["degraded"]`` holds causes found before fan-out, and ``ctx["files_failed"]``
    an unreadable file. Gates are never reported. No message words go in the DM.
    """
    report = list(report or [])
    reasons = list(ctx.get("degraded") or [])
    files_failed = bool(ctx.get("files_failed"))
    if not (report or reasons or files_failed) or not user_id:
        return
    refused = [(_build_target_label(sc, ws), error) for sc, ws, error, copied in report if not copied]
    failed = [(name, error) for name, error in refused if error != helpers.NO_RESPONSE]
    no_reply = [(name, error) for name, error in refused if error == helpers.NO_RESPONSE]
    reduced = [(_build_target_label(sc, ws), error) for sc, ws, error, copied in report if copied]
    lines: list[str] = []
    if failed:
        lines.append(_NOT_COPIED.get(action, _NOT_COPIED["create"]).format(names=_join_names([n for n, _ in failed])))
    if no_reply:
        whom = "that Workspace" if len(no_reply) == 1 else "those Workspaces"
        lines.append(
            _NO_REPLY.get(action, _NO_REPLY["create"]).format(names=_join_names([n for n, _ in no_reply]), whom=whom)
        )
    if reduced:
        what = "edit" if action == "edit" else "message"
        lines.append(_REDUCED.format(what=what, names=_join_names([n for n, _ in reduced])))
    if set(reasons) - {"download_empty"}:
        lines.append(_REDUCED_EVERYWHERE)
    if files_failed:
        lines.append(_FILE_NOT_COPIED)
    if any(error == "storage_limit_reached" for _n, error in failed):
        lines.append(_STORAGE_FULL)
    lines.append(_REPORT_IT)
    details: dict = {
        "event": "message_not_copied" if refused or nothing_posted else "message_degraded",
        "action": action,
        "channel": ctx.get("channel_id"),
        "ts": ctx.get("ts"),
        "reason": ", ".join(reasons) or None,
    }
    for label, error in refused + reduced:
        details[label] = error
    helpers.notify_source_user_error(
        source_client=client,
        source_user_id=user_id,
        summary=":warning: " + " ".join(lines),
        details=details,
    )


def _replay_pending_events(client: WebClient, logger: Logger, ctx: EventContext) -> None:
    """Replay the replies, edits, deletes, and reactions that came before this message's copy.

    Runs right after the create writes its PostMeta, on the ``REPLAY`` pending
    events left for it (``leave_pending_event``). Each is taken once. An edit,
    delete, or thread reply is read again from the synced Channel and sent
    through ``respond_to_message_event``, so every gate and echo guard runs
    again: a message still there is an edit, a gone one a delete, and a reply a
    new message. One read covers every edit and delete of this message. A
    reaction runs through ``_sync_reaction_records`` with its own ``event_ts``.
    """
    team_id = ctx.get("team_id")
    channel_id = ctx.get("channel_id")
    ts = ctx.get("ts")
    if not team_id or not channel_id or not ts:
        return
    read: set = set()
    for pending in helpers.take_pending_events_to_replay(team_id=team_id, channel_id=channel_id, message_ts=ts):
        if pending.action not in (helpers.ACTION_ADD, helpers.ACTION_REMOVE):
            if (pending.source_ts, pending.source_thread_ts) in read:
                continue
            read.add((pending.source_ts, pending.source_thread_ts))
        try:
            _replay_pending_event(client, logger, ctx, pending)
        except Exception as exc:
            log_warning("pending_event_replay_failed", channel=channel_id, ts=ts, error=str(exc))


def _replay_pending_event(client: WebClient, logger: Logger, ctx: EventContext, pending: schemas.PendingEvent) -> None:
    team_id = ctx.get("team_id")
    channel_id = ctx.get("channel_id")
    ts = ctx.get("ts")
    if pending.action in (helpers.ACTION_ADD, helpers.ACTION_REMOVE):
        from handlers.reaction_event import _sync_reaction_records

        event: dict = {
            "type": "reaction_added" if pending.action == helpers.ACTION_ADD else "reaction_removed",
            "user": pending.source_user_id,
            "reaction": pending.reaction,
            "item": {"type": "message", "channel": channel_id, "ts": ts},
        }
        if pending.event_ts is not None:
            event["event_ts"] = helpers.slack_message_ts(pending.event_ts)
        records = helpers.get_post_records(ts)
        if records:
            log_debug("pending_event_replayed", action=pending.action, channel=channel_id, ts=ts)
            _sync_reaction_records({"team_id": team_id, "event": event}, client, records)
        return
    if pending.action == helpers.ACTION_CREATE:
        # A new reply in this message's thread.
        reply_ts = helpers.slack_message_ts(pending.source_ts)
        message = helpers.fetch_source_message(client, channel_id, reply_ts, thread_ts=ts)
        if message is None:
            return
        event = {**message, "type": "message", "channel": channel_id}
    else:
        # An edit or a delete of this message: whatever it is now. Its own thread is its parent, if it is a reply.
        message = helpers.fetch_source_message(client, channel_id, ts, thread_ts=ctx.get("thread_ts"))
        if message is None:
            event = {
                "type": "message",
                "subtype": "message_deleted",
                "channel": channel_id,
                "deleted_ts": ts,
                "previous_message": {"ts": ts, "user": ctx.get("user_id")},
            }
        else:
            event = {"type": "message", "subtype": "message_changed", "channel": channel_id, "message": message}
    log_debug(
        "pending_event_replayed",
        action=pending.action,
        subtype=event.get("subtype"),
        channel=channel_id,
        ts=helpers.slack_message_ts(pending.source_ts),
    )
    respond_to_message_event({"team_id": team_id, "event": event}, client, logger, {})


def _get_post_records_or_leave_pending(
    ctx: EventContext, *, action: str, source_ts: str | None, source_thread_ts: str | None = None
) -> list:
    """PostMeta of the message this reply, edit, or delete acts on. ``[]`` means nothing to do now.

    A new reply acts on its parent (*source_thread_ts*); an edit or a delete on
    its own message (*source_ts*). A message from before the Sync never gets
    PostMeta. Otherwise its create may still be running, so the event is left
    pending for it (``leave_pending_event``).
    """
    message_ts = source_thread_ts or source_ts
    records = helpers.get_post_records(message_ts)
    if records:
        return records
    channel_id = ctx.get("channel_id")
    if helpers.is_before_sync(channel_id, message_ts):
        log_info("message_skip", reason="before_sync", channel=channel_id, ts=message_ts, action=action)
        return []
    return helpers.leave_pending_event(
        ctx.get("team_id"), channel_id, action=action, source_ts=source_ts, source_thread_ts=source_thread_ts
    )


def _download_forward_files(
    files: list[dict],
    client: WebClient,
    logger: Logger,
    team_id: str | None,
    user_id: str | None,
) -> list[dict]:
    """Read forward files with the source bot token, then the forwarder's token."""
    downloaded = helpers.download_slack_files(files, client, logger)
    have = {item.get("slack_file_id") for item in downloaded}
    missing = [
        item
        for item in files
        if isinstance(item, dict) and helpers.hosted_file_fetch_url(item) and str(item.get("id") or "") not in have
    ]
    if not missing or not team_id or not user_id:
        return downloaded
    token = helpers.get_user_token(team_id, user_id)
    if not token:
        return downloaded
    downloaded.extend(helpers.download_slack_files(missing, WebClient(token=token), logger))
    return downloaded


def _parse_event_fields(body: dict, client: WebClient) -> EventContext:
    """Extract message fields including Slack mention/history enrichment.

    Prefer :func:`_parse_event_fields_light` on the hot path and enrich only
    after membership and publish checks.
    """
    return _enrich_event_fields_for_sync(body, client, _parse_event_fields_light(body))


def _build_file_context(
    body: dict,
    client: WebClient,
    logger: Logger,
    block_file_ids: list[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Process files attached to a message event.

    Returns ``(photo_blocks, direct_files)`` where:

    * *photo_blocks* — Slack Block Kit ``image`` blocks for inline images
      (e.g. GIF picker URLs), ready for ``chat.postMessage``.
    * *direct_files* — files downloaded to ``/tmp`` for direct upload to
      each target channel.

    An image block that shows a Slack file not in ``files`` is read with
    ``files.info`` and downloaded the same way.
    """
    event = body.get("event", {})
    files = helpers.safe_get(event, "files") or helpers.safe_get(event, "message", "files") or []
    event_subtype = helpers.safe_get(event, "subtype")

    photo_blocks: list[dict] = []
    direct_files: list[dict] = []
    is_edit = event_subtype in ("message_changed", "message_deleted")

    if not is_edit:
        hosted = list(files) if isinstance(files, list) else []
        seen = {item.get("id") for item in hosted if isinstance(item, dict) and item.get("id")}
        for file_id in block_file_ids or []:
            if file_id in seen:
                continue
            try:
                info = client.files_info(file=file_id)
            except Exception as exc:
                from helpers.slack_api import slack_error_code

                log_error(
                    "file_share_failed",
                    reason="files_info_failed",
                    file_id=file_id,
                    error=slack_error_code(exc),
                )
                continue
            file_obj = info.get("file") if isinstance(info, dict) or hasattr(info, "get") else None
            if isinstance(file_obj, dict):
                hosted.append(file_obj)
                seen.add(file_id)
        direct_files = helpers.download_slack_files(hosted, client, logger)

    # Public GIF/image URLs (GIPHY, Slack GIF picker) stay on the message even
    # when a hosted file is attached too. Slack-hosted thumbs are the file.
    attachments = event.get("attachments") or helpers.safe_get(event, "message", "attachments") or []
    seen_urls: set[str] = set()
    for att in attachments:
        if not isinstance(att, dict) or not helpers.is_image_only_attachment(att):
            continue
        img_url = _attachment_image_url(att)
        if not img_url:
            continue
        if img_url in seen_urls or helpers.is_slack_hosted_media_url(img_url):
            continue
        seen_urls.add(img_url)
        name = att.get("fallback") or "attachment.gif"
        photo_blocks.append(orm.ImageBlock(image_url=img_url, alt_text=name).as_form_field())

    return photo_blocks, direct_files


def _attachment_image_url(att: dict) -> str | None:
    """Public image on a Slack attachment, including a GIF picker block."""
    img_url = att.get("image_url") or att.get("thumb_url")
    if img_url:
        return str(img_url)
    for blk in att.get("blocks") or []:
        if isinstance(blk, dict) and blk.get("type") == "image" and blk.get("image_url"):
            return str(blk["image_url"])
    return None


def _handle_new_post(
    body: dict,
    client: WebClient,
    logger: Logger,
    ctx: EventContext,
    photo_blocks: list[dict],
    direct_files: list[dict] | None = None,
) -> None:
    """Publish a brand-new top-level message through the target pipeline."""
    channel_id = ctx["channel_id"]
    user_id = ctx["user_id"]
    source_records = helpers.get_channel_memberships(channel_id)
    source_sync_channel = helpers.get_origin_sync_channel(channel_id)
    source_workspace = (
        next(
            (workspace for sync_channel, workspace in source_records if sync_channel.id == source_sync_channel.id),
            None,
        )
        if source_sync_channel
        else None
    )
    if not source_sync_channel or not source_workspace:
        helpers.cleanup_temp_files(None, direct_files)
        return

    user_name, user_profile_url = (
        helpers.get_user_info(client, user_id) if user_id else helpers.get_bot_info_from_event(body)
    )
    post_uuid = uuid.uuid4().hex
    source_ts = helpers.safe_get(body, "event", "ts")
    envelope = helpers.build_envelope(
        kind=helpers.KIND_MESSAGE,
        action=helpers.ACTION_CREATE,
        post_id=post_uuid,
        source_channel_id=channel_id,
        source_workspace_id=source_workspace.id,
        source_team_id=ctx.get("team_id"),
        source_sync_channel_id=source_sync_channel.id,
        people=_build_envelope_people(ctx, user_id, user_name, user_profile_url, source_workspace),
        text=ctx.get("msg_text") or "",
        blocks=ctx.get("content_blocks") or [],
        file_refs=direct_files or [],
        images=photo_blocks,
        attachments=ctx.get("attachments") or None,
        source_user_id=user_id,
        user_name=user_name,
        user_avatar_url=user_profile_url,
        workspace_name=helpers.resolve_workspace_name(source_workspace),
        source_ts=source_ts,
        reply_broadcast=bool(ctx.get("reply_broadcast")),
    )
    post_list = helpers.build_origin_post_meta_rows(
        source_records,
        channel_id,
        post_uuid,
        source_ts,
        user_id,
        source_workspace.id,
        source_sync_channel=source_sync_channel,
    )
    if not post_list:
        helpers.cleanup_temp_files(None, direct_files)
        return
    report: list = []
    try:
        synced = helpers.run_sync_pipeline(
            envelope,
            source_channel_id=channel_id,
            source_client=client,
            source_sync_channel=source_sync_channel,
            report=report,
        )
    finally:
        helpers.cleanup_temp_files(None, direct_files)
    DbManager.create_records(post_list)
    _replay_pending_events(client, logger, ctx)
    emit_metric("messages_synced", value=len(synced), sync_type="new_post")
    _notify_author(client, user_id, ctx, "create", report)


def _handle_thread_reply(
    body: dict,
    client: WebClient,
    logger: Logger,
    ctx: EventContext,
    photo_blocks: list[dict],
    direct_files: list[dict] | None = None,
) -> None:
    """Publish a threaded reply through the target pipeline.

    When the parent has no PostMeta yet, the reply is left pending for the parent's create.
    """
    channel_id = ctx["channel_id"]
    user_id = ctx["user_id"]
    thread_ts = ctx["thread_ts"]
    post_records = _get_post_records_or_leave_pending(
        ctx, action=helpers.ACTION_CREATE, source_ts=ctx.get("ts"), source_thread_ts=thread_ts
    )
    if not post_records:
        helpers.cleanup_temp_files(None, direct_files)
        return
    source_rows = helpers.get_publishing_post_records(post_records, channel_id)
    if not source_rows:
        helpers.cleanup_temp_files(None, direct_files)
        return
    parent_meta, source_sync_channel, source_workspace = source_rows[0]
    source_records = [(sync_channel, workspace) for _meta, sync_channel, workspace in source_rows]
    user_name, user_profile_url = (
        helpers.get_user_info(client, user_id) if user_id else helpers.get_bot_info_from_event(body)
    )
    post_uuid = uuid.uuid4().hex
    source_ts = helpers.safe_get(body, "event", "ts")
    envelope = helpers.build_envelope(
        kind=helpers.KIND_MESSAGE,
        action=helpers.ACTION_CREATE,
        post_id=post_uuid,
        source_channel_id=channel_id,
        source_workspace_id=source_workspace.id,
        source_team_id=ctx.get("team_id"),
        source_sync_channel_id=source_sync_channel.id,
        people=_build_envelope_people(ctx, user_id, user_name, user_profile_url, source_workspace),
        text=ctx.get("msg_text") or "",
        blocks=ctx.get("content_blocks") or [],
        file_refs=direct_files or [],
        images=photo_blocks,
        attachments=ctx.get("attachments") or None,
        thread_post_id=str(parent_meta.post_id),
        reply_broadcast=bool(ctx.get("reply_broadcast")),
        source_user_id=user_id,
        user_name=user_name,
        user_avatar_url=user_profile_url,
        workspace_name=helpers.resolve_workspace_name(source_workspace),
        source_ts=source_ts,
    )
    post_list = helpers.build_origin_post_meta_rows(
        source_records,
        channel_id,
        post_uuid,
        source_ts,
        user_id,
        source_workspace.id,
        source_sync_channel=source_sync_channel,
    )
    if not post_list:
        helpers.cleanup_temp_files(None, direct_files)
        return
    parent_ts_by_channel = {
        sync_channel.channel_id: helpers.slack_message_ts(post_meta.ts)
        for post_meta, sync_channel, _workspace in post_records
    }
    report: list = []
    try:
        synced = helpers.run_sync_pipeline(
            envelope,
            source_channel_id=channel_id,
            source_client=client,
            source_sync_channel=source_sync_channel,
            thread_parent_ts_by_channel=parent_ts_by_channel,
            report=report,
        )
    finally:
        helpers.cleanup_temp_files(None, direct_files)
    DbManager.create_records(post_list)
    _replay_pending_events(client, logger, ctx)
    emit_metric("messages_synced", value=len(synced), sync_type="thread_reply")
    _notify_author(client, user_id, ctx, "create", report)
    log_debug(
        "thread_reply_applied",
        post_id=post_uuid,
        thread_post_id=str(parent_meta.post_id),
        source_channel=channel_id,
        copied=len(synced),
        parent_channels=sorted(parent_ts_by_channel),
    )


def _handle_message_edit(
    client: WebClient,
    logger: Logger,
    ctx: EventContext,
    photo_blocks: list[dict],
) -> None:
    """Publish an edited message through the target pipeline.

    When the message has no PostMeta yet, the edit is left pending for its create.
    """
    channel_id = ctx["channel_id"]
    ts = ctx["ts"]
    post_records = _get_post_records_or_leave_pending(ctx, action=helpers.ACTION_EDIT, source_ts=ts)
    if not post_records:
        return
    source_rows = helpers.get_publishing_post_records(post_records, channel_id)
    if not source_rows:
        return
    post_meta, source_sync_channel, workspace = source_rows[0]
    user_id = ctx.get("user_id")
    envelope = helpers.build_envelope(
        kind=helpers.KIND_MESSAGE,
        action=helpers.ACTION_EDIT,
        post_id=str(post_meta.post_id),
        source_channel_id=channel_id,
        source_workspace_id=workspace.id,
        source_team_id=ctx.get("team_id"),
        source_sync_channel_id=source_sync_channel.id,
        people=_build_envelope_people(ctx, user_id, None, None, workspace),
        text=ctx.get("msg_text") or "",
        blocks=ctx.get("content_blocks") or [],
        images=photo_blocks,
        attachments=ctx.get("attachments") or None,
        source_user_id=user_id,
        workspace_name=helpers.resolve_workspace_name(workspace),
        source_ts=ts,
    )
    report: list = []
    helpers.run_sync_pipeline(
        envelope,
        source_channel_id=channel_id,
        source_client=client,
        source_sync_channel=source_sync_channel,
        report=report,
    )
    _notify_author(client, user_id, ctx, "edit", report)


def _handle_message_delete(
    ctx: EventContext,
    logger: Logger,
    client: WebClient | None = None,
    deleted_by: str | None = None,
) -> None:
    """Publish a deleted message through the target pipeline.

    When the message has no PostMeta yet, the delete is left pending for its create.
    """
    channel_id = ctx["channel_id"]
    ts = ctx["ts"]
    post_records = _get_post_records_or_leave_pending(ctx, action=helpers.ACTION_DELETE, source_ts=ts)
    if not post_records:
        return
    source_rows = helpers.get_publishing_post_records(post_records, channel_id)
    if not source_rows:
        return
    post_meta, source_sync_channel, workspace = source_rows[0]
    envelope = helpers.build_envelope(
        kind=helpers.KIND_MESSAGE,
        action=helpers.ACTION_DELETE,
        post_id=str(post_meta.post_id),
        source_channel_id=channel_id,
        source_workspace_id=workspace.id,
        source_team_id=ctx.get("team_id"),
        source_sync_channel_id=source_sync_channel.id,
        source_user_id=ctx.get("user_id"),
        workspace_name=helpers.resolve_workspace_name(workspace),
        source_ts=ts,
    )
    report: list = []
    helpers.run_sync_pipeline(
        envelope,
        source_channel_id=channel_id,
        source_sync_channel=source_sync_channel,
        report=report,
    )
    if client is not None:
        _notify_author(client, deleted_by, ctx, "delete", report)


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

    team_id = helpers.get_team_id_from_body(body)
    workspace = helpers.get_workspace_record(team_id, body, context, client) if team_id else None
    if not workspace:
        return False

    sync_channels = DbManager.find_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.workspace_id == workspace.id, schemas.SyncChannel.channel_id == channel_id],
    )
    if not sync_channels:
        return False

    from helpers.reaction_notice import get_post_meta_by_channel_ts, tombstone_reaction_notice_locally

    notice = get_post_meta_by_channel_ts(sync_channels[0].id, ts)
    if (
        not notice
        or getattr(notice, "kind", constants.POST_META_KIND_MESSAGE) != constants.POST_META_KIND_REACTION_NOTICE
    ):
        return False

    bot_client = WebClient(token=helpers.get_bot_token(workspace))
    tombstone_reaction_notice_locally(
        notice=notice,
        sync_channel=sync_channels[0],
        client=bot_client,
    )
    return True


def _handle_channel_state_subtype(ctx: EventContext, event_subtype: str | None) -> None:
    """Pause an archive, resume an unarchive, or soft-delete a privacy change."""
    from helpers.leave_sync import soft_delete_sync_channels
    from helpers.sync_lifecycle import pause_archived_channel, resume_unarchived_channel

    team_id = ctx.get("team_id")
    channel_id = ctx.get("channel_id")
    workspace = DbManager.get_record(schemas.Workspace, id=team_id) if team_id else None
    if not workspace or not channel_id:
        return
    if event_subtype == "channel_convert_to_private":
        soft_delete_sync_channels(workspace, channel_id, "became_private", is_private=True)
    elif event_subtype == "channel_convert_to_public":
        soft_delete_sync_channels(workspace, channel_id, "became_public", is_private=False)
    elif event_subtype in {"channel_archive", "group_archive"}:
        pause_archived_channel(workspace, channel_id)
    elif event_subtype in {"channel_unarchive", "group_unarchive"}:
        resume_unarchived_channel(workspace, channel_id)


def respond_to_message_event(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Dispatch incoming message events to the appropriate sub-handler."""
    ctx = _parse_event_fields_light(body)
    event_type = helpers.safe_get(body, "event", "type")
    event_subtype = ctx["event_subtype"]

    if event_type != "message":
        return

    raw_event = body.get("event") if isinstance(body.get("event"), dict) else {}
    event_files = helpers.safe_get(body, "event", "files") or helpers.safe_get(body, "event", "message", "files") or []
    if not isinstance(event_files, list):
        event_files = []
    raw_outside = [item for item in event_files if isinstance(item, dict) and helpers.is_outside_file(item)]
    outside_files: list = []
    if raw_outside:
        kept_event = raw_event
        if raw_event.get("subtype") == "message_changed" and isinstance(raw_event.get("message"), dict):
            kept_event = raw_event["message"]
        if helpers.event_keeps_hosted_files(
            kept_event,
            is_reply=bool(ctx.get("thread_ts")),
            text=ctx.get("msg_text") or "",
        ):
            outside_files = raw_outside
    if raw_outside:
        body = helpers.drop_outside_files(body)
        event_files = [item for item in event_files if not (isinstance(item, dict) and helpers.is_outside_file(item))]
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
    _SYSTEM_SUBTYPES = frozenset(
        {
            "channel_join",
            "channel_leave",
            "channel_name",
            "channel_topic",
            "channel_purpose",
            "channel_posting_permissions",
            "group_join",
            "group_leave",
            "group_name",
            "group_topic",
            "group_purpose",
            "pinned_item",
            "unpinned_item",
            "message_replied",
            "reminder_add",
            "file_mention",
            "file_comment",
            "document_mention",
            "ekm_access_denied",
            "assistant_app_thread",
            "huddle_thread",
            "bot_add",
            "bot_remove",
            "bot_enable",
            "bot_disable",
        }
    )
    _CHANNEL_STATE_SUBTYPES = frozenset(
        {
            "channel_convert_to_private",
            "channel_convert_to_public",
            "channel_archive",
            "group_archive",
            "channel_unarchive",
            "group_unarchive",
        }
    )

    def _sync_message() -> bool | None:
        if event_subtype in _CHANNEL_STATE_SUBTYPES:
            _handle_channel_state_subtype(ctx, event_subtype)
            return

        if event_subtype == "message_deleted" and _try_handle_reaction_notice_delete(body, client, context, ctx):
            return

        # Skip messages from SyncBot itself to prevent infinite sync loops.
        # Messages from OTHER bots are synced normally.
        if _is_own_bot_message(body, client, context):
            file_ids = helpers.file_ids_from_message_event(body)
            recorded = helpers.complete_copy_ts_from_pending_share(
                ctx.get("team_id"),
                ctx.get("channel_id"),
                ctx.get("ts"),
                file_ids,
            )
            if recorded is False:
                return False
            if recorded:
                log_debug(
                    "apply_create",
                    source="own_bot_share",
                    channel_id=ctx.get("channel_id"),
                    ts=ctx.get("ts"),
                )
                # The copy had no ts until now, so anything done on it was left pending.
                _replay_pending_events(client, logger, ctx)
                return
            log_debug(
                "message_skip",
                reason="own_bot",
                channel=ctx.get("channel_id"),
                subtype=event_subtype,
                ts=ctx.get("ts"),
            )
            return

        if event_subtype in _SYSTEM_SUBTYPES:
            log_debug(
                "message_skip",
                reason="system_subtype",
                subtype=event_subtype,
                channel=helpers.safe_get(body, "event", "channel"),
            )
            return

        if event_subtype not in _SYNCED_SUBTYPES:
            if raw_event.get("hidden") is True:
                log_warning(
                    "message_skip",
                    reason="unknown_subtype",
                    subtype=event_subtype,
                    channel=helpers.safe_get(body, "event", "channel"),
                )
                return
            log_warning(
                "message_degraded",
                reason="unknown_subtype",
                subtype=event_subtype,
                channel=helpers.safe_get(body, "event", "channel"),
            )
            ctx["degraded"] = ["unknown_subtype"]

        channel_id = ctx.get("channel_id")
        user_id = ctx.get("user_id")
        ts = ctx.get("ts")
        team_id = ctx.get("team_id")
        if not helpers.has_channel_sync(channel_id):
            if user_id:
                workspace = DbManager.get_record(schemas.Workspace, id=team_id) if team_id else None
                leave_channel_without_sync(client, channel_id, workspace=workspace)
            return

        is_reply = bool(ctx.get("thread_ts"))
        event = body.get("event") if isinstance(body.get("event"), dict) else {}
        is_new_file_share = helpers.event_is_new_file_share(event)
        this_message_files = helpers.event_keeps_hosted_files(event, is_reply=is_reply, text=ctx.get("msg_text") or "")
        hosted_media = helpers.event_has_slack_hosted_media_blocks(event)
        trace = {
            "channel": channel_id,
            "subtype": event_subtype,
            "ts": ts,
            "thread_ts": ctx.get("thread_ts"),
            "file_count": len(event_files),
            "upload": event.get("upload"),
            "new_file_share": is_new_file_share,
            "parent_user_id": helpers.safe_get(body, "event", "parent_user_id"),
        }
        if (
            not event_subtype
            and not is_reply
            and not helpers.event_has_downloadable_hosted_file(event)
            and (event_files or hosted_media)
        ):
            log_debug("message_skip", reason="pending_file_share", **trace)
            return

        if team_id and user_id:
            if this_message_files and any(
                helpers.check_pending_event(
                    helpers.FILE_ECHO, target_team_id=team_id, target_user_id=user_id, file_id=file_id
                )
                for file_id in helpers.file_ids_from_message_event(body)
            ):
                echo_file_ids = helpers.file_ids_from_message_event(body)
                recorded = helpers.complete_copy_ts_from_pending_share(
                    team_id,
                    channel_id,
                    ts,
                    echo_file_ids,
                )
                if recorded is False:
                    return False
                if recorded:
                    log_debug(
                        "apply_create",
                        source="file_echo_share",
                        channel_id=channel_id,
                        ts=ts,
                    )
                    _replay_pending_events(client, logger, ctx)
                    return
                log_debug("message_skip", reason="file_echo", **trace)
                return
            if helpers.check_pending_event(
                helpers.MESSAGE_ECHO,
                target_team_id=team_id,
                target_user_id=user_id,
                target_channel_id=channel_id,
                target_ts=ts,
            ):
                log_debug("message_skip", reason="message_echo", **trace)
                return
        is_create = event_subtype not in ("message_changed", "message_deleted")
        if is_create and helpers.post_meta_exists_for_channel_ts(channel_id, ts):
            log_debug("message_skip", reason="copy_exists", **trace)
            return
        if not helpers.origin_publishes_anywhere(channel_id):
            log_debug("message_skip", reason="not_publishing", **trace)
            return

        has_targets = bool(helpers.iter_publish_targets(channel_id))
        file_body = body
        if event_files and not this_message_files:
            event = dict(body.get("event") or {})
            event.pop("files", None)
            nested = event.get("message")
            if isinstance(nested, dict):
                event["message"] = {**nested, "files": []}
            file_body = {**body, "event": event}
        forward_missing = False
        forward_downloads: list[dict] = []
        if has_targets:
            _enrich_event_fields_for_sync(file_body, client, ctx)
            photo_blocks, direct_files = _build_file_context(
                file_body,
                client,
                logger,
                block_file_ids=ctx.get("block_file_ids") or [],
            )
            event_file_list = (
                helpers.safe_get(file_body, "event", "files")
                or helpers.safe_get(file_body, "event", "message", "files")
                or []
            )
            link_files = list(outside_files) + (event_file_list if isinstance(event_file_list, list) else [])
            if link_files:
                ctx["msg_text"], ctx["content_blocks"] = helpers.add_outside_file_links(
                    ctx.get("msg_text"),
                    ctx.get("content_blocks"),
                    link_files,
                )
            # A forward's own files. Only a create uploads, so an edit never downloads them.
            forward_files = [
                item
                for item in (ctx.get("share_files") or [])
                if is_create and not helpers.is_outside_file(item) and not helpers.get_link_only_file_url(item)
            ]
            if forward_files:
                forward_downloads = _download_forward_files(forward_files, client, logger, team_id, user_id)
                forward_missing = len(forward_downloads) < len(forward_files)
                direct_files = list(direct_files) + forward_downloads
        else:
            # Origin PostMeta still needed for later join/thread/reaction; skip Slack.
            photo_blocks, direct_files = [], []

        has_files = bool(photo_blocks or direct_files)
        own_downloads = len(direct_files) - len(forward_downloads)
        text_blank = not (ctx.get("msg_text") or "").strip()
        download_files = (
            helpers.safe_get(file_body, "event", "files")
            or helpers.safe_get(file_body, "event", "message", "files")
            or []
        )
        if not isinstance(download_files, list):
            download_files = []
        expected_files = [
            item for item in download_files if isinstance(item, dict) and helpers.hosted_file_fetch_url(item)
        ]
        only_links = bool(download_files) and all(
            isinstance(item, dict) and (helpers.get_link_only_file_url(item) or helpers.is_outside_file(item))
            for item in download_files
        )
        file_missing = bool(
            is_create
            and has_targets
            and (
                forward_missing
                or (
                    not only_links
                    and (
                        (expected_files and own_downloads < len(expected_files))
                        or (this_message_files and not (photo_blocks or own_downloads))
                        or (hosted_media and is_new_file_share and not (photo_blocks or own_downloads))
                    )
                )
            )
        )
        has_body = not text_blank or bool(ctx.get("content_blocks")) or bool(ctx.get("attachments")) or has_files
        if file_missing:
            log_error(
                "file_share_failed",
                reason="download_empty",
                channel=channel_id,
                ts=ts,
                thread_ts=ctx.get("thread_ts"),
                file_count=len(event_files) + len(ctx.get("share_files") or []),
            )
            ctx["files_failed"] = True
            ctx["degraded"] = [*(ctx.get("degraded") or []), "download_empty"]
        if is_create and has_targets and not has_body:
            # Only files, and none could be read: nothing posts. A later file_share may still bring them.
            if file_missing:
                _notify_author(client, user_id, ctx, "create", [], nothing_posted=True)
                log_debug("message_skip", reason="empty_file_share", has_files=has_files, **trace)
                return
            permalink = _get_source_permalink(client, channel_id, ts)
            details = {
                "subtype": event_subtype,
                "block_types": sorted(
                    {
                        str(block.get("type"))
                        for block in helpers.get_event_layout_blocks(raw_event)
                        if isinstance(block, dict) and block.get("type")
                    }
                ),
                "attachment_keys": sorted(
                    {str(key) for item in helpers.get_event_attachments(raw_event) for key in item}
                ),
                "channel": channel_id,
                "ts": ts,
            }
            if not permalink:
                log_warning("message_skip", reason="nothing_readable", **details)
                return
            log_warning("message_degraded", reason="nothing_readable", **details)
            ctx["msg_text"] = permalink
            ctx["degraded"] = [*(ctx.get("degraded") or []), "nothing_readable"]
        if is_create and has_targets and ctx.get("degraded"):
            ctx["msg_text"], ctx["content_blocks"] = helpers.add_copy_note(
                ctx.get("msg_text"), ctx.get("content_blocks")
            )
        if is_create:
            if not ctx["thread_ts"]:
                _handle_new_post(body, client, logger, ctx, photo_blocks, direct_files)
                return
            return _handle_thread_reply(body, client, logger, ctx, photo_blocks, direct_files)
        if event_subtype == "message_changed":
            if has_targets:
                return _handle_message_edit(client, logger, ctx, photo_blocks)
            return
        if event_subtype == "message_deleted":
            deleted_by = helpers.safe_get(body, "event", "previous_message", "user") or user_id
            return _handle_message_delete(ctx, logger, client, deleted_by)

    run_claimed(body, _sync_message)
