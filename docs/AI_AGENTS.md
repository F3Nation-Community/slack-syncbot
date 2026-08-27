# AI agents on SyncBot

This repository is set up so coding agents (Cursor, GitHub Copilot, Codex, Claude) can work safely with clear boundaries.

## Read these first

| File | Purpose |
|------|---------|
| [AGENTS.md](../AGENTS.md) | Primary guardrails, commands, pitfalls |
| [.github/copilot-instructions.md](../.github/copilot-instructions.md) | Short Copilot-specific checklist |
| [.cursor/rules/](../.cursor/rules/) | Cursor rules (architecture, tests, infra, no-touch files) |
| [CONTRIBUTING.md](../CONTRIBUTING.md) | Conventional Commits + workflow |

## How CI guards agents

On pull requests, [.github/workflows/ci.yml](../.github/workflows/ci.yml) includes:

- **`forbidden-edits`** — blocks removing the CHANGELOG `<!-- version list -->` marker, hand bumps of `version` in `pyproject.toml`, and `*requirements.txt` changes that are not paired with `poetry.lock` / `pyproject.toml`.
- **`forbidden-imports`** — blocks `boto3` / `google.cloud` imports under `syncbot/`.
- **`ruff`** — `ruff check` and `ruff format --check`.
- **`pip-audit`** — exports from `poetry.lock` and audits (runs when Python dependency files change).
- **`sam-lint`** — `sam validate --lint` (runs when AWS templates / SAM workflow pins change).
- **`terraform-validate`** — `terraform init -backend=false`, `validate`, and `fmt -check` in `infra/gcp` (path-filtered; skipped is OK).
- **`docker-build-gcp`** — `docker build -f infra/gcp/Dockerfile --platform linux/amd64 .` from the repo root, no push (path-filtered).
- **`test`** — `pytest` over `tests/` and infra tests (same as local command in [AGENTS.md](../AGENTS.md)).
- **`ci-gate`** — aggregator; the check to require on `main`. Skipped path-filtered jobs count as success.
- **`requirements-sync`** — on same-repo PRs to sprocktech/syncbot, may commit `*requirements.txt` from `poetry.lock` as **`sprocktech-automation[bot]`** **without** `[skip ci]` so `ci-gate` / `conventional` re-run on HEAD. Forks must export themselves.

Release automation and signed bot commits are described in [DEVELOPMENT.md](DEVELOPMENT.md) — Releases & Versioning and **Automation GitHub App**. `release.yml` uses the App token for `git.updateRef`; `GITHUB_TOKEN` cannot bypass the `main` ruleset. Dependabot auto-merge also uses the App token so merge can retrigger workflows.

## Filing an AI-friendly issue

Use **AI-eligible task** in GitHub’s issue templates. Include goal, acceptance criteria, how to test, and what’s out of scope.

## Reviewing AI-authored PRs

- Confirm the PR title matches **Conventional Commits** (required for squash merges).
- Look for forbidden-file edits; CI should fail them, but reviewers should still watch for secrets.
- Ensure tests cover behavior changes; spot-check Slack/event flows when touching handlers.

## Fork compatibility

`release.yml`, Dependabot auto-merge, and semantic-release config apply to **sprocktech/syncbot** only. Deploy forks keep `test`/`prod` Environments and must not mint duplicate GitHub Releases. CODEOWNERS handles are organization-specific; replace `@sprocktech-dev` on other orgs. See [INFRA_CONTRACT.md](INFRA_CONTRACT.md) Fork Compatibility Policy.

## Branch protection (sprocktech/syncbot)

Configure in GitHub **Settings → Rulesets / Branches** for `main`:

- Require a pull request before merging
- Required checks: **`ci-gate`**, **`conventional`**
- Do **not** require review from Code Owners (that would block Dependabot auto-merge)
- Do **not** add Dependabot, Write, or Maintain to the ruleset bypass list; `github-actions[bot]` cannot be added here. Organization admin bypass is for humans. Add GitHub App **`sprocktech-automation`** so Release `updateRef` and leftover requirements-sync pushes can land on `main` (see [DEVELOPMENT.md](DEVELOPMENT.md)).
- Allow auto-merge; squash only; do not require conversation resolution

Exact job names come from `.github/workflows/ci.yml` and `pr-title.yml`.
