"""Cross-workspace user matching and mention resolution."""

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

import constants
from db import DbManager, schemas
from helpers._cache import _CACHE, _USER_INFO_CACHE_TTL, _cache_get, _cache_set
from helpers.core import safe_get
from helpers.encryption import decrypt_bot_token
from helpers.slack_api import _users_info, get_user_info, slack_retry
from helpers.workspace import (
    get_workspace_by_id,
    resolve_workspace_name,
)

_logger = logging.getLogger(__name__)


def _get_user_profile(client: WebClient, user_id: str) -> dict[str, Any] | None:
    """Fetch a single user's profile with caching and retry."""
    cache_key = f"user_profile:{user_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        res = _users_info(client, user_id)
    except SlackApiError as exc:
        _logger.warning(f"Failed to look up user {user_id}: {exc}")
        return None

    profile = safe_get(res, "user", "profile") or {}
    user_name = profile.get("display_name") or profile.get("real_name") or user_id
    email = profile.get("email")

    result: dict[str, Any] = {"user_name": user_name, "email": email}
    _cache_set(cache_key, result, ttl=_USER_INFO_CACHE_TTL)
    return result


def _normalize_name(display_name: str) -> str:
    """Trim trailing title/qualifier from a display name (e.g. drop text in parens or after dash)."""
    name = re.split(r"\s+[\(\-]", display_name or "")[0]
    return name.strip()


def normalize_display_name(name: str | None) -> str:
    """Return display name with trailing paren/dash qualifiers stripped; fallback to original if empty."""
    if not name:
        return name or ""
    n = _normalize_name(name)
    return n if n else name


def _match_ttl(method: str) -> int:
    """Return the TTL in seconds for a given match method."""
    if method == "manual":
        return 0
    if method == "email":
        return constants.MATCH_TTL_EMAIL
    if method == "name":
        return constants.MATCH_TTL_NAME
    return constants.MATCH_TTL_NONE


def _is_mapping_fresh(mapping: schemas.UserMapping) -> bool:
    """Return True if a cached mapping is still within its TTL."""
    if mapping.match_method == "manual":
        return True
    ttl = _match_ttl(mapping.match_method)
    age = (datetime.now(UTC) - mapping.matched_at.replace(tzinfo=UTC)).total_seconds()
    return age < ttl


@slack_retry
def _users_list_page(client: WebClient, cursor: str = "") -> dict:
    """Fetch one page of users.list (with retry on rate-limit)."""
    return client.users_list(limit=200, cursor=cursor)


