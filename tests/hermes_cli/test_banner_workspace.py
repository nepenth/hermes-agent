from __future__ import annotations

from io import StringIO

from rich.console import Console


def test_workspace_banner_line_uses_default_workspace_when_enabled(monkeypatch, tmp_path):
    from hermes_cli import banner

    cfg = {
        "workspace": {"enabled": True, "path": str(tmp_path / "workspace")},
        "knowledgebase": {"enabled": True, "roots": []},
    }
    monkeypatch.setattr(banner, "load_config", lambda: cfg)

    line = banner._get_workspace_banner_line()

    assert line == "Activated Workspace(s): workspace"


def test_workspace_banner_line_lists_multiple_roots(monkeypatch, tmp_path):
    from hermes_cli import banner

    cfg = {
        "workspace": {"enabled": True, "path": str(tmp_path / "workspace")},
        "knowledgebase": {
            "enabled": True,
            "roots": [
                str(tmp_path / "workspace"),
                str(tmp_path / "notes"),
                str(tmp_path / "project-docs"),
            ],
        },
    }
    monkeypatch.setattr(banner, "load_config", lambda: cfg)

    line = banner._get_workspace_banner_line()

    assert line == "Activated Workspace(s): workspace, notes, project-docs"


def test_workspace_banner_line_omits_when_disabled(monkeypatch):
    from hermes_cli import banner

    cfg = {
        "workspace": {"enabled": False, "path": ""},
        "knowledgebase": {"enabled": False, "roots": []},
    }
    monkeypatch.setattr(banner, "load_config", lambda: cfg)

    assert banner._get_workspace_banner_line() is None


def test_build_welcome_banner_renders_workspace_line(monkeypatch):
    from hermes_cli import banner

    monkeypatch.setattr(banner, "check_for_updates", lambda: 0)
    monkeypatch.setattr(banner, "get_available_skills", lambda: {})
    monkeypatch.setattr(banner, "_get_workspace_banner_line", lambda: "Activated Workspace(s): workspace, notes")

    buf = StringIO()
    console = Console(file=buf, force_terminal=False, width=140, color_system=None)

    banner.build_welcome_banner(
        console=console,
        model="anthropic/claude-sonnet-4.5",
        cwd="/tmp/project",
        tools=[],
        enabled_toolsets=["hermes-cli"],
        session_id="sess-1",
        get_toolset_for_tool=lambda _: "other",
        context_length=200000,
    )

    rendered = buf.getvalue()
    assert "Activated Workspace(s): workspace, notes" in rendered
