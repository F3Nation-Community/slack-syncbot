# AGENTS — SyncBot

This file is the primary instruction set for AI coding agents (Cursor, GitHub Copilot, Codex, Claude via `CLAUDE.md`).

## Project snapshot

SyncBot is a **Slack app** that syncs messages, threads, edits, deletes, reactions, and media across **multiple Slack workspaces** (and optional **federation** between instances). The runtime is **Python 3.12+**, **Poetry** for dependencies, **SQLAlchemy** + **Alembic** for the database, and **Slack Bolt**.

Deployments supported in-repo: **AWS** (Lambda + SAM) and **GCP** (Cloud Run + Terraform). Application code under `syncbot/` must stay **cloud‑neutral**; provider-specific pieces live only under `infra/aws/`, `infra/gcp/`, and deploy workflows.

## Repo map (short)

- `syncbot/` — application (`app.py`, handlers, db, federation).
- `syncbot/db/alembic/` — schema migrations.
- `tests/`, `infra/aws/tests/`, `infra/gcp/tests/` — pytest suites.
- `infra/aws/`, `infra/gcp/` — SAM/Terraform, bootstrap, CI deploy helpers.
- `docs/` — architecture, deploy, infra contract, user guide.
- `.github/workflows/` — CI, deploy, release automation.

See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for Dev Container / Docker Compose / native setup.

## Setup (local)

```bash
poetry install --with dev
pre-commit install
pre-commit install --hook-type commit-msg
```

## Verify changes (run before opening a PR)

```bash
pre-commit run --all-files
poetry run ruff check .
poetry run ruff format --check .
poetry run pytest -q tests/ infra/aws/tests infra/gcp/tests
```

On PRs, GitHub Actions runs the same test command (see `.github/workflows/ci.yml`).

## Hard rules (guardrails)

1. **Provider-neutral `syncbot/`** — do not add `boto3`, `google.cloud`, or other cloud SDK imports under `syncbot/`. Use `infra/<provider>/` and workflows for provider code. This is also enforced in CI (`forbidden-imports` job).
2. **Version & changelog** — do not bump `pyproject.toml`’s `version` in a feature PR. Releases on `main` are automated with **python-semantic-release**. Changelog and GitHub Release notes use Keep a Changelog headings (**Added** / **Changed** / **Fixed**) and **[1.2.0](CHANGELOG.md)** length: one short line per bullet (what changed), not a why/how paragraph, not `### bug fixes`, not a raw commit subject. Polishing an already-released heading is OK. You may pre-write the next version's section below `<!-- version list -->` in a release PR so the GitHub Release copies it. See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) and [CONTRIBUTING.md](CONTRIBUTING.md).
3. **Requirements files** — do not edit `syncbot/requirements.txt` by hand. Change dependencies in `pyproject.toml` and run the pre-commit `sync-requirements` hook or `poetry export` as in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).
4. **Secrets** — never commit real `.env`, `.env.deploy.*` (only `*.example` templates), private keys, or large artifacts. Do not commit `.aws-sam/build` output.
5. **Deployment branches** — on a **fork**, do not push to `test` or `prod` as a casual step; those branches deploy. Canonical releases run only on **F3Nation-Community/slack-syncbot** `main` (see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)). Forks pull `main` and promote to `test`/`prod` themselves.
6. **Conventional Commits** — PR titles and commit subjects are one short imperative line (`fix: …`, about 72 characters). Skip the body unless you need `BREAKING CHANGE:` or a single clause that will not fit. See [CONTRIBUTING.md](CONTRIBUTING.md).
7. **Docs** — if behavior, env vars, or deploy steps change, update the relevant `docs/*.md` (and [docs/INFRA_CONTRACT.md](docs/INFRA_CONTRACT.md) if the runtime contract changes). Follow **Docs voice** below.

## Docs voice

Operator-facing docs (README, `docs/*.md`, `.env*.example`, `infra/gcp/example.tfvars`) should read like a helpful teammate: friendly and explanatory, not clipped or telegraphic. Write full sentences (do not drop “the” or “a” to shorten a line). Prefer a short paragraph over a dense jargon pile.

Stage is only **`test`** or **`prod`**. Do not use `YOURSTAGE` as if the name were arbitrary. Show the real values (`--env test`, `slack-manifest_test.json`, `syncbot_test`) and mention `prod` as the other choice. Keep `YOUR_*` for values that really vary (region, host, username).

**Changelog / GitHub Release notes** follow Keep a Changelog and **[1.2.0](CHANGELOG.md)** length: one short line per bullet, not the operator-docs paragraph style. See `.cursor/rules/50-changelog.mdc`.