def _refresh_user_directory(client: WebClient, workspace_id: int) -> None:
    """Crawl users.list for a workspace and upsert into user_directory.

    Active users are upserted in bulk. Deactivated users are soft-deleted.
    Users that disappeared from ``users.list`` are hard-deleted with their mappings.
    Cache ``dir_refresh:`` only after a complete crawl.
    """
    cache_key = f"dir_refresh:{workspace_id}"
    if _cache_get(cache_key):
        _logger.debug("user_directory_refresh_cached", extra={"workspace_id": workspace_id})
        return

    _logger.debug("user_directory_refresh_start", extra={"workspace_id": workspace_id})
    cursor = ""
    seen_user_ids: set[str] = set()
    members_by_id: dict[str, dict] = {}

    while True:
        res = _users_list_page(client, cursor=cursor)
        members = safe_get(res, "members") or []

        for member in members:
            if member.get("is_bot") or member.get("id") == "USLACKBOT":
                continue
            uid = member["id"]
            seen_user_ids.add(uid)
            members_by_id[uid] = member

        cursor = safe_get(res, "response_metadata", "next_cursor") or ""
        if not cursor:
            break

    existing_entries = DbManager.find_records(
        schemas.UserDirectory,
        [schemas.UserDirectory.workspace_id == workspace_id],
    )
    existing_by_uid = {e.slack_user_id: e for e in existing_entries}
    now = datetime.now(UTC)
    to_create: list[schemas.UserDirectory] = []

    for uid, member in members_by_id.items():
        profile = member.get("profile", {}) or {}
        display_name = profile.get("display_name") or ""
        real_name = profile.get("real_name") or ""
        email = profile.get("email")
        is_deleted = member.get("deleted", False)
        normalized = _normalize_name(display_name) if display_name else _normalize_name(real_name)
        current_name = display_name or real_name
        existing = existing_by_uid.get(uid)

        if is_deleted:
            if existing and existing.deleted_at is None:
                DbManager.update_records(
                    schemas.UserDirectory,
                    [schemas.UserDirectory.id == existing.id],
                    {schemas.UserDirectory.deleted_at: now, schemas.UserDirectory.updated_at: now},
                )
            _purge_mappings_for_user(uid, workspace_id)
            _CACHE.pop(f"user_info:{uid}", None)
            continue

        if existing:
            changed = (
                existing.email != email
                or existing.real_name != real_name
                or existing.display_name != display_name
                or existing.normalized_name != normalized
                or existing.deleted_at is not None
            )
            if changed:
                DbManager.update_records(
                    schemas.UserDirectory,
                    [schemas.UserDirectory.id == existing.id],
                    {
                        schemas.UserDirectory.email: email,
                        schemas.UserDirectory.real_name: real_name,
                        schemas.UserDirectory.display_name: display_name,
                        schemas.UserDirectory.normalized_name: normalized,
                        schemas.UserDirectory.updated_at: now,
                        schemas.UserDirectory.deleted_at: None,
                    },
                )
            if current_name:
                mappings = DbManager.find_records(
                    schemas.UserMapping,
                    [
                        schemas.UserMapping.source_workspace_id == workspace_id,
                        schemas.UserMapping.source_user_id == uid,
                    ],
                )
                for m in mappings:
                    if m.source_display_name != current_name:
                        DbManager.update_records(
                            schemas.UserMapping,
                            [schemas.UserMapping.id == m.id],
                            {schemas.UserMapping.source_display_name: current_name},
                        )
            _CACHE.pop(f"user_info:{uid}", None)
        else:
            to_create.append(
                schemas.UserDirectory(
                    workspace_id=workspace_id,
                    slack_user_id=uid,
                    email=email,
                    real_name=real_name,
                    display_name=display_name,
                    normalized_name=normalized,
                    updated_at=now,
                )
            )
            _CACHE.pop(f"user_info:{uid}", None)

    if to_create:
        DbManager.create_records(to_create)

    if seen_user_ids:
        for entry in existing_entries:
            if entry.slack_user_id not in seen_user_ids:
                _purge_mappings_for_user(entry.slack_user_id, workspace_id)
                DbManager.delete_records(
                    schemas.UserDirectory,
                    [schemas.UserDirectory.id == entry.id],
                )

    count = len(seen_user_ids)
    _logger.info("user_directory_refresh_done", extra={"workspace_id": workspace_id, "count": count})
    _cache_set(cache_key, True, ttl=constants.USER_DIR_REFRESH_TTL)


def _upsert_single_user_to_directory(member: dict, workspace_id: int) -> None:
    """Insert or update a single user in the directory and propagate name changes.

    If the user is deactivated (``member["deleted"] == True``), their
    directory entry is soft-deleted and all associated user mappings are
    removed.
    """
    profile = member.get("profile", {})
    display_name = profile.get("display_name") or ""
    real_name = profile.get("real_name") or ""
    email = profile.get("email")
    now = datetime.now(UTC)
    current_name = display_name or real_name
    is_deleted = member.get("deleted", False)

    existing = DbManager.find_records(
        schemas.UserDirectory,
        [
            schemas.UserDirectory.workspace_id == workspace_id,
            schemas.UserDirectory.slack_user_id == member["id"],
        ],
    )

    if is_deleted:
        if existing:
            DbManager.update_records(
                schemas.UserDirectory,
                [schemas.UserDirectory.id == existing[0].id],
                {schemas.UserDirectory.deleted_at: now, schemas.UserDirectory.updated_at: now},
            )
        _purge_mappings_for_user(member["id"], workspace_id)
        _CACHE.pop(f"user_info:{member['id']}", None)
        return

    if existing:
        DbManager.update_records(
            schemas.UserDirectory,
            [schemas.UserDirectory.id == existing[0].id],
            {
                schemas.UserDirectory.email: email,
                schemas.UserDirectory.real_name: real_name,
                schemas.UserDirectory.display_name: display_name,
                schemas.UserDirectory.normalized_name: _normalize_name(display_name)
                if display_name
                else _normalize_name(real_name),
                schemas.UserDirectory.updated_at: now,
                schemas.UserDirectory.deleted_at: None,
            },
        )
    else:
        DbManager.create_record(
            schemas.UserDirectory(
                workspace_id=workspace_id,
                slack_user_id=member["id"],
                email=email,
                real_name=real_name,
                display_name=display_name,
                normalized_name=_normalize_name(display_name) if display_name else _normalize_name(real_name),
                updated_at=now,
            )
        )

    if current_name:
        mappings = DbManager.find_records(
            schemas.UserMapping,
            [
                schemas.UserMapping.source_workspace_id == workspace_id,
                schemas.UserMapping.source_user_id == member["id"],
            ],
        )
        for m in mappings:
            if m.source_display_name != current_name:
                DbManager.update_records(
                    schemas.UserMapping,
                    [schemas.UserMapping.id == m.id],
                    {schemas.UserMapping.source_display_name: current_name},
                )

    _CACHE.pop(f"user_info:{member['id']}", None)


