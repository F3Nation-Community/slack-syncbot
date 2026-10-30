"""Who may see and run a Channel Sync, and who last brought SyncBot into the Channel.

``synced_by_user_id`` is that person. :func:`can_manage_sync` decides who sees or
runs the row. Pause, resume, rejoin, and the Slack-channel purge live in
:mod:`helpers.sync_lifecycle`.

Imports submodules only. This module does not import ``sync_lifecycle``.
"""

from dataclasses import dataclass

from db import DbManager, schemas
from helpers.channel_state import set_is_private
from helpers.conversations import (
    AUTHORIZE_HINT,
    has_user_token,
    private_channel_membership,
)
from helpers.core import is_workspace_manager
from helpers.workspace_settings import allow_private_channels
from logger import log_warning

_MANAGER_ONLY = ":lock: Only a workspace manager can"


@dataclass(frozen=True)
class Viewer:
    """One Home render's answers for who may see or run a Sync row.

    ``private_channel_ids`` is ``None`` when it was not loaded: this person is
    not a manager, private syncing is off, they have no user token, or Slack
    failed. An empty set means Slack answered and they are in no private Channel.
    """

    team_id: str | None
    user_id: str | None
    workspace_id: int | None
    is_manager: bool
    allow_private: bool
    has_user_token: bool
    private_channel_ids: frozenset[str] | None
    membership_error: str | None = None

    def channel_ids_for_hash(self) -> tuple[str, ...] | None:
        """Sorted Channel ids for the Home hash, or ``None`` when the set was not loaded."""
        if self.private_channel_ids is None:
            return None
        return tuple(sorted(self.private_channel_ids))


def viewer_context(workspace, user_id: str | None, client) -> Viewer:
    """Load this person's manager, token, and private-Channel answers once.

    The token is checked before ``users.conversations``. A missing token and a
    failed page are not cached; ``_private_channel_ids`` already does that.
    """
    team_id = getattr(workspace, "team_id", None)
    is_manager = bool(user_id and client is not None and is_workspace_manager(client, user_id, team_id))
    allow_private = bool(allow_private_channels(team_id))
    token = bool(user_id and has_user_token(team_id, user_id))
    channel_ids = None
    membership_error = None
    if is_manager and allow_private and token:
        channel_ids, membership_error = private_channel_membership(team_id, user_id)
    return Viewer(
        team_id=team_id,
        user_id=user_id,
        workspace_id=getattr(workspace, "id", None),
        is_manager=is_manager,
        allow_private=allow_private,
        has_user_token=token,
        private_channel_ids=channel_ids,
        membership_error=membership_error,
    )


def can_manage_sync(row, viewer: Viewer, *, action: str = "change it") -> tuple[bool, str]:
    """Whether *viewer* may see or run *row*. ``(True, "")`` or ``(False, reason)``.

    A public row, or one whose ``is_private`` is still unknown, is allowed for a
    manager. A private row then requires private syncing, a manager, and a user
    token. Membership is the only remaining branch: a row in the viewer's
    workspace requires that Channel id; a row in another workspace (a Join card)
    requires any private Channel. ``synced_by_user_id`` plays no part.
    """
    if getattr(row, "is_private", None) is not True:
        if viewer.is_manager:
            return True, ""
        return False, f"{_MANAGER_ONLY} {action}."
    if not viewer.allow_private:
        return False, ":lock: Private Channel syncing is off in this Workspace."
    if not viewer.user_id or not viewer.is_manager:
        return False, f"{_MANAGER_ONLY} {action}."
    if not viewer.has_user_token:
        return False, f":lock: {AUTHORIZE_HINT}"
    channel_ids = viewer.private_channel_ids
    if channel_ids is None:
        detail = viewer.membership_error or "no answer"
        return False, (
            f":lock: SyncBot could not check your private Channel membership (`{detail}`). Try again in a minute."
        )
    if getattr(row, "workspace_id", None) == viewer.workspace_id:
        channel_id = getattr(row, "channel_id", None)
        if channel_ids and channel_id in channel_ids:
            return True, ""
        return False, (
            f":lock: You are not a member of this private Channel, so you cannot {action}. "
            "Join the Channel, then try again."
        )
    if channel_ids:
        return True, ""
    return False, (
        ":lock: You are not a member of a private Channel, so you cannot join it. "
        "Join a private Channel, then try again."
    )


def refusal_code(sentence: str | None) -> str:
    """``not_manager``, ``no_token``, or ``denied`` for a sentence from :func:`can_manage_sync` or Resume.

    Matches ``_MANAGER_ONLY`` and ``AUTHORIZE_HINT``. A copy edit of those strings changes the code.
    """
    if not sentence:
        return "denied"
    if sentence.startswith(_MANAGER_ONLY):
        return "not_manager"
    if AUTHORIZE_HINT in sentence:
        return "no_token"
    return "denied"


def sync_privacy_conflict(
    sync_id: int | None, is_private: bool | None, *, exclude_row_id: int | None = None
) -> bool | None:
    """The other rows' privacy flag when it disagrees with *is_private*, else None.

    Other rows are this Sync's rows with ``deleted_at`` unset.
    """
    if not sync_id or is_private is None:
        return None
    rows = DbManager.find_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.sync_id == int(sync_id), schemas.SyncChannel.deleted_at.is_(None)],
    )
    flags = {
        row.is_private for row in rows if row.id != exclude_row_id and getattr(row, "is_private", None) is not None
    }
    other = not is_private
    return other if other in flags else None


def set_synced_by(sync_channel: schemas.SyncChannel, user_id: str | None) -> None:
    """Record the manager whose token or invite brought SyncBot into this private Channel."""
    if not sync_channel or not getattr(sync_channel, "id", None) or not user_id:
        return
    DbManager.update_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.id == sync_channel.id],
        {schemas.SyncChannel.synced_by_user_id: user_id},
    )
    sync_channel.synced_by_user_id = user_id


def set_channel_private(sync_channel: schemas.SyncChannel, user_id: str | None) -> None:
    """After the invite: this row gets the flag and the person; its siblings get the flag only."""
    from sqlalchemy.exc import OperationalError, ProgrammingError

    set_is_private(sync_channel, is_private=True)
    set_synced_by(sync_channel, user_id)
    if not sync_channel or not sync_channel.workspace_id or not sync_channel.channel_id:
        return
    try:
        siblings = DbManager.find_records(
            schemas.SyncChannel,
            [
                schemas.SyncChannel.workspace_id == sync_channel.workspace_id,
                schemas.SyncChannel.channel_id == sync_channel.channel_id,
                schemas.SyncChannel.deleted_at.is_(None),
            ],
        )
    except (ProgrammingError, OperationalError) as exc:
        log_warning("set_channel_private", channel_id=sync_channel.channel_id, error=str(exc))
        return
    for row in siblings:
        if row.id != sync_channel.id:
            set_is_private(row, is_private=True)
