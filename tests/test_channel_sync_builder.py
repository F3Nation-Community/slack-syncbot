"""Home-tab builder tests for Create / Join / Leave Sync buttons.

A private row shows, with every button, only to a workspace manager who has a
user token and is a member of that private Channel while private syncing is
on. ``synced_by_user_id`` plays no part. The gate is
:func:`helpers.synced_by.can_manage_sync` on the viewer these tests build.
"""

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from builders.channel_sync import _build_inline_channel_sync  # noqa: E402
from db.schemas import Sync  # noqa: E402
from helpers.synced_by import Viewer  # noqa: E402
from slack import actions  # noqa: E402

GROUP_ID = 5
SYNC_ID = 42
PUBLISHER_WS = 1
SUBSCRIBER_WS = 2
VIEWER_WS = 99

ALL_BUTTONS = [":pencil2: Edit Sync", ":double_vertical_bar: Pause Sync", ":wave: Leave Sync"]
PAUSED_BUTTONS = [":pencil2: Edit Sync", ":arrow_forward: Resume Sync", ":wave: Leave Sync"]


def _channel(cid, ws, *, publishes=True, subscribes=True):
    return SimpleNamespace(
        id=cid,
        workspace_id=ws,
        channel_id=f"C_{cid}",
        status="active",
        created_at=datetime.now(UTC).replace(tzinfo=None),
        publishes=publishes,
        subscribes=subscribes,
    )


def _private_row(*, status: str = "active", synced_by: str | None = None):
    """The viewer's own private Channel row."""
    mine = _channel(10, PUBLISHER_WS)
    mine.status = status
    mine.is_private = True
    mine.synced_by_user_id = synced_by
    return mine


def _buttons(blocks):
    """Flatten every (label, action_id) button across ActionsBlocks."""
    out = []
    for block in blocks:
        for element in getattr(block, "elements", None) or []:
            action = getattr(element, "action", None)
            if action:
                out.append((getattr(element, "label", None), action))
    return out


def _labels(blocks) -> list[str]:
    return [label for label, _ in _buttons(blocks)]


def _render(
    *,
    viewer_ws,
    publisher_ws,
    channels,
    is_owner: bool = False,
    sync_mode: str = "group",
    duplicate_source: bool = False,
    user_id: str | None = None,
    allow_private: bool = True,
    is_manager: bool = True,
    has_token: bool = False,
    in_channel: bool = False,
):
    sync = SimpleNamespace(
        id=SYNC_ID,
        group_id=GROUP_ID,
        title="2nd-f",
        sync_mode=sync_mode,
    )
    group = SimpleNamespace(id=GROUP_ID)
    workspace_record = SimpleNamespace(id=viewer_ws, team_id="T1")

    def find_records(model, filters):
        if model is Sync:
            return [sync]
        # One query per sync: ``sync_id ==`` plus ``or_(deleted_at IS NULL, status == "uninstalled")``,
        # so the fake hands back live and uninstalled rows together.
        assert len(filters) == 2
        return channels

    channel_ids = None
    if is_manager and allow_private and has_token:
        channel_ids = frozenset({"C_10"}) if in_channel else frozenset()
    viewer = Viewer(
        team_id="T1",
        user_id=user_id,
        workspace_id=viewer_ws,
        is_manager=is_manager,
        allow_private=allow_private,
        has_user_token=has_token,
        private_channel_ids=channel_ids,
    )

    blocks: list = []
    with (
        patch("builders.channel_sync.DbManager.find_records", side_effect=find_records),
        patch("builders.channel_sync.DbManager.count_records", return_value=0),
        patch("builders.channel_sync.helpers.resolve_workspace_name", return_value="WS"),
        patch("builders.channel_sync.helpers.lookup_channel_name", return_value="partner-ch"),
        patch("helpers.workspace.resolve_workspace_name", return_value="WS"),
        patch("helpers.workspace.lookup_channel_name", return_value="partner-ch"),
        patch("helpers.workspace_settings.allow_private_channels", return_value=False),
        patch(
            "builders.channel_sync.helpers.get_workspace_by_id",
            return_value=SimpleNamespace(id=2, team_id="T2", workspace_name="WS", bot_token=None),
        ),
        patch("builders.channel_sync.helpers.is_workspace_owner", return_value=is_owner),
        patch("builders.channel_sync.helpers.already_subscribed_to_source", return_value=duplicate_source),
        patch("builders.channel_sync._format_channel_ref", return_value="#c"),
    ):
        _build_inline_channel_sync(blocks, group, workspace_record, other_members=[], context={}, viewer=viewer)
    return blocks