def _purge_mappings_for_user(slack_user_id: str, workspace_id: int) -> None:
    """Hard-delete all user mappings where this user is source or target."""
    DbManager.delete_records(
        schemas.UserMapping,
        [
            schemas.UserMapping.source_workspace_id == workspace_id,
            schemas.UserMapping.source_user_id == slack_user_id,
        ],
    )
    DbManager.delete_records(
        schemas.UserMapping,
        [
            schemas.UserMapping.target_workspace_id == workspace_id,
            schemas.UserMapping.target_user_id == slack_user_id,
        ],
    )


@slack_retry
def _lookup_user_by_email(client: WebClient, email: str) -> str | None:
    """Resolve a user ID from an email address in the target workspace."""
    res = client.users_lookupByEmail(email=email)
    return safe_get(res, "user", "id")


def _match_from_directory(
    source_profile: dict[str, Any],
    target_candidates: list[schemas.UserDirectory],
    target_by_email: dict[str, list[schemas.UserDirectory]],
) -> tuple[str | None, str]:
    """Match using existing user_directory rows only (no Slack API)."""
    email = (source_profile.get("email") or "").strip()
    if email:
        hits = target_by_email.get(email.lower()) or []
        if len(hits) == 1:
            return hits[0].slack_user_id, "email"

    source_real = source_profile.get("real_name", "") or ""
    source_display = source_profile.get("display_name", "") or ""
    source_normalized = _normalize_name(source_display) if source_display else _normalize_name(source_real)
    if not source_normalized:
        return None, "none"

    name_matches = [
        c
        for c in target_candidates
        if c.normalized_name
        and c.normalized_name.lower() == source_normalized.lower()
        and c.real_name
        and source_real
        and c.real_name.lower() == source_real.lower()
    ]
    if len(name_matches) == 1:
        return name_matches[0].slack_user_id, "name"

    if source_real:
        real_only = [c for c in target_candidates if c.real_name and c.real_name.lower() == source_real.lower()]
        if len(real_only) == 1:
            return real_only[0].slack_user_id, "name"

    return None, "none"


