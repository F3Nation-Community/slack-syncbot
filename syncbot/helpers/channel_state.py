"""Keep a Channel Sync's stored ``is_private`` in line with Slack.

``null`` means unknown. A flip from a known value soft-deletes the row,
because a private Channel only syncs with private Channels.

Imports submodules only.
"""

from slack_sdk.web import WebClient

from db import DbManager, schemas
from helpers.core import safe_get
from helpers.workspace import get_workspace_by_id
from logger import log_warning


def get_slack_channel_private(client: WebClient, channel_id: str | None) -> bool | None:
    """Slack's ``is_private`` for this Channel, or None when ``conversations.info`` fails."""
    if not channel_id:
        return None
    try:
        response = client.conversations_info(channel=channel_id)
    except Exception as exc:
        log_warning("channel_info_failed", channel_id=channel_id, error=str(exc))
        return None
    channel = safe_get(response, "channel")
    if not isinstance(channel, dict) or not channel:
        return None
    return bool(channel.get("is_private"))


def refresh_channel_privacy(sync_channel: schemas.SyncChannel, client: WebClient) -> None:
    """Read Slack's flag once. A flip from a known value soft-deletes the row."""
    slack_private = get_slack_channel_private(client, getattr(sync_channel, "channel_id", None))
    if slack_private is not None and slack_private != getattr(sync_channel, "is_private", None):
        update_channel_privacy(sync_channel, is_private=slack_private)


def update_channel_privacy(sync_channel: schemas.SyncChannel, *, is_private: bool | None) -> bool:
    """Store Slack's privacy flag. A flip from a known value soft-deletes the row."""
    if sync_channel is None or not getattr(sync_channel, "id", None) or is_private is None:
        return False
    stored = getattr(sync_channel, "is_private", None)
    changed = set_is_private(sync_channel, is_private=is_private)
    if stored is not None and stored != is_private:
        from helpers.leave_sync import soft_delete_sync_channels

        workspace = get_workspace_by_id(sync_channel.workspace_id)
        if workspace and sync_channel.channel_id:
            soft_delete_sync_channels(
                workspace,
                sync_channel.channel_id,
                "became_private" if is_private else "became_public",
                sync_ids={sync_channel.sync_id} if sync_channel.sync_id else None,
                is_private=is_private,
            )
        return True
    return changed


def set_is_private(sync_channel: schemas.SyncChannel, *, is_private: bool | None) -> bool:
    """Write Slack's privacy flag without ending the Sync. Returns whether it changed."""
    if sync_channel is None or not getattr(sync_channel, "id", None) or is_private is None:
        return False
    if getattr(sync_channel, "is_private", None) == is_private:
        return False
    DbManager.update_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.id == sync_channel.id],
        {schemas.SyncChannel.is_private: is_private},
    )
    sync_channel.is_private = is_private
    return True
