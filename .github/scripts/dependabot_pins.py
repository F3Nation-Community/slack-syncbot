#!/usr/bin/env python3
"""Packages Dependabot named that poetry.lock has not reached yet.

The workflow runs ``list`` and then ``poetry update`` for those names.
``check`` fails when the lock is still behind a requested pin. A target
older than the lock is not printed and does not fail ``check``: that would
put the old pin back.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_NAME_VERSION = re.compile(r"^([A-Za-z0-9_.-]+)==([^ \t;]+)")
_LOCK_BLOCK = re.compile(
    r'\[\[package\]\]\s+name = "([^"]+)"\s+version = "([^"]+)"',
    re.MULTILINE,
)
_TABLE_ROW = re.compile(r"^\|(.+)\|\s*$")
_UPDATE_LINE = re.compile(
    r"^(?:Bumps|Updates)\s+"
    r"(?:\[([^\]]+)\]\([^)]*\)|`([^`]+)`|([A-Za-z0-9_.-]+))"
    r"\s+from\s+\S+\s+to\s+(\d\S*?)\.?\s*$"
)


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def version_key(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in value.split("."):
        digits = ""
        for char in piece:
            if char.isdigit():
                digits += char
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def lock_versions(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for name, version in _LOCK_BLOCK.findall(text):
        found[normalize(name)] = version
    return found


def _strip_cell(cell: str) -> str:
    text = cell.strip().strip("`").strip()
    link = re.match(r"\[([^\]]+)\]\([^)]*\)$", text)
    if link:
        return link.group(1).strip()
    return text


def _preamble(text: str) -> str:
    """Body text before Dependabot's changelog details block."""
    details = text.lower().find("<details>")
    if details == -1:
        return text
    return text[:details]


def body_targets(text: str) -> dict[str, str]:
    targets: dict[str, str] = {}
    for line in _preamble(text).splitlines():
        update = _UPDATE_LINE.match(line.strip())
        if not update:
            continue
        name = next(group for group in update.groups()[:3] if group)
        targets[normalize(name)] = update.group(4)
    for line in text.splitlines():
        match = _TABLE_ROW.match(line.strip())
        if not match:
            continue
        cells = [_strip_cell(cell) for cell in match.group(1).split("|")]
        if len(cells) < 3:
            continue
        name, _from_version, to_version = cells[0], cells[1], cells[2]
        if not name or name.lower() == "package" or set(name) <= {"-", ":"}:
            continue
        if not re.match(r"^[A-Za-z0-9_.-]+$", name):
            continue
        if not re.match(r"^[0-9]", to_version):
            continue
        targets[normalize(name)] = to_version
    return targets


def diff_targets(text: str) -> dict[str, str]:
    removed: dict[str, str] = {}
    added: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith(("+++", "---")):
            continue
        sign = line[:1]
        if sign not in {"+", "-"}:
            continue
        match = _NAME_VERSION.match(line[1:])
        if not match:
            continue
        name = normalize(match.group(1))
        version = match.group(2)
        if sign == "-":
            removed[name] = version
        else:
            added[name] = version
    targets: dict[str, str] = {}
    for name, version in added.items():
        old = removed.get(name)
        if old is not None and version_key(version) < version_key(old):
            continue
        targets[name] = version
    return targets


def requested_targets(body: str, requirements_diff: str) -> dict[str, str]:
    targets = body_targets(body)
    for name, version in diff_targets(requirements_diff).items():
        current = targets.get(name)
        if current is None or version_key(version) > version_key(current):
            targets[name] = version
    return targets


def packages_behind(targets: dict[str, str], locked: dict[str, str]) -> list[str]:
    behind: list[str] = []
    for name in sorted(targets):
        have = locked.get(name)
        if have is None or version_key(have) < version_key(targets[name]):
            behind.append(name)
    return behind


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("list", "check"))
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--body", required=True, type=Path)
    parser.add_argument("--requirements-diff", required=True, type=Path)
    args = parser.parse_args(argv)

    targets = requested_targets(
        args.body.read_text(encoding="utf-8"), args.requirements_diff.read_text(encoding="utf-8")
    )
    locked = lock_versions(args.lock.read_text(encoding="utf-8"))
    behind = packages_behind(targets, locked)
    if args.command == "list":
        for name in behind:
            print(name)
        return 0
    if behind:
        print("poetry.lock is still behind Dependabot:", ", ".join(behind), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
