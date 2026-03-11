"""ACP session manager — maps ACP sessions to hermes AIAgent instances."""

from __future__ import annotations

import copy
import logging
import uuid
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class SessionState:
    """Tracks per-session state for an ACP-managed hermes agent."""

    session_id: str
    agent: Any  # AIAgent instance
    cwd: str = "."
    model: str = ""
    history: List[Dict[str, Any]] = field(default_factory=list)
    cancel_event: Any = None  # threading.Event


class SessionManager:
    """Thread-safe manager for ACP sessions backed by hermes AIAgent instances."""

    def __init__(self, agent_factory=None):
        """
        Args:
            agent_factory: Callable that creates an AIAgent.
                           Defaults to ``AIAgent(platform="acp")``.
                           Accepts optional ``model`` kwarg.
        """
        self._sessions: Dict[str, SessionState] = {}
        self._lock = Lock()
        self._agent_factory = agent_factory

    # ---- public API ---------------------------------------------------------

    def create_session(self, cwd: str = ".") -> SessionState:
        """Create a new session with a unique ID and a fresh AIAgent."""
        import threading

        session_id = str(uuid.uuid4())
        agent = self._make_agent()
        state = SessionState(
            session_id=session_id,
            agent=agent,
            cwd=cwd,
            cancel_event=threading.Event(),
        )
        with self._lock:
            self._sessions[session_id] = state
        logger.info("Created ACP session %s (cwd=%s)", session_id, cwd)
        return state

    def get_session(self, session_id: str) -> Optional[SessionState]:
        """Return the session for *session_id*, or ``None``."""
        with self._lock:
            return self._sessions.get(session_id)

    def remove_session(self, session_id: str) -> bool:
        """Remove a session.  Returns True if it existed."""
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def fork_session(self, session_id: str, cwd: str = ".") -> Optional[SessionState]:
        """Deep-copy a session's history into a new session."""
        import threading

        with self._lock:
            original = self._sessions.get(session_id)
            if original is None:
                return None

            new_id = str(uuid.uuid4())
            agent = self._make_agent(model=original.model or None)
            state = SessionState(
                session_id=new_id,
                agent=agent,
                cwd=cwd,
                model=original.model,
                history=copy.deepcopy(original.history),
                cancel_event=threading.Event(),
            )
            self._sessions[new_id] = state
        logger.info("Forked ACP session %s -> %s", session_id, new_id)
        return state

    def list_sessions(self) -> List[Dict[str, Any]]:
        """Return lightweight info dicts for all sessions."""
        with self._lock:
            return [
                {
                    "session_id": s.session_id,
                    "cwd": s.cwd,
                    "model": s.model,
                    "history_len": len(s.history),
                }
                for s in self._sessions.values()
            ]

    def cleanup(self) -> None:
        """Remove all sessions."""
        with self._lock:
            self._sessions.clear()

    # ---- internal -----------------------------------------------------------

    def _make_agent(self, model: str | None = None):
        if self._agent_factory is not None:
            return self._agent_factory()
        # Default: import and construct AIAgent with ACP platform
        from run_agent import AIAgent
        kwargs = {"platform": "acp"}
        if model:
            kwargs["model"] = model
        return AIAgent(**kwargs)
