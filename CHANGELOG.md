# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

<!-- version list -->

## [1.4.0] - Unreleased

### Added

- Federation is enabled from the Settings modal on the primary workspace
- Extra workspace managers can configure groups and syncs without opening Settings
- Per-workspace Allow private Channels in Settings on every installed workspace
- Per-channel send/receive reaction direction and direct vs hybrid reaction type
- Edit reactions on synced Channel rows on Home

### Changed

- Leftover `SYNCBOT_FEDERATION_ENABLED` is ignored after a one-time upgrade seed
- Settings is available on every workspace for Slack admins; instance fields stay primary-only
- Leftover `REQUIRE_ADMIN` and `ALLOW_PRIVATE_CHANNELS` env vars are ignored
- Leftover instance-wide private-channel policy is copied to each workspace on upgrade

### Fixed

- Reaction notices no longer echo into the origin Channel


## [1.3.3] - 2026-09-01

### Changed

- Home no longer keeps leftover DeSync or Create new / Join existing Sync routes; Publish, Subscribe, Unpublish, and Stop Syncing are the Channel Sync path

### Fixed

- Data Migration export/import looks up Workspaces by integer id via `get_workspace_by_id`


## [1.3.2] - 2026-08-31

### Added

- **Authorize SyncBot** on Home for anyone missing user permissions (starts at `/slack/install`)

### Changed

- `REQUIRE_ADMIN` still limits configuration; every user can open Home, authorize, and Refresh
- `SYNCBOT_PUBLIC_URL` is ignored; Authorize and federation use the request Host

### Fixed

- Private Channels can be published and subscribed: SyncBot is invited in, stays when added by hand, and a failed invite rolls back with a DM
- OAuth Allow no longer fails with `invalid_browser`; Home updates after Authorize
- Revoking your own token no longer pauses the workspace; Authorize returns, and uninstall still clears all tokens
- Home lists available Channels by name, not Slack ID
- Private-Channel invites no longer fail with `user_not_found` after use in another workspace


## [1.3.1] - 2026-08-31

### Fixed

- Channel pickers search the whole workspace instead of stopping at 100 channels
- Picking a channel already in a Channel Sync explains the problem in the dialog

### Changed

- A channel may belong to only one Channel Sync instance-wide
- Retention, private-channel publishing, and the broadcast allow-list are set only in **Settings**
- Private channels are off by default; the dialog warns that messages will be copied
- **Publish Channel** and **Subscribe** replace Sync Channel and Start Syncing
- A channel SyncBot cannot read is rejected rather than failing later


## [1.3.0] - 2026-08-30

### Added

- Group ownership: **Promote to Owner** and **Give Up Ownership**, keeping at least one owner
- **Disband Group** for a sole owner that is also the sole publisher
- Group owners can cancel a pending invite
- Operator **Settings** for retention, private channels, and the broadcast allow-list

### Changed

- `SOFT_DELETE_RETENTION_DAYS`, `ALLOW_PRIVATE_CHANNELS`, and `BROADCAST_ALLOWED_WORKSPACES` are seed values; Settings wins once saved
- Retention changes apply without a restart
- Ownership survives uninstall until the workspace's data is deleted
- Destructive confirmations use a red button


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
