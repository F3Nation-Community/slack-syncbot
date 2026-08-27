"""Guardrails for GitHub App CI wiring and deploy.sh lockfile hygiene."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_release_uses_app_token_for_git_api() -> None:
    text = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "actions/create-github-app-token@v3" in text
    assert "github-token: ${{ steps.app-token.outputs.token }}" in text
    assert "GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}" in text


def test_requirements_sync_pushes_with_app_token() -> None:
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    req, _rest = ci.split("forbidden-edits:", 1)
    assert "actions/create-github-app-token@v3" in req
    assert "sprocktech-automation[bot]" in req
    assert "git push" in req
    assert "actions/github-script" not in req


def test_forbidden_edits_fetches_full_base_ref() -> None:
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    _pre, rest = ci.split("forbidden-edits:", 1)
    job = rest.split("forbidden-imports:", 1)[0]
    assert "--depth=1" not in job
    assert 'git fetch origin "${BASE_REF}"' in job


def test_dependabot_automerge_uses_app_token() -> None:
    text = (REPO_ROOT / ".github" / "workflows" / "dependabot-auto-merge.yml").read_text()
    assert "actions/create-github-app-token@v3" in text
    assert "GH_TOKEN: ${{ steps.app-token.outputs.token }}" in text


def test_deploy_sh_does_not_update_packages() -> None:
    text = (REPO_ROOT / "deploy.sh").read_text()
    assert not any(line.lstrip().startswith("poetry update") for line in text.splitlines())
