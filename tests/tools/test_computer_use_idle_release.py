"""Idle computer_use backends are released so a session that just ends does not keep its cua-driver.

Live (Nov 2026, SSH Bot Desktop): every finished one-shot ``hermes chat -q`` session left one
``cua-driver mcp`` on the desktop host until the gateway restarted (six orphans after a day), because
agent-cache eviction keeps session tool state for resume and nothing else released it.
"""
import threading

import pytest

from tools.computer_use import tool


class _FakeBackend:
    def __init__(self):
        self.stopped = 0

    def stop(self):
        self.stopped += 1


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(tool, "_ensure_idle_reaper", lambda: None)  # no background thread in tests
    tool.reset_backend_for_tests()
    yield
    tool.reset_backend_for_tests()


def _install(sid, last_used):
    backend = _FakeBackend()
    with tool._backend_lock:
        tool._install_backend(sid, backend, tool._cua_permission_mode(sid))
        tool._backend_last_used[sid] = last_used
    return backend


def test_idle_backend_is_released_and_fresh_one_kept():
    old, fresh = _install("old", 0.0), _install("fresh", 950.0)
    assert tool.release_idle_computer_use_backends(900, now=1000.0) == 1
    assert old.stopped == 1 and fresh.stopped == 0
    assert "old" not in tool._backends and "old" not in tool._backend_last_used
    assert tool._backends["fresh"] is fresh


def test_busy_backend_is_never_released():
    backend = _install("busy", 0.0)
    lock = tool._backend_call_locks["busy"]
    held, done = threading.Event(), threading.Event()

    def hold():
        with lock:
            held.set()
            done.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    held.wait(5)
    try:
        assert tool.release_idle_computer_use_backends(900, now=10_000.0) == 0
        assert backend.stopped == 0 and tool._backends["busy"] is backend
    finally:
        done.set()
        t.join(5)


@pytest.mark.parametrize("limit", [0, -1])
def test_zero_or_negative_disables(limit):
    backend = _install("s", 0.0)
    assert tool.release_idle_computer_use_backends(limit, now=10_000.0) == 0
    assert backend.stopped == 0


def test_config_default_is_used_when_no_limit_given(monkeypatch):
    monkeypatch.setattr(tool, "_idle_release_seconds", lambda: 60.0)
    backend = _install("s", 0.0)
    assert tool.release_idle_computer_use_backends(now=61.0) == 1 and backend.stopped == 1


def test_a_call_restamps_last_used(monkeypatch):
    backend = _install("s", 0.0)
    monkeypatch.setattr(tool, "_get_backend", lambda session_id="": backend)
    with tool._backend_for_call("s") as got:
        assert got is backend
    assert tool._backend_last_used["s"] > 0.0
    # Fresh after the call: a sweep right now keeps it.
    assert tool.release_idle_computer_use_backends(900) == 0 and backend.stopped == 0


def test_release_resets_screenshot_dedup(monkeypatch):
    seen = []
    monkeypatch.setattr(tool, "_reset_screenshot_dedup", lambda sid=None: seen.append(sid))
    _install("s", 0.0)
    tool.release_idle_computer_use_backends(900, now=1000.0)
    assert "s" in seen


def test_idle_release_seconds_reads_config(monkeypatch):
    import hermes_cli.config as cfg
    monkeypatch.setattr(cfg, "load_config_readonly", lambda: {"computer_use": {"idle_release_seconds": 120}})
    assert tool._idle_release_seconds() == 120.0
    monkeypatch.setattr(cfg, "load_config_readonly", dict)
    assert tool._idle_release_seconds() == 900.0
