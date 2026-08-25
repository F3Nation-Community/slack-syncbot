# Development Guide

How to run SyncBot locally (Dev Container, Docker Compose, native Python) and manage dependencies. For **cloud deploy** and CI/CD, see [DEPLOY.md](DEPLOY.md). For runtime env vars in any environment, see [INFRA_CONTRACT.md](INFRA_CONTRACT.md).

## Branching (upstream vs downstream)

The **upstream** repository ([sprocktech/syncbot](https://github.com/sprocktech/syncbot)) is the shared codebase. Each deployment maintains its own **fork**:

| Branch | Role |
|--------|------|
| **`main`** | Tracks upstream. Use it to merge PRs and to **sync with the upstream repository** (`git pull upstream main`, etc.). |
| **`test`** / **`prod`** | On your fork, use these for **deployments**: GitHub Actions deploy workflows run on **push** to `test` and `prod` (see [DEPLOY.md](DEPLOY.md)). |

Typical flow: develop on a feature branch → open a PR to **`main`** → merge → when ready to deploy, merge **`main`** into **`test`** or **`prod`** on your fork.

## Local development

### Dev Container (recommended)

**Needs:** [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or Docker Engine on Linux) + [Dev Containers](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers) in VS Code.

1. `cp .env.example .env` and set `SLACK_BOT_TOKEN` (`xoxb-...`).
2. **Dev Containers: Reopen in Container** — Python, MySQL, and deps run inside the container.
3. `cd syncbot && python app.py` → app on **port 3000** (forwarded).
4. Expose to Slack with **cloudflared** or **ngrok** from the host; set Slack **Event Subscriptions** / **Interactivity** URLs to the public URL.

Optional **SQLite**: in `.env` set `DATABASE_BACKEND=sqlite` and `DATABASE_URL=sqlite:////app/syncbot/syncbot.db`.

### Docker Compose (no Dev Container)

```bash
cp .env.example .env   # set SLACK_BOT_TOKEN
docker compose up --build
```

App on port **3000**; restart the `app` service after code changes.

### Native Python

**Needs:** Python 3.12+, Poetry. Run MySQL locally (e.g. `docker run ... mysql:8`) or SQLite. See [`.env.example`](../.env.example) and [INFRA_CONTRACT.md](INFRA_CONTRACT.md).

## Configuration reference

- **[`.env.example`](../.env.example)** — local env vars with comments.
- **[INFRA_CONTRACT.md](INFRA_CONTRACT.md)** — runtime contract for any cloud (DB, Slack, OAuth, production vs local).

## Project layout

```
syncbot/
├── syncbot/           # App (app.py); slack_manifest_scopes.py = bot/user OAuth scope lists (manifest + SLACK_BOT_SCOPES / SLACK_USER_SCOPES)
├── syncbot/db/alembic/  # Migrations (bundled with app for Lambda)
├── tests/
├── docs/
├── infra/aws/         # SAM, bootstrap stack
├── infra/gcp/         # Terraform
├── deploy.sh          # Root launcher (macOS / Linux / Git Bash)
├── deploy.ps1         # Windows launcher → Git Bash or WSL → infra/.../deploy.sh
├── slack-manifest.json
└── docker-compose.yml
```

## Dependency management

After `poetry add` / `poetry update`, keep `poetry.lock` and the pinned requirements files aligned:

- **Recommended:** Install [pre-commit](https://pre-commit.com) (`pip install pre-commit && pre-commit install && pre-commit install --hook-type commit-msg`). When you commit a change to `poetry.lock`, the **`sync-requirements`** hook runs `poetry export` and refreshes **`syncbot/requirements.txt`** and **`infra/aws/db_setup/requirements.txt`** (the DbSetup Lambda subset) automatically.

- **Without pre-commit:** Run the export yourself (Poetry 2.x needs the export plugin once: `poetry self add poetry-plugin-export`):

```bash
poetry export -f requirements.txt --without-hashes -o syncbot/requirements.txt
echo "# Required for MySQL 8+ caching_sha2_password; pin for reproducible CI (sam build)." > infra/aws/db_setup/requirements.txt
grep -E "^(pymysql|psycopg2-binary|cryptography)==" syncbot/requirements.txt >> infra/aws/db_setup/requirements.txt
```

The root **`./deploy.sh`** dependency-sync menu may run `poetry update` and regenerate both requirements files when Poetry is on your `PATH` (see [DEPLOY.md](DEPLOY.md)).

CI **`pip-audit`** exports from `poetry.lock` in the job (see [.github/workflows/ci.yml](../.github/workflows/ci.yml)). The requirements-sync job keeps committed `*requirements.txt` aligned with the lockfile.

## Releases & Versioning

- **Conventional Commits** are required for squash-merged PR titles (see [CONTRIBUTING.md](../CONTRIBUTING.md)); [.github/workflows/pr-title.yml](../.github/workflows/pr-title.yml) enforces the format (Dependabot uses `.github/dependabot.yml` prefixes).
- On **[sprocktech/syncbot](https://github.com/sprocktech/syncbot)** only, pushes to **`main`** run [.github/workflows/release.yml](../.github/workflows/release.yml) (**python-semantic-release**). It bumps `[tool.poetry] version` in `pyproject.toml`, updates `CHANGELOG.md` (inserting above `<!-- version list -->`), creates a **GitHub Release**, and tags the repo with `X.Y.Z` (no `v` prefix), matching existing tags. Forks should **not** run a second release stream.
- Release commits and requirements auto-fix commits are created via the **GitHub git API** so they show as **Verified** (GitHub `web-flow` signing) without storing a bot GPG key in CI.
- Forks pull `main` and deploy **`test`** / **`prod`** themselves (see [DEPLOY.md](DEPLOY.md)). There is no automated `main` → `test` promote PR from upstream.

## Required GitHub settings (manual)

These cannot be set from a PR — configure once on **sprocktech/syncbot** under **Settings**:

- **Allow auto-merge**; default merge method **Squash** (use the PR title as the squash commit subject).
- **Branch protection / ruleset** on `main`: require a pull request; required checks **`ci-gate`** and **`PR title / conventional`**. Do **not** require Code Owners or resolved conversations. Prefer not requiring “branch must be up to date” until Dependabot rebase is confirmed.
- **Bypass** for GitHub Actions (`github-actions[bot]`) on `main` so `release.yml` and `requirements-sync` can update the branch (PSR uses `git.updateRef`, which is a direct push).
- Dependabot **version updates** (from `.github/dependabot.yml`) and **security updates** enabled. Disable Dependabot on deploy forks (e.g. `f3-tulsa/syncbot`) so they do not open a second pile of PRs.
- **Secrets / variables** for deploy environments live on the **fork**, not on sprocktech — see [DEPLOY.md](DEPLOY.md).

## Commit signing (local)

Maintainers using **GPG-signed commits** should keep `commit.gpgsign` enabled locally; automated release commits use GitHub’s API signatures instead. See [AGENTS.md](../AGENTS.md) and [AI_AGENTS.md](AI_AGENTS.md).
