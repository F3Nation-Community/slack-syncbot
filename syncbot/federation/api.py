"""Federation API request handlers.

These handlers process incoming HTTP requests from remote SyncBot instances.
They are called by the federation HTTP server (local dev) or the Lambda
handler (production) and return ``(status_code, response_dict)`` tuples.

All federation endpoints require the ``SyncBot-Federation`` User-Agent;
requests without it receive an opaque 404, making the endpoints invisible
to scanners.

Endpoints:

* ``POST /api/federation/pair``     -- Accept an incoming connection request
* ``POST /api/federation/message``  -- Receive a forwarded message
* ``POST /api/federation/message/edit``   -- Receive a message edit
* ``POST /api/federation/message/delete`` -- Receive a message delete
* ``POST /api/federation/message/react``  -- Receive a reaction
* ``POST /api/federation/users``    -- Exchange user directory
* ``GET  /api/federation/ping``     -- Health check
"""

import json
import logging
import re
from datetime import UTC, datetime

from slack_sdk.web import WebClient

import constants
import helpers
from db import DbManager, schemas
from federation import core as federation

_logger = logging.getLogger(__name__)

_NOT_FOUND = (404, {"message": "Not Found"})


def _find_post_records(post_id: str, sync_channel_id: int) -> list[schemas.PostMeta]:
    """Look up PostMeta records for a given post_id + sync channel."""
    pid = post_id if isinstance(post_id, bytes) else post_id.encode()[:100]
    return DbManager.find_records(
        schemas.PostMeta,
        [schemas.PostMeta.post_id == pid, schemas.PostMeta.sync_channel_id == sync_channel_id],
    )


_PAIRING_CODE_RE = re.compile(r"^FED-[0-9A-Fa-f]{8}$")

_FIELD_MAX_LENGTHS = {
    "channel_id": 20,
    "text": 40_000,
    "post_id": 100,
    "reaction": 100,
    "instance_id": 64,
    "webhook_url": 500,
    "code": 20,
    "action": 10,
}


# ---------------------------------------------------------------------------
# Input validation helper
# ---------------------------------------------------------------------------


def _validate_fields(body: dict, required: list[str], extras: list[str] | None = None) -> str | None:
    """Check required fields are present, non-empty, and within length limits.

    Returns an error string on failure, or *None* if valid.
    """
    for field in required:
        val = body.get(field)
        if val is None or (isinstance(val, str) and not val.strip()):
            return f"missing_{field}"

    for field in required + (extras or []):
        val = body.get(field)
        max_len = _FIELD_MAX_LENGTHS.get(field)
        if max_len and isinstance(val, str) and len(val) > max_len:
            return f"{field}_too_long"

    return None


def _pick_user_mapping_for_federated_target(
    source_user_id: str, target_workspace_id: int
) -> schemas.UserMapping | None:
    maps = DbManager.find_records(
        schemas.UserMapping,
        [
            schemas.UserMapping.target_workspace_id == target_workspace_id,
            schemas.UserMapping.source_user_id == source_user_id,
        ],
    )
    if not maps:
        return None
    for m in maps:
        if m.target_user_id:
            return m
    return maps[0]


def _ensure_federated_author_mapped(
    source_user_id: str,
    target_workspace_id: int,
    target_client: WebClient | None,
) -> str | None:
    """On-the-fly email map for a federated author using local directory email only."""
    mapping = _pick_user_mapping_for_federated_target(source_user_id, target_workspace_id)
    method = getattr(mapping, "match_method", None) if mapping else None
    if mapping and mapping.target_user_id and method != "none":
        return mapping.target_user_id

    try:
        dir_rows = DbManager.find_records(
            schemas.UserDirectory,
            [
                schemas.UserDirectory.slack_user_id == source_user_id,
                schemas.UserDirectory.deleted_at.is_(None),
            ],
        )
        source_workspace_id = None
        for row in dir_rows:
            if row.email and str(row.email).strip():
                source_workspace_id = row.workspace_id
                break
        if not source_workspace_id or target_client is None:
            return None

        return helpers.ensure_mapped_target_user_id(
            source_user_id,
            source_workspace_id,
            target_workspace_id,
            source_client=None,
            target_client=target_client,
        )
    except Exception:
        _logger.debug(
            "federation_author_map_failed",
            extra={"source_user_id": source_user_id, "target_workspace_id": target_workspace_id},
        )
        return None