def test_synced_row_members_are_partner_channel_ticks():
    blocks = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, PUBLISHER_WS), _channel(11, SUBSCRIBER_WS)],
    )
    texts = "\n".join(_context_texts(blocks))
    assert "Members: `#partner-ch (WS) [Publish and Subscribe]`" in texts
    assert "Status: `Publish and Subscribe`" in texts


def test_synced_row_paused_partner_is_tagged():
    paused = _channel(11, SUBSCRIBER_WS)
    paused.status = "paused"
    blocks = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, PUBLISHER_WS), paused],
    )
    texts = "\n".join(_context_texts(blocks))
    assert "Members: `#partner-ch (WS) [Paused]`" in texts


def test_synced_row_uninstalled_partner_is_tagged():
    """The one ``or_()`` query also returns uninstalled rows, kept for the Members line only."""
    gone = _channel(11, SUBSCRIBER_WS)
    gone.status = "uninstalled"
    gone.deleted_at = datetime.now(UTC).replace(tzinfo=None)
    blocks = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, PUBLISHER_WS), gone],
    )
    texts = "\n".join(_context_texts(blocks))
    assert "Members: `#partner-ch (WS) [Uninstalled]`" in texts
    assert "Status: `No active publisher" not in texts


def test_active_row_offers_leave_sync():
    blocks = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, PUBLISHER_WS), _channel(11, SUBSCRIBER_WS)],
    )
    actions_seen = [a for _, a in _buttons(blocks)]

    assert any(a.startswith(actions.CONFIG_LEAVE_SYNC) for a in actions_seen)
    assert not any(a.startswith("unpublish_channel") for a in actions_seen)
    assert not any(a.startswith("stop_sync") for a in actions_seen)


def test_subscriber_active_row_also_offers_leave_sync():
    blocks = _render(
        viewer_ws=SUBSCRIBER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, SUBSCRIBER_WS, publishes=False), _channel(11, PUBLISHER_WS)],
    )
    actions_seen = [a for _, a in _buttons(blocks)]

    assert any(a.startswith(actions.CONFIG_LEAVE_SYNC) for a in actions_seen)
    assert not any(a.startswith("unpublish_channel") for a in actions_seen)
    assert not any(a.startswith("stop_sync") for a in actions_seen)
    texts = "\n".join(_context_texts(blocks))
    assert "Status: `Subscribe only`" in texts


def test_publisher_only_row_status_includes_participation():
    blocks = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, PUBLISHER_WS, subscribes=False), _channel(11, SUBSCRIBER_WS)],
    )
    texts = "\n".join(_context_texts(blocks))
    assert "Status: `Publish only`" in texts


def test_paused_local_row_status_includes_participation():
    paused_mine = _channel(10, PUBLISHER_WS)
    paused_mine.status = "paused"
    blocks = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[paused_mine, _channel(11, SUBSCRIBER_WS)],
    )
    texts = "\n".join(_context_texts(blocks))
    assert "Status: `Paused`" in texts


def test_active_row_edit_is_first_button():
    blocks = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, PUBLISHER_WS), _channel(11, SUBSCRIBER_WS)],
    )
    labels = _labels(blocks)
    assert labels[0] == ":pencil2: Edit Sync"
    assert labels == ALL_BUTTONS
    edit_action = _buttons(blocks)[0][1]
    assert edit_action == f"{actions.CONFIG_EDIT_SYNC}_c_10"


def test_subscriber_active_row_edit_then_pause_then_leave():
    blocks = _render(
        viewer_ws=SUBSCRIBER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, SUBSCRIBER_WS, publishes=False), _channel(11, PUBLISHER_WS)],
    )
    assert _labels(blocks) == ALL_BUTTONS


def test_waiting_publisher_has_edit_then_leave():
    blocks = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, PUBLISHER_WS)],
    )
    assert _labels(blocks) == [":pencil2: Edit Sync", ":wave: Leave Sync"]


