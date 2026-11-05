"""The inline desktop handoff waits through clarify and returns the human's outcome safely."""

from __future__ import annotations

import json

import pytest

from tools import clarify_gateway
from tools.bot_desktop import handoff, lease
from tools.desktop_handoff_tool import desktop_handoff_tool


@pytest.fixture(autouse=True)
def _clean_state():
    lease._reset_for_tests()
    yield
    clarify_gateway.clear_session("default")
    lease._reset_for_tests()


def _setup(monkeypatch):
    from tools.bot_desktop import runtime

    monkeypatch.setattr(runtime, "ensure_started_for_tool", lambda: None)
    monkeypatch.setattr("tools.desktop_handoff_tool._config", lambda: {
        "bot_desktop": {"handoff": {
            "public_url": "https://desktop.example.test/agent-a/",
            "link_ttl_seconds": 120,
            "session_max_seconds": 1800,
        }},
    })
    captured = {}
    real_mint = handoff.mint

    def capture(reason, **kwargs):
        result = real_mint(reason, **kwargs)
        captured.update(record=result[0], token=result[1])
        return result

    monkeypatch.setattr(handoff, "mint", capture)
    return captured


def test_unconfigured_and_missing_callback_return_clear_errors(monkeypatch):
    monkeypatch.setattr("tools.desktop_handoff_tool._config", lambda: {"bot_desktop": {"handoff": {}}})
    assert "desktop handoff is not configured" in desktop_handoff_tool("Sign in", callback=lambda _: {})

    monkeypatch.setattr("tools.desktop_handoff_tool._config", lambda: {
        "bot_desktop": {"handoff": {"public_url": "https://desktop.example.test/"}},
    })
    assert "no human is available to take over" in desktop_handoff_tool("Sign in")


def test_watcher_resolves_the_handoff_clarify_when_human_hands_back(monkeypatch):
    captured = _setup(monkeypatch)

    def callback(questions):
        question = questions[0]
        entry = clarify_gateway.register("handoff-card", "default", question["question"], None)
        assert "https://desktop.example.test/agent-a/#t=" in question["question"]
        assert "(handoff " + captured["record"]["id"] + ")" in question["question"]
        _, secret = handoff.redeem(captured["token"])
        handoff.take_control(secret)
        handoff.hand_back(secret)
        answer = clarify_gateway.wait_for_response(entry.clarify_id, timeout=4)
        return {"answers": {"q0": answer}, "outcome": "submitted"}

    result = json.loads(desktop_handoff_tool("Complete the login", callback=callback))

    assert result["outcome"] == "handed_back"
    assert "fresh screenshot" in result["next"].lower()
    assert handoff.get_state(captured["record"]["id"])["state"] == "handed_back"
    assert lease.get().holder == lease.AGENT


def test_human_chat_reply_releases_the_lease(monkeypatch):
    captured = _setup(monkeypatch)

    def callback(questions):
        entry = clarify_gateway.register("reply-card", "default", questions[0]["question"], None)
        _, secret = handoff.redeem(captured["token"])
        handoff.take_control(secret)
        clarify_gateway.resolve_gateway_clarify(entry.clarify_id, "done")
        answer = clarify_gateway.wait_for_response(entry.clarify_id, timeout=1)
        return {"answers": {"q0": answer}, "outcome": "submitted"}

    result = json.loads(desktop_handoff_tool("Confirm the payment", callback=callback))

    assert result["outcome"] == "human_replied"
    assert result["reply"] == "done"
    assert lease.get().holder == lease.AGENT
    assert handoff.get_state(captured["record"]["id"])["state"] == "cancelled"