def _resolve_mentions_for_federated(msg_text: str, target_workspace_id: int, remote_workspace_label: str) -> str:
    """Replace ``<@U_REMOTE>`` with native local mentions using *UserMapping* / *UserDirectory* on this instance."""
    if not msg_text:
        return msg_text

    user_ids = re.findall(r"<@(\w+)>", msg_text)
    if not user_ids:
        return msg_text

    for uid in dict.fromkeys(user_ids):
        mapping = _pick_user_mapping_for_federated_target(uid, target_workspace_id)
        if mapping and mapping.target_user_id:
            rep = f"<@{mapping.target_user_id}>"
        elif mapping and mapping.source_display_name:
            rep = f"`[@{mapping.source_display_name} ({remote_workspace_label})]`"
        else:
            display: str | None = None
            for entry in DbManager.find_records(
                schemas.UserDirectory,
                [schemas.UserDirectory.slack_user_id == uid, schemas.UserDirectory.deleted_at.is_(None)],
            ):
                display = entry.display_name or entry.real_name
                if display:
                    break
            if display:
                rep = f"`[@{display} ({remote_workspace_label})]`"
            else:
                rep = f"`[@{uid} ({remote_workspace_label})]`"
        msg_text = re.sub(rf"<@{re.escape(uid)}>", rep, msg_text)

    return msg_text


# ---------------------------------------------------------------------------
# Authentication helpers
# ---------------------------------------------------------------------------


def _has_federation_user_agent(headers: dict) -> bool:
    ua = headers.get("User-Agent", "") or headers.get("user-agent", "")
    return "SyncBot-Federation" in ua


def _verify_federated_request(body_str: str, headers: dict) -> schemas.FederatedWorkspace | None:
    """Verify the Ed25519 signature on an incoming federation request.

    Returns the :class:`FederatedWorkspace` record if valid, or *None*.
    """
    sig = headers.get("X-Federation-Signature", "")
    ts = headers.get("X-Federation-Timestamp", "")
    instance_id = headers.get("X-Federation-Instance", "")

    if not sig or not ts or not instance_id:
        return None

    matches = DbManager.find_records(
        schemas.FederatedWorkspace,
        [schemas.FederatedWorkspace.instance_id == instance_id],
    )
    fed_ws = matches[0] if matches else None
    if not fed_ws or fed_ws.status != "active":
        return None

    if not federation.federation_verify(body_str, sig, ts, fed_ws.public_key):
        _logger.warning(
            "federation_auth_failed — remote workspace may have regenerated its keypair; reconnection required",
            extra={"instance_id": instance_id},
        )
        return None

    return fed_ws


# ---------------------------------------------------------------------------
# Channel access scoping
# ---------------------------------------------------------------------------


def _federated_has_channel_access(fed_ws: schemas.FederatedWorkspace, sync_channel: schemas.SyncChannel) -> bool:
    """Return *True* if *fed_ws* is authorised to interact with *sync_channel*.

    The federated workspace must be linked to the sync's group via a
    WorkspaceGroupMember whose ``federated_workspace_id`` matches.
    """
    sync = DbManager.get_record(schemas.Sync, id=sync_channel.sync_id)
    if not sync or not sync.group_id:
        return False
    fed_members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == sync.group_id,
            schemas.WorkspaceGroupMember.federated_workspace_id == fed_ws.id,
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    return bool(fed_members)


def _resolve_channel_for_federated(
    channel_id: str,
    fed_ws: schemas.FederatedWorkspace,
    *,
    require_active: bool = False,
) -> tuple[schemas.SyncChannel, schemas.Workspace] | None:
    """Look up a sync channel, verify federated access, and return the workspace.

    Returns ``(sync_channel, workspace)`` or *None* if any check fails.
    """
    filters = [
        schemas.SyncChannel.channel_id == channel_id,
        schemas.SyncChannel.deleted_at.is_(None),
    ]
    if require_active:
        filters.append(schemas.SyncChannel.status == "active")

    records = DbManager.find_records(schemas.SyncChannel, filters)
    if not records:
        return None

    sync_channel = records[0]
    if not _federated_has_channel_access(fed_ws, sync_channel):
        return None

    workspace = helpers.get_workspace_by_id(sync_channel.workspace_id)
    if not workspace or not workspace.bot_token:
        return None

    return sync_channel, workspace


