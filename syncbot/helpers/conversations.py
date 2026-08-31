"""Getting SyncBot into a channel it is supposed to sync.

Slack draws a hard line between the two channel types:

* **Public** — the bot token can add the bot itself with ``conversations.join``.
* **Private** — ``conversations.join`` is rejected outright
  (``method_not_supported_for_channel_type``). Only a human who is already in
  the channel can add an app, with ``conversations.invite`` called using **their**
  user token (``xoxp``, ``groups:write``).

The person publishing or subscribing is by definition a member of the channel
they just picked, so their user token is the one that works. Those tokens are
already written to ``slack_installations`` by Bolt for anyone who completed the
OAuth install; this module is the only place that reads them.

Imports submodules only (``constants``, ``helpers._cache``, ``helpers.core``,
``helpers.oauth``, ``helpers.settings``, ``helpers.slack_api``) and never the
``helpers`` package, per the import-direction constraint documented in
``helpers/sync_cleanup.py``.
"""

import logging
import os
import urllib.parse

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

import constants
from helpers.core import safe_get
from helpers.settings import allow_private_channels
from helpers.slack_api import get_own_bot_user_id

_logger = logging.getLogger(__name__)

# Slack errors that mean "this bot cannot see the channel", which for a private
# channel is the expected answer rather than a failure.
_NOT_VISIBLE_ERRORS = frozenset({"channel_not_found", "missing_scope", "not_in_channel"})

# Errors that mean the bot is already where we want it.
_ALREADY_THERE_ERRORS = frozenset({"already_in_channel", "cant_invite_self", "method_post_only"})

AUTHORIZE_HINT = (
    "SyncBot needs your permission before it can add itself to a private Channel. "
    'Open the SyncBot Home tab, click "Authorize SyncBot", then try again.'
)


class ConversationAccessError(Exception):
    """SyncBot could not become a member of a channel.

    The message is written for the admin who clicked Publish or Subscribe, so it
    can be shown as a modal field error or sent as a DM without rewording.
    """


def _installation_store():
    """Return Bolt's installation store, or ``None`` in single-workspace mode.

    Built from the same OAuth settings the app serves ``/slack/install`` with, so
    there is no second source of truth for where installs are recorded.
    """
    from helpers.oauth import get_oauth_flow

    try:
        oauth_flow = get_oauth_flow()
    except Exception as exc:
        _logger.warning(f"installation store unavailable: {exc}")
        return None
    if oauth_flow is None:
        return None
    return oauth_flow.settings.installation_store


def get_user_token(team_id: str | None, user_id: str | None) -> str | None:
    """Return *user_id*'s own Slack user token, or ``None``.

    Only users who completed the OAuth install have one. Never log the return
    value.
    """
    if not team_id or not user_id:
        return None
    store = _installation_store()
    if store is None:
        return None
    try:
        installation = store.find_installation(enterprise_id=None, team_id=team_id, user_id=user_id)
    except Exception as exc:
        _logger.warning(f"find_installation failed for team {team_id}: {exc}")
        return None
    return _clean_token(getattr(installation, "user_token", None) if installation else None)


def _clean_token(token) -> str | None:
    """Normalize a stored token: blank or non-string means "no token"."""
    if not isinstance(token, str):
        return None
    return token.strip() or None


def has_user_token(team_id: str | None, user_id: str | None) -> bool:
    """Whether this specific user has authorized SyncBot to act as them.

    Drives whether the Home tab shows *Authorize SyncBot*. Deliberately ignores
    the team-level fallback in :func:`_team_user_token`: someone else's token
    happening to work is not this user's authorization.
    """
    return bool(get_user_token(team_id, user_id))


def _team_user_token(team_id: str | None) -> str | None:
    """Return any user token recorded for *team_id*, usually the installer's.

    A fallback for the case where the acting admin has not authorized but the
    installer is also in the private channel. When they are not, Slack answers
    ``not_in_channel`` and the caller reports that explicitly.
    """
    if not team_id:
        return None
    store = _installation_store()
    if store is None:
        return None
    try:
        installation = store.find_installation(enterprise_id=None, team_id=team_id)
    except Exception as exc:
        _logger.warning(f"find_installation (team-level) failed for team {team_id}: {exc}")
        return None
    return _clean_token(getattr(installation, "user_token", None) if installation else None)


def has_usable_user_token(team_id: str | None, user_id: str | None) -> bool:
    """Whether *some* user token exists that could invite the bot for this team."""
    return bool(get_user_token(team_id, user_id) or _team_user_token(team_id))


def _slack_error(exc: SlackApiError) -> str:
    return (safe_get(exc.response, "error") or "") if exc.response is not None else ""


