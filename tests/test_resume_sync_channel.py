"""Who may run a private Channel Sync, and the pause and resume helpers in ``helpers.sync_lifecycle``.

Rows are plain namespaces. ``DbManager`` is patched where the helper looks it up.
Names that module imports are patched on that module; lazy imports are patched at their own module.
"""

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

from slack_sdk.errors import SlackApiError

from helpers.conversations import AUTHORIZE_HINT, ConversationAccessError
from helpers.sync_lifecycle import (
    PRIVACY_CHANGED_ENDED,
    end_user_authorization,
    leave_channel_without_sync,
    pause_sync_channel,
    resume_sync_channel,
)
from helpers.synced_by import (
    Viewer,
    can_manage_sync,
    refusal_code,
    sync_privacy_conflict,
    viewer_context,
)

NOT_MANAGER = ":lock: Only a workspace manager can resume it."
NO_TOKEN = f":lock: {AUTHORIZE_HINT}"
NOT_MEMBER = (
    ":lock: You are not a member of this private Channel, so you cannot resume it. Join the Channel, then try again."
)
NOT_IN_ANY = (
    ":lock: You are not a member of a private Channel, so you cannot join it. Join a private Channel, then try again."
)
PRIVATE_OFF = ":lock: Private Channel syncing is off in this Workspace."
MEMBERS = "Workspace A [Paused]"


def _channel_notice(action: str, *, manager: str = "SyncBot", workspace: str = "Workspace A") -> str:
    icons = {
        "Pause": ":double_vertical_bar:",
        "Removed": ":double_vertical_bar:",
        "Resume": ":arrow_forward:",
    }
    shown = manager if " (" in manager else f"{manager} ({workspace})"
    body = "\n".join(
        [
            f"Acting Manager: {shown}",
            f"Action: {action}",
            f"Workspaces in Sync: {MEMBERS}",
        ]
    )
    return f"{icons[action]} *SyncBot Action Notice*\n```\n{body}\n```"


def _patch_member_text(stack: ExitStack) -> None:
    stack.enter_context(patch("helpers.sync_participation.format_workspaces_in_sync", return_value=MEMBERS))
    stack.enter_context(patch("helpers.workspace_kind.is_local_workspace", return_value=True))


def _slack_error(code: str) -> SlackApiError:
    return SlackApiError(code, {"ok": False, "error": code})


def _access_error(message: str, code: str | None = None) -> ConversationAccessError:
    """``ConversationAccessError`` the way ``ensure_bot_in_conversation`` raises it: ``from`` a Slack error."""
    exc = ConversationAccessError(message)
    if code:
        exc.__cause__ = _slack_error(code)
    return exc


def _workspace(*, ws_id: int = 1, team_id: str = "T1", name: str = "Workspace A") -> SimpleNamespace:
    return SimpleNamespace(id=ws_id, team_id=team_id, workspace_name=name)


