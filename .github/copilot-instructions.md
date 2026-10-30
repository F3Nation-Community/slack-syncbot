# Copilot instructions — SyncBot

Coding rules are in [AGENTS.md](../AGENTS.md). CI and branch protection are in [docs/DEVELOPMENT.md](../docs/DEVELOPMENT.md).

Python 3.12+, Poetry, Slack Bolt, SQLAlchemy, Alembic. App code in `syncbot/` must not import `boto3` or `google.cloud`.

```bash
poetry run ruff check .
poetry run pytest -q tests/ infra/aws/tests infra/gcp/tests
```

Do not bump `pyproject.toml` `version` or add a `## [X.Y.Z]` heading. Notes go under `## [Unreleased]`. Do not hand-edit `syncbot/requirements.txt`. Route Slack events through `routing.py` only.