def _get_local_workspace_ids(fed_ws: schemas.FederatedWorkspace) -> set[int]:
    """Return local workspace IDs that participate in groups shared with *fed_ws*."""
    fed_members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.federated_workspace_id == fed_ws.id,
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    ws_ids: set[int] = set()
    for fed_member in fed_members:
        group_members = DbManager.find_records(
            schemas.WorkspaceGroupMember,
            [
                schemas.WorkspaceGroupMember.group_id == fed_member.group_id,
                schemas.WorkspaceGroupMember.workspace_id.isnot(None),
                schemas.WorkspaceGroupMember.status == "active",
                schemas.WorkspaceGroupMember.deleted_at.is_(None),
            ],
        )
        for m in group_members:
            if m.workspace_id:
                ws_ids.add(m.workspace_id)
    return ws_ids


# ---------------------------------------------------------------------------
# POST /api/federation/pair
# ---------------------------------------------------------------------------


def handle_pair(body: dict, body_str: str, headers: dict) -> tuple[int, dict]:
    """Accept an incoming connection request from a remote instance.

    The remote instance sends its ``code``, ``webhook_url``, ``instance_id``,
    and ``public_key``.  The request must be signed with the sender's private
    key so we can verify it matches the included public key.
    """
    err = _validate_fields(body, ["code", "webhook_url", "instance_id", "public_key"])
    if err:
        return 400, {"error": err}

    code = body["code"]
    remote_url = body["webhook_url"]
    remote_instance_id = body["instance_id"]
    remote_public_key = body["public_key"]

    if not _PAIRING_CODE_RE.match(code):
        return 400, {"error": "invalid_code_format"}

    if not federation.validate_webhook_url(remote_url):
        return 400, {"error": "invalid_webhook_url"}

    sig = headers.get("X-Federation-Signature", "")
    ts = headers.get("X-Federation-Timestamp", "")
    if not sig or not ts:
        return 401, {"error": "missing_signature"}

    if not federation.federation_verify(body_str, sig, ts, remote_public_key):
        return 401, {"error": "invalid_signature"}

    groups = DbManager.find_records(
        schemas.WorkspaceGroup,
        [schemas.WorkspaceGroup.invite_code == code, schemas.WorkspaceGroup.status == "active"],
    )
    if not groups:
        return _NOT_FOUND
    group = groups[0]

    existing_fed = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group.id,
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    for m in existing_fed:
        if m.federated_workspace_id:
            fed_ws_check = DbManager.get_record(schemas.FederatedWorkspace, id=m.federated_workspace_id)
            if fed_ws_check and fed_ws_check.instance_id == remote_instance_id:
                return 409, {"error": "already_connected"}

    fed_ws_name = f"Connection {remote_instance_id[:8]}"
    _team_id = body.get("team_id")
    primary_team_id = _team_id.strip() if isinstance(_team_id, str) and _team_id.strip() else None
    primary_workspace_name = body.get("workspace_name") if isinstance(body.get("workspace_name"), str) else None

    fed_ws = federation.get_or_create_federated_workspace(
        instance_id=remote_instance_id,
        webhook_url=remote_url,
        public_key=remote_public_key,
        name=fed_ws_name,
        primary_team_id=primary_team_id,
        primary_workspace_name=primary_workspace_name,
    )

    now = datetime.now(UTC)
    member = schemas.WorkspaceGroupMember(
        group_id=group.id,
        federated_workspace_id=fed_ws.id,
        status="active",
        role="member",
        joined_at=now,
    )
    DbManager.create_record(member)

    # Instance A detection: if the connecting side sent team_id, soft-delete the matching local workspace
    if primary_team_id:
        local_workspaces = DbManager.find_records(
            schemas.Workspace,
            [schemas.Workspace.team_id == primary_team_id],
        )
        if local_workspaces:
            local_ws = local_workspaces[0]
            DbManager.update_records(
                schemas.Workspace,
                [schemas.Workspace.id == local_ws.id],
                {schemas.Workspace.deleted_at: now},
            )
            _logger.info(
                "federation_local_workspace_soft_deleted",
                extra={"team_id": primary_team_id, "workspace_id": local_ws.id},
            )

    _, our_public_key = federation.get_or_create_instance_keypair()

    _logger.info(
        "federation_connection_accepted",
        extra={
            "group_id": group.id,
            "remote_instance": remote_instance_id,
        },
    )

    return 200, {
        "ok": True,
        "instance_id": federation.get_instance_id(),
        "public_key": our_public_key,
        "group_id": group.id,
    }


