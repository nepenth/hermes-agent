"""Inline tool that asks the current human to take over Bot Desktop.

The clarify callback is the existing cross-surface waiting primitive, so the agent turn remains
blocked until the person hands back, replies in chat, or the bounded desktop session expires.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Callable, Optional

from tools.registry import registry, tool_error

_NEXT = "Capture a fresh screenshot before acting."
_WATCH_RESOLUTION = "\x00desktop-handoff-resolved"
logger = logging.getLogger(__name__)


def _config() -> dict:
    from hermes_cli.config import load_config

    return load_config() or {}


def _result(outcome: str, **fields) -> str:
    return json.dumps({"outcome": outcome, **fields, "next": _NEXT}, ensure_ascii=False)


def _tool_error(message: str) -> str:
    return tool_error(message, next=_NEXT)


def _reply_answer(reply: dict) -> tuple[str, Optional[str]]:
    answers = reply.get("answers") or {}
    if "q0" not in answers:
        return "", None
    raw = answers.get("q0")
    return ("" if raw in (None, "") else str(raw)), ("skipped" if raw in (None, "") else "answered")


def _state_outcome(record: Optional[dict]) -> Optional[str]:
    if not record:
        return None
    if record.get("state") == "handed_back":
        return "handed_back"
    if record.get("state") == "expired":
        return "expired_unopened" if record.get("session_deadline") is None else "session_expired"
    if record.get("state") == "cancelled":
        return "cancelled"
    return None


def _watch_handoff(record_id: str, session_key: str, stop: threading.Event) -> None:
    from tools.bot_desktop import handoff
    from tools import clarify_gateway

    # Keep going until the tool returns (`stop`): the card may register after the state already went
    # terminal, and resolving is idempotent. Heartbeat so the store can tell this tool is still waiting.
    while not stop.is_set():
        try:
            handoff.heartbeat(record_id)
            record = handoff.get_state(record_id)
            if _state_outcome(record):
                entry = clarify_gateway.get_pending_for_session(session_key, include_choice_prompts=True)
                if entry is not None and record_id in entry.question:
                    clarify_gateway.resolve_gateway_clarify(entry.clarify_id, _WATCH_RESOLUTION)
        except Exception:
            logger.exception("desktop handoff watcher tick failed for id=%s", record_id)
        stop.wait(1)


def desktop_handoff_tool(
    reason: str,
    timeout_minutes: int = 20,
    callback: Optional[Callable] = None,
) -> str:
    """Ask the human to take over the desktop using a single-use browser link."""
    config = _config()
    bot_desktop = config.get("bot_desktop") or {}
    handoff_config = bot_desktop.get("handoff") or {}
    public_url = str(handoff_config.get("public_url") or "").strip()
    if not public_url:
        return _tool_error("desktop handoff is not configured: set bot_desktop.handoff.public_url")
    if callback is None:
        return _tool_error("no human is available to take over")

    try:
        from tools.bot_desktop.runtime import ensure_started_for_tool
        ensure_started_for_tool()
    except Exception as exc:
        logger.exception("desktop handoff could not start the desktop")
        return _tool_error(f"could not start the desktop for handoff: {exc}")

    timeout = max(1, min(60, int(timeout_minutes)))
    link_ttl = max(0, int(handoff_config.get("link_ttl_seconds", 600)))
    configured_max = max(0, int(handoff_config.get("session_max_seconds", 1800)))
    session_max = min(timeout * 60, configured_max)
    # The wait itself is bounded by the clarify timeout; keep the session inside it so a timed-out
    # wait never cancels a human mid-task. (<= 0 means unlimited.)
    from tools.clarify_gateway import get_clarify_timeout

    clarify_timeout = int(get_clarify_timeout())
    if clarify_timeout > 0:
        session_max = max(60, min(session_max, clarify_timeout - link_ttl))
    base = public_url.rstrip("/") + "/"
    from tools.bot_desktop import handoff
    record, token = handoff.mint(str(reason).strip(), link_ttl=link_ttl, session_max=session_max)
    link = f"{base}#t={token}"
    question = (
        f"🖥️ I need you on my desktop: {str(reason).strip()}\n{link}\n"
        f"Single-use link, opens within {link_ttl // 60} min. Click \"Hand back to agent\" on the page "
        f"when you're done — or reply here. (handoff {record['id']})"
    )
    from tools.clarify_tool import _normalize_questions

    normalized, error = _normalize_questions([{"question": question}])
    if error:
        handoff.cancel(record["id"])
        return _tool_error(error)

    from tools.approval_context import get_current_session_key

    session_key = get_current_session_key()
    stop = threading.Event()
    from agent.memory_provider import spawn_context_thread

    watcher = spawn_context_thread(
        _watch_handoff, args=(record["id"], session_key, stop),
        name=f"desktop-handoff-{record['id']}", daemon=True,
    )
    watcher.start()
    outcome = "cancelled"
    reply_text = None
    try:
        reply = callback(normalized)
        if not isinstance(reply, dict):
            reply = {"answers": {}, "outcome": "undelivered"}
        reply_text, answer_status = _reply_answer(reply)
        state = handoff.get_state(record["id"])
        terminal = _state_outcome(state)
        if terminal:
            outcome = terminal
        elif reply.get("outcome") == "undelivered":
            outcome = "undelivered"
        elif reply.get("outcome") == "timed_out":
            outcome = "timed_out"
        elif reply.get("outcome") == "cancelled":
            outcome = "cancelled"
        elif answer_status == "skipped":
            outcome = "skipped"
        elif answer_status == "answered":
            outcome = "human_replied"
        else:
            outcome = "undelivered"
    except Exception as exc:
        logger.exception("desktop handoff callback failed for id=%s", record["id"])
        return _tool_error(f"desktop handoff failed: {exc}")
    finally:
        stop.set()
        watcher.join(timeout=1.2)
        current = handoff.get_state(record["id"])
        if current and current.get("state") in {"pending", "opened"}:
            handoff.cancel(record["id"])
        if outcome == "human_replied" or outcome in {
            "skipped", "cancelled", "undelivered", "timed_out",
        }:
            from tools.bot_desktop import lease

            lease.release(record["viewer_id"])

    fields = {"reason": str(reason).strip()}
    if outcome == "human_replied":
        fields["reply"] = reply_text
    return _result(outcome, **fields)


def check_desktop_handoff_requirements() -> bool:
    """Use the same backend reachability gate as ``computer_use``."""
    from tools.computer_use.tool import check_computer_use_requirements

    return check_computer_use_requirements()


DESKTOP_HANDOFF_SCHEMA = {
    "name": "desktop_handoff",
    "description": (
        "Hand the live Bot Desktop to a human for CAPTCHAs, logins/2FA, payments, or anything a human "
        "must do on the desktop. Never try to solve CAPTCHAs yourself. The human can use the single-use "
        "link or reply in chat; after this returns handed_back, take a fresh capture before acting."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "One sentence telling the human what to do."},
            "timeout_minutes": {
                "type": "integer", "default": 20,
                "description": "Maximum minutes to wait for the human (1–60; default 20).",
            },
        },
        "required": ["reason"],
    },
}


registry.register(
    name="desktop_handoff",
    toolset="computer_use",
    schema=DESKTOP_HANDOFF_SCHEMA,
    handler=lambda args, **kw: desktop_handoff_tool(
        args.get("reason", ""), args.get("timeout_minutes", 20), callback=kw.get("callback")),
    check_fn=check_desktop_handoff_requirements,
)
