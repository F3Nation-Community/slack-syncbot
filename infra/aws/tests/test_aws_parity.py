"""AWS SAM / Litestream / deploy-script assertions after stack-RDS removal."""

from __future__ import annotations

import inspect
from pathlib import Path

import db as db_mod

INFRA_AWS = Path(__file__).resolve().parent.parent
REPO_ROOT = INFRA_AWS.parent.parent
TEMPLATE = (INFRA_AWS / "template.yaml").read_text(encoding="utf-8")
BOOTSTRAP = (INFRA_AWS / "template.bootstrap.yaml").read_text(encoding="utf-8")
MAKEFILE = (INFRA_AWS / "lambda" / "Makefile").read_text(encoding="utf-8")
HANDLER = (INFRA_AWS / "lambda" / "handler.py").read_text(encoding="utf-8")
LITESTREAM_YML = (INFRA_AWS / "lambda" / "litestream.yml").read_text(encoding="utf-8")
DEPLOY_SH = (INFRA_AWS / "scripts" / "deploy.sh").read_text(encoding="utf-8")
CI_SAM = (INFRA_AWS / "scripts" / "ci_sam_deploy_with_fallback.sh").read_text(encoding="utf-8")
WORKFLOW = (REPO_ROOT / ".github" / "workflows" / "deploy-aws.yml").read_text(encoding="utf-8")


def test_template_has_no_rds_or_vpc() -> None:
    assert "AWS::RDS::" not in TEMPLATE
    assert "RDSInstance" not in TEMPLATE
    assert "!GetAtt RDSInstance" not in TEMPLATE
    assert "DbSetup" not in TEMPLATE
    assert "CreateDatabase" not in TEMPLATE
    assert "VpcConfig" not in TEMPLATE
    assert "AWSLambdaVPCAccessExecutionRole" not in TEMPLATE
    assert "ExistingDatabaseAdmin" not in TEMPLATE
    assert "DatabaseInstanceClass" not in TEMPLATE
    assert "VpcCidr" not in TEMPLATE


def test_template_sqlite_and_keep_warm() -> None:
    assert "DatabaseMode:" in TEMPLATE
    assert "EnableKeepWarm:" in TEMPLATE
    assert "sqlite:////tmp/syncbot.db" in TEMPLATE
    assert "LITESTREAM_S3_BUCKET" in TEMPLATE
    assert "ReservedConcurrentExecutions" in TEMPLATE
    assert "State: !If [KeepWarmEnabled, ENABLED, DISABLED]" in TEMPLATE
    assert "Handler: handler.handler" in TEMPLATE
    assert "CodeUri: lambda/" in TEMPLATE
    assert "BuildMethod: makefile" in TEMPLATE
    assert "DatabaseHostInUse:" in TEMPLATE
    assert "LitestreamBucketName:" in TEMPLATE
    assert "RDSEndpoint" not in TEMPLATE
    assert "VpcId" not in TEMPLATE


def test_bootstrap_has_no_rds_or_vpc_create() -> None:
    assert "rds:" not in BOOTSTRAP.lower()
    assert "ec2:CreateVpc" not in BOOTSTRAP
    assert "AWS::RDS::" not in BOOTSTRAP


def test_makefile_pins_litestream_and_installs_app() -> None:
    assert "LITESTREAM_VERSION := v0.3.13" in MAKEFILE
    assert "LITESTREAM_SHA256 := eb75a3de5cab03875cdae9f5f539e6aedadd66607003d9b1e7a9077948818ba0" in MAKEFILE
    assert 'pip install -r "$(SYNCBOT_SRC)/requirements.txt"' in MAKEFILE
    assert "sha256sum -c" in MAKEFILE


def test_wrapper_restore_then_init_then_replicate() -> None:
    body = HANDLER[HANDLER.find("def _bootstrap_sqlite") :]
    restore_at = body.find('"restore"')
    init_at = body.find("initialize_database")
    replicate_at = body.find('"replicate"')
    assert 0 <= restore_at < init_at < replicate_at
    assert "-if-replica-exists" in body
    assert "type: s3" in LITESTREAM_YML
    assert "sync-interval: 1s" in LITESTREAM_YML
    assert "region: ${AWS_REGION}" in LITESTREAM_YML


