"""Helpers for optional rich agent UI payloads.

These helpers are platform-neutral. Matrix uses them when a homeserver exposes
structured AgentFirstModule metadata, but callers can fall back to normal text
rendering when a platform has no native UI surface.
"""

from __future__ import annotations

from typing import Any, Dict, List


def render_approval_request(approval_request: Dict[str, Any]) -> Dict[str, Any]:
    """Return a normalized approval request payload."""
    request = approval_request if isinstance(approval_request, dict) else {}
    tool_name = str(request.get("tool") or request.get("tool_name") or "tool")
    approvers = request.get("approvers")
    if not isinstance(approvers, list):
        approvers = []

    return {
        "type": "approval_request",
        "tool": tool_name,
        "prompt": str(request.get("prompt") or request.get("description") or ""),
        "requires_reaction": str(
            request.get("requires_reaction") or request.get("reaction") or "✅"
        ),
        "approvers": [str(approver) for approver in approvers],
        "buttons": [
            {"label": "Approve", "action": "approve_tool", "value": "approve"},
            {"label": "Deny", "action": "deny_tool", "value": "deny"},
        ],
    }


def render_tool_status(status: str) -> str:
    """Return a compact human-readable tool status line."""
    status_map = {
        "pending_approval": "Awaiting human approval...",
        "executing": "Executing tool...",
        "completed": "Tool completed",
        "failed": "Tool failed",
        "aborted": "Tool aborted",
    }
    return status_map.get(str(status or "").strip().lower(), str(status or ""))
