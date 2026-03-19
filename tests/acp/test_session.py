"""Tests for acp_adapter.session — SessionManager and SessionState."""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from acp_adapter.session import SessionManager, SessionState


def _mock_agent():
    return MagicMock(name="MockAIAgent")


@pytest.fixture()
def manager():
    """SessionManager with a mock agent factory (avoids needing API keys)."""
    return SessionManager(agent_factory=_mock_agent)


# ---------------------------------------------------------------------------
# create / get
# ---------------------------------------------------------------------------


class TestCreateSession:
    def test_create_session_returns_state(self, manager):
        state = manager.create_session(cwd="/tmp/work")
        assert isinstance(state, SessionState)
        assert state.cwd == "/tmp/work"
        assert state.session_id
        assert state.history == []
        assert state.agent is not None

    def test_create_session_registers_task_cwd(self, manager, monkeypatch):
        calls = []
        monkeypatch.setattr("acp_adapter.session._register_task_cwd", lambda task_id, cwd: calls.append((task_id, cwd)))
        state = manager.create_session(cwd="/tmp/work")
        assert calls == [(state.session_id, "/tmp/work")]

    def test_session_ids_are_unique(self, manager):
        s1 = manager.create_session()
        s2 = manager.create_session()
        assert s1.session_id != s2.session_id

    def test_get_session(self, manager):
        state = manager.create_session()
        fetched = manager.get_session(state.session_id)
        assert fetched is state

    def test_get_nonexistent_session_returns_none(self, manager):
        assert manager.get_session("does-not-exist") is None


# ---------------------------------------------------------------------------
# fork
# ---------------------------------------------------------------------------


class TestForkSession:
    def test_fork_session_deep_copies_history(self, manager):
        original = manager.create_session()
        original.history.append({"role": "user", "content": "hello"})
        original.history.append({"role": "assistant", "content": "hi"})

        forked = manager.fork_session(original.session_id, cwd="/new")
        assert forked is not None

        # History should be equal in content
        assert len(forked.history) == 2
        assert forked.history[0]["content"] == "hello"

        # But a deep copy — mutating one doesn't affect the other
        forked.history.append({"role": "user", "content": "extra"})
        assert len(original.history) == 2
        assert len(forked.history) == 3

    def test_fork_session_has_new_id(self, manager):
        original = manager.create_session()
        forked = manager.fork_session(original.session_id)
        assert forked is not None
        assert forked.session_id != original.session_id

    def test_fork_nonexistent_returns_none(self, manager):
        assert manager.fork_session("bogus-id") is None


# ---------------------------------------------------------------------------
# list / cleanup / remove
# ---------------------------------------------------------------------------


class TestListAndCleanup:
    def test_list_sessions_empty(self, manager):
        assert manager.list_sessions() == []

    def test_list_sessions_returns_created(self, manager):
        s1 = manager.create_session(cwd="/a")
        s2 = manager.create_session(cwd="/b")
        listing = manager.list_sessions()
        ids = {s["session_id"] for s in listing}
        assert s1.session_id in ids
        assert s2.session_id in ids
        assert len(listing) == 2

    def test_cleanup_clears_all(self, manager):
        manager.create_session()
        manager.create_session()
        assert len(manager.list_sessions()) == 2
        manager.cleanup()
        assert manager.list_sessions() == []

    def test_remove_session(self, manager):
        state = manager.create_session()
        assert manager.remove_session(state.session_id) is True
        assert manager.get_session(state.session_id) is None
        # Removing again returns False
        assert manager.remove_session(state.session_id) is False


# ---------------------------------------------------------------------------
# persistence — sessions survive process restarts
# ---------------------------------------------------------------------------


