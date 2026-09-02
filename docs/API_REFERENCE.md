# API Reference

## HTTP Endpoints (Lambda Function URL / Cloud Run)

A single public HTTPS base serves every path. On AWS that is the Lambda Function URL; on GCP it is the Cloud Run URL. After you deploy, point Slack at the `/slack/*` URLs. The `/api/federation/*` endpoints are for cross-instance communication when External Connections are enabled.

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/slack/events` | Receives all Slack events (messages, actions, view submissions) and slash commands |
| `GET` | `/slack/install` | Starts OAuth: sets Bolt's state cookie and redirects the browser to Slack's authorization screen |
| `GET` | `/slack/oauth_redirect` | OAuth callback after the user approves. On success, SyncBot publishes that user's Home tab so **Authorize SyncBot** can disappear without a Refresh |
| `POST` | `/api/federation/pair` | Accept an incoming external connection request |
| `POST` | `/api/federation/message` | Receive a forwarded message from a connected instance; resolves `@` mentions and `#` channel references locally before posting |
| `POST` | `/api/federation/message/edit` | Receive a message edit from a connected instance; applies the same local mention and channel resolution before updating |
| `POST` | `/api/federation/message/delete` | Receive a message deletion from a connected instance |
| `POST` | `/api/federation/message/react` | Receive a reaction add or remove from a connected instance; applies the destination channel's reaction type (native, Hybrid thread, or skip) and deletes Hybrid notices on unreact |
| `POST` | `/api/federation/users` | Exchange user directory with a connected instance |
| `GET` | `/api/federation/ping` | Health check for connected instances (still answers when federation is off in Settings) |

## Subscribed Slack Events

| Event | Handler | Description |
|-------|---------|-------------|
| `app_home_opened` | `handle_app_home_opened` | Publishes the Home tab with workspace groups, channel syncs, and user matching. |
| `app_uninstalled` | `handle_app_uninstalled` | Workspace uninstall: Bolt `InstallationStore.delete_all` (bot + every user install row), then pause groups and channel syncs. |
| `member_joined_channel` | `handle_member_joined_channel` | Detects when SyncBot is added to an unconfigured channel; posts a message and leaves. |
| `message.channels` / `message.groups` | `respond_to_message_event` | Fires on new messages, edits, deletes, and file shares in public/private channels. Dispatches to sub-handlers for new posts, thread replies, edits, deletes, and reactions. Deleting a Hybrid reaction notice is a local tombstone only. |
| `reaction_added` / `reaction_removed` | `_handle_reaction` | Syncs emoji reactions to linked channels; skips user-token echo events SyncBot applied on the destination. |
| `team_join` | `handle_team_join` | Fires when a new user joins a connected workspace. Adds the user to the directory and re-checks unmatched user mappings. |
| `tokens_revoked` | `handle_tokens_revoked` | User-token revoke: Bolt `delete_installation` for that person, then republish Home. A `tokens.bot` array is treated as uninstall only when the stored bot token fails `auth.test`. |
| `user_profile_changed` | `handle_user_profile_changed` | Detects display name or email changes and updates the user directory and mappings. |
