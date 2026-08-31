# SyncBot User Guide

This guide is for **workspace admins and people using SyncBot in Slack**. If you are installing or hosting the app (AWS, GCP, Docker, GitHub Actions), see **[DEPLOY.md](DEPLOY.md)** and the root **[README](../README.md)**.

## Getting Started

1. Click the install link from a desktop browser (make sure you have selected the correct workspace in the upper right).
2. Open the **SyncBot** app from the sidebar and click the **Home** tab (this requires a workspace admin or owner).
3. The Home tab shows everything in one view:
   - **SyncBot Configuration (bottom row)** — **Refresh**, plus **Backup/Restore** and **Settings** only if the host set `PRIMARY_WORKSPACE` (the Slack Team ID) and redeployed. If you do not see them, ask the operator. **Settings** is where the operator adjusts instance-wide policy, such as how long the data of a workspace that uninstalled is kept before it is deleted for good.
   - **Workspace Groups** — create or join groups of workspaces that can sync channels together.
   - **Per-group sections** — for each group you can **Publish Channel**, manage user mapping (a dedicated Home tab screen), and see or manage channel syncs inline. Other workspaces in the group see published channels as **Subscribe**.
   - **Synced Channels** — each row shows the local channel and workspace list in brackets (for example _[Any: Your Workspace, Other Workspace]_), with pause/resume and stop controls, a synced-since date, and a tracked message count.
   - **External Connections** *(when federation is enabled)* — Generate or Enter a Connection Code, and **Data Migration** (export workspace data to another instance, or import a migration file).

## Things to Know

- Only workspace **admins and owners** can configure syncs (set `REQUIRE_ADMIN=false` to allow all users). Everyone can still open the Home tab; what `REQUIRE_ADMIN` restricts is the configuration itself, such as creating a group, publishing a channel, or opening Settings.
- Messages, threads, edits, deletes, reactions, images, videos, and GIFs are all synced.
- **@mentions and #channel links** in synced messages are rewritten per target workspace: mapped users are tagged with the local Slack user, and channels that are part of the same sync are shown as native local channel links; otherwise users fall back to a code-style label and channels use a link back to the source workspace (or a code-style label if that cannot be built).
- Messages from other bots are synced; only SyncBot's own messages are filtered to prevent loops.
- Existing messages are not back-filled; syncing starts from the moment a channel is linked.
- Do not add SyncBot manually to channels. SyncBot adds itself when you Publish or Subscribe. If it detects it was added to an unconfigured channel, it posts a message and leaves automatically.
- When you pick a channel to publish or subscribe, SyncBot uses Slack's own channel search, so you can reach any channel in your workspace by typing a few letters. There is no limit on how many channels it can show.
- A channel can belong to only one Channel Sync at a time. If you pick one that is already syncing, SyncBot tells you so in the dialog and asks for a different channel rather than quietly doing nothing. A channel you previously unpublished is free to use again.
- Public channels are supported out of the box. Private channels are only available if the operator turned them on in **Settings**; if they have not, SyncBot asks you to pick a public channel. When they are allowed, you publish or subscribe a private channel the same way you would a public one, and SyncBot adds itself for you using your permission to invite it — see **Authorize SyncBot** below.

## Authorize SyncBot

Slack does not allow an app to add itself to a private channel. Only someone who is already in that channel can add it, acting as themselves. So the first time you use SyncBot, you may see an **Authorize SyncBot** section at the top of the Home tab with a short explanation and a button.

Clicking the button walks you through the same install screen you saw when SyncBot was added to the workspace, and it takes a few seconds. The screen arrives with this workspace already selected, so if you belong to several you do not have to hunt for the right one. It does not ask for any new permissions from your workspace; it simply records that SyncBot may act on your behalf. Once that is done, the section disappears for you, and publishing or subscribing a private channel works without any extra steps.

Everyone sees this section until they have authorized, whether or not they are an admin. Whoever installed SyncBot originally will usually never see it, because that first install already stored their own permission. A colleague's authorization is not reused: SyncBot only invites itself into a private channel as the person who picked it. If you pick a private channel before authorizing, SyncBot tells you in the dialog and points you here rather than failing after the dialog closes.

## Workspace Groups

Workspaces must belong to the same **group** before they can sync channels or map users. Admins can create a new group (which generates an invite code) or join an existing group by entering a code. A workspace can be in multiple groups with different combinations of other workspaces.

### Group owners

Every group has at least one **owner** workspace. The workspace that creates a group is its first owner. Owners are the workspaces that can promote other owners and disband the group; everyone else is a member. Inviting another workspace stays open to any member, not just owners.

An owner can share that responsibility by clicking **Promote to Owner** next to another workspace in the group. There is no matching "demote" button for other workspaces — an owner can only step down itself, using **Give Up Ownership**, and only when another owner remains. That keeps one workspace from quietly taking a group over by demoting everyone else.

For the same reason, a group can never be left with no owner. If you are the only owner, SyncBot will not let your workspace leave the group until you have promoted another workspace to owner. It explains this instead of failing silently, so you know what to do next.

Uninstalling SyncBot does not hand your ownership to anyone else. Your membership is only paused, so reinstalling within the retention period gives you the group back exactly as it was. Ownership passes to another workspace only when your data is actually deleted, either because the retention period expired or because an operator purged it. In that case SyncBot promotes the longest-standing remaining member so the group is not stranded.