class TestPersistence:
    """Verify that sessions are persisted to disk and can be restored."""

    def test_create_session_writes_json_file(self, manager):
        state = manager.create_session(cwd="/project")
        path = manager._session_path(state.session_id)
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["session_id"] == state.session_id
        assert data["cwd"] == "/project"
        assert data["history"] == []

    def test_get_session_restores_from_disk(self, manager):
        """Simulate process restart: create session, drop from memory, get again."""
        state = manager.create_session(cwd="/work")
        state.history.append({"role": "user", "content": "hello"})
        state.history.append({"role": "assistant", "content": "hi there"})
        manager.save_session(state.session_id)

        sid = state.session_id

        # Drop from in-memory store (simulates process restart).
        with manager._lock:
            del manager._sessions[sid]

        # get_session should transparently restore from disk.
        restored = manager.get_session(sid)
        assert restored is not None
        assert restored.session_id == sid
        assert restored.cwd == "/work"
        assert len(restored.history) == 2
        assert restored.history[0]["content"] == "hello"
        assert restored.history[1]["content"] == "hi there"
        # Agent should have been recreated.
        assert restored.agent is not None

    def test_save_session_updates_disk(self, manager):
        state = manager.create_session()
        state.history.append({"role": "user", "content": "test"})
        manager.save_session(state.session_id)

        data = json.loads(manager._session_path(state.session_id).read_text())
        assert len(data["history"]) == 1
        assert data["history"][0]["content"] == "test"

    def test_remove_session_deletes_file(self, manager):
        state = manager.create_session()
        path = manager._session_path(state.session_id)
        assert path.exists()
        manager.remove_session(state.session_id)
        assert not path.exists()

    def test_cleanup_removes_all_files(self, manager):
        s1 = manager.create_session()
        s2 = manager.create_session()
        p1 = manager._session_path(s1.session_id)
        p2 = manager._session_path(s2.session_id)
        assert p1.exists() and p2.exists()
        manager.cleanup()
        assert not p1.exists()
        assert not p2.exists()

    def test_list_sessions_includes_disk_only(self, manager):
        """Sessions only on disk (not in memory) appear in list_sessions."""
        state = manager.create_session(cwd="/disk-only")
        sid = state.session_id

        # Drop from memory.
        with manager._lock:
            del manager._sessions[sid]

        listing = manager.list_sessions()
        ids = {s["session_id"] for s in listing}
        assert sid in ids

    def test_fork_restores_source_from_disk(self, manager):
        """Forking a session that is only on disk should work."""
        original = manager.create_session()
        original.history.append({"role": "user", "content": "context"})
        manager.save_session(original.session_id)

        # Drop original from memory.
        with manager._lock:
            del manager._sessions[original.session_id]

        forked = manager.fork_session(original.session_id, cwd="/fork")
        assert forked is not None
        assert len(forked.history) == 1
        assert forked.history[0]["content"] == "context"
        assert forked.session_id != original.session_id

    def test_update_cwd_restores_from_disk(self, manager):
        state = manager.create_session(cwd="/old")
        sid = state.session_id

        with manager._lock:
            del manager._sessions[sid]

        updated = manager.update_cwd(sid, "/new")
        assert updated is not None
        assert updated.cwd == "/new"

        # Should also be persisted.
        data = json.loads(manager._session_path(sid).read_text())
        assert data["cwd"] == "/new"


# ---------------------------------------------------------------------------
# TTL / expiry
# ---------------------------------------------------------------------------


class TestSessionExpiry:
    def test_expired_session_not_restored(self, tmp_path):
        """Sessions past TTL are deleted on access, not restored."""
        mgr = SessionManager(agent_factory=_mock_agent, sessions_dir=tmp_path, ttl_days=1)
        state = mgr.create_session()
        sid = state.session_id

        # Manually backdate the persisted timestamp.
        path = mgr._session_path(sid)
        data = json.loads(path.read_text())
        from datetime import datetime, timedelta
        data["updated_at"] = (datetime.utcnow() - timedelta(days=10)).isoformat()
        path.write_text(json.dumps(data))

        # Drop from memory.
        with mgr._lock:
            del mgr._sessions[sid]

        assert mgr.get_session(sid) is None
        assert not path.exists()

    def test_expired_sessions_pruned_on_startup(self, tmp_path):
        """Creating a new SessionManager prunes expired session files."""
        # Create a stale session file.
        from datetime import datetime, timedelta
        stale_id = "stale-session-id"
        stale_data = {
            "session_id": stale_id,
            "cwd": ".",
            "model": "",
            "history": [],
            "updated_at": (datetime.utcnow() - timedelta(days=30)).isoformat(),
        }
        stale_path = tmp_path / f"{stale_id}.json"
        stale_path.write_text(json.dumps(stale_data))
        assert stale_path.exists()

        # Creating a SessionManager should prune it.
        SessionManager(agent_factory=_mock_agent, sessions_dir=tmp_path, ttl_days=7)
        assert not stale_path.exists()

    def test_no_expiry_with_zero_ttl(self, tmp_path):
        """ttl_days=0 means sessions never expire."""
        from datetime import datetime, timedelta
        old_id = "ancient"
        old_data = {
            "session_id": old_id,
            "cwd": ".",
            "model": "",
            "history": [{"role": "user", "content": "old"}],
            "updated_at": (datetime.utcnow() - timedelta(days=365)).isoformat(),
        }
        old_path = tmp_path / f"{old_id}.json"
        old_path.write_text(json.dumps(old_data))

        mgr = SessionManager(agent_factory=_mock_agent, sessions_dir=tmp_path, ttl_days=0)
        # Should still exist and be loadable.
        assert old_path.exists()
        restored = mgr.get_session(old_id)
        assert restored is not None
        assert len(restored.history) == 1
