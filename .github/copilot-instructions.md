# Copilot instructions — SyncBot

Short directives for GitHub Copilot (coding agent / workspace). Full context: [AGENTS.md](../AGENTS.md) and [docs/AI_AGENTS.md](../docs/AI_AGENTS.md).

## Stack

- Python 3.12+, Poetry, Slack Bolt, SQLAlchemy, Alembic.
- App code in `syncbot/` must stay **free of AWS/GCP SDK imports** (`boto3`, `google.cloud`) — put cloud logic under `infra/` only.

## Commands (run after edits)

```bash
poetry install --with dev
pre-commit run --all-files
poetry run ruff check .
poetry run pytest -q tests/ infra/aws/tests infra/gcp/tests
```

## Do not

- Bump `pyproject.toml` `version` or add a new CHANGELOG version heading in a PR (releases own those). Do not hand-edit `*requirements.txt` (exports handle those).
- Commit `.env` secrets or `.aws-sam/` build output.

## PR rules

- Title must be a **Conventional Commit** (squash merge).
- Link issues with `Fixes #n` when fixing bugs.

## Optional: CI parity check

Repository workflow `.github/workflows/copilot-setup-steps.yml` mirrors installing Poetry deps like CI.