def _find_user_match(
    source_user_id: str,
    source_profile: dict[str, Any],
    target_client: WebClient | None,
    target_workspace_id: int,
    *,
    target_candidates: list[schemas.UserDirectory] | None = None,
    target_by_email: dict[str, list[schemas.UserDirectory]] | None = None,
    allow_slack_email_lookup: bool = True,
    email_lookup_denied: list[bool] | None = None,
) -> tuple[str | None, str]:
    """Match one source user against one target workspace.

    Prefer ``user_directory`` email, then name uniqueness. Optional Slack
    ``users.lookupByEmail`` is a fallback only when the directory has no row
    for that email.
    """
    if target_candidates is None:
        target_candidates = DbManager.find_records(
            schemas.UserDirectory,
            [
                schemas.UserDirectory.workspace_id == target_workspace_id,
                schemas.UserDirectory.deleted_at.is_(None),
            ],
        )
    if target_by_email is None:
        target_by_email = {}
        for entry in target_candidates:
            if not entry.email:
                continue
            key = entry.email.strip().lower()
            if key:
                target_by_email.setdefault(key, []).append(entry)

    target_uid, method = _match_from_directory(source_profile, target_candidates, target_by_email)
    if target_uid:
        return target_uid, method

    email = (source_profile.get("email") or "").strip()
    denied = bool(email_lookup_denied and email_lookup_denied[0])
    if (
        allow_slack_email_lookup
        and not denied
        and email
        and target_client is not None
        and email.lower() not in target_by_email
    ):
        try:
            looked_up = _lookup_user_by_email(target_client, email)
            if looked_up:
                return looked_up, "email"
        except SlackApiError as exc:
            err = ""
            try:
                err = str(exc.response.get("error") if exc.response is not None else exc)
            except Exception:
                err = str(exc)
            if err in ("missing_scope", "invalid_auth", "not_allowed"):
                _logger.warning(
                    "match_user_email_lookup_denied",
                    extra={"workspace_id": target_workspace_id, "error": err},
                )
                if email_lookup_denied is not None:
                    email_lookup_denied[0] = True
            elif err == "users_not_found":
                _logger.debug(
                    "match_user_email_lookup_users_not_found",
                    extra={"workspace_id": target_workspace_id},
                )
            else:
                _logger.debug(
                    "match_user_email_lookup_failed",
                    extra={"workspace_id": target_workspace_id, "error": err},
                )

    return None, "none"


def _source_profile_from_directory(workspace_id: int, slack_user_id: str) -> dict[str, Any] | None:
    """Build a match profile from user_directory when present."""
    rows = DbManager.find_records(
        schemas.UserDirectory,
        [
            schemas.UserDirectory.workspace_id == workspace_id,
            schemas.UserDirectory.slack_user_id == slack_user_id,
            schemas.UserDirectory.deleted_at.is_(None),
        ],
    )
    if not rows:
        return None
    entry = rows[0]
    return {
        "display_name": entry.display_name or "",
        "real_name": entry.real_name or "",
        "email": entry.email,
    }


def _get_source_profile_full(client: WebClient, user_id: str) -> dict[str, Any] | None:
    """Fetch full profile fields needed for matching."""
    cache_key = f"user_profile_full:{user_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        res = _users_info(client, user_id)
    except SlackApiError as exc:
        _logger.warning(f"Failed to look up user {user_id}: {exc}")
        return None

    profile = safe_get(res, "user", "profile") or {}
    result: dict[str, Any] = {
        "display_name": profile.get("display_name") or "",
        "real_name": profile.get("real_name") or "",
        "email": profile.get("email"),
    }
    _cache_set(cache_key, result, ttl=_USER_INFO_CACHE_TTL)
    return result


def get_mapped_target_user_id(
    source_user_id: str,
    source_workspace_id: int,
    target_workspace_id: int,
) -> str | None:
    """Return the mapped target user ID, or *None* if unmapped."""
    mappings = DbManager.find_records(
        schemas.UserMapping,
        [
            schemas.UserMapping.source_workspace_id == source_workspace_id,
            schemas.UserMapping.source_user_id == source_user_id,
            schemas.UserMapping.target_workspace_id == target_workspace_id,
            schemas.UserMapping.target_user_id.isnot(None),
            schemas.UserMapping.match_method != "none",
        ],
    )
    return mappings[0].target_user_id if mappings else None


def get_display_name_and_icon_for_synced_message(
    source_user_id: str,
    source_workspace_id: int,
    source_display_name: str | None,
    source_icon_url: str | None,
    target_client: WebClient,
    target_workspace_id: int,
) -> tuple[str | None, str | None, bool]:
    """Return (display_name, icon_url, is_mapped) when syncing into the target workspace.

    If the source user is mapped to a user in the target workspace, returns that
    local user's display name and profile image (third element ``True``). Otherwise
    returns the source display name and icon (``False``). Display names are
    normalized (text in parens or after a dash at the end is dropped). Callers
    omit the remote workspace suffix in the posted username when ``is_mapped``
    is true.
    """
    mapped_id = get_mapped_target_user_id(source_user_id, source_workspace_id, target_workspace_id)
    if mapped_id:
        local_name, local_icon = get_user_info(target_client, mapped_id)
        if local_name:
            return normalize_display_name(local_name), local_icon or source_icon_url, True
    return normalize_display_name(source_display_name), source_icon_url, False


