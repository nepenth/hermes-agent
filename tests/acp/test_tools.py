"""Tests for acp_adapter.tools — tool kind mapping and ACP content building."""

import pytest

from acp_adapter.tools import (
    TOOL_KIND_MAP,
    build_tool_complete,
    build_tool_start,
    extract_locations,
    get_tool_kind,
)
from acp.schema import (
    FileEditToolCallContent,
    ContentToolCallContent,
    ToolCallLocation,
    ToolCallStart,
    ToolCallProgress,
)


# ---------------------------------------------------------------------------
# TOOL_KIND_MAP coverage
# ---------------------------------------------------------------------------


COMMON_HERMES_TOOLS = ["read_file", "search_files", "terminal", "patch", "write_file", "process"]


class TestToolKindMap:
    def test_all_hermes_tools_have_kind(self):
        """Every common hermes tool should appear in TOOL_KIND_MAP."""
        for tool in COMMON_HERMES_TOOLS:
            assert tool in TOOL_KIND_MAP, f"{tool} missing from TOOL_KIND_MAP"

    def test_tool_kind_read_file(self):
        assert get_tool_kind("read_file") == "read"

    def test_tool_kind_terminal(self):
        assert get_tool_kind("terminal") == "execute"

    def test_tool_kind_patch(self):
        assert get_tool_kind("patch") == "edit"

    def test_tool_kind_write_file(self):
        assert get_tool_kind("write_file") == "edit"

    def test_unknown_tool_returns_other_kind(self):
        assert get_tool_kind("nonexistent_tool_xyz") == "other"


# ---------------------------------------------------------------------------
# build_tool_start
# ---------------------------------------------------------------------------


class TestBuildToolStart:
    def test_build_tool_start_for_patch(self):
        """patch should produce a FileEditToolCallContent (diff)."""
        args = {
            "path": "src/main.py",
            "old_string": "print('hello')",
            "new_string": "print('world')",
        }
        result = build_tool_start("tc-1", "patch", args)
        assert isinstance(result, ToolCallStart)
        assert result.kind == "edit"
        # The first content item should be a diff
        assert len(result.content) >= 1
        diff_item = result.content[0]
        assert isinstance(diff_item, FileEditToolCallContent)
        assert diff_item.path == "src/main.py"
        assert diff_item.new_text == "print('world')"
        assert diff_item.old_text == "print('hello')"

    def test_build_tool_start_for_terminal(self):
        """terminal should produce text content with the command."""
        args = {"command": "ls -la /tmp"}
        result = build_tool_start("tc-2", "terminal", args)
        assert isinstance(result, ToolCallStart)
        assert result.kind == "execute"
        assert len(result.content) >= 1
        content_item = result.content[0]
        assert isinstance(content_item, ContentToolCallContent)
        # The wrapped text block should contain the command
        text = content_item.content.text
        assert "ls -la /tmp" in text

    def test_build_tool_start_for_read_file(self):
        """read_file should include the path in content."""
        args = {"path": "/etc/hosts", "offset": 1, "limit": 50}
        result = build_tool_start("tc-3", "read_file", args)
        assert isinstance(result, ToolCallStart)
        assert result.kind == "read"
        assert len(result.content) >= 1
        content_item = result.content[0]
        assert isinstance(content_item, ContentToolCallContent)
        assert "/etc/hosts" in content_item.content.text


# ---------------------------------------------------------------------------
# build_tool_complete
# ---------------------------------------------------------------------------


class TestBuildToolComplete:
    def test_build_tool_complete_for_terminal(self):
        """Completed terminal call should include output text."""
        result = build_tool_complete("tc-2", "terminal", "total 42\ndrwxr-xr-x 2 root root 4096 ...")
        assert isinstance(result, ToolCallProgress)
        assert result.status == "completed"
        assert len(result.content) >= 1
        content_item = result.content[0]
        assert isinstance(content_item, ContentToolCallContent)
        assert "total 42" in content_item.content.text


# ---------------------------------------------------------------------------
# extract_locations
# ---------------------------------------------------------------------------


class TestExtractLocations:
    def test_extract_locations_with_path(self):
        args = {"path": "src/app.py", "offset": 42}
        locs = extract_locations(args)
        assert len(locs) == 1
        assert isinstance(locs[0], ToolCallLocation)
        assert locs[0].path == "src/app.py"
        assert locs[0].line == 42

    def test_extract_locations_without_path(self):
        args = {"command": "echo hi"}
        locs = extract_locations(args)
        assert locs == []