# ---------------------------------------------------------------------------
# POST /api/federation/message
# ---------------------------------------------------------------------------


def handle_message(body: dict, fed_ws: schemas.FederatedWorkspace) -> tuple[int, dict]:
    """Receive and post a forwarded message from a federated workspace."""
    err = _validate_fields(body, ["channel_id"], extras=["text", "post_id"])
    if err:
        return 400, {"error": err}

    channel_id = body["channel_id"]
    text = body.get("text", "")
    user = body.get("user", {})
    post_id = body.get("post_id", "")
    thread_post_id = body.get("thread_post_id")
    images = body.get("images", [])[:10]

    resolved = _resolve_channel_for_federated(channel_id, fed_ws, require_active=True)
    if not resolved:
        return _NOT_FOUND
    sync_channel, workspace = resolved

    user_name = user.get("display_name", "Remote User")
    user_avatar = user.get("avatar_url")
    workspace_name = user.get("workspace_name", "Remote")
    remote_label_for_mentions = workspace_name

    bot_token = helpers.decrypt_bot_token(workspace.bot_token)
    ws_client = WebClient(token=bot_token)

    source_user_id = user.get("user_id")
    if source_user_id:
        mapped_local = _ensure_federated_author_mapped(source_user_id, workspace.id, ws_client)
        if mapped_local:
            local_name, local_icon = helpers.get_user_info(ws_client, mapped_local)
            if local_name:
                user_name = helpers.normalize_display_name(local_name)
                user_avatar = local_icon or user_avatar
                workspace_name = None

    text = _resolve_mentions_for_federated(text, workspace.id, remote_label_for_mentions)
    text = helpers.resolve_channel_references(text, ws_client, None, target_workspace_id=workspace.id)

    try:
        thread_ts = None
        if thread_post_id:
            post_records = DbManager.find_records(
                schemas.PostMeta,
                [
                    schemas.PostMeta.post_id == thread_post_id,
                    schemas.PostMeta.sync_channel_id == sync_channel.id,
                ],
            )
            if post_records:
                thread_ts = str(post_records[0].ts)

        photo_blocks = []
        if images:
            for img in images:
                photo_blocks.append(
                    {
                        "type": "image",
                        "image_url": img.get("url", ""),
                        "alt_text": img.get("alt_text", "Shared image"),
                    }
                )

        res = helpers.post_message(
            bot_token=bot_token,
            channel_id=channel_id,
            msg_text=text,
            user_name=user_name,
            user_profile_url=user_avatar,
            workspace_name=workspace_name,
            blocks=photo_blocks if photo_blocks else None,
            thread_ts=thread_ts,
        )

        ts = helpers.safe_get(res, "ts")

        if post_id and ts:
            post_meta = schemas.PostMeta(
                post_id=post_id if isinstance(post_id, bytes) else post_id.encode()[:100],
                sync_channel_id=sync_channel.id,
                ts=float(ts),
            )
            DbManager.create_record(post_meta)

        _logger.info(
            "federation_message_received",
            extra={"channel_id": channel_id, "remote": fed_ws.instance_id},
        )

        return 200, {"ok": True, "ts": ts}

    except Exception:
        _logger.exception("federation_message_error", extra={"channel_id": channel_id})
        return 500, {"error": "internal_error"}


# ---------------------------------------------------------------------------
# POST /api/federation/message/edit
# ---------------------------------------------------------------------------


def handle_message_edit(body: dict, fed_ws: schemas.FederatedWorkspace) -> tuple[int, dict]:
    """Receive and apply a message edit from a federated workspace."""
    err = _validate_fields(body, ["post_id", "channel_id"], extras=["text"])
    if err:
        return 400, {"error": err}

    post_id = body["post_id"]
    text = body.get("text", "")
    channel_id = body["channel_id"]

    resolved = _resolve_channel_for_federated(channel_id, fed_ws)
    if not resolved:
        return _NOT_FOUND
    sync_channel, workspace = resolved

    remote_label = fed_ws.primary_workspace_name or fed_ws.name or "Remote"
    text = _resolve_mentions_for_federated(text, workspace.id, remote_label)
    ws_client = WebClient(token=helpers.decrypt_bot_token(workspace.bot_token))
    text = helpers.resolve_channel_references(text, ws_client, None, target_workspace_id=workspace.id)

    post_records = _find_post_records(post_id, sync_channel.id)

    updated = 0
    for post_meta in post_records:
        try:
            ws_client.chat_update(channel=channel_id, ts=str(post_meta.ts), text=text)
            updated += 1
        except Exception:
            _logger.warning("federation_edit_failed", extra={"channel_id": channel_id, "ts": str(post_meta.ts)})

    return 200, {"ok": True, "updated": updated}


