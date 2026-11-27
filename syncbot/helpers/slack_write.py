"""Actor that posts, updates, deletes, or uploads on a target Channel."""

from __future__ import annotations

import re
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from helpers.conversations import get_user_token
from helpers.core import format_synced_from_line, safe_get
from helpers.files import upload_files_to_slack
from helpers.message_blocks import (
    add_copy_note,
    blocks_include_body,
    clamp_section_text_in_blocks,
    rewrite_attachments,
    rewrite_content_blocks,
    text_from_attachments,
    trim_target_blocks,
)
from helpers.pending_events import FILE_ECHO, MESSAGE_ECHO, PENDING_SHARE, remember_pending_event
from helpers.slack_api import delete_message, post_message, slack_error_code
from helpers.slack_ts import slack_message_ts
from helpers.user_map import (
    apply_mentioned_users,
    format_unmapped_author_label,
    get_display_name_and_icon_for_synced_message,
    parse_mentioned_users,
    resolve_channel_references,
    resolve_mention_for_workspace,
)
from helpers.workspace import get_bot_token
from logger import log_debug, log_error, log_info, log_warning

_NO_AUTHORIZE_ERRORS = frozenset({"invalid_auth", "not_authed", "token_revoked", "missing_scope", "account_inactive"})
_USER_WRITE_ERRORS = _NO_AUTHORIZE_ERRORS | frozenset({"not_in_channel", "channel_not_found", "restricted_action"})
# Reasons SyncBot gives itself. Any other failure without a Slack code is retried by the pipeline.
_WRITE_REASONS = frozenset({"bot_token_unavailable", "empty_message_create"})
_BLOCK_ERRORS = frozenset({"invalid_blocks", "invalid_blocks_format", "msg_blocks_too_long", "invalid_attachments"})


def _bot_token_for(workspace) -> str:
    token = get_bot_token(workspace)
    if not token:
        raise ValueError("bot_token_unavailable")
    return token


def pick_write_token(workspace, mapped_user_id: str | None) -> tuple[str, str | None]:
    """Return ``(token, posted_as_user_id)``. Null posted_as means bot customize."""
    if mapped_user_id:
        user_token = get_user_token(workspace.team_id, mapped_user_id)
        if user_token:
            return user_token, mapped_user_id
    return _bot_token_for(workspace), None


def _remember_message_echo(team_id: str | None, user_id: str | None, channel_id: str, ts: str | None) -> None:
    """SyncBot wrote *ts* as *user_id*: remember it, so the events that brings are skipped."""
    remember_pending_event(
        MESSAGE_ECHO, target_team_id=team_id, target_user_id=user_id, target_channel_id=channel_id, target_ts=ts
    )


def build_target_blocks(
    *,
    content_blocks: list[dict],
    photo_blocks: list[dict],
    source_client: WebClient,
    target_client: WebClient,
    source_workspace_id: int,
    target_workspace_id: int,
    source_ws,
    source_workspace_name: str | None,
    mentioned_users: list[dict],
) -> list[dict]:
    """Content blocks rewritten for the target, plus image blocks."""
    if not content_blocks:
        return list(photo_blocks or [])

    def rewrite_mrkdwn(text: str) -> str:
        adapted = resolve_channel_references(text, source_client, source_ws)

        def repl(match: re.Match) -> str:
            return resolve_mention_for_workspace(
                source_client,
                match.group(1),
                source_workspace_id,
                target_client,
                target_workspace_id,
            )

        return re.sub(r"<@(\w+)>", repl, adapted or "")

    resolved_by_uid: dict[str, str] = {}

    def map_user_id(uid: str) -> str | None:
        tag = resolve_mention_for_workspace(
            source_client,
            uid,
            source_workspace_id,
            target_client,
            target_workspace_id,
        )
        resolved_by_uid[uid] = tag
        m = re.fullmatch(r"<@(\w+)>", tag or "")
        return m.group(1) if m else None

    names = {u.get("user_id"): u.get("user_name") for u in mentioned_users or []}

    def unmapped_label(uid: str) -> str:
        tag = resolved_by_uid.get(uid)
        if tag and not re.fullmatch(r"<@\w+>", tag):
            return tag
        return format_unmapped_author_label(names.get(uid) or uid, source_workspace_name)

    rewritten = rewrite_content_blocks(content_blocks, rewrite_mrkdwn, map_user_id, unmapped_label)
    return trim_target_blocks(rewritten + (photo_blocks or []))