def resolve_mention_for_workspace(
    source_client: WebClient,
    source_user_id: str,
    source_workspace_id: int,
    target_client: WebClient,
    target_workspace_id: int,
) -> str:
    """Resolve a single @mention from source workspace to target workspace."""
    source_ws = get_workspace_by_id(source_workspace_id)
    source_ws_name = resolve_workspace_name(source_ws) if source_ws else None

    def _unmapped_label(name: str) -> str:
        if source_ws_name:
            return f"`[@{name} ({source_ws_name})]`"
        return f"`[@{name}]`"

    mappings = DbManager.find_records(
        schemas.UserMapping,
        [
            schemas.UserMapping.source_workspace_id == source_workspace_id,
            schemas.UserMapping.source_user_id == source_user_id,
            schemas.UserMapping.target_workspace_id == target_workspace_id,
        ],
    )

    if mappings and _is_mapping_fresh(mappings[0]):
        mapping = mappings[0]
        if mapping.target_user_id:
            return f"<@{mapping.target_user_id}>"
        return _unmapped_label(mapping.source_display_name or source_user_id)

    source_profile = _get_source_profile_full(source_client, source_user_id)
    if not source_profile:
        return _unmapped_label(source_user_id)

    target_uid, method = _find_user_match(source_user_id, source_profile, target_client, target_workspace_id)

    display = source_profile.get("display_name") or source_profile.get("real_name") or source_user_id
    now = datetime.now(UTC)

    if mappings:
        DbManager.update_records(
            schemas.UserMapping,
            [schemas.UserMapping.id == mappings[0].id],
            {
                schemas.UserMapping.target_user_id: target_uid,
                schemas.UserMapping.match_method: method,
                schemas.UserMapping.source_display_name: display,
                schemas.UserMapping.matched_at: now,
            },
        )
    else:
        DbManager.create_record(
            schemas.UserMapping(
                source_workspace_id=source_workspace_id,
                source_user_id=source_user_id,
                target_workspace_id=target_workspace_id,
                target_user_id=target_uid,
                match_method=method,
                source_display_name=display,
                matched_at=now,
                group_id=None,
            )
        )

    if target_uid:
        return f"<@{target_uid}>"
    return _unmapped_label(display)


_MAX_MENTIONS = 50


def parse_mentioned_users(msg_text: str, client: WebClient) -> list[dict[str, Any]]:
    """Extract mentioned user IDs from a message and resolve their profiles."""
    user_ids = re.findall(r"<@(\w+)>", msg_text or "")[:_MAX_MENTIONS]
    if not user_ids:
        return []

    results: list[dict[str, Any]] = []
    for uid in user_ids:
        profile = _get_user_profile(client, uid)
        if profile:
            results.append({"user_id": uid, **profile})
        else:
            results.append({"user_id": uid, "user_name": uid, "email": None})
    return results


def apply_mentioned_users(
    msg_text: str,
    source_client: WebClient,
    target_client: WebClient,
    mentioned_user_info: list[dict[str, Any]],
    source_workspace_id: int,
    target_workspace_id: int,
) -> str:
    """Re-map @mentions from the source workspace to the target workspace."""
    msg_text = msg_text or ""
    if not mentioned_user_info:
        return msg_text

    replace_list: list[str] = []
    for user_info in mentioned_user_info:
        uid = user_info.get("user_id", "")
        try:
            resolved = resolve_mention_for_workspace(
                source_client=source_client,
                source_user_id=uid,
                source_workspace_id=source_workspace_id,
                target_client=target_client,
                target_workspace_id=target_workspace_id,
            )
            replace_list.append(resolved)
        except Exception as exc:
            _logger.error(f"Failed to resolve mention for user {uid}: {exc}")
            fallback = user_info.get("user_name") or uid
            source_ws = get_workspace_by_id(source_workspace_id) if source_workspace_id else None
            ws_label = resolve_workspace_name(source_ws) if source_ws else None
            if ws_label:
                replace_list.append(f"`[@{fallback} ({ws_label})]`")
            else:
                replace_list.append(f"`[@{fallback}]`")

    replace_iter = iter(replace_list)
    return re.sub(r"<@\w+>", lambda _: next(replace_iter), msg_text)