def _row(**overrides) -> SimpleNamespace:
    fields = {
        "id": 10,
        "sync_id": 5,
        "workspace_id": 1,
        "channel_id": "C1",
        "channel_name": "general",
        "status": "active",
        "publishes": True,
        "subscribes": True,
        "is_private": None,
        "synced_by_user_id": None,
        "deleted_at": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _fake_find_records(*, twins=(), live=(), deleted=()):
    """Answer ``DbManager.find_records`` by the columns the caller filtered on.

    ``status ==`` is :func:`leave_channel_without_sync` asking for deleted rows; ``sync_id ==``
    is the other rows of the Sync; anything else is this workspace's live rows.
    """

    def find_records(_model, filters):
        names = {getattr(getattr(item, "left", None), "name", None) for item in filters}
        if "status" in names:
            return list(deleted)
        if "sync_id" in names:
            return list(twins)
        return list(live)

    return find_records


def _written(update: MagicMock, column: str) -> list:
    """Every value ``update_records`` wrote to *column*, in order."""
    values = []
    for recorded in update.call_args_list:
        for key, value in recorded.args[2].items():
            if key.key == column:
                values.append(value)
    return values


def _viewer(
    *,
    is_manager: bool = True,
    allow_private: bool = True,
    has_user_token: bool = True,
    private_channel_ids: frozenset[str] | None = frozenset({"C1"}),
    membership_error: str | None = None,
    user_id: str | None = "UADA",
    workspace_id: int | None = 1,
) -> Viewer:
    return Viewer(
        team_id="T1",
        user_id=user_id,
        workspace_id=workspace_id,
        is_manager=is_manager,
        allow_private=allow_private,
        has_user_token=has_user_token,
        private_channel_ids=private_channel_ids,
        membership_error=membership_error,
    )


class TestCanManageSync:
    def test_public_and_unknown_rows_are_allowed_for_a_manager(self):
        viewer = _viewer(allow_private=False, is_manager=True, has_user_token=False, private_channel_ids=None)
        for is_private in (False, None):
            assert can_manage_sync(_row(is_private=is_private), viewer, action="resume it") == (True, "")

    def test_public_row_is_refused_for_anyone_else(self):
        viewer = _viewer(is_manager=False)
        allowed, reason = can_manage_sync(_row(is_private=False), viewer, action="resume it")
        assert allowed is False
        assert reason == NOT_MANAGER

    def test_private_syncing_off_is_the_first_sentence(self):
        viewer = _viewer(allow_private=False, is_manager=False, has_user_token=False, private_channel_ids=None)
        assert can_manage_sync(_row(is_private=True), viewer) == (False, PRIVATE_OFF)

    def test_not_a_manager_names_the_action(self):
        viewer = _viewer(is_manager=False, has_user_token=False, private_channel_ids=None)
        assert can_manage_sync(_row(is_private=True), viewer, action="resume it") == (False, NOT_MANAGER)

    def test_default_action_is_change_it(self):
        viewer = _viewer(is_manager=False)
        assert can_manage_sync(_row(is_private=True), viewer) == (
            False,
            ":lock: Only a workspace manager can change it.",
        )

    def test_no_user_id_counts_as_not_a_manager(self):
        viewer = _viewer(is_manager=True, user_id=None, has_user_token=False, private_channel_ids=None)
        assert can_manage_sync(_row(is_private=True), viewer) == (
            False,
            ":lock: Only a workspace manager can change it.",
        )

    def test_no_user_token_shows_the_authorize_hint(self):
        viewer = _viewer(has_user_token=False, private_channel_ids=None)
        assert can_manage_sync(_row(is_private=True), viewer) == (False, NO_TOKEN)

    def test_not_a_member_of_the_channel(self):
        viewer = _viewer(private_channel_ids=frozenset())
        assert can_manage_sync(_row(is_private=True), viewer, action="resume it") == (False, NOT_MEMBER)

    def test_a_failed_lookup_is_not_an_empty_membership(self):
        viewer = _viewer(private_channel_ids=None, membership_error="missing_scope")
        allowed, reason = can_manage_sync(_row(is_private=True), viewer, action="resume it")
        assert allowed is False
        assert "could not check your private Channel membership (`missing_scope`)" in reason
        assert "not a member" not in reason

    def test_all_good_is_allowed(self):
        assert can_manage_sync(_row(is_private=True), _viewer()) == (True, "")

    def test_synced_by_user_id_of_someone_else_changes_nothing(self):
        row = _row(is_private=True, synced_by_user_id="UBOB")
        assert can_manage_sync(row, _viewer()) == (True, "")
        refused = _viewer(private_channel_ids=frozenset())
        assert can_manage_sync(row, refused) == (
            False,
            ":lock: You are not a member of this private Channel, so you cannot change it. "
            "Join the Channel, then try again.",
        )

    def test_join_row_needs_any_private_channel_not_the_publishers(self):
        publisher = _row(is_private=True, workspace_id=2, channel_id="C_PUB")
        assert can_manage_sync(publisher, _viewer(private_channel_ids=frozenset({"C_LOCAL"})), action="join it") == (
            True,
            "",
        )
        assert can_manage_sync(publisher, _viewer(private_channel_ids=frozenset()), action="join it") == (
            False,
            NOT_IN_ANY,
        )
        failed = _viewer(private_channel_ids=None, membership_error="ratelimited")
        allowed, reason = can_manage_sync(publisher, failed, action="join it")
        assert allowed is False
        assert "could not check your private Channel membership (`ratelimited`)" in reason


class TestViewerContext:
    def test_membership_is_loaded_only_for_a_manager_with_a_token(self):
        workspace = _workspace()
        with (
            patch("helpers.synced_by.is_workspace_manager", return_value=True),
            patch("helpers.synced_by.allow_private_channels", return_value=True),
            patch("helpers.synced_by.has_user_token", return_value=False),
            patch("helpers.synced_by.private_channel_membership") as ids,
        ):
            viewer = viewer_context(workspace, "UADA", MagicMock())
        assert viewer.private_channel_ids is None
        assert viewer.membership_error is None
        assert viewer.has_user_token is False
        ids.assert_not_called()

    def test_a_loaded_set_is_kept_including_empty(self):
        workspace = _workspace()
        with (
            patch("helpers.synced_by.is_workspace_manager", return_value=True),
            patch("helpers.synced_by.allow_private_channels", return_value=True),
            patch("helpers.synced_by.has_user_token", return_value=True),
            patch("helpers.synced_by.private_channel_membership", return_value=(frozenset(), None)) as ids,
        ):
            viewer = viewer_context(workspace, "UADA", MagicMock())
        assert viewer.private_channel_ids == frozenset()
        assert viewer.membership_error is None
        assert viewer.channel_ids_for_hash() == ()
        ids.assert_called_once_with("T1", "UADA")


class TestDenialKind:
    def test_manager_sentence_is_not_manager(self):
        assert refusal_code(NOT_MANAGER) == "not_manager"
        assert refusal_code(":lock: Only a workspace manager can resume this Sync.") == "not_manager"

    def test_authorize_hint_is_no_token(self):
        assert refusal_code(NO_TOKEN) == "no_token"

    def test_anything_else_is_denied(self):
        assert refusal_code(NOT_MEMBER) == "denied"
        assert refusal_code(PRIVATE_OFF) == "denied"
        assert refusal_code(":warning: SyncBot could not join that Channel (`is_archived`).") == "denied"
        assert refusal_code(None) == "denied"
        assert refusal_code("") == "denied"


class TestSyncPrivacyConflict:
    def test_disagreeing_flag_of_the_other_rows_is_returned(self):
        rows = [_row(id=10, is_private=True), _row(id=11, workspace_id=2, is_private=False)]
        with patch("helpers.synced_by.DbManager.find_records", return_value=rows) as find:
            assert sync_privacy_conflict(5, True, exclude_row_id=10) is False
            assert sync_privacy_conflict(5, False, exclude_row_id=11) is True
        assert find.call_count == 2

    def test_agreeing_flags_are_no_conflict(self):
        rows = [_row(id=10, is_private=True), _row(id=11, workspace_id=2, is_private=True)]
        with patch("helpers.synced_by.DbManager.find_records", return_value=rows):
            assert sync_privacy_conflict(5, True, exclude_row_id=10) is None

    def test_all_null_flags_are_no_conflict(self):
        rows = [_row(id=10, is_private=None), _row(id=11, workspace_id=2, is_private=None)]
        with patch("helpers.synced_by.DbManager.find_records", return_value=rows):
            assert sync_privacy_conflict(5, True, exclude_row_id=10) is None
            assert sync_privacy_conflict(5, False) is None

    def test_the_row_itself_is_excluded(self):
        rows = [_row(id=10, is_private=False)]
        with patch("helpers.synced_by.DbManager.find_records", return_value=rows):
            assert sync_privacy_conflict(5, True, exclude_row_id=10) is None
            assert sync_privacy_conflict(5, True) is False

    def test_no_sync_or_unknown_flag_skips_the_database(self):
        with patch("helpers.synced_by.DbManager.find_records") as find:
            assert sync_privacy_conflict(None, True) is None
            assert sync_privacy_conflict(5, None) is None
        find.assert_not_called()


def _enter_pause_patches(stack: ExitStack, workspace, *, twins=(), live=()) -> SimpleNamespace:
    mocks = SimpleNamespace()
    mocks.update = stack.enter_context(patch("helpers.sync_lifecycle.DbManager.update_records"))
    stack.enter_context(
        patch("helpers.sync_lifecycle.DbManager.find_records", side_effect=_fake_find_records(twins=twins, live=live))
    )
    stack.enter_context(patch("helpers.sync_lifecycle.get_workspace_by_id", return_value=workspace))
    mocks.invalidate = stack.enter_context(patch("helpers.sync_lifecycle.invalidate_sync_fanout_for_syncs"))
    mocks.replicate = stack.enter_context(patch("helpers.sync_lifecycle.replicate_sync_channel_status"))
    mocks.others = stack.enter_context(patch("helpers.sync_lifecycle.post_to_other_sync_channels"))
    mocks.post = stack.enter_context(patch("helpers.sync_lifecycle.notify_channel_or_managers", return_value=True))
    mocks.managers = stack.enter_context(patch("helpers.sync_lifecycle.notify_managers"))
    stack.enter_context(patch("helpers.sync_lifecycle.get_bot_token", return_value="xoxb-1"))
    stack.enter_context(patch("helpers.sync_lifecycle.is_local_workspace", return_value=True))
    _patch_member_text(stack)
    mocks.leave = stack.enter_context(patch("helpers.leave_sync._leave_channel_id"))
    mocks.log_info = stack.enter_context(patch("helpers.sync_lifecycle.log_info"))
    stack.enter_context(
        patch(
            "helpers.leave_sync.DbManager.get_record",
            return_value=SimpleNamespace(id=5, title="general", group_id=None),
        )
    )
    return mocks


class TestPauseSyncChannel:
    def test_already_paused_returns_false_and_writes_nothing(self):
        row = _row(status="paused")
        with ExitStack() as stack:
            mocks = _enter_pause_patches(stack, _workspace())
            assert pause_sync_channel(row, via="home") is False
        mocks.update.assert_not_called()
        mocks.invalidate.assert_not_called()
        mocks.others.assert_not_called()
        mocks.post.assert_not_called()
        mocks.log_info.assert_not_called()

    def test_none_row_returns_false(self):
        with ExitStack() as stack:
            mocks = _enter_pause_patches(stack, _workspace())
            assert pause_sync_channel(None) is False
        mocks.update.assert_not_called()

    def test_writes_paused_invalidates_fan_out_and_replicates(self):
        row = _row()
        workspace = _workspace()
        with ExitStack() as stack:
            mocks = _enter_pause_patches(stack, workspace)
            assert pause_sync_channel(row, via="home") is True
        assert row.status == "paused"
        assert _written(mocks.update, "status") == ["paused"]
        mocks.invalidate.assert_called_once_with([5])
        sentence = _channel_notice("Pause")
        mocks.replicate.assert_called_once_with(row, workspace, sync=None, notice=sentence)
        mocks.leave.assert_not_called()

    def test_home_pause_names_the_action(self):
        row = _row()
        workspace = _workspace()
        with ExitStack() as stack:
            mocks = _enter_pause_patches(stack, workspace)
            pause_sync_channel(row, via="home", who="`Ada Lovelace (Workspace A)`")
        sentence = _channel_notice("Pause", manager="Ada Lovelace (Workspace A)")
        mocks.post.assert_called_once_with(workspace, "C1", sentence, fallback_dm=False, private=False)
        mocks.others.assert_called_once_with(workspace, 5, sentence)

    def test_home_without_a_notice_posts_the_pause_sentence(self):
        row = _row()
        with ExitStack() as stack:
            mocks = _enter_pause_patches(stack, _workspace())
            pause_sync_channel(row, via="home")
        sentence = _channel_notice("Pause")
        mocks.post.assert_called_once()
        assert mocks.post.call_args.args[2] == sentence
        mocks.others.assert_called_once()
        assert mocks.others.call_args.args[2] == sentence

    def test_kick_posts_the_same_sentence_everywhere(self):
        row = _row()
        workspace = _workspace()
        with ExitStack() as stack:
            mocks = _enter_pause_patches(stack, workspace)
            pause_sync_channel(row, via="kick")
        sentence = _channel_notice("Removed")
        mocks.post.assert_not_called()
        mocks.others.assert_called_once_with(workspace, 5, sentence)

    def test_kick_names_the_channel_on_the_manager_dm(self):
        with ExitStack() as stack:
            mocks = _enter_pause_patches(stack, _workspace())
            pause_sync_channel(_row(channel_name=None), via="kick")
        mocks.post.assert_not_called()
        assert "Action: Removed" in mocks.others.call_args.args[2]
        assert "Affected Channel: #C1" in mocks.managers.call_args.args[1]
        assert "Note: SyncBot is not in the Channel." in mocks.managers.call_args.args[1]

    def test_pause_keeps_the_workspace_in_the_list(self):
        row = _row()
        other = _row(id=11, workspace_id=2, channel_id="C2", publishes=False, subscribes=True)
        workspace = _workspace()
        with ExitStack() as stack:
            mocks = _enter_pause_patches(stack, workspace, twins=[row, other])
            pause_sync_channel(row, via="home")
        sentence = _channel_notice("Pause")
        mocks.others.assert_called_once_with(workspace, 5, sentence)
        assert "no publisher" not in sentence

    def test_another_active_publisher_keeps_the_twin_sentence_plain(self):
        row = _row()
        other = _row(id=11, workspace_id=2, channel_id="C2", publishes=True)
        with ExitStack() as stack:
            mocks = _enter_pause_patches(stack, _workspace(), twins=[row, other])
            pause_sync_channel(row, via="home")
        assert mocks.others.call_args.args[2] == _channel_notice("Pause")
        assert "no publisher" not in mocks.others.call_args.args[2]

    def test_leave_without_other_rows_leaves_the_channel(self):
        row = _row()
        workspace = _workspace()
        with ExitStack() as stack:
            mocks = _enter_pause_patches(stack, workspace, live=[row])
            pause_sync_channel(row, via="home", leave=True)
        mocks.leave.assert_called_once_with(workspace, "C1")

    def test_leave_with_a_paused_sibling_still_leaves(self):
        row = _row()
        sibling = _row(id=12, sync_id=6, status="paused")
        workspace = _workspace()
        with ExitStack() as stack:
            mocks = _enter_pause_patches(stack, workspace, live=[row, sibling])
            pause_sync_channel(row, via="home", leave=True)
        mocks.leave.assert_called_once_with(workspace, "C1")

    def test_leave_with_an_active_sibling_keeps_syncbot_in_the_channel(self):
        row = _row()
        sibling = _row(id=12, sync_id=6, status="active")
        with ExitStack() as stack:
            mocks = _enter_pause_patches(stack, _workspace(), live=[row, sibling])
            pause_sync_channel(row, via="home", leave=True)
        mocks.leave.assert_not_called()

    def test_logs_sync_paused_with_via(self):
        with ExitStack() as stack:
            mocks = _enter_pause_patches(stack, _workspace())
            pause_sync_channel(_row(), via="kick")
        mocks.log_info.assert_called_once_with("sync_paused", sync_channel_id=10, channel_id="C1", via="kick")

    def test_no_sync_id_skips_fan_out_and_twins(self):
        row = _row(sync_id=None)
        with ExitStack() as stack:
            mocks = _enter_pause_patches(stack, _workspace())
            assert pause_sync_channel(row, via="home") is True
        mocks.invalidate.assert_not_called()
        mocks.others.assert_not_called()
        assert row.status == "paused"


def _enter_resume_patches(stack: ExitStack, *, twins=(), live=(), manager: bool = True) -> SimpleNamespace:
    mocks = SimpleNamespace()
    mocks.update = stack.enter_context(patch("helpers.sync_lifecycle.DbManager.update_records"))
    stack.enter_context(
        patch("helpers.sync_lifecycle.DbManager.find_records", side_effect=_fake_find_records(twins=twins, live=live))
    )
    stack.enter_context(
        patch("helpers.synced_by.DbManager.find_records", side_effect=_fake_find_records(twins=twins, live=live))
    )
    mocks.manager = stack.enter_context(patch("helpers.sync_lifecycle.is_workspace_manager", return_value=manager))
    mocks.viewer = stack.enter_context(patch("helpers.sync_lifecycle.viewer_context", return_value=_viewer()))
    mocks.bot_token = stack.enter_context(patch("helpers.sync_lifecycle.get_bot_token", return_value="xoxb-1"))
    stack.enter_context(patch("helpers.sync_lifecycle.is_local_workspace", return_value=True))
    mocks.ensure = stack.enter_context(
        patch("helpers.sync_lifecycle.ensure_bot_in_conversation", return_value=False),
    )
    mocks.info = stack.enter_context(patch("helpers.sync_lifecycle.get_slack_channel_private", return_value=False))
    mocks.apply_state = stack.enter_context(patch("helpers.sync_lifecycle.update_channel_privacy", return_value=True))
    mocks.store_flags = stack.enter_context(patch("helpers.sync_lifecycle.set_is_private", return_value=True))
    mocks.invalidate = stack.enter_context(patch("helpers.sync_lifecycle.invalidate_sync_fanout_for_syncs"))
    mocks.replicate = stack.enter_context(patch("helpers.sync_lifecycle.replicate_sync_channel_status"))
    mocks.others = stack.enter_context(patch("helpers.sync_lifecycle.post_to_other_sync_channels"))
    mocks.dm = stack.enter_context(patch("helpers.sync_lifecycle.notify_user_dm"))
    _patch_member_text(stack)
    mocks.soft_delete = stack.enter_context(patch("helpers.leave_sync.soft_delete_sync_channels", return_value=1))
    mocks.drop = stack.enter_context(patch("helpers.sync_lifecycle.purge_slack_deleted_channel"))
    mocks.leave = stack.enter_context(patch("helpers.leave_sync._leave_channel_id"))
    mocks.post = stack.enter_context(patch("helpers.sync_lifecycle.notify_channel_or_managers", return_value=True))
    mocks.managers = stack.enter_context(patch("helpers.sync_lifecycle.notify_managers"))
    stack.enter_context(patch("helpers.sync_lifecycle.format_admin_label", return_value="`Ada Lovelace (Workspace A)`"))
    mocks.log_info = stack.enter_context(patch("helpers.sync_lifecycle.log_info"))
    mocks.log_warning = stack.enter_context(patch("helpers.sync_lifecycle.log_warning"))
    stack.enter_context(
        patch(
            "helpers.leave_sync.DbManager.get_record",
            return_value=SimpleNamespace(id=5, title="general", group_id=None),
        )
    )
    return mocks


class TestResumeSyncChannel:
    def test_non_manager_gets_the_resume_sentence_before_any_write(self):
        row = _row(status="paused")
        client = MagicMock()
        with ExitStack() as stack:
            mocks = _enter_resume_patches(stack, manager=False)
            denied = resume_sync_channel(row, _workspace(), "UADA", client=client, join=True)
        assert denied == ":lock: Only a workspace manager can resume this Sync."
        mocks.manager.assert_called_once_with(client, "UADA", "T1")
        mocks.update.assert_not_called()
        mocks.ensure.assert_not_called()
        assert row.status == "paused"

    def test_predicate_denial_is_returned_before_any_write(self):
        row = _row(status="paused", is_private=True)
        with ExitStack() as stack:
            mocks = _enter_resume_patches(stack)
            mocks.viewer.return_value = _viewer(private_channel_ids=frozenset())
            denied = resume_sync_channel(row, _workspace(), "UADA", client=MagicMock(), join=True)
        assert denied == NOT_MEMBER
        mocks.update.assert_not_called()
        mocks.ensure.assert_not_called()
        mocks.bot_token.assert_not_called()
        assert row.status == "paused"

    def test_no_bot_token_is_a_warning_without_a_write(self):
        row = _row(status="paused")
        with ExitStack() as stack:
            mocks = _enter_resume_patches(stack)
            mocks.bot_token.return_value = None
            denied = resume_sync_channel(row, _workspace(), "UADA", client=MagicMock(), join=True)
        assert denied == ":warning: SyncBot has no bot token for this Workspace, so it cannot be added to the Channel."
        mocks.update.assert_not_called()

    def test_active_is_written_before_the_invite(self):
        row = _row(status="paused")
        workspace = _workspace()
        seen: list[str] = []
        context: dict = {}

        def ensure(_client, channel_id, **kwargs):
            seen.append(f"{row.status}:{channel_id}")
            assert kwargs == {"team_id": "T1", "acting_user_id": "UADA", "context": context}
            return False

        with ExitStack() as stack:
            mocks = _enter_resume_patches(stack)
            mocks.ensure.side_effect = ensure
            denied = resume_sync_channel(row, workspace, "UADA", client=MagicMock(), join=True, context=context)
        assert denied is None
        assert seen == ["active:C1"]
        assert _written(mocks.update, "status") == ["active"]
        mocks.info.assert_not_called()

    def test_archived_channel_stays_paused(self):
        row = _row(status="paused")
        workspace = _workspace()
        with ExitStack() as stack:
            mocks = _enter_resume_patches(stack)
            mocks.ensure.side_effect = _access_error(
                "SyncBot could not join that Channel (`is_archived`).", "is_archived"
            )
            denied = resume_sync_channel(row, workspace, "UADA", client=MagicMock(), join=True)
        assert denied == (
            ":warning: This Channel is archived, so this Sync stays paused. It resumes when the Channel is unarchived."
        )
        mocks.soft_delete.assert_not_called()
        assert _written(mocks.update, "status") == ["active", "paused"]

    def test_channel_not_found_on_a_stored_public_row_is_channel_deleted(self):
        row = _row(status="paused", is_private=False)
        workspace = _workspace()
        with ExitStack() as stack:
            mocks = _enter_resume_patches(stack)
            mocks.ensure.side_effect = _access_error("SyncBot could not join that Channel.", "channel_not_found")
            denied = resume_sync_channel(row, workspace, "UADA", client=MagicMock(), join=True)
        assert denied == ":warning: This Channel was deleted, so this Workspace's synced history for it was deleted."
        mocks.drop.assert_called_once_with(workspace, "C1")
        mocks.dm.assert_not_called()

    def test_channel_not_found_on_a_private_row_pauses_back_and_dms(self):
        row = _row(status="paused", is_private=True)
        client = MagicMock()
        with ExitStack() as stack:
            mocks = _enter_resume_patches(stack)
            mocks.ensure.side_effect = _access_error(
                "SyncBot could not be added to that private Channel (`channel_not_found`).", "channel_not_found"
            )
            denied = resume_sync_channel(row, _workspace(), "UADA", client=client, join=True)
        assert denied == ":warning: SyncBot could not be added to that private Channel (`channel_not_found`)."
        mocks.soft_delete.assert_not_called()
        assert _written(mocks.update, "status") == ["active", "paused"]
        assert row.status == "paused"
        mocks.dm.assert_called_once_with(
            client, "UADA", ":warning: SyncBot could not be added to that private Channel (`channel_not_found`)."
        )
        mocks.log_warning.assert_called_once_with("resume_sync_membership_failed", channel_id="C1", error=ANY)

    def test_channel_not_found_on_an_unknown_flag_pauses_back(self):
        row = _row(status="paused", is_private=None)
        with ExitStack() as stack:
            mocks = _enter_resume_patches(stack)
            mocks.ensure.side_effect = _access_error("gone", "channel_not_found")
            denied = resume_sync_channel(row, _workspace(), "UADA", client=MagicMock(), join=True)
        assert denied == ":warning: gone"
        mocks.soft_delete.assert_not_called()
        assert row.status == "paused"

    def test_any_other_access_error_pauses_back_and_dms(self):
        row = _row(status="paused", is_private=True)
        client = MagicMock()
        with ExitStack() as stack:
            mocks = _enter_resume_patches(stack)
            mocks.ensure.side_effect = _access_error(AUTHORIZE_HINT)
            denied = resume_sync_channel(row, _workspace(), "UADA", client=client, join=True)
        assert denied == f":warning: {AUTHORIZE_HINT}"
        assert refusal_code(denied) == "no_token"
        assert _written(mocks.update, "status") == ["active", "paused"]
        mocks.dm.assert_called_once_with(client, "UADA", f":warning: {AUTHORIZE_HINT}")
        mocks.soft_delete.assert_not_called()
        mocks.post.assert_not_called()
        mocks.others.assert_not_called()
        mocks.replicate.assert_not_called()

    def test_flipped_stored_flag_applies_channel_state_and_ends(self):
        row = _row(status="paused", is_private=False)
        with ExitStack() as stack:
            mocks = _enter_resume_patches(stack)
            mocks.ensure.return_value = True
            denied = resume_sync_channel(row, _workspace(), "UADA", client=MagicMock(), join=True)
        assert denied == PRIVACY_CHANGED_ENDED
        mocks.apply_state.assert_called_once_with(row, is_private=True)
        mocks.store_flags.assert_not_called()
        mocks.post.assert_not_called()
        mocks.others.assert_not_called()
        mocks.replicate.assert_not_called()

    def test_private_channel_in_a_public_sync_pauses_back_and_leaves(self):
        row = _row(status="paused", is_private=None)
        twin = _row(id=11, workspace_id=2, channel_id="C2", is_private=False)
        workspace = _workspace()
        with ExitStack() as stack:
            mocks = _enter_resume_patches(stack, twins=[row, twin], live=[row])
            mocks.ensure.return_value = True
            denied = resume_sync_channel(row, workspace, "UADA", client=MagicMock(), join=True)
        assert denied == ":warning: This Channel is private, but this Sync has public Channels."
        assert _written(mocks.update, "status") == ["active", "paused"]
        assert row.status == "paused"
        mocks.leave.assert_called_once_with(workspace, "C1")
        mocks.store_flags.assert_not_called()
        mocks.post.assert_not_called()
        mocks.others.assert_not_called()
        mocks.replicate.assert_not_called()

    def test_public_channel_in_a_private_sync_keeps_syncbot_when_a_sibling_is_active(self):
        row = _row(status="paused", is_private=None)
        twin = _row(id=11, workspace_id=2, channel_id="C2", is_private=True)
        sibling = _row(id=12, sync_id=6, status="active")
        with ExitStack() as stack:
            mocks = _enter_resume_patches(stack, twins=[row, twin], live=[row, sibling])
            mocks.ensure.return_value = False
            denied = resume_sync_channel(row, _workspace(), "UADA", client=MagicMock(), join=True)
        assert denied == ":warning: This Channel is public, but this Sync has private Channels."
        assert row.status == "paused"
        mocks.leave.assert_not_called()

    def test_success_on_a_private_row_stamps_the_acting_manager(self):
        row = _row(status="paused", is_private=True, synced_by_user_id="UBOB")
        workspace = _workspace()
        client = MagicMock()
        with ExitStack() as stack:
            mocks = _enter_resume_patches(stack, twins=[row])
            mocks.ensure.return_value = True
            denied = resume_sync_channel(row, workspace, "UADA", client=client, join=True)
        assert denied is None
        assert row.status == "active"
        assert row.synced_by_user_id == "UADA"
        assert _written(mocks.update, "synced_by_user_id") == ["UADA"]
        mocks.store_flags.assert_called_once_with(row, is_private=True)
        mocks.apply_state.assert_not_called()
        mocks.invalidate.assert_called_once_with([5])
        sentence = _channel_notice("Resume", manager="Ada Lovelace (Workspace A)")
        mocks.post.assert_called_once_with(workspace, "C1", sentence, fallback_dm=False, private=True)
        mocks.others.assert_called_once_with(workspace, 5, sentence)
        mocks.replicate.assert_called_once_with(row, workspace, sync=None, notice=sentence)
        mocks.log_info.assert_called_once_with("sync_resumed", sync_channel_id=10, channel_id="C1", via="home")
        mocks.dm.assert_not_called()
        mocks.soft_delete.assert_not_called()
        mocks.leave.assert_not_called()

    def test_success_on_a_public_row_does_not_stamp_anyone(self):
        row = _row(status="paused", is_private=False)
        with ExitStack() as stack:
            mocks = _enter_resume_patches(stack)
            mocks.ensure.return_value = False
            assert resume_sync_channel(row, _workspace(), "UADA", client=MagicMock(), join=True) is None
        assert row.synced_by_user_id is None
        assert _written(mocks.update, "synced_by_user_id") == []
        mocks.store_flags.assert_called_once_with(row, is_private=False)

    def test_member_joined_reads_the_flag_instead_of_inviting(self):
        row = _row(status="paused", is_private=True, synced_by_user_id="UBOB")
        workspace = _workspace()
        with ExitStack() as stack:
            mocks = _enter_resume_patches(stack)
            mocks.info.return_value = True
            denied = resume_sync_channel(row, workspace, "UADA", client=MagicMock(), join=False)
        assert denied is None
        mocks.ensure.assert_not_called()
        mocks.info.assert_called_once()
        assert mocks.info.call_args.args[1] == "C1"
        assert row.synced_by_user_id == "UADA"
        mocks.store_flags.assert_called_once_with(row, is_private=True)
        sentence = _channel_notice("Resume", manager="Ada Lovelace (Workspace A)")
        mocks.post.assert_called_once_with(workspace, "C1", sentence, fallback_dm=False, private=True)
        mocks.others.assert_called_once_with(workspace, 5, sentence)
        mocks.replicate.assert_called_once_with(row, workspace, sync=None, notice=sentence)
        mocks.log_info.assert_called_once_with("sync_resumed", sync_channel_id=10, channel_id="C1", via="member_joined")

    def test_member_joined_with_a_flipped_flag_ends_the_sync(self):
        row = _row(status="paused", is_private=True)
        with ExitStack() as stack:
            mocks = _enter_resume_patches(stack)
            mocks.info.return_value = False
            denied = resume_sync_channel(row, _workspace(), "UADA", client=MagicMock(), join=False)
        assert denied == PRIVACY_CHANGED_ENDED
        mocks.apply_state.assert_called_once_with(row, is_private=False)
        mocks.post.assert_not_called()

    def test_unreadable_flag_resumes_without_storing_it(self):
        row = _row(status="paused", is_private=True)
        with ExitStack() as stack:
            mocks = _enter_resume_patches(stack)
            mocks.info.return_value = None
            assert resume_sync_channel(row, _workspace(), "UADA", client=MagicMock(), join=False) is None
        mocks.apply_state.assert_not_called()
        mocks.store_flags.assert_called_once_with(row, is_private=None)
        assert row.status == "active"


class TestLeaveUnconfiguredChannel:
    def test_deleted_row_says_how_long_the_history_waits(self):
        client = MagicMock()
        workspace = _workspace()
        deleted = [_row(status="deleted", deleted_at="2026-10-01")]
        with (
            patch("helpers.sync_lifecycle.DbManager.find_records", side_effect=_fake_find_records(deleted=deleted)),
            patch("helpers.sync_lifecycle.soft_delete_retention_days", return_value=30),
        ):
            outcome = leave_channel_without_sync(client, "C1", workspace=workspace)
        assert outcome == "left_deleted"
        client.chat_postMessage.assert_called_once_with(
            channel="C1",
            text=":wave: Sync data will be retained for 30 days.",
        )
        client.conversations_leave.assert_called_once_with(channel="C1")

    def test_no_row_says_hello_and_leaves(self):
        client = MagicMock()
        with patch("helpers.sync_lifecycle.DbManager.find_records", side_effect=_fake_find_records()) as find:
            outcome = leave_channel_without_sync(client, "C1", workspace=_workspace())
        assert outcome == "left_no_sync"
        find.assert_called_once()
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert text.startswith(":wave: Hello! I'm SyncBot.")
        assert "Create Sync or Join Sync" in text
        client.conversations_leave.assert_called_once_with(channel="C1")

    def test_without_a_workspace_the_database_is_not_asked(self):
        client = MagicMock()
        with patch("helpers.sync_lifecycle.DbManager.find_records") as find:
            assert leave_channel_without_sync(client, "C1") == "left_no_sync"
        find.assert_not_called()
        client.conversations_leave.assert_called_once_with(channel="C1")

    def test_slack_failure_is_logged_and_the_outcome_still_returned(self):
        client = MagicMock()
        client.chat_postMessage.side_effect = _slack_error("not_in_channel")
        with (
            patch("helpers.sync_lifecycle.DbManager.find_records", side_effect=_fake_find_records()),
            patch("helpers.sync_lifecycle.log_warning") as warn,
        ):
            outcome = leave_channel_without_sync(client, "C1", workspace=_workspace())
        assert outcome == "left_no_sync"
        warn.assert_called_once_with("leave_channel_without_sync_failed", channel_id="C1", error=ANY)
        assert "not_in_channel" in warn.call_args.kwargs["error"]
        client.conversations_leave.assert_not_called()


class TestEndUserAuthorization:
    def test_clears_the_token_then_soft_deletes_each_channel_the_person_runs(self):
        workspace = _workspace()
        client = MagicMock()
        rows = [
            _row(id=10, channel_id="C1", synced_by_user_id="UADA", is_private=True),
            _row(id=11, sync_id=6, channel_id="C1", synced_by_user_id="UADA", is_private=True),
            _row(id=12, channel_id="C2", synced_by_user_id="UADA", is_private=True),
            _row(id=13, channel_id="C3", synced_by_user_id="UBOB", is_private=True),
            _row(id=14, channel_id="C4", synced_by_user_id=None),
        ]
        order: list[str] = []

        def clear(team_id, user_id):
            order.append(f"clear:{team_id}:{user_id}")
            return True

        def soft_delete(_workspace, channel_id, reason, **kwargs):
            order.append(f"delete:{channel_id}:{reason}:{kwargs['synced_by_user_id']}")
            return 1

        with (
            patch("helpers.sync_lifecycle.clear_user_authorization", side_effect=clear) as cleared,
            patch("helpers.sync_lifecycle.DbManager.get_record", return_value=workspace) as get_record,
            patch("helpers.sync_lifecycle.DbManager.find_records", side_effect=_fake_find_records(live=rows)),
            patch("helpers.leave_sync.soft_delete_sync_channels", side_effect=soft_delete) as deleted,
        ):
            marked = end_user_authorization("T1", "UADA", client=client)

        assert marked == 2
        cleared.assert_called_once_with("T1", "UADA")
        get_record.assert_called_once()
        assert get_record.call_args.kwargs == {"id": "T1"}
        assert order[0] == "clear:T1:UADA"
        assert sorted(order[1:]) == ["delete:C1:authorization_ended:UADA", "delete:C2:authorization_ended:UADA"]
        for recorded in deleted.call_args_list:
            assert recorded.args[0] is workspace
            assert recorded.kwargs == {"synced_by_user_id": "UADA"}

    def test_no_workspace_clears_and_returns_zero(self):
        with (
            patch("helpers.sync_lifecycle.clear_user_authorization") as cleared,
            patch("helpers.sync_lifecycle.DbManager.get_record", return_value=None),
            patch("helpers.leave_sync.soft_delete_sync_channels") as deleted,
        ):
            assert end_user_authorization("T1", "UADA", client=MagicMock()) == 0
        cleared.assert_called_once_with("T1", "UADA")
        deleted.assert_not_called()

    def test_no_team_skips_the_lookup(self):
        with (
            patch("helpers.sync_lifecycle.clear_user_authorization") as cleared,
            patch("helpers.sync_lifecycle.DbManager.get_record") as get_record,
        ):
            assert end_user_authorization(None, "UADA", client=MagicMock()) == 0
        cleared.assert_called_once_with(None, "UADA")
        get_record.assert_not_called()

    def test_no_user_returns_zero_without_touching_rows(self):
        with (
            patch("helpers.sync_lifecycle.clear_user_authorization"),
            patch("helpers.sync_lifecycle.DbManager.get_record", return_value=_workspace()),
            patch("helpers.sync_lifecycle.DbManager.find_records") as find,
        ):
            assert end_user_authorization("T1", None, client=MagicMock()) == 0
        find.assert_not_called()

    def test_person_running_nothing_returns_zero(self):
        rows = [_row(id=13, channel_id="C3", synced_by_user_id="UBOB", is_private=True)]
        with (
            patch("helpers.sync_lifecycle.clear_user_authorization"),
            patch("helpers.sync_lifecycle.DbManager.get_record", return_value=_workspace()),
            patch("helpers.sync_lifecycle.DbManager.find_records", side_effect=_fake_find_records(live=rows)),
            patch("helpers.leave_sync.soft_delete_sync_channels") as deleted,
        ):
            assert end_user_authorization("T1", "UADA", client=MagicMock()) == 0
        deleted.assert_not_called()