def _blocks_for_file_share(adapted_text: str, target_blocks: list[dict] | None) -> list[dict] | None:
    """Message body for a file share. Slack drops blocks when a comment is also set."""
    blocks = list(target_blocks or [])
    text = (adapted_text or "").strip()
    if text and not blocks_include_body(blocks):
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}] + blocks
    return blocks or None


def _upload_target_files(
    *,
    token: str,
    channel_id: str,
    files: list,
    initial_comment: str | None,
    thread_ts: str | None,
    reply_broadcast: bool,
    team_id: str | None,
    as_user: str | None,
    post_id: str | None = None,
    sync_channel_id: int | None = None,
    source_user_id: str | None = None,
    source_team_id: str | None = None,
    blocks: list[dict] | None = None,
    username: str | None = None,
    icon_url: str | None = None,
) -> str | None:
    """Upload files on the target; remember echo when posted as a mapped user."""

    uploaded_ids: list[str] = []

    def _remember_files(file_ids) -> None:
        uploaded_ids.extend(str(file_id) for file_id in file_ids if file_id)
        if as_user:
            for file_id in file_ids:
                if file_id:
                    remember_pending_event(
                        FILE_ECHO, target_team_id=team_id, target_user_id=as_user, file_id=str(file_id)
                    )

    def _remember_ts(ts: str) -> None:
        if as_user:
            _remember_message_echo(team_id, as_user, channel_id, ts)

    _, file_ts = upload_files_to_slack(
        bot_token=token,
        channel_id=channel_id,
        files=files,
        initial_comment=initial_comment,
        blocks=blocks,
        username=username,
        icon_url=icon_url,
        thread_ts=thread_ts,
        reply_broadcast=reply_broadcast,
        after_upload=_remember_files,
        after_share_ts=_remember_ts,
    )
    if not file_ts and post_id and team_id and sync_channel_id:
        for file_id in uploaded_ids:
            remember_pending_event(
                PENDING_SHARE,
                target_team_id=team_id,
                target_channel_id=channel_id,
                file_id=file_id,
                post_id=post_id,
                target_sync_channel_id=sync_channel_id,
                source_team_id=source_team_id,
                source_user_id=source_user_id,
                target_user_id=as_user,
            )
        if uploaded_ids:
            log_debug(
                "file_share_ts",
                channel_id=channel_id,
                ts=None,
                source="pending",
                post_id=post_id,
                file_id=uploaded_ids[0],
            )
    return file_ts


def _post_target_text(
    *,
    token: str,
    channel_id: str,
    adapted_text: str,
    target_blocks: list[dict] | None,
    attachments: list[dict] | None,
    thread_ts: str | None,
    reply_broadcast: bool,
    customize: bool,
    name_for_target: str,
    target_icon_url: str | None,
    user_avatar_url: str | None,
    remote_workspace_label: str | None,
    team_id: str | None,
    as_user: str | None,
    post_id: str | None = None,
) -> tuple[str | None, str | None]:
    """Post a target message that has no file."""
    post_kwargs: dict[str, Any] = {
        "bot_token": token,
        "channel_id": channel_id,
        "msg_text": adapted_text,
        "blocks": target_blocks or None,
        "thread_ts": thread_ts,
        "reply_broadcast": reply_broadcast,
        "post_id": post_id,
    }
    if attachments:
        post_kwargs["attachments"] = attachments
    if customize:
        post_kwargs["user_name"] = name_for_target
        post_kwargs["user_profile_url"] = target_icon_url or user_avatar_url
        post_kwargs["workspace_name"] = remote_workspace_label

    res = post_message(**post_kwargs)
    ts = safe_get(res, "ts")
    if as_user and ts:
        _remember_message_echo(team_id, as_user, channel_id, ts)
    return ts, as_user