def find_synced_channel_in_target(source_channel_id: str, target_workspace_id: int) -> str | None:
    """If *source_channel_id* belongs to an active sync that *target_workspace_id* also has a channel in, return the local channel ID."""
    source_rows = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.channel_id == source_channel_id,
            schemas.SyncChannel.deleted_at.is_(None),
            schemas.SyncChannel.status == "active",
        ],
    )
    if not source_rows:
        return None
    sync_id = source_rows[0].sync_id
    target_rows = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.sync_id == sync_id,
            schemas.SyncChannel.workspace_id == target_workspace_id,
            schemas.SyncChannel.deleted_at.is_(None),
            schemas.SyncChannel.status == "active",
        ],
    )
    if not target_rows:
        return None
    return target_rows[0].channel_id


_ARCHIVE_LINK_PATTERN = re.compile(r"<https://([a-z0-9-]+)\.slack\.com/archives/(C[A-Z0-9]+)\|([^>]+)>")


def _rewrite_slack_archive_links_to_native_channels(msg_text: str, target_workspace_id: int) -> str:
    """Replace Slack archive mrkdwn links with native ``<#C_LOCAL>`` when that channel is synced to *target_workspace_id*."""
    if not msg_text or not target_workspace_id:
        return msg_text

    def repl(m: re.Match) -> str:
        ch_id = m.group(2)
        local = find_synced_channel_in_target(ch_id, target_workspace_id)
        if local:
            return f"<#{local}>"
        return m.group(0)

    return _ARCHIVE_LINK_PATTERN.sub(repl, msg_text)


def _get_workspace_domain(client: WebClient, team_id: str) -> str | None:
    """Return the workspace subdomain (e.g. ``acme`` for ``acme.slack.com``) from ``team.info``, cached."""
    cache_key = f"ws_domain:{team_id}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    try:
        info = client.team_info()
        domain = safe_get(info, "team", "domain")
        if domain:
            _cache_set(cache_key, domain, ttl=86400)
        return domain
    except Exception as exc:
        _logger.debug("get_workspace_domain_failed", extra={"team_id": team_id, "error": str(exc)})
        return None


def resolve_channel_references(
    msg_text: str,
    source_client: WebClient | None,
    source_workspace: "schemas.Workspace | None" = None,
    target_workspace_id: int | None = None,
) -> str:
    """Replace ``<#CHANNEL_ID>`` references with native local channels when synced, else archive URLs or fallbacks.

    When *target_workspace_id* is set, Slack archive links from federated senders may be rewritten to
    ``<#C_LOCAL>`` if that source channel is synced to the target workspace.
    """
    if not msg_text:
        return msg_text

    if target_workspace_id:
        msg_text = _rewrite_slack_archive_links_to_native_channels(msg_text, target_workspace_id)

    channel_pattern = re.compile(r"<#(C[A-Z0-9]+)(?:\|([^>]*))?>")
    pair_tuples = channel_pattern.findall(msg_text)
    if not pair_tuples:
        return msg_text

    by_channel_id: dict[str, str | None] = {}
    for cid, pipe in pair_tuples:
        if cid not in by_channel_id:
            by_channel_id[cid] = pipe.strip() if pipe and pipe.strip() else None

    team_id = getattr(source_workspace, "team_id", None) if source_workspace else None
    ws_name = resolve_workspace_name(source_workspace) if source_workspace else None

    for ch_id, inline_label in by_channel_id.items():
        if target_workspace_id:
            local_ch = find_synced_channel_in_target(ch_id, target_workspace_id)
            if local_ch:
                replacement = f"<#{local_ch}>"
                msg_text = channel_pattern.sub(
                    lambda m, _cid=ch_id, _rep=replacement: _rep if m.group(1) == _cid else m.group(0),
                    msg_text,
                )
                continue

        ch_name = ch_id
        if source_client:
            try:
                info = source_client.conversations_info(channel=ch_id)
                ch_name = safe_get(info, "channel", "name") or ch_id
            except Exception as exc:
                _logger.debug(
                    "resolve_channel_reference_failed",
                    extra={"channel_id": ch_id, "error": str(exc)},
                )
                if inline_label:
                    ch_name = inline_label
        elif inline_label:
            ch_name = inline_label

        if ch_name != ch_id:
            label = f"#{ch_name} ({ws_name})" if ws_name else f"#{ch_name}"
            domain = _get_workspace_domain(source_client, team_id) if source_client and team_id else None
            if domain:
                deep_link = f"https://{domain}.slack.com/archives/{ch_id}"
                replacement = f"<{deep_link}|{label}>"
            else:
                replacement = f"`[{label}]`"
        else:
            replacement = f"#{ch_id}"

        msg_text = channel_pattern.sub(
            lambda m, _cid=ch_id, _rep=replacement: _rep if m.group(1) == _cid else m.group(0),
            msg_text,
        )

    return msg_text


