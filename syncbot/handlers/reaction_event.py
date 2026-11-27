"""Reaction add/remove event handlers."""

import logging

from slack_sdk.web import WebClient

import helpers
from db.event_claims import run_claimed
from logger import log_debug, log_info


def _sync_reaction_records(body: dict, client: WebClient, reacted_records: list[tuple]) -> None:
    """Publish a reaction add/remove through the target pipeline."""
    event = body.get("event", {})
    reaction = event.get("reaction")
    user_id = event.get("user")
    item = event.get("item", {})
    channel_id = item.get("channel")
    event_type = event.get("type")
    action = helpers.ACTION_ADD if event_type == "reaction_added" else helpers.ACTION_REMOVE
    source_rows = helpers.get_publishing_post_records(reacted_records, channel_id)
    if not source_rows:
        return
    post_meta, source_sync_channel, source_workspace = source_rows[0]
    user_name, user_profile_url = helpers.get_user_info(client, user_id) if user_id else (None, None)
    people = [helpers.build_people_entry(user_id, name=user_name, avatar_url=user_profile_url)] if user_id else None
    raw_event_ts = event.get("event_ts") or event.get("event_time")
    event_ts = helpers.slack_message_ts(raw_event_ts) if raw_event_ts else None
    envelope = helpers.build_envelope(
        kind=helpers.KIND_REACTION,
        action=action,
        post_id=str(post_meta.post_id),
        source_channel_id=channel_id,
        source_workspace_id=source_workspace.id,
        source_team_id=helpers.get_team_id_from_body(body),
        source_sync_channel_id=source_sync_channel.id,
        people=people,
        reaction=reaction,
        source_user_id=user_id,
        user_name=user_name,
        user_avatar_url=user_profile_url,
        workspace_name=helpers.resolve_workspace_name(source_workspace),
        source_ts=item.get("ts"),
        event_ts=event_ts,
    )
    helpers.run_sync_pipeline(
        envelope,
        source_channel_id=channel_id,
        source_client=client,
        source_sync_channel=source_sync_channel,
    )


def handle_reaction(
    body: dict,
    client: WebClient,
    logger: logging.Logger,
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

    team_id = helpers.get_team_id_from_body(body)

    def _sync_reaction() -> bool | None:
        # SyncBot's own reaction as a mapped person: the echo's kind is Slack's event type.
        if helpers.take_pending_event(
            event_type,
            target_team_id=str(team_id) if team_id else None,
            target_user_id=user_id,
            target_channel_id=channel_id,
            target_ts=msg_ts,
            reaction=reaction,
        ):
            return
        if not helpers.has_channel_sync(channel_id):
            return
        if not helpers.origin_publishes_anywhere(channel_id):
            return

        reacted_records = helpers.get_post_records(msg_ts, channel_id=channel_id)
        if not reacted_records:
            log_debug("reaction_no_post_meta", msg_ts=msg_ts, channel_id=channel_id)
            if helpers.is_before_sync(channel_id, msg_ts):
                log_info("reaction_skip", reason="before_sync", channel=channel_id, ts=msg_ts)
                return
            # The message's create may still be running: leave this reaction pending for it.
            if not user_id:
                return
            reacted_records = helpers.leave_pending_event(
                team_id,
                channel_id,
                action=helpers.ACTION_ADD if event_type == "reaction_added" else helpers.ACTION_REMOVE,
                source_ts=msg_ts,
                source_user_id=user_id,
                reaction=reaction,
                event_ts=event.get("event_ts") or event.get("event_time"),
            )
            if reacted_records is None:
                # Nothing was kept: release the claim so a resend can try again.
                return False
            if not reacted_records:
                return
        _sync_reaction_records(body, client, reacted_records)

    run_claimed(body, _sync_reaction)