def test_pending_row_shows_leave_sync_only():
    """A row waiting for migration cannot be edited, paused, or resumed; it can only be left."""
    mine = _channel(10, PUBLISHER_WS)
    mine.status = "pending"
    blocks = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[mine, _channel(11, SUBSCRIBER_WS)],
    )
    assert _labels(blocks) == [":wave: Leave Sync"]
    assert "Status: `Waiting for migration" in "\n".join(_context_texts(blocks))


def test_pending_private_row_shows_leave_sync_only_to_an_in_channel_manager():
    blocks = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_private_row(status="pending"), _channel(11, SUBSCRIBER_WS)],
        user_id="U1",
        has_token=True,
        in_channel=True,
    )
    assert _labels(blocks) == [":wave: Leave Sync"]


def test_public_row_shows_buttons_to_every_manager():
    """A public row is never gated: no user token and no Channel membership are needed."""
    blocks = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, PUBLISHER_WS), _channel(11, SUBSCRIBER_WS)],
        user_id="U1",
        has_token=False,
        in_channel=False,
    )
    assert _labels(blocks) == ALL_BUTTONS


def test_row_with_unknown_privacy_is_never_denied():
    """``is_private`` still null is not a private row, so the gate stays out of it."""
    mine = _channel(10, PUBLISHER_WS)
    mine.is_private = None
    blocks = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[mine, _channel(11, SUBSCRIBER_WS)],
        user_id="U1",
        has_token=False,
        in_channel=False,
    )
    assert _labels(blocks) == ALL_BUTTONS


def test_private_row_hides_actions_when_private_syncing_is_off():
    blocks = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_private_row(synced_by="U1"), _channel(11, SUBSCRIBER_WS)],
        user_id="U1",
        allow_private=False,
        has_token=True,
        in_channel=True,
    )
    # The whole Sync is skipped, so it is not offered as Available either.
    assert blocks == []


def test_private_row_shows_to_an_in_channel_manager_whatever_synced_by_holds():
    """Who sees a private row is membership, not the stamped person."""
    stamped = _private_row(synced_by="U_OWNER")
    shown_to_other = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[stamped, _channel(11, SUBSCRIBER_WS)],
        user_id="U_OTHER",
        has_token=True,
        in_channel=True,
    )
    hidden_from_owner = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[stamped, _channel(11, SUBSCRIBER_WS)],
        user_id="U_OWNER",
        has_token=False,
        in_channel=True,
    )
    assert _labels(shown_to_other) == ALL_BUTTONS
    assert hidden_from_owner == []


def test_private_row_is_hidden_from_a_non_manager():
    blocks = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_private_row(), _channel(11, SUBSCRIBER_WS)],
        user_id="U1",
        is_manager=False,
        has_token=True,
        in_channel=True,
    )
    assert blocks == []


def test_private_row_is_hidden_without_a_token_or_membership():
    without_token = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_private_row(), _channel(11, SUBSCRIBER_WS)],
        user_id="U1",
        has_token=False,
        in_channel=True,
    )
    not_in_channel = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_private_row(), _channel(11, SUBSCRIBER_WS)],
        user_id="U1",
        has_token=True,
        in_channel=False,
    )
    assert without_token == []
    assert not_in_channel == []


def test_paused_private_row_hides_resume_without_a_token_or_membership():
    without_token = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_private_row(status="paused"), _channel(11, SUBSCRIBER_WS)],
        user_id="U1",
        has_token=False,
        in_channel=True,
    )
    not_in_channel = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_private_row(status="paused"), _channel(11, SUBSCRIBER_WS)],
        user_id="U1",
        has_token=True,
        in_channel=False,
    )
    assert _buttons(without_token) == []
    assert _buttons(not_in_channel) == []


def test_hidden_private_sync_is_not_offered_as_available():
    """A denied private row drops the Sync from Home entirely rather than demoting it to Join Sync."""
    blocks = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_private_row(), _channel(11, SUBSCRIBER_WS)],
        user_id="U1",
        has_token=False,
        in_channel=False,
    )
    assert not any(a.startswith(actions.CONFIG_JOIN_SYNC) for _, a in _buttons(blocks))
    assert not any("Available Sync Relationships" in (getattr(block, "label", None) or "") for block in blocks)
    assert blocks == []