def _mentioned_users_from_envelope(envelope: dict[str, Any]) -> list[dict]:
    """Mention rows from envelope ``people`` (author excluded)."""
    people = envelope.get("people") or []
    source_user_id = envelope.get("source_user_id")
    out: list[dict] = []
    for person in people:
        uid = person.get("user_id")
        if not uid or uid == source_user_id:
            continue
        out.append(
            {
                "user_id": uid,
                "user_name": person.get("name") or uid,
                "email": person.get("email"),
                "user_profile_url": person.get("avatar_url"),
            }
        )
    return out


def _build_target_attachments(
    envelope: dict[str, Any],
    *,
    source_client: WebClient | None,
    target_client: WebClient,
    source_workspace_id: int,
    target_workspace_id: int,
    source_ws,
    workspace_name: str | None,
    mentioned_users: list[dict],
) -> list[dict]:
    """Envelope attachments with the same rewrites as the message body.

    A peer's envelope was already rewritten on arrival (``_prepare_inbound_envelope``).
    """
    raw = [item for item in (envelope.get("attachments") or []) if isinstance(item, dict)]
    if not raw or not source_client:
        return [dict(item) for item in raw]

    def rewrite_mrkdwn(value: str) -> str:
        adapted = apply_mentioned_users(
            value,
            source_client,
            target_client,
            mentioned_users,
            source_workspace_id=source_workspace_id,
            target_workspace_id=target_workspace_id,
        )
        return resolve_channel_references(adapted, source_client, source_ws)

    def rewrite_blocks(blocks: list[dict]) -> list[dict]:
        return build_target_blocks(
            content_blocks=blocks,
            photo_blocks=[],
            source_client=source_client,
            target_client=target_client,
            source_workspace_id=source_workspace_id,
            target_workspace_id=target_workspace_id,
            source_ws=source_ws,
            source_workspace_name=workspace_name,
            mentioned_users=mentioned_users,
        )

    return rewrite_attachments(raw, rewrite_mrkdwn, rewrite_blocks)


def _build_upload_attachment_blocks(attachments: list[dict]) -> list[dict]:
    """Blocks for attachments on a file share. An upload takes ``blocks``, not ``attachments``.

    A card (blocks only) keeps its blocks. Other attachments become a section of their words.
    """
    blocks: list[dict] = []
    for attachment in attachments:
        nested = attachment.get("blocks")
        if isinstance(nested, list) and nested and set(attachment) <= {"blocks", "color", "fallback"}:
            blocks.extend(nested)
            continue
        words = text_from_attachments([attachment]).strip()
        if words:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": words}})
    return clamp_section_text_in_blocks(blocks)


def _build_refused_copy_text(text: str | None, attachments: list[dict]) -> str:
    """Text for a copy whose blocks or attachments Slack refused: their words, then the copy note."""
    parts = [(text or "").strip(), text_from_attachments(attachments).strip()]
    return add_copy_note("\n".join(part for part in parts if part), [])[0] or ""