# ---------------------------------------------------------------------------
# POST /api/federation/message/delete
# ---------------------------------------------------------------------------


def handle_message_delete(body: dict, fed_ws: schemas.FederatedWorkspace) -> tuple[int, dict]:
    """Receive and apply a message deletion from a federated workspace."""
    err = _validate_fields(body, ["post_id", "channel_id"])
    if err:
        return 400, {"error": err}

    post_id = body["post_id"]
    channel_id = body["channel_id"]

    resolved = _resolve_channel_for_federated(channel_id, fed_ws)
    if not resolved:
        return _NOT_FOUND
    sync_channel, workspace = resolved

    post_records = _find_post_records(post_id, sync_channel.id)

    deleted = 0
    ws_client = WebClient(token=helpers.decrypt_bot_token(workspace.bot_token))
    for post_meta in post_records:
        try:
            ws_client.chat_delete(channel=channel_id, ts=str(post_meta.ts))
            deleted += 1
        except Exception:
            _logger.warning("federation_delete_failed", extra={"channel_id": channel_id, "ts": str(post_meta.ts)})

    return 200, {"ok": True, "deleted": deleted}


# ---------------------------------------------------------------------------
# POST /api/federation/message/react
# ---------------------------------------------------------------------------


def handle_message_react(body: dict, fed_ws: schemas.FederatedWorkspace) -> tuple[int, dict]:
    """Receive and apply a reaction add/remove from a federated workspace."""
    from helpers.reactions import apply_reaction_to_target, channel_receives_reactions

    err = _validate_fields(body, ["post_id", "channel_id", "reaction"], extras=["action"])
    if err:
        return 400, {"error": err}

    post_id = body["post_id"]
    channel_id = body["channel_id"]
    reaction = body["reaction"]
    action = body.get("action", "add")
    user_name = body.get("user_name") or "Remote User"
    user_avatar_url = body.get("user_avatar_url")
    workspace_name = body.get("workspace_name") or "Remote"

    resolved = _resolve_channel_for_federated(channel_id, fed_ws)
    if not resolved:
        return _NOT_FOUND
    sync_channel, workspace = resolved

    if not channel_receives_reactions(sync_channel):
        return 200, {"ok": True, "applied": 0}

    post_records = _find_post_records(post_id, sync_channel.id)
    source_user_id = body.get("user_id")
    mapped_local = None
    if source_user_id:
        dest_client = None
        try:
            dest_client = WebClient(token=helpers.decrypt_bot_token(workspace.bot_token))
        except Exception:
            _logger.debug(
                "federation_react_dest_client_failed",
                extra={"workspace_id": workspace.id},
            )
        mapped_local = _ensure_federated_author_mapped(source_user_id, workspace.id, dest_client)
        if mapped_local and dest_client is not None:
            local_name, local_icon = helpers.get_user_info(dest_client, mapped_local)
            if local_name:
                user_name = helpers.normalize_display_name(local_name)
                user_avatar_url = local_icon or user_avatar_url
                workspace_name = None

    applied = 0
    synthetic_source = schemas.SyncChannel(reaction_direction=constants.REACTION_DIRECTION_SEND)
    name_probe_cache: dict[tuple[str, str], bool] = {}
    notice_rows: list[schemas.PostMeta] = []

    for post_meta in post_records:
        try:
            result, notice = apply_reaction_to_target(
                action=action,
                reaction=reaction,
                source_user_id=source_user_id,
                source_workspace_id=None,
                source_sync_channel=synthetic_source,
                target_post_meta=post_meta,
                target_sync_channel=sync_channel,
                target_workspace=workspace,
                display_name=user_name,
                icon_url=user_avatar_url,
                posted_from=f"({workspace_name})" if workspace_name else "",
                author_is_mapped=bool(mapped_local),
                mapped_user_id=mapped_local,
                name_probe_cache=name_probe_cache,
                federated_instance_id=fed_ws.instance_id,
                event_workspace_id=workspace.id,
            )
            if notice:
                notice_rows.append(notice)
            if result in ("direct", "thread"):
                applied += 1
        except Exception:
            _logger.warning("federation_react_failed", extra={"channel_id": channel_id, "ts": str(post_meta.ts)})

    if notice_rows:
        DbManager.create_records(notice_rows)

    return 200, {"ok": True, "applied": applied}


