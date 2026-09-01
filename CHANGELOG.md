# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

<!-- version list -->


## [1.3.2] - 2026-08-31

### Fixed

- Private Channels can now actually be synced when an operator allows them in **Settings**. Publishing or subscribing a private Channel adds SyncBot to it for you, using the permission of the admin who picked it, because Slack does not let an app add itself to a private Channel. Previously the Channel was silently never published and SyncBot never appeared in it.
- Adding SyncBot to a private Channel by hand no longer backfires. SyncBot is now recorded as belonging to the Channel before it joins, so it stays instead of announcing that the Channel is not part of a Channel Sync and leaving.
- If SyncBot cannot be added to the Channel after all, the half-finished Channel Sync is removed and the admin is sent a direct message explaining why, rather than leaving a Channel on the Home tab that SyncBot cannot read.
- Clicking **Authorize SyncBot** and then Allow no longer fails with `invalid_browser`. The button now always starts at this instance's `/slack/install` URL, which is the only starting point Bolt will accept. After Allow, the Home tab updates so the Authorize section disappears without a manual Refresh.
- Revoking your own authorization no longer pauses syncing for the whole workspace. Slack can include `tokens.bot` on a personal revoke; SyncBot now checks that the bot token still works before treating it as an uninstall.
- After a revoke, **Authorize SyncBot** comes back and that person's Home tab still opens. SyncBot deletes their installation row instead of leaving an empty one that made Slack show "This is still a work in progress." **Refresh** is on Home for everyone so a non-admin can reload the tab if it did not update on its own.
- Uninstalling SyncBot now drops every stored bot and user token for that workspace (Bolt's `delete_all`), so a later reinstall does not reuse dead authorizations. Paste the updated app manifest so Slack also sends `app_uninstalled`.

### Added

- **Authorize SyncBot** on the Home tab: a short section, shown to anyone who has not granted every current user permission, with a plain-language list of what is still needed (and, on a later scope change, a checkmarked list of what they already allowed so it does not look like a redo). It disappears once that person is fully authorized. The original installer already has this from adding the app, so they usually never see the button; another admin's authorization is not reused. Picking a private Channel without it now explains the problem in the dialog instead of failing after the dialog closes. The button opens this instance's `/slack/install` page (not Slack's copy of the authorize URL) so the browser can complete OAuth after Allow.

### Changed

- `REQUIRE_ADMIN` no longer blanks the whole Home tab for non-admins. It restricts configuration — creating groups, publishing, Settings — while every user can open Home, authorize SyncBot, and use **Refresh**. **SyncBot Configuration** sits directly under **Authorize SyncBot**; the rest of Home stays behind "This area of SyncBot is limited to Workspace Admins."
- `SYNCBOT_PUBLIC_URL` is leftover deploy config and is ignored. Authorize SyncBot and federation use the Host of incoming Slack requests (the same origin as the Event URL) instead.


## [1.3.1] - 2026-08-31

### Fixed

- Channel pickers no longer stop at the first 100 channels. Publishing and subscribing now use Slack's own channel search, so every channel in the workspace is reachable by typing a few letters, however many channels you have.
- Picking a channel that is already part of a Channel Sync now explains the problem in the dialog and asks for a different channel, instead of closing the modal as though it had worked. Subscribing reports this the same way publishing already did.

### Changed

- A channel may belong to only one Channel Sync at a time, and this is now enforced instance-wide rather than per workspace. Two syncs sharing a channel had no defined message routing. Channels that were previously unpublished are still free to reuse.
- Retention, private-channel publishing, and the broadcast allow-list are set only in the **Settings** modal. They are no longer read from the environment; leftover deploy values are ignored and a warning is logged.
- Private channels are off by default. When an operator turns them on in **Settings**, the dialog warns that a private channel's messages will be copied into the other workspaces in the group. The new and join sync dialogs follow the same policy.
- **Publish Channel** vs **Subscribe** — The group button that used to say Sync Channel is now **Publish Channel**, matching the Unpublish teardown on the publishing side. Other workspaces join with **Subscribe** rather than Start Syncing. Channel notices match: subscribe posts say a workspace subscribed, and publishing no longer says "for Syncing". Pause, Resume, and Stop Syncing are unchanged: they still describe a live two-way link, not the join action.
- A channel SyncBot cannot read is rejected rather than accepted and then failing during setup.


## [1.3.0] - 2026-08-30

### Added

- Group ownership: an owner can promote another workspace with **Promote to Owner**, and step down itself with **Give Up Ownership** while another owner remains. A group always keeps at least one owner, so a sole owner is asked to promote a successor before leaving instead of failing silently.
- **Disband Group** removes a group, its syncs, and its user mappings in one step, offered only to a workspace that is both the sole owner and the sole publisher so it cannot destroy another workspace's syncs. It confirms first and lists what will be removed.
- A group owner can now cancel a pending invite, alongside the workspace that sent it.
- Operator **Settings** modal in the Home tab, visible only to `PRIMARY_WORKSPACE`, for instance-wide policy: soft-delete retention days, whether private channels may be synced, and which workspaces may publish broadcasts. Settings are stored in the database, so changing them no longer needs a redeploy.

### Changed

- `SOFT_DELETE_RETENTION_DAYS`, `ALLOW_PRIVATE_CHANNELS`, and `BROADCAST_ALLOWED_WORKSPACES` are now seed values: the database value from the Settings modal wins once saved, otherwise the environment variable applies, otherwise a built-in default. `PRIMARY_WORKSPACE`, `ENABLE_DB_RESET`, and `REQUIRE_ADMIN` stay environment-only on purpose, since they gate who can reach the modal and the destructive actions beside it.
- Retention is read at call time rather than at process start, so a change applies without a restart. The private-channel and broadcast settings are stored now and begin taking effect with the channel picker rework and broadcast channels.
- Ownership survives an uninstall: it passes to the longest-standing remaining member only when a workspace's data is actually deleted, so reinstalling within the retention window restores the group unchanged.
- Destructive confirmations (leave group, disband group, stop syncing) now show a red confirmation button in the dialog, matching the Reset Database prompt, so the action you are about to take reads as destructive rather than routine.


## [1.2.6] - 2026-08-30

### Fixed

- Repair channel unpublish and publisher teardown (#25)



## [1.2.5] - 2026-08-30

### Fixed

- Authorize group invite accept, decline, and cancel (#24)



## [1.2.4] - 2026-08-30

### Fixed

- Trust GitHub's immutable OIDC subject claim (#22)



## [1.2.3] - 2026-08-30

### Fixed

- Build Lambda in source so SAM can reach syncbot/ (#21)



## [1.2.2] - 2026-08-30

### Changed

- Cloud deploy uses `DATABASE_BACKEND` (`mysql` / `postgresql` / `sqlite`); old alias names warn until 2.0.0
- Provider knobs are `AWS_*` / `GCP_*`; GitHub Actions picks a provider with `GITHUB_DEPLOY_TARGET`
- `./deploy.sh` reads `CLOUD_PROVIDER` from the env file; there is no `aws` or `gcp` command-line argument
- AWS GitHub Actions sets stage from the job (`test` or `prod`) instead of a `STAGE_NAME` variable
- The first AWS deploy creates the bootstrap stack when it is missing
- GCP GitHub Actions stays image-only and prints Slack install / OAuth / event URLs in the job summary
- First-time deploy docs and `.env.deploy.example` match the AWS and GCP paths
- AWS region default is `us-east-1`

### Fixed

- `./deploy.sh` no longer looks up leftover Secrets Manager / Secret Manager IDs

## [1.2.1] - 2026-08-27

### Fixed

- Drop AWS stack RDS and add SQLite Litestream to S3 (#17)

## [1.2.0] - 2026-08-27

### Added

- GCP: SQLite + Litestream to GCS as the free Cloud Run default; existing MySQL/TiDB remains opt-in
- Canonical sprocktech/syncbot release automation (python-semantic-release, Dependabot auto-merge)

### Changed

- Cloud Run image updates are CI-only; later `terraform apply` does not revert the image
- `./deploy.sh` does not run `poetry update`; local and GitHub deploys install committed pins
- Bumped Python and GitHub Actions dependencies (patch/minor)

### Fixed

- Slack message and reaction sync is idempotent on envelope `event_id`, so retries do not double-post

## [1.1.0] - 2026-04-21

### Added

- `--bootstrap`, `--setup-github`, `--update-stack`, `--verbose` deploy flags (both interactive and non-interactive)
- `GITHUB_REPO` env var to skip interactive repo prompt when multiple remotes exist
- `.env.deploy.example` template for cloud deployments
- CI: bootstrap sync, `workflow_dispatch`, concurrency groups, `pip-audit`
- AWS: auto-fallback to `update-stack` when `sam deploy` fails on changeset validation
- Deploy summary with OAuth redirect URL, consistent across all paths

### Changed

- AWS: Lambda Function URLs replace API Gateway; Secrets Manager removed
- GCP: Secret Manager removed (secrets via Terraform variables)
- `TOKEN_ENCRYPTION_KEY` renamed to `DATA_ENCRYPTION_KEY` (legacy fallback kept)
- Deploy env vars simplified: `DATABASE_*` replaces `EXISTING_DATABASE_*`
- `DATABASE_USER` is a GitHub environment variable, not a secret
- `DatabaseSchema` convention (`syncbot_<stage>`) documented in prompts, example, and docs
- `DbSetup` skipped when `DATABASE_USER` + `DATABASE_PASSWORD` provided directly
- Bumped GitHub Actions dependencies (`checkout` v6, `setup-python` v6, etc.)

### Fixed

- Interactive GitHub push: Lambda SG ID and `SLACK_CLIENT_ID` now set correctly
- CI script: log group cleanup output to stderr; defensive `mkdir` before `sam package`

## [1.0.2] - 2026-03-28

### Added

- External DB deploy parameters: `ExistingDatabasePort`, `ExistingDatabaseCreateAppUser`, `ExistingDatabaseCreateSchema`, `ExistingDatabaseUsernamePrefix`, `ExistingDatabaseAppUsername` (AWS) / GCP equivalents — support TiDB Cloud and other managed DB providers with cluster-prefixed usernames and 32-char limits

### Changed

- Synced message author shows local display name and avatar for mapped users, including federated messages (no workspace suffix)
- Shortened default DB usernames: `sbadmin_{stage}` (was `syncbot_admin_{stage}`), `sbapp_{stage}` (was `syncbot_user_{stage}`). Existing RDS instances keep their original master username.
- Bumped GitHub Actions: `actions/checkout` v6, `actions/setup-python` v6, `actions/upload-artifact` v7, `actions/download-artifact` v8, `aws-actions/configure-aws-credentials` v6
- Dependabot: ignore semver-major updates for the Docker `python` image (keeps base image on Python 3.12.x line)
- AWS Lambda: Alembic migrations now run via a post-deploy invoke instead of on every cold start, fixing Slack ack timeouts after deployment; Cloud Run and local dev unchanged
- AWS Lambda memory increased from 128 MB to 256 MB for faster cold starts
- EventBridge keep-warm invokes now return a clean JSON response instead of falling through to Slack Bolt
- AWS bootstrap deploy policy: added `lambda:InvokeFunction` -- **re-run the deploy script (Bootstrap task) or `aws cloudformation deploy` the bootstrap stack to pick up this permission**

### Fixed

- Replaced deprecated `datetime.utcnow()` with `datetime.now(UTC)` in backup/migration export helpers

## [1.0.1] - 2026-03-26

### Changed

- Cross-workspace `#channel` links resolve to native local channels when the channel is part of the same sync; otherwise use workspace archive URLs with a code-formatted fallback
- `@mentions` and `#channel` links in federated messages are now resolved on the receiving instance (native tags when mapped/synced, fallbacks otherwise)
- `ENABLE_DB_RESET` is now a boolean (`true` / `1` / `yes`) instead of a Slack Team ID; requires `PRIMARY_WORKSPACE` to match

### Added

- `PRIMARY_WORKSPACE` env var: must be set to a Slack Team ID for backup/restore to appear. Also scopes DB reset to that workspace.

## [1.0.0] - 2026-03-25

### Added

- Multi-workspace message sync: messages, threads, edits, deletes, reactions, images, videos, and GIFs
- Cross-workspace @mention resolution (email, name, and manual matching)
- Workspace Groups with invite codes (many-to-many collaboration; direct and group-wide sync modes)
- Pause, resume, and stop per-channel sync controls
- App Home tab for configuration (no slash commands)
- Cross-instance federation (optional, HMAC-authenticated)
- Backup/restore and workspace data migration
- Bot token encryption at rest (Fernet)
- AWS deployment (SAM/CloudFormation) with optional CI/CD via GitHub Actions
- GCP deployment (Terraform/Cloud Run) with interactive deploy script; GitHub Actions workflow for GCP is not yet fully wired
- Dev Container and Docker Compose for local development
- Structured JSON logging with correlation IDs and CloudWatch alarms (AWS)
- PostgreSQL, MySQL, and SQLite database backends
- Alembic-managed schema migrations applied at startup
