## Summary

<!-- What does this PR change and why? -->

## PR title (Conventional Commit)

<!-- Squash merge uses the PR title as the commit subject. Example: feat: add channel sync toggle -->

## How to test

<!-- Steps or scenarios reviewers can run -->

## AI-authored?

<!-- If an agent helped: which platform (Cursor / Copilot / other) and link the issue. -->

## Checklist

- [ ] PR title is a valid **Conventional Commit** (see [CONTRIBUTING.md](../CONTRIBUTING.md))
- [ ] No manual edits to `CHANGELOG.md`, `pyproject.toml` `version`, or `*requirements.txt` (releases / poetry export own these)
- [ ] CI passes (`ci-gate`: requirements sync, forbidden path checks, ruff, SAM lint when infra changes, pip-audit when deps change, tests)
- [ ] Docs updated if behavior or deploy steps changed
- [ ] No new cloud-provider-specific code under `syncbot/` (keep infra in `infra/` and workflows)