def slack_write_create(
    *,
    envelope: dict[str, Any],
    sync_channel,
    workspace,
    source_client: WebClient | None = None,
    thread_ts: str | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Create a message on the target. Returns ``(ts, posted_as, error)``."""
    from helpers.workspace import get_workspace_by_id

    source_workspace_id = envelope.get("source_workspace_id") or 0
    source_user_id = envelope.get("source_user_id")
    user_name = envelope.get("user_name")
    user_avatar_url = envelope.get("user_avatar_url")
    workspace_name = envelope.get("workspace_name")
    msg_text = envelope.get("text") or ""
    reply_broadcast = bool(envelope.get("reply_broadcast"))
    file_refs = list(envelope.get("file_refs") or [])
    content_blocks = list(envelope.get("blocks") or [])
    images = list(envelope.get("images") or [])
    post_id = envelope.get("post_id")

    bot_token = _bot_token_for(workspace)
    target_client = WebClient(token=bot_token)
    mapped_user_id = envelope.get("mapped_user_id")
    if mapped_user_id:
        target_display_name, target_icon_url, author_is_mapped = user_name, user_avatar_url, True
    else:
        target_display_name, target_icon_url, author_is_mapped, mapped_user_id = (
            get_display_name_and_icon_for_synced_message(
                source_user_id or "",
                source_workspace_id,
                user_name,
                user_avatar_url,
                target_client,
                workspace.id,
                source_client=source_client,
            )
        )
    name_for_target = target_display_name or user_name or "Someone"
    remote_workspace_label = None if author_is_mapped else workspace_name

    write_token, posted_as = pick_write_token(workspace, mapped_user_id)
    use_customize = posted_as is None

    mentioned_users = _mentioned_users_from_envelope(envelope)
    if source_client and msg_text and "<@" in msg_text and not mentioned_users:
        mentioned_users = parse_mentioned_users(msg_text, source_client)

    adapted_text = msg_text
    source_ws = get_workspace_by_id(source_workspace_id) if source_workspace_id else None
    if source_client:
        adapted_text = apply_mentioned_users(
            msg_text,
            source_client,
            target_client,
            mentioned_users,
            source_workspace_id=source_workspace_id,
            target_workspace_id=workspace.id,
        )
        adapted_text = resolve_channel_references(adapted_text, source_client, source_ws)

    target_blocks: list[dict] = []
    if source_client and (content_blocks or images):
        target_blocks = build_target_blocks(
            content_blocks=content_blocks,
            photo_blocks=images,
            source_client=source_client,
            target_client=target_client,
            source_workspace_id=source_workspace_id,
            target_workspace_id=workspace.id,
            source_ws=source_ws,
            source_workspace_name=workspace_name,
            mentioned_users=mentioned_users,
        )
    elif content_blocks:
        target_blocks = trim_target_blocks(list(content_blocks) + list(images or []))
    elif images:
        target_blocks = list(images)

    adapted_attachments = _build_target_attachments(
        envelope,
        source_client=source_client,
        target_client=target_client,
        source_workspace_id=source_workspace_id,
        target_workspace_id=workspace.id,
        source_ws=source_ws,
        workspace_name=workspace_name,
        mentioned_users=mentioned_users,
    )
    refused_text = _build_refused_copy_text(adapted_text, adapted_attachments)

    pending_apply = {
        "sync_channel_id": sync_channel.id,
        "source_user_id": source_user_id or None,
        "source_team_id": envelope.get("source_team_id") or None,
    }

    def _write(
        token: str,
        customize: bool,
        as_user: str | None,
        *,
        blocks: list[dict] | None = None,
        use_attachments: bool = True,
        text: str | None = None,
    ) -> tuple[str | None, str | None]:
        use_blocks = target_blocks if blocks is None else blocks
        post_text = adapted_text if text is None else text
        post_attachments = adapted_attachments if use_attachments else []
        # The file share is the message. Blocks and a comment together drop the blocks.
        # Bot customize sets the from line on every share, including file-only.
        if file_refs:
            upload_attachment_blocks = _build_upload_attachment_blocks(post_attachments)
            if upload_attachment_blocks:
                share_blocks = trim_target_blocks(
                    (_blocks_for_file_share(post_text, use_blocks) or []) + upload_attachment_blocks
                )
            else:
                share_blocks = _blocks_for_file_share(post_text, use_blocks) if use_blocks else None
            file_ts = _upload_target_files(
                token=token,
                channel_id=sync_channel.channel_id,
                files=file_refs,
                initial_comment=None if share_blocks else ((post_text or "").strip() or None),
                blocks=share_blocks,
                username=format_synced_from_line(name_for_target, remote_workspace_label) if customize else None,
                icon_url=(target_icon_url or user_avatar_url) if customize else None,
                thread_ts=thread_ts,
                reply_broadcast=reply_broadcast,
                team_id=workspace.team_id,
                as_user=as_user,
                post_id=post_id,
                **pending_apply,
            )
            return file_ts, as_user
        if not (post_text or "").strip() and not use_blocks and not post_attachments:
            raise RuntimeError("empty_message_create")
        return _post_target_text(
            token=token,
            channel_id=sync_channel.channel_id,
            adapted_text=post_text,
            target_blocks=use_blocks,
            attachments=post_attachments,
            thread_ts=thread_ts,
            reply_broadcast=reply_broadcast,
            customize=customize,
            name_for_target=name_for_target,
            target_icon_url=target_icon_url,
            user_avatar_url=user_avatar_url,
            remote_workspace_label=remote_workspace_label,
            team_id=workspace.team_id,
            as_user=as_user,
            post_id=post_id,
        )

    def _with_error(pair: tuple[str | None, str | None]) -> tuple[str | None, str | None, str | None]:
        return pair[0], pair[1], None

    def _fail_create(exc: Exception) -> tuple[None, None, str]:
        code = slack_error_code(exc) or (str(exc) if str(exc) in _WRITE_REASONS else None)
        log_warning("slack_write_create_failed", channel_id=sync_channel.channel_id, error=code or str(exc))
        if code is None:
            # Not an answer from Slack (a timeout or a dropped connection): the pipeline tries again.
            raise exc
        if file_refs:
            log_error(
                "file_share_failed",
                reason="upload_failed",
                channel_id=sync_channel.channel_id,
                error=code,
                post_id=envelope.get("post_id"),
                thread_post_id=envelope.get("thread_post_id"),
            )
        return None, None, code

    try:
        return _with_error(_write(write_token, use_customize, posted_as))
    except SlackApiError as exc:
        code = slack_error_code(exc)
        # Slack refuses the whole message for one block or attachment it will not take.
        # Post the text, which already carries those words.
        if code in _BLOCK_ERRORS and (target_blocks or adapted_attachments):
            log_warning(
                "slack_write_blocks_refused",
                channel_id=sync_channel.channel_id,
                error=code,
                block_types=sorted({b.get("type") for b in target_blocks if isinstance(b, dict) and b.get("type")}),
            )
            try:
                ts, as_user = _write(
                    write_token, use_customize, posted_as, blocks=[], use_attachments=False, text=refused_text
                )
            except Exception as retry_exc:
                return _fail_create(retry_exc)
            # Posted, in a reduced form: the code tells the pipeline so the author hears about it.
            return ts, as_user, (code if ts else None)
        if posted_as and code in _USER_WRITE_ERRORS:
            log_info("slack_write_user_fallback_bot", channel_id=sync_channel.channel_id, error=code)
            try:
                return _with_error(_write(bot_token, True, None))
            except Exception as retry_exc:
                return _fail_create(retry_exc)
        return _fail_create(exc)
    except Exception as exc:
        return _fail_create(exc)


def slack_write_edit(
    *,
    envelope: dict[str, Any],
    sync_channel,
    workspace,
    target_post_meta,
    source_client: WebClient | None = None,
) -> tuple[bool, str | None]:
    """Edit an existing target message. Sticky token from posted_as_user_id.

    Returns ``(ok, error)``. A refused card retried as text is ``(True, code)``.
    """
    from helpers.workspace import get_workspace_by_id

    posted_as = getattr(target_post_meta, "posted_as_user_id", None)
    update_ts = slack_message_ts(target_post_meta.ts)
    msg_text = envelope.get("text") or ""
    content_blocks = list(envelope.get("blocks") or [])
    images = list(envelope.get("images") or [])
    source_workspace_id = envelope.get("source_workspace_id") or 0
    workspace_name = envelope.get("workspace_name")

    bot_token = _bot_token_for(workspace)
    target_client = WebClient(token=bot_token)
    token = bot_token
    used_user_token = False
    if posted_as:
        user_token = get_user_token(workspace.team_id, posted_as)
        if user_token:
            token = user_token
            used_user_token = True
        else:
            log_info("slack_write_user_fallback_bot", channel_id=sync_channel.channel_id, error="no_user_token")

    mentioned_users: list[dict] = []
    adapted_text = msg_text
    target_blocks: list[dict] = []
    source_ws = get_workspace_by_id(source_workspace_id) if source_workspace_id else None
    if source_client:
        mentioned_users = _mentioned_users_from_envelope(envelope)
        if msg_text and "<@" in msg_text and not mentioned_users:
            mentioned_users = parse_mentioned_users(msg_text, source_client)
        adapted_text = apply_mentioned_users(
            msg_text,
            source_client,
            target_client,
            mentioned_users,
            source_workspace_id=source_workspace_id,
            target_workspace_id=workspace.id,
        )
        adapted_text = resolve_channel_references(adapted_text, source_client, source_ws)
        if content_blocks or images:
            target_blocks = build_target_blocks(
                content_blocks=content_blocks,
                photo_blocks=images,
                source_client=source_client,
                target_client=target_client,
                source_workspace_id=source_workspace_id,
                target_workspace_id=workspace.id,
                source_ws=source_ws,
                source_workspace_name=workspace_name,
                mentioned_users=mentioned_users,
            )
    elif content_blocks:
        target_blocks = trim_target_blocks(list(content_blocks) + list(images or []))
    elif images:
        target_blocks = list(images)
    adapted_attachments = _build_target_attachments(
        envelope,
        source_client=source_client,
        target_client=target_client,
        source_workspace_id=source_workspace_id,
        target_workspace_id=workspace.id,
        source_ws=source_ws,
        workspace_name=workspace_name,
        mentioned_users=mentioned_users,
    )

    refused_text = _build_refused_copy_text(adapted_text, adapted_attachments)

    def _update(write_token: str, as_user: str | None, *, text_only: bool = False) -> bool:
        # Text only is the retry after Slack refused the blocks or attachments.
        use_blocks = [] if text_only else target_blocks
        post_message(
            bot_token=write_token,
            channel_id=sync_channel.channel_id,
            msg_text=refused_text if text_only else adapted_text,
            update_ts=update_ts,
            blocks=use_blocks or None,
            attachments=[] if text_only else adapted_attachments,
        )
        if as_user:
            _remember_message_echo(workspace.team_id, as_user, sync_channel.channel_id, update_ts)
        return True

    try:
        return _update(token, posted_as if used_user_token else None), None
    except SlackApiError as exc:
        code = slack_error_code(exc)
        if code in _BLOCK_ERRORS and (target_blocks or adapted_attachments):
            log_warning(
                "slack_write_blocks_refused",
                channel_id=sync_channel.channel_id,
                error=code,
                block_types=sorted({b.get("type") for b in target_blocks if isinstance(b, dict) and b.get("type")}),
            )
            try:
                return _update(token, posted_as if used_user_token else None, text_only=True), code
            except Exception as retry_exc:
                retry_code = slack_error_code(retry_exc)
                log_warning(
                    "slack_write_edit_failed",
                    channel_id=sync_channel.channel_id,
                    error=retry_code or str(retry_exc),
                )
                return False, retry_code or type(retry_exc).__name__
        if used_user_token and code in _USER_WRITE_ERRORS | {"cant_update_message"}:
            log_info("slack_write_user_fallback_bot", channel_id=sync_channel.channel_id, error=code)
            try:
                return _update(bot_token, None), None
            except SlackApiError as retry_exc:
                retry_code = slack_error_code(retry_exc)
                log_warning(
                    "slack_write_edit_failed", channel_id=sync_channel.channel_id, error=retry_code or str(retry_exc)
                )
                return False, retry_code or "slack_error"
            except Exception as retry_exc:
                log_warning("slack_write_edit_failed", channel_id=sync_channel.channel_id, error=str(retry_exc))
                return False, type(retry_exc).__name__
        log_warning("slack_write_edit_failed", channel_id=sync_channel.channel_id, error=code or str(exc))
        return False, code or "slack_error"
    except Exception as exc:
        log_warning("slack_write_edit_failed", channel_id=sync_channel.channel_id, error=str(exc))
        return False, type(exc).__name__


def slack_write_delete(*, sync_channel, workspace, target_post_meta) -> tuple[bool, str | None]:
    """Delete a target message. Sticky token from posted_as_user_id.

    Returns ``(ok, error)``. A copy that is already gone is ``(True, None)``.
    """
    posted_as = getattr(target_post_meta, "posted_as_user_id", None)
    ts = slack_message_ts(target_post_meta.ts)
    bot_token = _bot_token_for(workspace)
    token = bot_token
    if posted_as:
        user_token = get_user_token(workspace.team_id, posted_as)
        if user_token:
            token = user_token
        else:
            log_warning(
                "slack_write_delete_skip_no_user_token", channel_id=sync_channel.channel_id, posted_as=posted_as
            )
            return False, "no_user_token"
    try:
        delete_message(bot_token=token, channel_id=sync_channel.channel_id, ts=ts)
        if posted_as:
            _remember_message_echo(workspace.team_id, posted_as, sync_channel.channel_id, ts)
        return True, None
    except Exception as exc:
        code = slack_error_code(exc)
        if code == "message_not_found":
            return True, None
        log_warning("slack_write_delete_failed", channel_id=sync_channel.channel_id, error=code or str(exc))
        return False, code or type(exc).__name__
