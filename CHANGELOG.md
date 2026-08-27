# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

<!-- version list -->

## v1.2.0 (2026-08-27)

### Bug Fixes

- **sync**: Make Slack event processing idempotent (at-least-once safe)
  ([#13](https://github.com/sprocktech/syncbot/pull/13),
  [`c7c94bd`](https://github.com/sprocktech/syncbot/commit/c7c94bd35cde4cc0a9ada728cf852d3a4aabdf54))

Claim envelope event_id before message and reaction side effects so Slack retries recover failures
  without double-posting, and drop the GCP min=0 rare-drop caveat.

Co-authored-by: Cursor <cursoragent@cursor.com>

### Chores

- **deps**: Bump python-semantic-release/python-semantic-release from 9.21.1 to 9.21.2 in the
  github-actions group ([#7](https://github.com/sprocktech/syncbot/pull/7),
  [`dfdcf78`](https://github.com/sprocktech/syncbot/commit/dfdcf787649cba774fb761207e5f3c021b7278f5))

chore(deps): bump python-semantic-release/python-semantic-release

Bumps the github-actions group with 1 update:
  [python-semantic-release/python-semantic-release](https://github.com/python-semantic-release/python-semantic-release).

Updates `python-semantic-release/python-semantic-release` from 9.21.1 to 9.21.2 - [Release
  notes](https://github.com/python-semantic-release/python-semantic-release/releases) -
  [Changelog](https://github.com/python-semantic-release/python-semantic-release/blob/master/CHANGELOG.rst)
  -
  [Commits](https://github.com/python-semantic-release/python-semantic-release/compare/v9.21.1...v9.21.2)

--- updated-dependencies: - dependency-name: python-semantic-release/python-semantic-release
  dependency-version: 9.21.2

dependency-type: direct:production

update-type: version-update:semver-patch

dependency-group: github-actions ...

Signed-off-by: dependabot[bot] <support@github.com>

Co-authored-by: dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>

- **deps**: Bump the python-patch-minor group across 1 directory with 10 updates
  ([#12](https://github.com/sprocktech/syncbot/pull/12),
  [`d635e2b`](https://github.com/sprocktech/syncbot/commit/d635e2b36d9854e24c8a897732679776c3b18fdc))

* chore(deps): bump the python-patch-minor group across 1 directory with 10 updates

Bumps the python-patch-minor group with 10 updates in the / directory:

| Package | From | To | | --- | --- | --- | | [alembic](https://github.com/sqlalchemy/alembic) |
  `1.18.4` | `1.19.1` | | [python-dotenv](https://github.com/theskumar/python-dotenv) | `1.2.2` |
  `1.2.3` | | [slack-bolt](https://github.com/slackapi/bolt-python) | `1.28.0` | `1.30.0` | |
  [sqlalchemy](https://github.com/sqlalchemy/sqlalchemy) | `2.0.49` | `2.0.52` | |
  [pymysql](https://github.com/PyMySQL/PyMySQL) | `1.1.2` | `1.2.0` | |
  [requests](https://github.com/psf/requests) | `2.33.1` | `2.34.2` | |
  [certifi](https://github.com/certifi/python-certifi) | `2026.2.25` | `2026.7.22` | |
  [charset-normalizer](https://github.com/jawah/charset_normalizer) | `3.4.7` | `3.5.1` | |
  [slack-sdk](https://github.com/slackapi/python-slack-sdk) | `3.41.0` | `3.43.0` | |
  [typing-extensions](https://github.com/python/typing_extensions) | `4.15.0` | `4.16.0` |

Updates `alembic` from 1.18.4 to 1.19.1 - [Release
  notes](https://github.com/sqlalchemy/alembic/releases) -
  [Changelog](https://github.com/sqlalchemy/alembic/blob/main/CHANGES) -
  [Commits](https://github.com/sqlalchemy/alembic/commits)

Updates `python-dotenv` from 1.2.2 to 1.2.3 - [Release
  notes](https://github.com/theskumar/python-dotenv/releases) -
  [Changelog](https://github.com/theskumar/python-dotenv/blob/main/CHANGELOG.md) -
  [Commits](https://github.com/theskumar/python-dotenv/compare/v1.2.2...v1.2.3)

Updates `slack-bolt` from 1.28.0 to 1.30.0 - [Release
  notes](https://github.com/slackapi/bolt-python/releases) -
  [Commits](https://github.com/slackapi/bolt-python/compare/v1.28.0...v1.30.0)

Updates `sqlalchemy` from 2.0.49 to 2.0.52 - [Release
  notes](https://github.com/sqlalchemy/sqlalchemy/releases) -
  [Changelog](https://github.com/sqlalchemy/sqlalchemy/blob/main/CHANGES.rst) -
  [Commits](https://github.com/sqlalchemy/sqlalchemy/commits)

Updates `pymysql` from 1.1.2 to 1.2.0 - [Release notes](https://github.com/PyMySQL/PyMySQL/releases)
  - [Changelog](https://github.com/PyMySQL/PyMySQL/blob/main/CHANGELOG.md) -
  [Commits](https://github.com/PyMySQL/PyMySQL/compare/v1.1.2...v1.2.0)

Updates `requests` from 2.33.1 to 2.34.2 - [Release notes](https://github.com/psf/requests/releases)
  - [Changelog](https://github.com/psf/requests/blob/main/HISTORY.md) -
  [Commits](https://github.com/psf/requests/compare/v2.33.1...v2.34.2)

Updates `certifi` from 2026.2.25 to 2026.7.22 -
  [Commits](https://github.com/certifi/python-certifi/compare/2026.02.25...2026.07.22)

Updates `charset-normalizer` from 3.4.7 to 3.5.1 - [Release
  notes](https://github.com/jawah/charset_normalizer/releases) -
  [Changelog](https://github.com/jawah/charset_normalizer/blob/master/CHANGELOG.md) -
  [Commits](https://github.com/jawah/charset_normalizer/compare/3.4.7...3.5.1)

Updates `slack-sdk` from 3.41.0 to 3.43.0 - [Release
  notes](https://github.com/slackapi/python-slack-sdk/releases) -
  [Commits](https://github.com/slackapi/python-slack-sdk/compare/v3.41.0...v3.43.0)

Updates `typing-extensions` from 4.15.0 to 4.16.0 - [Release
  notes](https://github.com/python/typing_extensions/releases) -
  [Changelog](https://github.com/python/typing_extensions/blob/main/CHANGELOG.md) -
  [Commits](https://github.com/python/typing_extensions/compare/4.15.0...4.16.0)

--- updated-dependencies: - dependency-name: alembic dependency-version: 1.19.1

dependency-type: direct:production

update-type: version-update:semver-minor

dependency-group: python-patch-minor

- dependency-name: certifi dependency-version: 2026.7.22

- dependency-name: charset-normalizer dependency-version: 3.5.1

- dependency-name: pymysql dependency-version: 1.2.0

- dependency-name: python-dotenv dependency-version: 1.2.3

update-type: version-update:semver-patch

- dependency-name: requests dependency-version: 2.34.2

- dependency-name: slack-bolt dependency-version: 1.30.0

- dependency-name: slack-sdk dependency-version: 3.43.0

- dependency-name: sqlalchemy dependency-version: 2.0.52

- dependency-name: typing-extensions dependency-version: 4.16.0

dependency-group: python-patch-minor ...

Signed-off-by: dependabot[bot] <support@github.com>

* chore: sync requirements.txt files with poetry.lock

Automated export from poetry.lock.

---------

Co-authored-by: dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>

Co-authored-by: github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>

- **deps**: Bump the python-patch-minor group across 1 directory with 12 updates
  ([#8](https://github.com/sprocktech/syncbot/pull/8),
  [`a24c8dc`](https://github.com/sprocktech/syncbot/commit/a24c8dc0640b1938d22976e883b8d55278afbb78))

* chore(deps): bump the python-patch-minor group across 1 directory with 12 updates

Bumps the python-patch-minor group with 12 updates in the / directory:

| Package | From | To | | --- | --- | --- | | [alembic](https://github.com/sqlalchemy/alembic) |
  `1.18.4` | `1.19.1` | | [python-dotenv](https://github.com/theskumar/python-dotenv) | `1.2.2` |
  `1.2.3` | | [slack-bolt](https://github.com/slackapi/bolt-python) | `1.28.0` | `1.30.0` | |
  [sqlalchemy](https://github.com/sqlalchemy/sqlalchemy) | `2.0.49` | `2.0.52` | |
  [pymysql](https://github.com/PyMySQL/PyMySQL) | `1.1.2` | `1.2.0` | |
  [requests](https://github.com/psf/requests) | `2.33.1` | `2.34.2` | |
  [boto3](https://github.com/boto/boto3) | `1.42.93` | `1.43.78` | |
  [pytest](https://github.com/pytest-dev/pytest) | `9.0.3` | `9.1.1` | |
  [certifi](https://github.com/certifi/python-certifi) | `2026.2.25` | `2026.7.22` | |
  [charset-normalizer](https://github.com/jawah/charset_normalizer) | `3.4.7` | `3.5.1` | |
  [slack-sdk](https://github.com/slackapi/python-slack-sdk) | `3.41.0` | `3.43.0` | |
  [typing-extensions](https://github.com/python/typing_extensions) | `4.15.0` | `4.16.0` |

Updates `alembic` from 1.18.4 to 1.19.1 - [Release
  notes](https://github.com/sqlalchemy/alembic/releases) -
  [Changelog](https://github.com/sqlalchemy/alembic/blob/main/CHANGES) -
  [Commits](https://github.com/sqlalchemy/alembic/commits)

Updates `python-dotenv` from 1.2.2 to 1.2.3 - [Release
  notes](https://github.com/theskumar/python-dotenv/releases) -
  [Changelog](https://github.com/theskumar/python-dotenv/blob/main/CHANGELOG.md) -
  [Commits](https://github.com/theskumar/python-dotenv/compare/v1.2.2...v1.2.3)

Updates `slack-bolt` from 1.28.0 to 1.30.0 - [Release
  notes](https://github.com/slackapi/bolt-python/releases) -
  [Commits](https://github.com/slackapi/bolt-python/compare/v1.28.0...v1.30.0)

Updates `sqlalchemy` from 2.0.49 to 2.0.52 - [Release
  notes](https://github.com/sqlalchemy/sqlalchemy/releases) -
  [Changelog](https://github.com/sqlalchemy/sqlalchemy/blob/main/CHANGES.rst) -
  [Commits](https://github.com/sqlalchemy/sqlalchemy/commits)

Updates `pymysql` from 1.1.2 to 1.2.0 - [Release notes](https://github.com/PyMySQL/PyMySQL/releases)
  - [Changelog](https://github.com/PyMySQL/PyMySQL/blob/main/CHANGELOG.md) -
  [Commits](https://github.com/PyMySQL/PyMySQL/compare/v1.1.2...v1.2.0)

Updates `requests` from 2.33.1 to 2.34.2 - [Release notes](https://github.com/psf/requests/releases)
  - [Changelog](https://github.com/psf/requests/blob/main/HISTORY.md) -
  [Commits](https://github.com/psf/requests/compare/v2.33.1...v2.34.2)

Updates `boto3` from 1.42.93 to 1.43.78 - [Release notes](https://github.com/boto/boto3/releases) -
  [Commits](https://github.com/boto/boto3/compare/1.42.93...1.43.78)

Updates `pytest` from 9.0.3 to 9.1.1 - [Release
  notes](https://github.com/pytest-dev/pytest/releases) -
  [Changelog](https://github.com/pytest-dev/pytest/blob/main/CHANGELOG.rst) -
  [Commits](https://github.com/pytest-dev/pytest/compare/9.0.3...9.1.1)

Updates `certifi` from 2026.2.25 to 2026.7.22 -
  [Commits](https://github.com/certifi/python-certifi/compare/2026.02.25...2026.07.22)

Updates `charset-normalizer` from 3.4.7 to 3.5.1 - [Release
  notes](https://github.com/jawah/charset_normalizer/releases) -
  [Changelog](https://github.com/jawah/charset_normalizer/blob/master/CHANGELOG.md) -
  [Commits](https://github.com/jawah/charset_normalizer/compare/3.4.7...3.5.1)

Updates `slack-sdk` from 3.41.0 to 3.43.0 - [Release
  notes](https://github.com/slackapi/python-slack-sdk/releases) -
  [Commits](https://github.com/slackapi/python-slack-sdk/compare/v3.41.0...v3.43.0)

Updates `typing-extensions` from 4.15.0 to 4.16.0 - [Release
  notes](https://github.com/python/typing_extensions/releases) -
  [Changelog](https://github.com/python/typing_extensions/blob/main/CHANGELOG.md) -
  [Commits](https://github.com/python/typing_extensions/compare/4.15.0...4.16.0)

--- updated-dependencies: - dependency-name: alembic dependency-version: 1.19.1

dependency-type: direct:production

update-type: version-update:semver-minor

dependency-group: python-patch-minor

- dependency-name: boto3 dependency-version: 1.43.78

dependency-type: direct:development

- dependency-name: certifi dependency-version: 2026.7.22

- dependency-name: charset-normalizer dependency-version: 3.5.1

- dependency-name: pymysql dependency-version: 1.2.0

- dependency-name: pytest dependency-version: 9.1.1

- dependency-name: python-dotenv dependency-version: 1.2.3

update-type: version-update:semver-patch

- dependency-name: requests dependency-version: 2.34.2

- dependency-name: slack-bolt dependency-version: 1.30.0

- dependency-name: slack-sdk dependency-version: 3.43.0

- dependency-name: sqlalchemy dependency-version: 2.0.52

- dependency-name: typing-extensions dependency-version: 4.16.0

dependency-group: python-patch-minor ...

Signed-off-by: dependabot[bot] <support@github.com>

* chore: sync requirements.txt files with poetry.lock

Automated export from poetry.lock.

---------

Co-authored-by: dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>

Co-authored-by: github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>

### Continuous Integration

- Automate releases and Dependabot patch/security merges
  ([#6](https://github.com/sprocktech/syncbot/pull/6),
  [`ca3750d`](https://github.com/sprocktech/syncbot/commit/ca3750d8d31ff735e156e7a5c63676aad483ff7f))

* ci: automate releases and Dependabot patch/security merges

Canonical sprocktech/syncbot gets python-semantic-release, grouped patch/minor Dependabot with
  security auto-merge, and CI that can go green without stranding unrelated PRs.

Co-authored-by: Cursor <cursoragent@cursor.com>

* ci: unblock ruff format and pytest without Slack secrets

CI ruff 0.8.6 flagged UP038 and unformatted files; pytest collected app.py without
  LOCAL_DEVELOPMENT/.env and raised on missing Slack vars.

---------

- Keep Dependabot PR checks green after requirements-sync
  ([#10](https://github.com/sprocktech/syncbot/pull/10),
  [`503b492`](https://github.com/sprocktech/syncbot/commit/503b4927226cf760f9b94b1d8e23fac03d3033f6))

Drop [skip ci] from export commits on pull requests so ci-gate and conventional attach to HEAD.
  Detect Dependabot by PR author, not pusher.

Co-authored-by: Cursor <cursoragent@cursor.com>

- Use GitHub App for release and Dependabot sync
  ([#15](https://github.com/sprocktech/syncbot/pull/15),
  [`f1fa2e5`](https://github.com/sprocktech/syncbot/commit/f1fa2e5dccbdba96f6a3eadad73a0b79c6fc86d2))

Authenticate release, requirements-sync, and Dependabot auto-merge as sprocktech-automation so
  updateRef can bypass the main ruleset and pushes retrigger checks. Stop deploy.sh from running
  poetry update.

Co-authored-by: Cursor <cursoragent@cursor.com>

- **release**: Pin GitPython below 3.1.60 ([#14](https://github.com/sprocktech/syncbot/pull/14),
  [`8846b06`](https://github.com/sprocktech/syncbot/commit/8846b06aa33db6d6a3e3cabd92da44d767a11725))

Run python-semantic-release on the runner instead of the unpinned Docker action so GitPython 3.1.60
  cannot break Actor.name_email_regex.

Co-authored-by: Cursor <cursoragent@cursor.com>

### Documentation

- Point contributing and development guides at sprocktech/syncbot
  ([#5](https://github.com/sprocktech/syncbot/pull/5),
  [`8cf154f`](https://github.com/sprocktech/syncbot/commit/8cf154fc047b8d6b0ed43af59e28279c76a384da))

Canonical public code lives at sprocktech/syncbot rather than F3Nation-Community/syncbot.

Co-authored-by: Cursor <cursoragent@cursor.com>

### Features

- **gcp**: Default Cloud Run to SQLite with Litestream
  ([#11](https://github.com/sprocktech/syncbot/pull/11),
  [`65956b8`](https://github.com/sprocktech/syncbot/commit/65956b8e7a2990850df6dbabedb1375c3a61726e))

Use SQLite plus Litestream to GCS as the free GCP default, keep existing MySQL/TiDB as opt-in, and
  replace the deploy-gcp stub with image-only CI.

Co-authored-by: Cursor <cursoragent@cursor.com>


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