def seed_user_mappings(source_workspace_id: int, target_workspace_id: int, group_id: int | None = None) -> int:
    """Create stub UserMapping records for all active users in the source directory."""
    directory = DbManager.find_records(
        schemas.UserDirectory,
        [schemas.UserDirectory.workspace_id == source_workspace_id, schemas.UserDirectory.deleted_at.is_(None)],
    )

    existing = DbManager.find_records(
        schemas.UserMapping,
        [
            schemas.UserMapping.source_workspace_id == source_workspace_id,
            schemas.UserMapping.target_workspace_id == target_workspace_id,
        ],
    )
    existing_by_uid = {m.source_user_id: m for m in existing}

    now = datetime.now(UTC)
    to_create: list[schemas.UserMapping] = []
    for entry in directory:
        current_name = entry.display_name or entry.real_name
        if entry.slack_user_id in existing_by_uid:
            mapping = existing_by_uid[entry.slack_user_id]
            if mapping.source_display_name != current_name:
                DbManager.update_records(
                    schemas.UserMapping,
                    [schemas.UserMapping.id == mapping.id],
                    {schemas.UserMapping.source_display_name: current_name},
                )
            continue
        to_create.append(
            schemas.UserMapping(
                source_workspace_id=source_workspace_id,
                source_user_id=entry.slack_user_id,
                target_workspace_id=target_workspace_id,
                target_user_id=None,
                match_method="none",
                source_display_name=current_name,
                matched_at=now,
                group_id=group_id,
            )
        )

    if to_create:
        DbManager.create_records(to_create)
    return len(to_create)


def run_auto_match_for_workspace(
    target_client: WebClient | None,
    target_workspace_id: int,
    *,
    allow_slack_email_lookup: bool = True,
    seeded: int | None = None,
) -> tuple[int, int]:
    """Re-run auto-matching for unmatched mappings targeting a workspace.

    Uses existing ``user_directory`` rows. Does **not** start a ``users.list``
    crawl. Returns ``(newly_matched, still_unmatched)``.
    """
    unmatched = DbManager.find_records(
        schemas.UserMapping,
        [
            schemas.UserMapping.target_workspace_id == target_workspace_id,
            schemas.UserMapping.match_method == "none",
        ],
    )

    target_candidates = DbManager.find_records(
        schemas.UserDirectory,
        [
            schemas.UserDirectory.workspace_id == target_workspace_id,
            schemas.UserDirectory.deleted_at.is_(None),
        ],
    )
    target_by_email: dict[str, list[schemas.UserDirectory]] = {}
    for entry in target_candidates:
        if not entry.email:
            continue
        key = entry.email.strip().lower()
        if key:
            target_by_email.setdefault(key, []).append(entry)
    emails_present = len(target_by_email)

    newly_matched = 0
    still_unmatched = 0
    by_email = 0
    by_name = 0
    email_lookup_denied = [False]

    for mapping in unmatched:
        source_profile = _source_profile_from_directory(mapping.source_workspace_id, mapping.source_user_id)
        if not source_profile and allow_slack_email_lookup:
            # Slack users.info only when the caller allowed per-user lookups
            # (not Auto Map Now, which must stay directory-only).
            source_workspace = get_workspace_by_id(mapping.source_workspace_id)
            if source_workspace and source_workspace.bot_token:
                source_client = WebClient(token=decrypt_bot_token(source_workspace.bot_token))
                source_profile = _get_source_profile_full(source_client, mapping.source_user_id)
        if not source_profile:
            still_unmatched += 1
            continue

        target_uid, method = _find_user_match(
            mapping.source_user_id,
            source_profile,
            target_client,
            target_workspace_id,
            target_candidates=target_candidates,
            target_by_email=target_by_email,
            allow_slack_email_lookup=allow_slack_email_lookup,
            email_lookup_denied=email_lookup_denied,
        )

        if target_uid:
            display = source_profile.get("display_name") or source_profile.get("real_name") or mapping.source_user_id
            DbManager.update_records(
                schemas.UserMapping,
                [schemas.UserMapping.id == mapping.id],
                {
                    schemas.UserMapping.target_user_id: target_uid,
                    schemas.UserMapping.match_method: method,
                    schemas.UserMapping.source_display_name: display,
                    schemas.UserMapping.matched_at: datetime.now(UTC),
                },
            )
            newly_matched += 1
            if method == "email":
                by_email += 1
            elif method == "name":
                by_name += 1
        else:
            still_unmatched += 1

    extras: dict[str, Any] = {
        "workspace_id": target_workspace_id,
        "newly_matched": newly_matched,
        "still_unmatched": still_unmatched,
        "by_email": by_email,
        "by_name": by_name,
        "emails_present": emails_present,
    }
    if seeded is not None:
        extras["seeded"] = seeded
    _logger.info("user_auto_match_complete", extra=extras)
    return newly_matched, still_unmatched


