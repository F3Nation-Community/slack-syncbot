"""Data migration export looks up Workspaces by integer primary key."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from helpers.export_import import build_migration_export


class TestBuildMigrationExportWorkspaceLookup:
    def test_finds_workspace_by_integer_pk(self):
        workspace = SimpleNamespace(
            id=42,
            team_id="T1",
            workspace_name="Alpha",
            deleted_at=None,
        )
        with (
            patch("helpers.export_import.get_workspace_by_id", return_value=workspace) as get_ws,
            patch("helpers.export_import.DbManager.find_records", return_value=[]),
            patch("helpers.export_import.os.environ.get", return_value=""),
        ):
            payload = build_migration_export(42, include_source_instance=False)

        get_ws.assert_called_with(42)
        assert payload["workspace"] == {"team_id": "T1", "workspace_name": "Alpha"}
        assert payload["syncs"] == []
        assert payload["groups"] == []

    def test_raises_when_workspace_missing(self):
        with (
            patch("helpers.export_import.get_workspace_by_id", return_value=None),
            pytest.raises(ValueError, match="Workspace not found"),
        ):
            build_migration_export(99)