def test_deploy_scripts_modes_and_rds_abort() -> None:
    assert "AWS_DATABASE_MODE" in DEPLOY_SH
    assert "abort_if_stack_managed_rds" in DEPLOY_SH
    assert "rds_lookup_" not in DEPLOY_SH
    assert "stack-managed RDS vs existing" not in DEPLOY_SH
    assert "Use stack-managed RDS" not in DEPLOY_SH
    assert "ENABLE_KEEP_WARM" in DEPLOY_SH
    assert "Existing database host (TiDB / MySQL / your own RDS)" in DEPLOY_SH
    assert "SQLite + Litestream to S3" in DEPLOY_SH
    assert "docs/BACKUP_AND_MIGRATION.md" in DEPLOY_SH
    assert "There is no in-place migrate from stack RDS" in DEPLOY_SH

    assert "abort_if_stack_managed_rds" in CI_SAM
    assert "DatabaseMode=" in CI_SAM
    assert "EnableKeepWarm=" in CI_SAM
    assert "ExistingDatabaseAdminUser" not in CI_SAM
    assert "DatabaseInstanceClass" not in CI_SAM
    assert "VpcCidr" not in CI_SAM
    assert "DATABASE_NETWORK_MODE" not in CI_SAM
    assert "docs/BACKUP_AND_MIGRATION.md" in CI_SAM


def test_deploy_aws_workflow_drops_admin_and_passes_mode() -> None:
    assert "AWS_DATABASE_MODE" in WORKFLOW
    assert "ENABLE_KEEP_WARM" in WORKFLOW
    assert "DATABASE_ADMIN_USER" not in WORKFLOW
    assert "DATABASE_ADMIN_PASSWORD" not in WORKFLOW
    assert "DATABASE_NETWORK_MODE" not in WORKFLOW
    assert "DATABASE_CREATE_APP_USER" not in WORKFLOW
    assert "DATABASE_USERNAME_PREFIX" not in WORKFLOW


def test_initialize_database_does_not_emit_create_database() -> None:
    assert not hasattr(db_mod, "_ensure_database_exists")
    source = inspect.getsource(db_mod)
    assert "def _ensure_database_exists" not in source
    assert "CREATE DATABASE IF NOT EXISTS" not in source
    init_src = inspect.getsource(db_mod.initialize_database)
    assert "_run_alembic_upgrade" in init_src


def test_bootstrap_sqlite_restore_then_migrate_then_replicate(monkeypatch, tmp_path) -> None:
    import sys
    from unittest.mock import MagicMock

    import handler as h

    monkeypatch.setattr(h, "_DB_PATH", tmp_path / "syncbot.db")
    monkeypatch.setattr(h, "_sqlite_ready", False)
    commands: list[tuple[str, list[str]]] = []

    def fake_run(cmd, check=True, **kwargs):  # noqa: ARG001
        commands.append(("run", list(cmd)))

    def fake_popen(cmd, **kwargs):  # noqa: ARG001
        commands.append(("popen", list(cmd)))
        return MagicMock()

    monkeypatch.setattr(h.subprocess, "run", fake_run)
    monkeypatch.setattr(h.subprocess, "Popen", fake_popen)
    fake_db = MagicMock()
    inits: list[int] = []
    fake_db.initialize_database = lambda: inits.append(1)
    monkeypatch.setitem(sys.modules, "db", fake_db)

    h._bootstrap_sqlite()

    assert commands[0][0] == "run"
    assert "restore" in commands[0][1]
    assert "-if-replica-exists" in commands[0][1]
    assert inits == [1]
    assert commands[1][0] == "popen"
    assert "replicate" in commands[1][1]
    assert h._sqlite_ready is True


def test_handler_without_litestream_bucket_skips_bootstrap(monkeypatch) -> None:
    import sys
    from types import SimpleNamespace

    import handler as h

    monkeypatch.delenv("LITESTREAM_S3_BUCKET", raising=False)
    boot: list[str] = []
    monkeypatch.setattr(h, "_bootstrap_sqlite", lambda: boot.append("yes"))
    monkeypatch.setitem(sys.modules, "app", SimpleNamespace(handler=lambda event, context: {"ok": True}))
    assert h.handler({"action": "ping"}, None) == {"ok": True}
    assert boot == []
