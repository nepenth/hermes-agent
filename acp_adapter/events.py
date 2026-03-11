"""Callback factories for bridging AIAgent events to ACP notifications.

Each factory returns a callable with the signature that AIAgent expects
for its callbacks.  Internally, the callbacks push ACP session updates
to the client via ``conn.session_update()`` using
``asyncio.run_coroutine_threadsafe()`` (since AIAgent runs in a worker
thread while the event loop lives on the main thread).
"""

import asyncio
import json
import logging
from typing import Any, Callable, Dict

import acp

from .tools import (
    build_tool_start,
    build_tool_complete,
    build_tool_title,
    get_tool_kind,
    make_tool_call_id,
)

logger = logging.getLogger(__name__)


def _send_update(
    conn: acp.Client,
    session_id: str,
    loop: asyncio.AbstractEventLoop,
    update: Any,
) -> None:
    """Fire-and-forget an ACP session update from a worker thread.

    Swallows exceptions so agent execution is never interrupted by a
    notification failure.
    """
    try:
        future = asyncio.run_coroutine_threadsafe(
            conn.session_update(session_id, update), loop
        )
        # Don't block indefinitely; 5 s is generous for a notification
        future.result(timeout=5)
    except Exception:
        logger.debug("Failed to send ACP update", exc_info=True)


# ------------------------------------------------------------------
# Tool progress callback
# ------------------------------------------------------------------

def make_tool_progress_cb(
    conn: acp.Client,
    session_id: str,
    loop: asyncio.AbstractEventLoop,
    tool_call_ids: Dict[str, str],
) -> Callable:
    """Create a ``tool_progress_callback`` for AIAgent.

    Signature expected by AIAgent::

        tool_progress_callback(name: str, preview: str, args: dict)

    Emits ``ToolCallStart`` on the first call for a tool invocation.
    """

    def _tool_progress(name: str, preview: str, args: Any = None) -> None:
        # Parse args if it's a string
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, TypeError):
                args = {"raw": args}
        if not isinstance(args, dict):
            args = {}

        tc_id = make_tool_call_id()
        tool_call_ids[name] = tc_id

        update = build_tool_start(tc_id, name, args)
        _send_update(conn, session_id, loop, update)

    return _tool_progress


# ------------------------------------------------------------------
# Thinking callback
# ------------------------------------------------------------------

def make_thinking_cb(
    conn: acp.Client,
    session_id: str,
    loop: asyncio.AbstractEventLoop,
) -> Callable:
    """Create a ``thinking_callback`` for AIAgent.

    Signature expected by AIAgent::

        thinking_callback(text: str)

    Emits an ``AgentThoughtChunk`` via ``update_agent_thought_text()``.
    """

    def _thinking(text: str) -> None:
        if not text:
            return
        update = acp.update_agent_thought_text(text)
        _send_update(conn, session_id, loop, update)

    return _thinking


# ------------------------------------------------------------------
# Step callback
# ------------------------------------------------------------------

def make_step_cb(
    conn: acp.Client,
    session_id: str,
    loop: asyncio.AbstractEventLoop,
    tool_call_ids: Dict[str, str],
) -> Callable:
    """Create a ``step_callback`` for AIAgent.

    Signature expected by AIAgent::

        step_callback(api_call_count: int, prev_tools: list)

    Marks previously-started tool calls as completed and can emit
    intermediate agent messages.
    """

    def _step(api_call_count: int, prev_tools: Any = None) -> None:
        # Mark previously tracked tool calls as completed
        if prev_tools and isinstance(prev_tools, list):
            for tool_info in prev_tools:
                tool_name = None
                result = None

                if isinstance(tool_info, dict):
                    tool_name = tool_info.get("name") or tool_info.get("function_name")
                    result = tool_info.get("result") or tool_info.get("output")
                elif isinstance(tool_info, str):
                    tool_name = tool_info

                if tool_name and tool_name in tool_call_ids:
                    tc_id = tool_call_ids.pop(tool_name)
                    update = build_tool_complete(
                        tc_id, tool_name, result=str(result) if result else None
                    )
                    _send_update(conn, session_id, loop, update)

    return _step


# ------------------------------------------------------------------
# Agent message callback (streams final response chunks)
# ------------------------------------------------------------------

def make_message_cb(
    conn: acp.Client,
    session_id: str,
    loop: asyncio.AbstractEventLoop,
) -> Callable:
    """Create a callback that streams agent response text to the editor.

    Used to send the agent's final response incrementally.
    """

    def _message(text: str) -> None:
        if not text:
            return
        update = acp.update_agent_message_text(text)
        _send_update(conn, session_id, loop, update)

    return _message