### Disbanding a group

An owner can **Disband Group** to remove a group entirely, along with its syncs and user mappings. Because this cannot be undone and affects other workspaces, SyncBot only offers it when your workspace is the sole owner *and* the sole publisher of every channel in the group. If another workspace owns the group or has published a channel into it, disbanding is declined with an explanation of who else is involved — ask them to unpublish or leave first, or just leave the group yourself instead.

Disbanding always asks for confirmation before anything is removed, and tells you how many workspaces, syncs, and channels it will affect. Note that the user mappings scoped to the group go with it, and those took auto-matching and manual edits to build, so re-creating the group later means establishing those matches again.

## Sync Modes

When publishing a channel inside a group, use **Publish Channel**. The first step chooses either **1-to-1** (only a specific workspace can subscribe) or **group-wide** (any group member can subscribe independently). Other workspaces then use **Subscribe** to pick a local channel that receives the published one.

## Pause / Resume / Stop

- **Pause/Resume** — Individual channel syncs can be paused and resumed without losing configuration. Paused channels do not sync any messages, threads, or reactions.
- **Selective Stop** — When a *subscribing* workspace stops syncing a channel, only that workspace's history is removed. Other workspaces continue syncing uninterrupted. The published channel remains available until the original publisher unpublishes it.
- **Unpublish** — The *publishing* workspace does not "Stop Syncing" its own channel — it **Unpublishes**, which removes the sync for everyone in it. (The publisher is the source of the channel, so there is no way for it to leave while keeping the sync alive for others.) If unpublishing fails for any reason, SyncBot tells you the channel is still published rather than showing it as gone, so you can try again.
- **Stranded syncs self-heal** — If a publisher ever leaves a sync behind, the remaining subscriber sees "publisher left; no longer syncing" and can clear it with Stop Syncing; removing the last channel deletes the empty sync automatically.

## Uninstall / Reinstall

If a workspace uninstalls SyncBot, group memberships and syncs are paused (not deleted). Reinstalling within the retention period (default 30 days, which the operator can change in **Settings**) automatically restores everything, including group ownership. Group members are notified via DMs and channel messages.

## User Mapping

Users are automatically mapped across workspaces by email or display name. Admins can manually edit mappings via the User Mapping screen (scoped per group). On that screen, remote users are listed as "Display Name (Workspace Name)" and sorted by normalized name. In synced messages, a mapped author appears with their **local** display name and profile photo (no workspace suffix in the author line); an unmapped author uses the remote display name and photo, with the source workspace in parentheses. The same applies to messages delivered over **External Connections** (cross-instance federation). In message text, a mapped user is mentioned with a normal `@` tag in the receiving workspace; unmapped users appear as a code-style `[@Name (Workspace)]` label. Channel names that point at another synced channel in the same sync group are shown as native `#channel` links in each workspace.

## Refresh Behavior

The Home tab and User Mapping screens have Refresh buttons. To keep API usage low, repeated clicks with no data changes are handled lightly: a 60-second cooldown applies, and when nothing has changed the app reuses cached content and shows "No new data. Wait __ seconds before refreshing again."

## Media Sync

Images and videos are downloaded from the source and uploaded directly to each target channel. GIFs from the Slack GIF picker or GIPHY are synced as image blocks.

| Source message | What appears in target workspace |
|---|---|
| Text only | Single message with text, shown under the original poster's name and avatar |
| GIF (Slack picker / GIPHY) | Single message with the GIF embedded inline via image block, under the poster's name |
| GIF + text | Single message with text and GIF together, under the poster's name |
| Photo or video only (no text) | Single file upload with `Shared by @User` (tagged if mapped, plain name otherwise) |
| Text + photo or video | Text message under the poster's name, then the file in a thread reply with `Shared by @User in this message` linking back to the text |
| Multiple files | Same as above; all files are uploaded together in a single thread reply |

## External Connections

*(Opt-in — set `SYNCBOT_FEDERATION_ENABLED=true` and `SYNCBOT_PUBLIC_URL` to enable)*

Workspaces running their own SyncBot deployment can be connected via the "External Connections" section on the Home tab. One admin generates a connection code and shares it out-of-band; the other admin enters it. Messages, edits, deletes, reactions, and user matching work across instances. The receiving SyncBot instance rewrites `@` mentions and `#` channel links using the same rules as same-instance sync (native tags when mapped / synced, fallbacks otherwise).

**Data Migration** in the same section lets you export your workspace data (syncs, channels, post meta, user directory, user mappings) for moving to another instance, or import a migration file after connecting. See [Backup and Migration](BACKUP_AND_MIGRATION.md) for details.

## Backup / Restore

**Backup/Restore** appears on the Home tab only when the operator has set `PRIMARY_WORKSPACE` to this workspace’s Slack Team ID (env, SAM, Terraform, or GitHub variable) and **redeployed**. When it is unset, backup is hidden everywhere.

Use it to download a full-instance backup (all durable tables as JSON) or restore from a backup file. Intended for disaster recovery (e.g. before rebuilding AWS). See [Backup and Migration](BACKUP_AND_MIGRATION.md).