# ---------------------------------------------------------------------------
# POST /api/federation/users
# ---------------------------------------------------------------------------


def handle_users(body: dict, fed_ws: schemas.FederatedWorkspace) -> tuple[int, dict]:
    """Exchange user directory with a federated workspace.

    Only returns users from workspaces that share groups with this federated workspace.
    """
    remote_users = body.get("users", [])[:5000]
    workspace_id = body.get("workspace_id")

    if remote_users and workspace_id:
        now = datetime.now(UTC)
        for u in remote_users:
            existing = DbManager.find_records(
                schemas.UserDirectory,
                [
                    schemas.UserDirectory.workspace_id == workspace_id,
                    schemas.UserDirectory.slack_user_id == u.get("user_id", ""),
                ],
            )
            if existing:
                DbManager.update_records(
                    schemas.UserDirectory,
                    [schemas.UserDirectory.id == existing[0].id],
                    {
                        schemas.UserDirectory.email: u.get("email"),
                        schemas.UserDirectory.real_name: u.get("real_name"),
                        schemas.UserDirectory.display_name: u.get("display_name"),
                        schemas.UserDirectory.updated_at: now,
                    },
                )
            else:
                record = schemas.UserDirectory(
                    workspace_id=workspace_id,
                    slack_user_id=u.get("user_id", ""),
                    email=u.get("email"),
                    real_name=u.get("real_name"),
                    display_name=u.get("display_name"),
                    updated_at=now,
                )
                DbManager.create_record(record)

        _logger.info(
            "federation_users_received",
            extra={"remote": fed_ws.instance_id, "count": len(remote_users)},
        )

    allowed_ws_ids = _get_local_workspace_ids(fed_ws)

    local_users = []
    for ws_id in allowed_ws_ids:
        ws = helpers.get_workspace_by_id(ws_id)
        if not ws or ws.deleted_at:
            continue
        users = DbManager.find_records(
            schemas.UserDirectory,
            [schemas.UserDirectory.workspace_id == ws_id, schemas.UserDirectory.deleted_at.is_(None)],
        )
        for u in users:
            local_users.append(
                {
                    "user_id": u.slack_user_id,
                    "email": u.email,
                    "real_name": u.real_name,
                    "display_name": u.display_name,
                    "workspace_id": ws_id,
                }
            )

    return 200, {"ok": True, "users": local_users}


# ---------------------------------------------------------------------------
# GET /api/federation/ping
# ---------------------------------------------------------------------------


def handle_ping() -> tuple[int, dict]:
    """Health check -- returns instance identity."""
    return 200, {
        "ok": True,
        "instance_id": federation.get_instance_id(),
        "timestamp": datetime.now(UTC).isoformat(),
    }


# ---------------------------------------------------------------------------
# Request dispatcher
# ---------------------------------------------------------------------------


def dispatch_federation_request(method: str, path: str, body_str: str, headers: dict) -> tuple[int, dict]:
    """Route an incoming federation HTTP request to the appropriate handler.

    Returns ``(status_code, response_dict)``.

    Requests without the ``SyncBot-Federation`` User-Agent receive a plain
    404 identical to Lambda Function URL's response for non-existent paths.
    """
    if not _has_federation_user_agent(headers):
        return _NOT_FOUND

    if path == "/api/federation/ping" and method == "GET":
        return handle_ping()

    if not helpers.federation_enabled():
        return _NOT_FOUND

    if method != "POST":
        return _NOT_FOUND

    try:
        body = json.loads(body_str) if body_str else {}
    except json.JSONDecodeError:
        return 400, {"error": "invalid_json"}

    if path == "/api/federation/pair":
        return handle_pair(body, body_str, headers)

    fed_ws = _verify_federated_request(body_str, headers)
    if not fed_ws:
        return _NOT_FOUND

    if path == "/api/federation/message":
        return handle_message(body, fed_ws)
    elif path == "/api/federation/message/edit":
        return handle_message_edit(body, fed_ws)
    elif path == "/api/federation/message/delete":
        return handle_message_delete(body, fed_ws)
    elif path == "/api/federation/message/react":
        return handle_message_react(body, fed_ws)
    elif path == "/api/federation/users":
        return handle_users(body, fed_ws)

    return _NOT_FOUND
