"""ACP session manager — maps ACP sessions to Hermes AIAgent instances.

Sessions are persisted to ``~/.hermes/acp_sessions/`` as JSON files so they
survive process restarts.  When the editor reconnects after idle/restart, the
``load_session`` / ``resume_session`` calls will find the persisted session on
disk and restore the full conversation history.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Default session time-to-live: sessions older than this are pruned on startup.
_DEFAULT_TTL_DAYS = 7


def _register_task_cwd(task_id: str, cwd: str) -> None:
    """Bind a task/session id to the editor's working directory for tools."""
    if not task_id:
        return
    try:
        from tools.terminal_tool import register_task_env_overrides
        register_task_env_overrides(task_id, {"cwd": cwd})
    except Exception:
        logger.debug("Failed to register ACP task cwd override", exc_info=True)


def _clear_task_cwd(task_id: str) -> None:
    """Remove task-specific cwd overrides for an ACP session."""
    if not task_id:
        return
    try:
        from tools.terminal_tool import clear_task_env_overrides
        clear_task_env_overrides(task_id)
    except Exception:
        logger.debug("Failed to clear ACP task cwd override", exc_info=True)


@dataclass
class SessionState:
    """Tracks per-session state for an ACP-managed Hermes agent."""

    session_id: str
    agent: Any  # AIAgent instance
    cwd: str = "."
    model: str = ""
    history: List[Dict[str, Any]] = field(default_factory=list)
    cancel_event: Any = None  # threading.Event


