"""Terraform validation for the module next to this package.

``terraform init -backend=false`` may need network access to download providers.
Uses ``TF_DATA_DIR`` in a temp directory so the repo tree is not modified.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

INFRA_GCP = Path(__file__).resolve().parent.parent


def _which(name: str) -> str | None:
    return shutil.which(name)


def test_terraform_validates() -> None:
    tf = _which("terraform")
    if not tf:
        pytest.skip("terraform not on PATH")
    assert INFRA_GCP.is_dir()
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ)
        env["TF_DATA_DIR"] = tmp
        init = subprocess.run(
            [tf, "init", "-backend=false", "-input=false"],
            cwd=INFRA_GCP,
            capture_output=True,
            text=True,
            env=env,
            timeout=180,
        )
        if init.returncode != 0:
            pytest.skip(
                f"terraform init failed (terraform missing or no network for providers?):\n{init.stdout}\n{init.stderr}"
            )
        validate = subprocess.run(
            [tf, "validate"],
            cwd=INFRA_GCP,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        assert validate.returncode == 0, f"terraform validate failed:\n{validate.stdout}\n{validate.stderr}"


def test_gcp_module_has_no_cloud_sql() -> None:
    main_tf = (INFRA_GCP / "main.tf").read_text(encoding="utf-8")
    assert "google_sql_" not in main_tf
    assert "sqladmin" not in main_tf
    assert "random_password" not in main_tf
    assert 'DATABASE_URL               = "sqlite:////data/syncbot.db"' in main_tf


def test_dockerfile_pins_litestream_sha256() -> None:
    dockerfile = (INFRA_GCP / "Dockerfile").read_text(encoding="utf-8")
    assert "LITESTREAM_VERSION=v0.3.13" in dockerfile
    assert "LITESTREAM_SHA256=eb75a3de5cab03875cdae9f5f539e6aedadd66607003d9b1e7a9077948818ba0" in dockerfile
    assert "COPY infra/gcp/litestream.yml" in dockerfile
    assert "COPY infra/gcp/entrypoint.sh" in dockerfile


def test_entrypoint_does_not_exec_python_while_litestream_runs() -> None:
    entry = (INFRA_GCP / "entrypoint.sh").read_text(encoding="utf-8")
    assert entry.count("exec python app.py") == 1
    assert "python app.py &" in entry
    assert "trap cleanup" in entry
    unset_branch, _, litestream_branch = entry.partition("litestream replicate")
    assert "exec python app.py" in unset_branch
    assert "exec python" not in litestream_branch


def test_deploy_script_sqlite_skips_required_password() -> None:
    script = (INFRA_GCP / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert "DATABASE_PASSWORD:?" not in script
    assert "GCP_DATABASE_MODE" in script
    assert "GCP_CLOUD_RUN_MIN_INSTANCES" in script
    assert "ENABLE_KEEP_WARM" in script
    assert "GCP_CLOUD_RUN_IMAGE" in script
    vars_tf = (INFRA_GCP / "variables.tf").read_text(encoding="utf-8")
    assert '"sqlite"' in vars_tf
    assert 'default     = "sqlite"' in vars_tf


def test_deploy_gcp_workflow_is_gated_and_not_a_stub() -> None:
    workflow = (INFRA_GCP.parents[1] / ".github" / "workflows" / "deploy-gcp.yml").read_text(encoding="utf-8")
    assert "vars.DEPLOY_TARGET == 'gcp'" in workflow
    assert "docker build -f infra/gcp/Dockerfile" in workflow
    assert "gcloud run services update" in workflow
    assert "GCP deploy is not implemented" not in workflow
    assert "placeholder until" not in workflow.lower()
