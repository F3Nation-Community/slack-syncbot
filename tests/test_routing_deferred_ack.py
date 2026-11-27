"""Routing invariants: deferred-ack views keep their ack and work handlers, and dropped Slack ids stay unrouted."""

from routing import ACTION_MAPPER, VIEW_ACK_MAPPER, VIEW_MAPPER
from slack import actions

# Dropped Slack ids must not stay routed. Refresh Home after a rename.
_REMOVED_SLACK_IDS = (
    "publish_channel",
    "publish_channel_select",
    "publish_channel_submit",
    "publish_participation",
    "publish_participation_submit",
    "publish_reaction_style",
    "unpublish_channel",
    "stop_sync",
    "confirm_stop_sync",
    "subscribe_channel",
    "subscribe_channel_select",
    "subscribe_channel_submit",
    "subscribe_participation",
    "subscribe_participation_submit",
    "subscribe_reaction_style",
    "join_sync_select",
    "manage_user_matching",
    "user_mapping_auto_match",
    "accept_group_request",
    "decline_group_request",
    "cancel_group_request",
)


def _work_partners(ack_handler) -> list:
    """Work handlers named after *ack_handler*: ``handle_x`` or ``handle_x_work`` for ``handle_x_ack``."""
    import handlers

    base = ack_handler.__name__.removesuffix("_ack")
    return [getattr(handlers, name) for name in (base, f"{base}_work") if hasattr(handlers, name)]


def test_a_view_that_acks_first_keeps_its_work_handler():
    """An early ack with a work partner must route that partner after the ack, or the work never runs."""
    for callback_id, ack_handler in VIEW_ACK_MAPPER.items():
        partners = _work_partners(ack_handler)
        if partners:
            assert VIEW_MAPPER.get(callback_id) in partners, f"{callback_id!r} acks first but routes no work"


def test_a_view_whose_work_has_an_ack_routes_that_ack():
    """Work with an ``_ack`` partner must keep it, or Slack waits on the work instead of the fast ack."""
    import handlers

    for callback_id, work_handler in VIEW_MAPPER.items():
        base = work_handler.__name__.removesuffix("_work")
        ack_handler = getattr(handlers, f"{base}_ack", None)
        if ack_handler is not None:
            assert VIEW_ACK_MAPPER.get(callback_id) is ack_handler, f"{callback_id!r} lost its ack handler"


def test_join_group_checks_the_code_in_the_ack():
    import handlers

    assert VIEW_ACK_MAPPER[actions.CONFIG_JOIN_GROUP_SUBMIT] is handlers.handle_join_group_submit_ack
    assert VIEW_MAPPER[actions.CONFIG_JOIN_GROUP_SUBMIT] is handlers.handle_join_group_submit


def test_create_and_join_submit_on_the_final_modal():
    assert actions.CONFIG_CREATE_SYNC_SUBMIT in VIEW_ACK_MAPPER
    assert actions.CONFIG_CREATE_SYNC_SUBMIT in VIEW_MAPPER
    assert actions.CONFIG_JOIN_SYNC_SUBMIT in VIEW_ACK_MAPPER
    assert actions.CONFIG_JOIN_SYNC_SUBMIT in VIEW_MAPPER


def test_removed_slack_ids_are_not_routed():
    routed = set(ACTION_MAPPER) | set(VIEW_MAPPER) | set(VIEW_ACK_MAPPER)
    for leftover in _REMOVED_SLACK_IDS:
        assert leftover not in routed
