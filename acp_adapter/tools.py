"""ACP tool-call helpers for mapping hermes tools to ACP ToolKind and building content."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import acp
from acp.schema import (
    ToolCallLocation,
    ToolCallStart,
    ToolCallProgress,
    ToolKind,
)

# ---------------------------------------------------------------------------
# Map hermes tool names -> ACP ToolKind
# ---------------------------------------------------------------------------

TOOL_KIND_MAP: Dict[str, ToolKind] = {
    "read_file": "read",
    "search_files": "search",
    "terminal": "execute",
    "patch": "edit",
    "write_file": "edit",
    "process": "execute",
    "vision_analyze": "read",
}


def get_tool_kind(tool_name: str) -> ToolKind:
    """Return the ACP ToolKind for a hermes tool, defaulting to 'other'."""
    return TOOL_KIND_MAP.get(tool_name, "other")


# ---------------------------------------------------------------------------
# Build ACP content objects for tool-call events
# ---------------------------------------------------------------------------


def build_tool_start(
    tool_call_id: str,
    tool_name: str,
    arguments: Dict[str, Any],
) -> ToolCallStart:
    """Create a ToolCallStart event for the given hermes tool invocation."""
    kind = get_tool_kind(tool_name)
    title = tool_name
    locations = extract_locations(arguments)

    if tool_name == "patch":
        # Produce a diff content block
        path = arguments.get("path", "")
        old = arguments.get("old_string", "")
        new = arguments.get("new_string", "")
        content = [acp.tool_diff_content(path=path, new_text=new, old_text=old)]
        return acp.start_tool_call(
            tool_call_id, title, kind=kind, content=content, locations=locations,
            raw_input=arguments,
        )

    if tool_name == "terminal":
        command = arguments.get("command", "")
        content = [acp.tool_content(acp.text_block(f"$ {command}"))]
        return acp.start_tool_call(
            tool_call_id, title, kind=kind, content=content, locations=locations,
            raw_input=arguments,
        )

    if tool_name == "read_file":
        path = arguments.get("path", "")
        content = [acp.tool_content(acp.text_block(f"Reading {path}"))]
        return acp.start_tool_call(
            tool_call_id, title, kind=kind, content=content, locations=locations,
            raw_input=arguments,
        )

    # Generic fallback
    content = [acp.tool_content(acp.text_block(str(arguments)))]
    return acp.start_tool_call(
        tool_call_id, title, kind=kind, content=content, locations=locations,
        raw_input=arguments,
    )


def build_tool_complete(
    tool_call_id: str,
    tool_name: str,
    result: str,
) -> ToolCallProgress:
    """Create a ToolCallUpdate (progress) event for a completed tool call."""
    kind = get_tool_kind(tool_name)
    content = [acp.tool_content(acp.text_block(result))]
    return acp.update_tool_call(
        tool_call_id,
        kind=kind,
        status="completed",
        content=content,
        raw_output=result,
    )


# ---------------------------------------------------------------------------
# Location extraction
# ---------------------------------------------------------------------------


def extract_locations(
    arguments: Dict[str, Any],
) -> List[ToolCallLocation]:
    """Extract file-system locations from tool arguments."""
    locations: List[ToolCallLocation] = []
    path = arguments.get("path")
    if path:
        line = arguments.get("offset") or arguments.get("line")
        locations.append(ToolCallLocation(path=path, line=line))
    return locations
