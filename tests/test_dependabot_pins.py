"""Dependabot pin selection. Runs the script by path and does not call Poetry."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "dependabot_pins.py"

LOCK = """\
[[package]]
name = "certifi"
version = "2026.2.25"

[[package]]
name = "mako"
version = "1.4.1"
"""

RAISED_CERTIFI = """\
-certifi==2026.2.25 ; python_version >= "3.12" and python_version < "4.0"
+certifi==2026.10.19 ; python_version >= "3.12" and python_version < "4.0"
"""

MAKO_BODY = """\
| Package | From | To |
| --- | --- | --- |
| [Mako](https://github.com/sqlalchemy/mako) | `1.4.1` | `1.4.2` |
"""


def _run(tmp_path: Path, command: str, lock: str, body: str, diff: str) -> subprocess.CompletedProcess[str]:
    lock_path = tmp_path / "poetry.lock"
    body_path = tmp_path / "body.md"
    diff_path = tmp_path / "requirements.diff"
    lock_path.write_text(lock, encoding="utf-8")
    body_path.write_text(body, encoding="utf-8")
    diff_path.write_text(diff, encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            command,
            "--lock",
            str(lock_path),
            "--body",
            str(body_path),
            "--requirements-diff",
            str(diff_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_requirements_diff_that_raises_certifi_names_certifi(tmp_path: Path) -> None:
    result = _run(tmp_path, "list", LOCK, "", RAISED_CERTIFI)
    assert result.returncode == 0
    assert result.stdout.split() == ["certifi"]
    assert "2026.2.25" not in result.stdout


def test_body_table_names_mako_until_the_lock_catches_up(tmp_path: Path) -> None:
    behind = _run(tmp_path, "list", LOCK, MAKO_BODY, "")
    assert behind.returncode == 0
    assert behind.stdout.split() == ["mako"]
    behind_check = _run(tmp_path, "check", LOCK, MAKO_BODY, "")
    assert behind_check.returncode == 1

    caught_up = LOCK.replace('name = "mako"\nversion = "1.4.1"', 'name = "mako"\nversion = "1.4.2"')
    done = _run(tmp_path, "list", caught_up, MAKO_BODY, "")
    assert done.returncode == 0
    assert done.stdout.strip() == ""
    checked = _run(tmp_path, "check", caught_up, MAKO_BODY, "")
    assert checked.returncode == 0


def test_bump_sentence_names_certifi_and_ignores_the_changelog(tmp_path: Path) -> None:
    body = """\
Bumps [certifi](https://github.com/certifi/python-certifi) from 2026.2.25 to 2026.10.18.
<details>
<summary>Commits</summary>
Bumps actions/checkout from 1.0.0 to 2.0.0.
</details>
"""
    result = _run(tmp_path, "list", LOCK, body, "")
    assert result.returncode == 0
    assert result.stdout.split() == ["certifi"]


def test_a_table_inside_the_changelog_is_not_a_package_list(tmp_path: Path) -> None:
    body = """\
Bumps the python-patch-minor group with 2 updates:

| Package | From | To |
| --- | --- | --- |
| [certifi](https://github.com/certifi/python-certifi) | `2026.2.25` | `2026.10.23` |

<details>
<summary>Release notes</summary>

| Package | From | To |
| --- | --- | --- |
| [actions/checkout](https://github.com/actions/checkout) | `1.0.0` | `2.0.0` |
</details>
"""
    result = _run(tmp_path, "list", LOCK, body, "")
    assert result.returncode == 0
    assert result.stdout.split() == ["certifi"]


def test_a_lowered_pin_is_not_printed(tmp_path: Path) -> None:
    lowered = """\
-certifi==2026.10.19 ; python_version >= "3.12"
+certifi==2026.2.25 ; python_version >= "3.12"
"""
    lock = LOCK.replace('version = "2026.2.25"', 'version = "2026.10.19"')
    result = _run(tmp_path, "list", lock, "", lowered)
    assert result.returncode == 0
    assert result.stdout.strip() == ""
    assert "certifi" not in result.stdout