def test_active_private_row_with_no_person_shows_buttons_to_a_manager():
    """A null ``synced_by_user_id`` behaves the same as a stamped one."""
    unstamped = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_private_row(synced_by=None), _channel(11, SUBSCRIBER_WS)],
        user_id="U1",
        has_token=True,
        in_channel=True,
    )
    stamped = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_private_row(synced_by="U_SOMEONE"), _channel(11, SUBSCRIBER_WS)],
        user_id="U1",
        has_token=True,
        in_channel=True,
    )
    assert _labels(unstamped) == ALL_BUTTONS
    assert _labels(stamped) == ALL_BUTTONS


def test_paused_private_row_shows_resume_to_an_in_channel_manager():
    blocks = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_private_row(status="paused", synced_by=None), _channel(11, SUBSCRIBER_WS)],
        user_id="U1",
        has_token=True,
        in_channel=True,
    )
    assert _labels(blocks) == PAUSED_BUTTONS


def test_private_row_gate_can_be_stubbed_at_the_builder():
    """The builder reads the predicate by its own name, so a test may stub the gate there."""
    with patch("builders.channel_sync.can_manage_sync", return_value=(False, ":lock: nope")) as gate:
        blocks = _render(
            viewer_ws=PUBLISHER_WS,
            publisher_ws=PUBLISHER_WS,
            channels=[_private_row(), _channel(11, SUBSCRIBER_WS)],
            user_id="U1",
            has_token=True,
            in_channel=True,
        )
    assert blocks == []
    assert gate.call_args.args[1].user_id == "U1"
    assert gate.call_args.args[0].is_private is True


def test_stranded_member_has_leave_without_edit():
    blocks = _render(
        viewer_ws=SUBSCRIBER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, SUBSCRIBER_WS, publishes=False, subscribes=True)],
    )
    assert _labels(blocks) == [":pencil2: Edit Sync", ":wave: Leave Sync"]


def test_available_row_has_one_join_action():
    owner_blocks = _render(
        viewer_ws=VIEWER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, PUBLISHER_WS)],
        is_owner=True,
    )
    assert _labels(owner_blocks) == [":inbox_tray: Join Sync"]
    headers = [
        getattr(block, "label", None) or ""
        for block in owner_blocks
        if "Available Sync Relationships" in (getattr(block, "label", None) or "")
    ]
    assert len(headers) == 1

    member_blocks = _render(
        viewer_ws=VIEWER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, PUBLISHER_WS)],
        is_owner=False,
    )
    assert _labels(member_blocks) == [":inbox_tray: Join Sync"]


def test_duplicate_source_disables_join():
    blocks = _render(
        viewer_ws=VIEWER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, PUBLISHER_WS)],
        duplicate_source=True,
    )
    assert not _buttons(blocks)
    assert any("Already subscribed" in text for text in _context_texts(blocks))


def test_home_does_not_show_one_to_one_type_label():
    group_blocks = _render(
        viewer_ws=VIEWER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, PUBLISHER_WS)],
    )
    direct_blocks = _render(
        viewer_ws=VIEWER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, PUBLISHER_WS)],
        sync_mode="direct",
    )
    assert not any("Type:" in text for text in _context_texts(group_blocks))
    assert not any("1-to-1" in text for text in _context_texts(direct_blocks))


def test_orphaned_sync_is_not_listed_as_available():
    """A sync with no remaining publishers must not be offered as Join Sync."""
    blocks = _render(
        viewer_ws=VIEWER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(11, SUBSCRIBER_WS, publishes=False)],
    )
    actions_seen = [a for _, a in _buttons(blocks)]

    assert not any(a.startswith(actions.CONFIG_JOIN_SYNC) for a in actions_seen)
    assert not any(a.startswith("subscribe_channel") for a in actions_seen)


def test_stranded_member_can_leave_the_orphan():
    """The remaining subscriber (no publishers) gets Leave Sync."""
    blocks = _render(
        viewer_ws=SUBSCRIBER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, SUBSCRIBER_WS, publishes=False)],
    )
    actions_seen = [a for _, a in _buttons(blocks)]

    assert any(a.startswith(actions.CONFIG_LEAVE_SYNC) for a in actions_seen)


def _context_texts(blocks) -> list[str]:
    texts = []
    for block in blocks:
        label = getattr(block, "label", None)
        if isinstance(label, str) and label:
            texts.append(label)
        element = getattr(block, "element", None)
        if element is not None:
            value = getattr(element, "initial_value", "") or ""
            if value and value not in texts:
                texts.append(value)
    return texts


