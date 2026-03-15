from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


def test_workspace_roots_add_defaults_to_non_recursive(tmp_path, monkeypatch):
    from hermes_cli.workspace import add_workspace_root

    config = {
        "workspace": {"enabled": True, "path": str(tmp_path / "workspace")},
        "knowledgebase": {"enabled": True, "roots": []},
    }
    extra = tmp_path / "notes"
    extra.mkdir()

    monkeypatch.setattr("hermes_cli.workspace.load_config", lambda: config)
    with patch("hermes_cli.workspace.save_config") as save_config:
        result = add_workspace_root(str(extra), recursive=False)

    assert result["success"] is True
    assert result["root"]["recursive"] is False
    save_config.assert_called_once()


def test_workspace_roots_remove_by_path(tmp_path, monkeypatch):
    from hermes_cli.workspace import remove_workspace_root

    extra = tmp_path / "notes"
    config = {
        "workspace": {"enabled": True, "path": str(tmp_path / "workspace")},
        "knowledgebase": {
            "enabled": True,
            "roots": [{"path": str(extra), "recursive": False}],
        },
    }

    monkeypatch.setattr("hermes_cli.workspace.load_config", lambda: config)
    with patch("hermes_cli.workspace.save_config") as save_config:
        result = remove_workspace_root(str(extra))

    assert result["success"] is True
    assert result["removed"]["path"] == str(extra)
    save_config.assert_called_once()