_LAST_AUTO_MATCH_KEY = "last_auto_match"
_AUTO_MATCH_RUNNING_TTL = 45


def auto_match_running_key(workspace_id: int) -> str:
    return f"auto_match_running:{workspace_id}"


def get_last_auto_match(workspace_id: int) -> dict[str, Any] | None:
    """Return the last Auto Map Now summary for *workspace_id*, or None."""
    from helpers.workspace_settings import get_raw_workspace_setting

    raw = get_raw_workspace_setting(workspace_id, _LAST_AUTO_MATCH_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def set_last_auto_match(
    workspace_id: int,
    *,
    newly_matched: int,
    still_unmatched: int,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Persist last Auto Map Now summary without Settings-modal logging."""
    now = at or datetime.now(UTC)
    payload = {
        "at": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "newly_matched": int(newly_matched),
        "still_unmatched": int(still_unmatched),
    }
    value = json.dumps(payload)
    existing = DbManager.find_records(
        schemas.WorkspaceSetting,
        [
            schemas.WorkspaceSetting.workspace_id == workspace_id,
            schemas.WorkspaceSetting.key == _LAST_AUTO_MATCH_KEY,
        ],
    )
    if existing:
        DbManager.update_records(
            schemas.WorkspaceSetting,
            [
                schemas.WorkspaceSetting.workspace_id == workspace_id,
                schemas.WorkspaceSetting.key == _LAST_AUTO_MATCH_KEY,
            ],
            {
                schemas.WorkspaceSetting.value: value,
                schemas.WorkspaceSetting.updated_at: now,
            },
        )
    else:
        DbManager.create_record(
            schemas.WorkspaceSetting(
                workspace_id=workspace_id,
                key=_LAST_AUTO_MATCH_KEY,
                value=value,
                updated_at=now,
            )
        )
    from helpers._cache import _cache_delete

    _cache_delete(f"workspace_setting:{workspace_id}:{_LAST_AUTO_MATCH_KEY}")
    return payload


def format_last_auto_match_line(last: dict[str, Any] | None) -> str:
    """One-line status under Auto Map Now (UTC date)."""
    if not last:
        return "_No auto map yet. Incomplete lists usually mean the directory is still filling in._"
    newly = int(last.get("newly_matched") or 0)
    at_raw = last.get("at") or ""
    date_label = "an earlier date"
    try:
        parsed = datetime.fromisoformat(str(at_raw).replace("Z", "+00:00")).astimezone(UTC)
        date_label = f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"
    except Exception:
        pass
    return f"Last run on {date_label} with {newly} new found."
