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
2. **Version & changelog** — do not bump `pyproject.toml`’s `version` or add a **new** CHANGELOG version heading in a feature PR. Releases on `main` are automated with **python-semantic-release**. Polishing notes under an already-released heading is OK (Keep a Changelog: Added / Changed / Fixed). Use Conventional Commits (see [CONTRIBUTING.md](CONTRIBUTING.md)).
3. **Requirements files** — do not edit `syncbot/requirements.txt` by hand. Change dependencies in `pyproject.toml` and run the pre-commit `sync-requirements` hook or `poetry export` as in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).
4. **Secrets** — never commit real `.env`, `.env.deploy.*` (only `*.example` templates), private keys, or large artifacts. Do not commit `.aws-sam/build` output.
5. **Deployment branches** — on a **fork**, do not push to `test` or `prod` as a casual step; those branches deploy. Canonical releases run only on **sprocktech/syncbot** `main` (see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)). Forks pull `main` and promote to `test`/`prod` themselves.
6. **Conventional Commits** — PR titles must be valid Conventional Commits for squash merges; see [CONTRIBUTING.md](CONTRIBUTING.md).
7. **Docs** — if behavior, env vars, or deploy steps change, update the relevant `docs/*.md` (and [docs/INFRA_CONTRACT.md](docs/INFRA_CONTRACT.md) if the runtime contract changes).

## Definition of done (AI-resolved issues)

1. **Tests** — add or update tests under `tests/` or `infra/*/tests/` for the change.
2. **Pre-commit** — `pre-commit run --all-files` passes.
3. **CI** — no new failures in `ci-gate` (requirements sync, ruff, SAM lint, pip-audit, forbidden path checks, tests).
4. **PR title** — Conventional Commit format (enforced by `.github/workflows/pr-title.yml`).
5. **PR description** — use `.github/pull_request_template.md`; link issues with `Fixes #123` when applicable.

## Common pitfalls

- **Lambda migrations**: AWS deploy invokes migrations **once post-deploy** — avoid relying on slow migration work during Slack request handling / cold start ack timeouts.
- **SQLite vs MySQL/Postgres** — local SQLite behaves differently for locking and types; don’t assume parity without checking migration scripts.
- **`ENABLE_DB_RESET`** — boolean (`true`/`1`/`yes`), gated by `PRIMARY_WORKSPACE`; don't treat as team-id string anymore.
- **`DATABASE_*`** env naming — use current `DATABASE_*` vars per [docs/INFRA_CONTRACT.md](docs/INFRA_CONTRACT.md); older `EXISTING_DATABASE_*` names are obsolete.
- **Fork vs upstream** — `origin` may be your fork; open PRs against the repo you were asked to target (usually **sprocktech/syncbot** `main`). `release.yml` and Dependabot auto-merge run only on that canonical repository. Canonical GitHub bot identity is App **`sprocktech-automation`** (see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)); forks do not install it.

## More detail

- AI-focused workflow: [docs/AI_AGENTS.md](docs/AI_AGENTS.md)