## Definition of done (AI-resolved issues)

1. **Tests** — add or update tests under `tests/` or `infra/*/tests/` for the change.
2. **Pre-commit** — `pre-commit run --all-files` passes.
3. **CI** — no new failures in `ci-gate` (requirements sync, ruff, SAM lint, pip-audit, forbidden path checks, tests).
4. **PR title** — Conventional Commit format (enforced by `.github/workflows/pr-title.yml`).
5. **PR description** — use `.github/pull_request_template.md`; a few short bullets, not a design essay. Link issues with `Fixes #123` when applicable.

## Common pitfalls

- **Bot identity is per workspace.** Never cache `auth.test` under a process-wide key. A warm Lambda handles many workspaces; reusing workspace A's bot member ID on B makes `conversations.invite` fail with `user_not_found` and can skip the unconfigured-channel leave handler. Prefer Bolt's request-scoped `context["bot_user_id"]`, and cache `auth.test` keyed by bot token.
- **Settings are modal-only.** `allow_private_channels`, `broadcast_allowed_workspaces`, and `soft_delete_retention_days` resolve database then hardcoded default; leftover env is ignored and warned. `PRIMARY_WORKSPACE`, `ENABLE_DB_RESET`, and `REQUIRE_ADMIN` stay env.
- **Private membership.** Write `Sync`/`SyncChannel` then invite. Public = bot `conversations.join`; private = `conversations.invite` with the acting user's `xoxp` only. Failed invite rolls back and DMs. Leave-on-unconfigured stays. Treat `already_in_channel` / `cant_invite_self` / `method_post_only` as success. Invite only `U…` member IDs, never `B…`. Resume passes Bolt `context` only when the channel's workspace is this request's workspace. Use `lookup_channel_meta` for Channel names (the bot cannot see a private Channel it has not joined).
- **Native `conversations_select`.** Do not rebuild channel pickers from `conversations.list` (~100 cap). The filter is advisory; validate on submit.
- **Helpers import submodules only.** Never `import helpers` inside `helpers/*.py` — `helpers/__init__.py` circular-imports at Lambda cold start. See [`syncbot/helpers/sync_cleanup.py`](syncbot/helpers/sync_cleanup.py).
- **`REQUIRE_ADMIN` gates configuration, not the whole Home tab.** Authorize SyncBot, Refresh, and viewing stay visible. Config actions still use `is_user_authorized`.
- **Home hash is per user** (`home_tab_hash:{team_id}:{user_id}`). Prefix delete on restore still works. Hash payload must include that user's permission lists.
- **Route through `routing.py`.** Do not add Bolt `@app.action` / `@app.event` — `app.py` already matches `.*` into `MAIN_MAPPER` / `VIEW_ACK_MAPPER`. A second decorator double-fires.
- **Modal field errors only in the ack phase.** `VIEW_ACK_MAPPER` may return `{"response_action": "errors", ...}` (3s budget, no Slack/DB of consequence). After ack, work-phase failures DM the user.
- **`DbManager.get_record` uses each model's `get_id()` column, not always the integer PK.** Pass that value positional or as `id=` only (`team_id=` TypeErrors). `Workspace` → Slack `team_id`. `SyncChannel` → Slack `channel_id`. `PostMeta` → `post_id`. Integer PK lookups use `find_records(... id == n)` or `get_workspace_by_id`. Objects are expunged; each call is its own session.
- **Soft-delete + no `ON DELETE CASCADE`.** Active queries need `deleted_at.is_(None)`. Hard deletes go through `purge_sync` / `purge_workspace` (children first, including soft-deleted rows).
- **Imports are `import helpers`, not `from syncbot.helpers`.** Pytest `pythonpath` is `syncbot/`.
- **Link buttons still need a no-op in `ACTION_MAPPER`.** Slack fires `block_actions` for URL buttons (`handle_authorize_syncbot` is the example); without a handler you get `no_handler` in the logs.
- **Never log tokens.** `_redact_sensitive` must include `user_token` and `bot_token`. Do not print `xoxp` / `xoxb`.
- **Unpublish is a full `purge_sync`.** Pause/resume only toggles that workspace's channel. Home teardown is **Unpublish** / **Stop Syncing** (`CONFIG_UNPUBLISH_CHANNEL` / `CONFIG_STOP_SYNC`). `CONFIG_REMOVE_SYNC` / `handle_remove_sync` is leftover DeSync — do not wire new buttons to it.
- **Events API dedup is envelope `event_id` + `team_id`** (`run_claimed`), not `event.ts` or `X-Slack-Retry-Num`. Missing `event_id` always runs. Failed work releases the claim.
- **Message loop / double-post.** Skip only SyncBot's own `bot_id`, not other bots. A plain `message` with files waits for `file_share`. Other bots' `bot_message` events are synced.
- **In-process cache is per warm container (60s).** Writes that change fan-out or Home must invalidate (`sync_list:{channel_id}`, settings keys, `home_tab_hash:` prefix).
- **Scope lockstep.** `slack_manifest_scopes.py` + `slack-manifest.json` / `_test` / `_prod` + SAM/Terraform defaults + env examples. User scopes that do not match the Slack app fail install with `invalid_scope`.
- **`get_oauth_flow()` is `None` in local single-workspace mode.** Hide Authorize if there is no install URL.
- **`PRIMARY_WORKSPACE` gates Settings, backup/restore, and DB reset** — not Authorize. Backup/reset also need the matching team; reset also needs `ENABLE_DB_RESET`.
- **A Slack channel belongs to at most one sync, instance-wide** (lookup by `channel_id`). Reject on submit with a field error.
- **Files live in `/tmp`.** That is the only writable disk on Lambda. Clean up on failure.
- **Encrypt bot tokens on write, decrypt on read.** Encryption is off when the key is missing, shorter than 16 characters, or a placeholder (`123`, `changeme`, `secret`, `password`). Federation webhooks are HTTPS-only in production; HTTP only when `LOCAL_DEVELOPMENT`.
- **Group role `admin` is reserved and never written.** Do not start using it; no permission attaches yet.
- **Lambda migrations**: AWS deploy invokes migrations **once post-deploy** — avoid relying on slow migration work during Slack request handling / cold start ack timeouts.
- **SQLite vs MySQL/Postgres** — local SQLite behaves differently for locking and types; don’t assume parity without checking migration scripts.
- **`ENABLE_DB_RESET`** — boolean (`true`/`1`/`yes`), gated by `PRIMARY_WORKSPACE`; don't treat as team-id string anymore.
- **`DATABASE_BACKEND`** + **`DATABASE_*`** — use `DATABASE_BACKEND` (`mysql` / `postgresql` / `sqlite`) and `DATABASE_HOST` / `DATABASE_USER` / `DATABASE_PASSWORD` / `DATABASE_SCHEMA` per [docs/INFRA_CONTRACT.md](docs/INFRA_CONTRACT.md).
- **Fork vs upstream** — `origin` may be your fork; open PRs against the repo you were asked to target (usually **F3Nation-Community/slack-syncbot** `main`). `release.yml` and Dependabot auto-merge run only on that canonical repository. Canonical GitHub bot identity is App **`f3n-community-automation`** (see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)); forks do not install it.
- **User scopes on Home** — do not paste Slack API names (`groups:write`) onto **Authorize SyncBot**. Add the scope to `USER_SCOPES` *and* a row (or an existing fold) in `USER_PERMISSION_GROUPS` in [`syncbot/slack_manifest_scopes.py`](syncbot/slack_manifest_scopes.py). The comment on that constant is the labeling recipe: Slack scopes docs → 2–4 ordinary words, fold read/write twins, keep `groups:write` separate. Tests in `tests/test_slack_manifest_scopes.py` require every user scope in exactly one group.
- **OAuth starts at `/slack/install`.** Never link at `slack.com/oauth/v2/authorize`. Bolt requires the state cookie that only this instance's install path sets; otherwise Allow fails with `invalid_browser`. After a successful callback, refresh that user's Home (`refresh_home_after_oauth_install`) so Authorize disappears without a manual Refresh. On Lambda Function URL payload 2.0, put that cookie in the `cookies` array (not `Set-Cookie` headers) and 404 stray GETs such as `/favicon.ico` so they do not overwrite it. After a user-token revoke, `InstallationStore.delete_installation` for that `user_id` (do not leave a tokenless row). After uninstall, `delete_all` plus workspace pause. Do not enable Bolt's `enable_token_revocation_listeners()`: Slack may send `tokens.bot` on a personal revoke, and that builtin would drop the workspace bot token. Uninstall only if `auth.test` on the stored bot token fails, or on `app_uninstalled`.
- **Public origin is request Host, not `SYNCBOT_PUBLIC_URL`.** Use `get_public_base_url` / `capture_public_base` (Authorize and federation). Leftover `SYNCBOT_PUBLIC_URL` is ignored and warned, like the old Settings env vars. Do not add it back as a required env var.

## More detail

- AI-focused workflow: [docs/AI_AGENTS.md](docs/AI_AGENTS.md)