class SessionManager:
    """Thread-safe manager for ACP sessions backed by Hermes AIAgent instances.

    Sessions are held in-memory for fast access **and** persisted to disk as
    JSON files under *sessions_dir* so they survive process restarts.
    """

    def __init__(self, agent_factory=None, sessions_dir: Path | str | None = None,
                 ttl_days: int = _DEFAULT_TTL_DAYS):
        """
        Args:
            agent_factory: Optional callable that creates an AIAgent-like object.
                           Used by tests. When omitted, a real AIAgent is created
                           using the current Hermes runtime provider configuration.
            sessions_dir:  Directory for persisted session JSON files.
                           Defaults to ``~/.hermes/acp_sessions/``.
            ttl_days:      Sessions older than this many days are pruned on
                           startup and during ``list_sessions``. 0 = no expiry.
        """
        self._sessions: Dict[str, SessionState] = {}
        self._lock = Lock()
        self._agent_factory = agent_factory
        self._ttl = timedelta(days=ttl_days) if ttl_days > 0 else None

        if sessions_dir is not None:
            self._sessions_dir = Path(sessions_dir)
        else:
            hermes_home = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
            self._sessions_dir = hermes_home / "acp_sessions"

        # Ensure the directory exists (no-op if already present).
        try:
            self._sessions_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("Cannot create ACP sessions dir %s", self._sessions_dir, exc_info=True)

        # Prune expired sessions on startup.
        self._expire_old_sessions()

    # ---- public API ---------------------------------------------------------

    def create_session(self, cwd: str = ".") -> SessionState:
        """Create a new session with a unique ID and a fresh AIAgent."""
        import threading

        session_id = str(uuid.uuid4())
        agent = self._make_agent(session_id=session_id, cwd=cwd)
        state = SessionState(
            session_id=session_id,
            agent=agent,
            cwd=cwd,
            model=getattr(agent, "model", "") or "",
            cancel_event=threading.Event(),
        )
        with self._lock:
            self._sessions[session_id] = state
        _register_task_cwd(session_id, cwd)
        self._persist(state)
        logger.info("Created ACP session %s (cwd=%s)", session_id, cwd)
        return state

    def get_session(self, session_id: str) -> Optional[SessionState]:
        """Return the session for *session_id*, or ``None``.

        If the session is not in memory but exists on disk (e.g. after a
        process restart), it is transparently restored.
        """
        with self._lock:
            state = self._sessions.get(session_id)
        if state is not None:
            return state
        # Attempt to restore from disk.
        return self._restore(session_id)

    def remove_session(self, session_id: str) -> bool:
        """Remove a session from memory and disk. Returns True if it existed."""
        with self._lock:
            existed = self._sessions.pop(session_id, None) is not None
        disk_existed = self._delete_persisted(session_id)
        if existed or disk_existed:
            _clear_task_cwd(session_id)
        return existed or disk_existed

    def fork_session(self, session_id: str, cwd: str = ".") -> Optional[SessionState]:
        """Deep-copy a session's history into a new session."""
        import threading

        original = self.get_session(session_id)  # checks disk too
        if original is None:
            return None

        new_id = str(uuid.uuid4())
        agent = self._make_agent(
            session_id=new_id,
            cwd=cwd,
            model=original.model or None,
        )
        state = SessionState(
            session_id=new_id,
            agent=agent,
            cwd=cwd,
            model=getattr(agent, "model", original.model) or original.model,
            history=copy.deepcopy(original.history),
            cancel_event=threading.Event(),
        )
        with self._lock:
            self._sessions[new_id] = state
        _register_task_cwd(new_id, cwd)
        self._persist(state)
        logger.info("Forked ACP session %s -> %s", session_id, new_id)
        return state

    def list_sessions(self) -> List[Dict[str, Any]]:
        """Return lightweight info dicts for all sessions (memory + disk)."""
        # Collect in-memory sessions first.
        with self._lock:
            seen_ids = set(self._sessions.keys())
            results = [
                {
                    "session_id": s.session_id,
                    "cwd": s.cwd,
                    "model": s.model,
                    "history_len": len(s.history),
                }
                for s in self._sessions.values()
            ]

        # Merge any persisted sessions not currently in memory.
        try:
            for path in self._sessions_dir.glob("*.json"):
                sid = path.stem
                if sid in seen_ids:
                    continue
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    # Check TTL.
                    if self._ttl and self._is_expired(data):
                        path.unlink(missing_ok=True)
                        continue
                    results.append({
                        "session_id": sid,
                        "cwd": data.get("cwd", "."),
                        "model": data.get("model", ""),
                        "history_len": len(data.get("history", [])),
                    })
                except Exception:
                    logger.debug("Skipping unreadable session file %s", path)
        except OSError:
            pass

        return results

    def update_cwd(self, session_id: str, cwd: str) -> Optional[SessionState]:
        """Update the working directory for a session and its tool overrides."""
        state = self.get_session(session_id)  # checks disk too
        if state is None:
            return None
        state.cwd = cwd
        _register_task_cwd(session_id, cwd)
        self._persist(state)
        return state

    def cleanup(self) -> None:
        """Remove all sessions (memory and disk) and clear task-specific cwd overrides."""
        with self._lock:
            session_ids = list(self._sessions.keys())
            self._sessions.clear()
        for session_id in session_ids:
            _clear_task_cwd(session_id)
            self._delete_persisted(session_id)
        # Also remove any disk-only sessions not currently in memory.
        try:
            for path in self._sessions_dir.glob("*.json"):
                _clear_task_cwd(path.stem)
                path.unlink(missing_ok=True)
        except OSError:
            pass

    def save_session(self, session_id: str) -> None:
        """Persist the current state of a session to disk.

        Called by the server after prompt completion, slash commands that
        mutate history, and model switches.
        """
        with self._lock:
            state = self._sessions.get(session_id)
        if state is not None:
            self._persist(state)

    # ---- persistence ---------------------------------------------------------

    def _session_path(self, session_id: str) -> Path:
        return self._sessions_dir / f"{session_id}.json"

    def _persist(self, state: SessionState) -> None:
        """Write session state to disk atomically."""
        data = {
            "session_id": state.session_id,
            "cwd": state.cwd,
            "model": state.model,
            "history": state.history,
            "updated_at": datetime.utcnow().isoformat(),
        }
        path = self._session_path(state.session_id)
        try:
            # Atomic write: write to temp file in same directory, then rename.
            fd, tmp = tempfile.mkstemp(dir=self._sessions_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, default=str)
                os.replace(tmp, path)
            except Exception:
                # Clean up temp file on failure.
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception:
            logger.warning("Failed to persist ACP session %s", state.session_id, exc_info=True)

    def _restore(self, session_id: str) -> Optional[SessionState]:
        """Load a session from disk into memory, recreating the AIAgent."""
        import threading

        path = self._session_path(session_id)
        if not path.exists():
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Failed to read persisted ACP session %s", session_id, exc_info=True)
            return None

        # Check TTL.
        if self._ttl and self._is_expired(data):
            logger.info("Persisted ACP session %s is expired, removing", session_id)
            path.unlink(missing_ok=True)
            return None

        cwd = data.get("cwd", ".")
        model = data.get("model") or None
        history = data.get("history", [])

        try:
            agent = self._make_agent(session_id=session_id, cwd=cwd, model=model)
        except Exception:
            logger.warning("Failed to recreate agent for ACP session %s", session_id, exc_info=True)
            return None

        state = SessionState(
            session_id=session_id,
            agent=agent,
            cwd=cwd,
            model=model or getattr(agent, "model", "") or "",
            history=history,
            cancel_event=threading.Event(),
        )
        with self._lock:
            self._sessions[session_id] = state
        _register_task_cwd(session_id, cwd)
        logger.info("Restored ACP session %s from disk (%d messages)", session_id, len(history))
        return state

    def _delete_persisted(self, session_id: str) -> bool:
        """Delete a persisted session file. Returns True if it existed."""
        path = self._session_path(session_id)
        try:
            path.unlink(missing_ok=False)
            return True
        except FileNotFoundError:
            return False
        except OSError:
            logger.debug("Failed to delete session file %s", path, exc_info=True)
            return False

    def _is_expired(self, data: dict) -> bool:
        """Check whether a persisted session dict has exceeded the TTL."""
        if not self._ttl:
            return False
        updated = data.get("updated_at")
        if not updated:
            return True  # no timestamp → treat as expired
        try:
            ts = datetime.fromisoformat(updated)
            return datetime.utcnow() - ts > self._ttl
        except (ValueError, TypeError):
            return True

    def _expire_old_sessions(self) -> None:
        """Remove persisted session files that have exceeded the TTL."""
        if not self._ttl:
            return
        try:
            for path in self._sessions_dir.glob("*.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if self._is_expired(data):
                        logger.info("Expiring old ACP session %s", path.stem)
                        path.unlink(missing_ok=True)
                except Exception:
                    logger.debug("Skipping unreadable session file during expiry: %s", path)
        except OSError:
            pass

    # ---- internal -----------------------------------------------------------

    def _make_agent(
        self,
        *,
        session_id: str,
        cwd: str,
        model: str | None = None,
    ):
        if self._agent_factory is not None:
            return self._agent_factory()

        from run_agent import AIAgent
        from hermes_cli.config import load_config
        from hermes_cli.runtime_provider import resolve_runtime_provider

        config = load_config()
        model_cfg = config.get("model")
        default_model = "anthropic/claude-opus-4.6"
        requested_provider = None
        if isinstance(model_cfg, dict):
            default_model = str(model_cfg.get("default") or default_model)
            requested_provider = model_cfg.get("provider")
        elif isinstance(model_cfg, str) and model_cfg.strip():
            default_model = model_cfg.strip()

        kwargs = {
            "platform": "acp",
            "enabled_toolsets": ["hermes-acp"],
            "quiet_mode": True,
            "session_id": session_id,
            "model": model or default_model,
        }

        try:
            runtime = resolve_runtime_provider(requested=requested_provider)
            kwargs.update(
                {
                    "provider": runtime.get("provider"),
                    "api_mode": runtime.get("api_mode"),
                    "base_url": runtime.get("base_url"),
                    "api_key": runtime.get("api_key"),
                    "command": runtime.get("command"),
                    "args": list(runtime.get("args") or []),
                }
            )
        except Exception:
            logger.debug("ACP session falling back to default provider resolution", exc_info=True)

        _register_task_cwd(session_id, cwd)
        return AIAgent(**kwargs)