def _channel_visibility(client: WebClient, channel_id: str) -> tuple[bool, bool]:
    """Return ``(is_private, is_member)`` as seen by the bot token.

    A private channel the bot has never been in is invisible to the bot token, so
    ``channel_not_found`` is treated as "private, not a member" when the
    private-channel policy is on, and as a hard failure when it is off. This is
    the one decision that must not reuse the picker's skip-the-lookup shortcut:
    join and invite are different API calls and we have to pick the right one.
    """
    try:
        response = client.conversations_info(channel=channel_id)
    except SlackApiError as exc:
        error = _slack_error(exc)
        if error in _NOT_VISIBLE_ERRORS and allow_private_channels():
            return True, False
        raise ConversationAccessError(
            f"SyncBot could not read that Channel (`{error or 'unknown error'}`). Pick a Channel it can reach."
        ) from exc
    except Exception as exc:
        if allow_private_channels():
            return True, False
        raise ConversationAccessError(
            "SyncBot could not read that Channel. Pick a public Channel it can join."
        ) from exc

    channel = safe_get(response, "channel") or {}
    return bool(channel.get("is_private")), bool(channel.get("is_member"))


def ensure_bot_in_conversation(
    client: WebClient,
    channel_id: str,
    *,
    team_id: str | None,
    acting_user_id: str | None,
) -> None:
    """Make SyncBot a member of *channel_id*, whichever type it is.

    Public channels are joined with the bot token. Private channels are joined by
    inviting the bot as *acting_user_id* (falling back to the installer), because
    a bot cannot add itself to one.

    Raises :class:`ConversationAccessError` with admin-facing text on failure. The
    caller is expected to undo whatever it wrote before calling, so a channel is
    never listed as synced while the bot cannot read it.
    """
    if not channel_id:
        raise ConversationAccessError("No Channel was selected.")

    is_private, is_member = _channel_visibility(client, channel_id)
    if is_member:
        return

    if not is_private:
        try:
            client.conversations_join(channel=channel_id)
        except SlackApiError as exc:
            error = _slack_error(exc)
            if error in _ALREADY_THERE_ERRORS:
                return
            raise ConversationAccessError(
                f"SyncBot could not join that Channel (`{error or 'unknown error'}`)."
            ) from exc
        return

    if not allow_private_channels():
        raise ConversationAccessError("Private Channels cannot be synced. Pick a public Channel.")

    bot_user_id = get_own_bot_user_id(client)
    if not bot_user_id:
        raise ConversationAccessError("SyncBot could not determine its own identity. Please try again.")

    token = get_user_token(team_id, acting_user_id) or _team_user_token(team_id)
    if not token:
        raise ConversationAccessError(AUTHORIZE_HINT)

    try:
        WebClient(token=token).conversations_invite(channel=channel_id, users=bot_user_id)
    except SlackApiError as exc:
        error = _slack_error(exc)
        if error in _ALREADY_THERE_ERRORS:
            return
        if error == "not_in_channel":
            raise ConversationAccessError(
                "SyncBot could not be added to that private Channel, because the account that "
                "authorized SyncBot is not a member of it. Open the SyncBot Home tab, click "
                "*Authorize SyncBot* as yourself, and try again."
            ) from exc
        raise ConversationAccessError(
            f"SyncBot could not be added to that private Channel (`{error or 'unknown error'}`)."
        ) from exc


def authorize_url(team_id: str | None = None) -> str | None:
    """Return the URL that starts an OAuth install for the current user.

    Prefers this deployment's own ``/slack/install`` so Bolt issues and verifies
    the ``state`` itself. When no public URL is configured, falls back to Slack's
    authorize endpoint with a state from the same store Bolt uses. Returns
    ``None`` when neither is possible, so the Home tab can hide the button rather
    than render a dead link.

    *team_id* pre-selects the workspace on Slack's authorize screen, which
    matters because most people belong to several and the screen otherwise
    defaults to whichever one their browser used last. Bolt passes the ``team``
    query parameter through to the same place.
    """
    client_id = os.environ.get(constants.SLACK_CLIENT_ID, "").strip()
    if not client_id:
        return None

    base = os.environ.get(constants.SYNCBOT_PUBLIC_URL, "").strip().rstrip("/")
    if base:
        install_url = f"{base}/slack/install"
        if team_id:
            install_url += "?" + urllib.parse.urlencode({"team": team_id})
        return install_url

    from slack_manifest_scopes import USER_SCOPES

    bot_scopes = os.environ.get(constants.SLACK_BOT_SCOPES, "").strip()
    user_scopes = os.environ.get(constants.SLACK_USER_SCOPES, "").strip() or ",".join(USER_SCOPES)

    params = {"client_id": client_id, "scope": bot_scopes, "user_scope": user_scopes}
    state = _issue_oauth_state()
    if state:
        params["state"] = state
    if team_id:
        params["team"] = team_id
    return "https://slack.com/oauth/v2/authorize?" + urllib.parse.urlencode(params)


def _issue_oauth_state() -> str | None:
    """Issue an OAuth ``state`` value from Bolt's own state store."""
    from helpers.oauth import get_oauth_flow

    try:
        oauth_flow = get_oauth_flow()
        if oauth_flow is None:
            return None
        return oauth_flow.settings.state_store.issue()
    except Exception as exc:
        _logger.warning(f"could not issue OAuth state: {exc}")
        return None
