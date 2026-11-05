"""The Bot Desktop handoff store keeps bearer secrets private and permits one live handoff."""

from __future__ import annotations

import json
import stat

import pytest

from tools.bot_desktop import handoff, lease


@pytest.fixture(autouse=True)
def _clean_state():
    lease._reset_for_tests()
    yield
    lease._reset_for_tests()


def test_mint_redeem_is_single_use_and_only_hashes_are_persisted():
    record, token = handoff.mint("Log in to the bank", link_ttl=60, session_max=300)

    opened, session_secret = handoff.redeem(token)

    assert opened["id"] == record["id"]
    assert opened["state"] == "opened"
    assert opened["session_deadline"] > opened["created"]
    with pytest.raises(handoff.HandoffInvalid):
        handoff.redeem(token)
    state_file = handoff._path(None)
    stored = state_file.read_text(encoding="utf-8")
    assert token not in stored
    assert session_secret not in stored
    assert json.loads(stored)["records"][0]["token_sha256"]
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600


def test_expiry_of_link_and_open_session_releases_lease(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(handoff.time, "time", lambda: now[0])
    pending, _ = handoff.mint("Open the page", link_ttl=10, session_max=30)
    opened_link, token = handoff.mint("Use the page", link_ttl=10, session_max=30)
    now[0] += 11
    assert handoff.get_state(pending["id"])["state"] == "cancelled"
    assert handoff.get_state(opened_link["id"])["state"] == "expired"
    with pytest.raises(handoff.HandoffInvalid):
        handoff.redeem(token)

    opened, link = handoff.mint("Confirm payment", link_ttl=10, session_max=30)
    _, secret = handoff.redeem(link)
    handoff.take_control(secret)
    assert lease.human_holds()
    now[0] += 31
    assert handoff.get_state(opened["id"])["state"] == "expired"
    assert lease.get().holder == lease.AGENT


def test_remint_cancels_previous_live_session_and_releases_control():
    first, token = handoff.mint("First request", link_ttl=60, session_max=120)
    _, secret = handoff.redeem(token)
    handoff.take_control(secret)

    second, _ = handoff.mint("Second request", link_ttl=60, session_max=120)

    assert handoff.get_state(first["id"])["state"] == "cancelled"
    assert handoff.get_state(second["id"])["state"] == "pending"
    assert lease.get().holder == lease.AGENT


def test_take_control_and_hand_back_move_the_shared_lease():
    record, token = handoff.mint("Enter the one-time code", link_ttl=60, session_max=120)
    _, secret = handoff.redeem(token)

    held = handoff.take_control(secret)
    assert held.holder == lease.HUMAN
    assert held.viewer_id == record["viewer_id"]
    assert held.reason == "handoff: Enter the one-time code"

    final = handoff.hand_back(secret)
    assert final["state"] == "handed_back"
    assert lease.get().holder == lease.AGENT


def test_cancel_releases_opened_lease_and_is_idempotent():
    record, token = handoff.mint("Finish the form", link_ttl=60, session_max=120)
    _, secret = handoff.redeem(token)
    handoff.take_control(secret)

    assert handoff.cancel(record["id"])["state"] == "cancelled"
    assert handoff.cancel(record["id"])["state"] == "cancelled"
    assert lease.get().holder == lease.AGENT


def test_stale_heartbeat_expires_live_handoff_and_releases_lease(monkeypatch):
    """A link whose waiting tool died (no heartbeat) must not keep or hand out control."""
    now = [5000.0]
    monkeypatch.setattr(handoff.time, "time", lambda: now[0])
    record, token = handoff.mint("Approve the login", link_ttl=600, session_max=1800)
    _, secret = handoff.redeem(token)
    handoff.take_control(secret)
    assert lease.get().holder == lease.HUMAN

    now[0] += handoff.HEARTBEAT_STALE_SECONDS - 1
    handoff.heartbeat(record["id"])  # a live tool keeps it open
    now[0] += handoff.HEARTBEAT_STALE_SECONDS - 1
    assert handoff.get_state(record["id"])["state"] == "opened"

    now[0] += handoff.HEARTBEAT_STALE_SECONDS  # tool gone: no heartbeat
    assert handoff.get_state(record["id"])["state"] == "expired"
    assert lease.get().holder == lease.AGENT
    with pytest.raises(handoff.HandoffInvalid):
        handoff.take_control(secret)


def test_stale_pending_link_cannot_be_redeemed(monkeypatch):
    now = [9000.0]
    monkeypatch.setattr(handoff.time, "time", lambda: now[0])
    _, token = handoff.mint("Solve the CAPTCHA", link_ttl=600, session_max=1800)
    now[0] += handoff.HEARTBEAT_STALE_SECONDS + 1
    with pytest.raises(handoff.HandoffInvalid):
        handoff.redeem(token)