def test_synced_channel_details_are_body_text():
    from slack.orm import SectionBlock

    blocks = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, PUBLISHER_WS), _channel(11, SUBSCRIBER_WS)],
    )
    status = next(block for block in blocks if "Status:" in (getattr(block, "label", None) or ""))
    assert isinstance(status, SectionBlock)


def test_available_row_shows_the_channel_without_repeating_the_workspace():
    """``sync.title`` was the Channel ID when the bot looked up the name before joining."""
    sync = SimpleNamespace(
        id=SYNC_ID,
        group_id=GROUP_ID,
        title="C_10",
        sync_mode="group",
    )
    group = SimpleNamespace(id=GROUP_ID)
    workspace_record = SimpleNamespace(id=VIEWER_WS, team_id="T1")
    channels = [_channel(10, PUBLISHER_WS)]
    channels[0].is_private = True
    channels[0].channel_name = "2nd-f"

    def find_records(model, _filters):
        return [sync] if model is Sync else channels

    blocks: list = []
    with (
        patch("builders.channel_sync.DbManager.find_records", side_effect=find_records),
        patch("builders.channel_sync.DbManager.count_records", return_value=0),
        patch("builders.channel_sync.helpers.resolve_workspace_name", return_value="WS"),
        patch("helpers.workspace.resolve_workspace_name", return_value="WS"),
        patch("helpers.workspace_settings.allow_private_channels", return_value=True),
        patch(
            "builders.channel_sync.helpers.get_workspace_by_id",
            return_value=SimpleNamespace(id=1, team_id="T1", workspace_name="WS", bot_token=None),
        ),
        patch("builders.channel_sync.helpers.lookup_channel_name", return_value="2nd-f"),
        patch("builders.channel_sync.helpers.is_workspace_owner", return_value=False),
        patch("builders.channel_sync.helpers.already_subscribed_to_source", return_value=False),
        patch("builders.channel_sync._format_channel_ref", return_value="#c"),
    ):
        _build_inline_channel_sync(
            blocks,
            group,
            workspace_record,
            other_members=[],
            context={},
            viewer=Viewer(
                team_id="T1",
                user_id="U1",
                workspace_id=VIEWER_WS,
                is_manager=True,
                allow_private=True,
                has_user_token=True,
                private_channel_ids=frozenset({"C_OTHER"}),
            ),
        )

    joined = "\n".join(_context_texts(blocks))
    assert "Channel: `#2nd-f [Publish and Subscribe]`" in joined
    assert "Privacy: `Private`" in joined
    assert "`C_10`" not in joined
    assert ":lock:" not in joined
    assert any(a.startswith(actions.CONFIG_JOIN_SYNC) for _, a in _buttons(blocks))


def test_available_private_sync_is_hidden_when_the_viewer_is_not_in_one():
    sync = SimpleNamespace(id=SYNC_ID, group_id=GROUP_ID, title="leadership", sync_mode="group")
    group = SimpleNamespace(id=GROUP_ID)
    workspace_record = SimpleNamespace(id=VIEWER_WS, team_id="T1")
    channels = [_channel(10, PUBLISHER_WS)]
    channels[0].is_private = True

    def find_records(model, _filters):
        return [sync] if model is Sync else channels

    blocks: list = []
    with (
        patch("builders.channel_sync.DbManager.find_records", side_effect=find_records),
        patch("builders.channel_sync.helpers.get_workspace_by_id", return_value=SimpleNamespace(bot_token=None)),
    ):
        _build_inline_channel_sync(
            blocks,
            group,
            workspace_record,
            other_members=[],
            context={},
            viewer=Viewer(
                team_id="T1",
                user_id="U1",
                workspace_id=VIEWER_WS,
                is_manager=True,
                allow_private=True,
                has_user_token=True,
                private_channel_ids=frozenset(),
            ),
        )

    assert not any(a.startswith(actions.CONFIG_JOIN_SYNC) for _, a in _buttons(blocks))


def test_available_card_hides_when_any_publisher_is_private():
    """A public publisher does not keep a card whose other publisher is private and refused."""
    private = _channel(10, PUBLISHER_WS)
    private.is_private = True
    public = _channel(12, SUBSCRIBER_WS)
    public.is_private = False
    blocks = _render(
        viewer_ws=VIEWER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[private, public],
        user_id="U1",
        has_token=True,
        in_channel=False,
    )
    assert blocks == []
