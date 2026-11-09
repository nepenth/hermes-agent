"""Matrix clarify ("question") cards: wire payload, seeded reactions, answers, settle edit."""

from __future__ import annotations

import json
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig

ROOM = "!room:example.org"
OWNER = "@owner:example.org"
OTHER = "@other:example.org"
BOT = "@hermes:example.org"
SKIP_LINE = 'Reply "skip" to skip this question.'


@pytest.fixture(autouse=True)
def _clean_clarify_state():
    from tools import clarify_gateway as cg
    with cg._lock:
        cg._entries.clear()
        cg._session_index.clear()
    with patch("tools.clarify_gateway.get_clarify_timeout", return_value=600):
        yield
    with cg._lock:
        cg._entries.clear()
        cg._session_index.clear()


def _adapter(monkeypatch):
    monkeypatch.setenv("MATRIX_ALLOWED_USERS", f"{OWNER},{OTHER}")
    from plugins.platforms.matrix.adapter import MatrixAdapter

    adapter = MatrixAdapter(PlatformConfig(enabled=True, token="tok", extra={
        "homeserver": "https://matrix.example.org", "user_id": BOT}))
    adapter._client = types.SimpleNamespace()
    adapter.sent = []  # every m.room.message content, in order
    counter = iter(range(1, 1000))

    async def _send_room_message(chat_id, content):
        adapter.sent.append(content)
        return f"$card{next(counter)}"

    async def _send_content_event(room_id, content):
        adapter.sent.append(content)
        return types.SimpleNamespace(success=True, message_id=f"$edit{next(counter)}", error=None)

    adapter._send_room_message = _send_room_message
    adapter._send_content_event = _send_content_event
    adapter._send_reaction = AsyncMock(side_effect=lambda room, evt, emoji: f"$r-{emoji}")
    adapter._schedule_reaction_redaction = MagicMock()
    adapter._background_read_receipt = MagicMock()
    adapter._send_invalid_reaction_feedback = AsyncMock(return_value=True)
    adapter.handle_message = AsyncMock()
    return adapter


def _register(clarify_id="cid1", choices=("Staging (Recommended)", "Production", "Both"), multi=False):
    from tools import clarify_gateway as cg
    return cg.register(clarify_id=clarify_id, session_key="sess", question="Deploy where?",
                       choices=list(choices) if choices else None, multi_select=multi)


async def _send_card(adapter, clarify_id="cid1", choices=("Staging (Recommended)", "Production", "Both"),
                     requester=OWNER):
    meta = {"requester_user_id": requester} if requester else None
    return await adapter.send_clarify(
        chat_id=ROOM, question=f"Deploy where?\n{SKIP_LINE}", choices=list(choices) if choices else None,
        clarify_id=clarify_id, session_key="sess", metadata=meta)


def _seeded(adapter):
    return [call.args[2] for call in adapter._send_reaction.await_args_list]


async def _base_text(adapter, clarify_id, choices):
    """What the base adapter's plain-text send_clarify would post."""
    from gateway.platforms.base import BasePlatformAdapter
    adapter.send = AsyncMock(return_value=types.SimpleNamespace(success=True, message_id="$x"))
    try:
        await BasePlatformAdapter.send_clarify(
            adapter, chat_id=ROOM, question=f"Deploy where?\n{SKIP_LINE}", choices=choices,
            clarify_id=clarify_id, session_key="sess")
        return adapter.send.await_args.kwargs["content"]
    finally:
        del adapter.send


class TestCardContent:
    @pytest.mark.asyncio
    async def test_single_select_card_shape(self, monkeypatch):
        adapter = _adapter(monkeypatch)
        _register()
        choices = ["Staging (Recommended)", "Production", "Both"]
        expected_body = await _base_text(adapter, "cid1", choices)

        result = await _send_card(adapter)

        assert result.success and result.message_id == "$card1"
        content = adapter.sent[0]
        assert content["msgtype"] == "m.text"
        assert content["body"] == expected_body  # no regression for clients without card support
        assert content["format"] == "org.matrix.custom.html"
        html = content["formatted_body"]
        assert html.startswith("<p><strong>❓ Deploy where?</strong></p>")
        assert "<ol><li>Staging <em>(Recommended)</em></li><li>Production</li><li>Both</li></ol>" in html
        assert "skip" not in html.split("<ol>")[0]
        payload = content["org.hermes.clarify"]
        expires = payload.pop("expires_at_ms")
        assert isinstance(expires, int) and expires > 0
        assert payload == {
            "v": 1, "clarify_id": "cid1", "question": "Deploy where?",
            "choices": [
                {"index": 1, "label": "Staging", "recommended": True},
                {"index": 2, "label": "Production", "recommended": False},
                {"index": 3, "label": "Both", "recommended": False},
            ],
            "multi_select": False, "allow_other": True, "allow_skip": True,
            "requester_user_id": OWNER, "state": "pending",
        }

    @pytest.mark.asyncio
    async def test_open_ended_card_body_matches_base_and_has_no_choices(self, monkeypatch):
        adapter = _adapter(monkeypatch)
        _register(choices=None)
        expected_body = await _base_text(adapter, "cid1", None)
        await _send_card(adapter, choices=None, requester=None)
        content = adapter.sent[0]
        assert content["body"] == expected_body
        payload = content["org.hermes.clarify"]
        assert payload["choices"] == [] and payload["multi_select"] is False
        assert payload["requester_user_id"] is None
        assert "<ol>" not in content["formatted_body"]

    @pytest.mark.asyncio
    async def test_unlimited_timeout_has_no_expiry(self, monkeypatch):
        adapter = _adapter(monkeypatch)
        _register()
        with patch("tools.clarify_gateway.get_clarify_timeout", return_value=0):
            await _send_card(adapter)
        assert adapter.sent[0]["org.hermes.clarify"]["expires_at_ms"] is None

    @pytest.mark.asyncio
    async def test_card_keeps_text_capture_armed(self, monkeypatch):
        adapter = _adapter(monkeypatch)
        entry = _register()
        await _send_card(adapter)
        assert entry.awaiting_text is True

    @pytest.mark.asyncio
    async def test_not_connected_fails_so_gateway_falls_back_to_text(self, monkeypatch):
        adapter = _adapter(monkeypatch)
        adapter._client = None
        _register()
        result = await _send_card(adapter)
        assert result.success is False and adapter.sent == []


class TestSeededReactions:
    @pytest.mark.asyncio
    async def test_single_select_seeds_keycaps_and_skip(self, monkeypatch):
        adapter = _adapter(monkeypatch)
        _register()
        await _send_card(adapter)
        assert _seeded(adapter) == ["1️⃣", "2️⃣", "3️⃣", "⏭️"]

    @pytest.mark.asyncio
    async def test_multi_select_seeds_only_skip(self, monkeypatch):
        adapter = _adapter(monkeypatch)
        _register(multi=True)
        await _send_card(adapter)
        assert _seeded(adapter) == ["⏭️"]
        payload = adapter.sent[0]["org.hermes.clarify"]
        assert payload["multi_select"] is True
        assert "numbers separated by commas" in adapter.sent[0]["body"]

    @pytest.mark.asyncio
    async def test_open_ended_seeds_only_skip(self, monkeypatch):
        adapter = _adapter(monkeypatch)
        _register(choices=None)
        await _send_card(adapter, choices=None)
        assert _seeded(adapter) == ["⏭️"]


def _last_edit(adapter):
    edit = adapter.sent[-1]
    assert edit["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$card1"}
    return edit["m.new_content"]


class TestReactionAnswers:
    @pytest.mark.asyncio
    async def test_keycap_resolves_choice_and_settles_card(self, monkeypatch):
        adapter = _adapter(monkeypatch)
        entry = _register()
        await _send_card(adapter)

        assert await adapter._handle_clarify_reaction(ROOM, "$card1", "2️⃣", OWNER) is True

        assert entry.event.is_set() and entry.response == "Production"
        new = _last_edit(adapter)
        assert new["body"] == "❓ Deploy where?\n✅ Production"
        payload = new["org.hermes.clarify"]
        assert payload["state"] == "answered" and payload["answer"] == "Production"
        assert payload["clarify_id"] == "cid1" and payload["v"] == 1
        redacted = sorted(call.args[1] for call in adapter._schedule_reaction_redaction.call_args_list)
        assert redacted == sorted(["$r-1️⃣", "$r-2️⃣", "$r-3️⃣", "$r-⏭️"])
        assert "$card1" not in adapter._clarify_cards_by_event

    @pytest.mark.asyncio
    async def test_recommended_label_is_stripped_from_answer(self, monkeypatch):
        adapter = _adapter(monkeypatch)
        entry = _register()
        await _send_card(adapter)
        await adapter._handle_clarify_reaction(ROOM, "$card1", "1⃣", OWNER)  # no variation selector
        assert entry.response == "Staging (Recommended)"  # clarify_tool strips the label itself
        assert _last_edit(adapter)["org.hermes.clarify"]["answer"] == "Staging"

    @pytest.mark.asyncio
    async def test_skip_reaction_skips(self, monkeypatch):
        from tools.clarify_gateway import SKIPPED
        adapter = _adapter(monkeypatch)
        entry = _register(choices=None)
        await _send_card(adapter, choices=None)
        await adapter._handle_clarify_reaction(ROOM, "$card1", "⏭️", OWNER)
        assert entry.response == SKIPPED
        new = _last_edit(adapter)
        assert new["body"] == "❓ Deploy where?\n⏭️ Skipped"
        assert new["org.hermes.clarify"]["state"] == "skipped"
        assert new["org.hermes.clarify"]["answer"] is None

    @pytest.mark.asyncio
    async def test_reaction_from_non_requester_is_rejected(self, monkeypatch):
        adapter = _adapter(monkeypatch)
        entry = _register()
        await _send_card(adapter)
        assert await adapter._handle_clarify_reaction(ROOM, "$card1", "1️⃣", OTHER) is True
        assert not entry.event.is_set()
        adapter._send_invalid_reaction_feedback.assert_awaited_once()
        assert len(adapter.sent) == 1  # no edit

    @pytest.mark.asyncio
    async def test_unrelated_emoji_and_unseeded_keycap_are_ignored(self, monkeypatch):
        adapter = _adapter(monkeypatch)
        entry = _register(multi=True)
        await _send_card(adapter)
        assert await adapter._handle_clarify_reaction(ROOM, "$card1", "👍", OWNER) is True
        assert await adapter._handle_clarify_reaction(ROOM, "$card1", "1️⃣", OWNER) is True  # multi: no keycaps
        assert not entry.event.is_set()
        adapter._send_invalid_reaction_feedback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_on_reaction_dispatches_to_clarify(self, monkeypatch):
        adapter = _adapter(monkeypatch)
        entry = _register()
        await _send_card(adapter)
        event = types.SimpleNamespace(
            sender=OWNER, event_id="$react", room_id=ROOM,
            content={"m.relates_to": {"rel_type": "m.annotation", "event_id": "$card1", "key": "3️⃣"}})
        await adapter._on_reaction(event)
        assert entry.response == "Both"


def _reply(body, target="$card1", **extra):
    return {"msgtype": "m.text", "body": body,
            "m.relates_to": {"m.in_reply_to": {"event_id": target}, **extra}}


async def _deliver(adapter, content, sender=OWNER, event_id="$msg"):
    await adapter._handle_text_message(ROOM, sender, event_id, 0.0, content, content.get("m.relates_to", {}))


class TestReplyAnswers:
    @pytest.mark.asyncio
    async def test_reply_with_number_resolves_choice(self, monkeypatch):
        adapter = _adapter(monkeypatch)
        entry = _register()
        await _send_card(adapter)
        await _deliver(adapter, _reply(f"> <{BOT}> ❓ Deploy where?\n\n2"))
        assert entry.response == "Production"
        adapter.handle_message.assert_not_awaited()
        assert _last_edit(adapter)["org.hermes.clarify"]["answer"] == "Production"

    @pytest.mark.asyncio
    async def test_reply_with_free_text_and_label(self, monkeypatch):
        adapter = _adapter(monkeypatch)
        entry = _register()
        await _send_card(adapter)
        await _deliver(adapter, _reply("> <@hermes:example.org> q\n\nonly the canary hosts"))
        assert entry.response == "only the canary hosts"
        new = _last_edit(adapter)
        assert new["body"] == "❓ Deploy where?\n✅ only the canary hosts"

    @pytest.mark.asyncio
    async def test_reply_skip(self, monkeypatch):
        from tools.clarify_gateway import SKIPPED
        adapter = _adapter(monkeypatch)
        entry = _register()
        await _send_card(adapter)
        await _deliver(adapter, _reply("skip"))
        assert entry.response == SKIPPED
        assert _last_edit(adapter)["org.hermes.clarify"]["state"] == "skipped"

    @pytest.mark.asyncio
    async def test_reply_multi_select_list(self, monkeypatch):
        adapter = _adapter(monkeypatch)
        entry = _register(multi=True)
        await _send_card(adapter)
        await _deliver(adapter, _reply("1, 3"))
        assert json.loads(entry.response) == ["Staging (Recommended)", "Both"]
        assert _last_edit(adapter)["org.hermes.clarify"]["answer"] == "Staging, Both"

    @pytest.mark.asyncio
    async def test_reply_from_non_requester_routes_normally(self, monkeypatch):
        adapter = _adapter(monkeypatch)
        entry = _register()
        await _send_card(adapter)
        assert await adapter._answer_clarify_reply(ROOM, OTHER, "$m", "2", _reply("2")["m.relates_to"]) is False
        assert not entry.event.is_set()

    @pytest.mark.asyncio
    async def test_thread_fallback_reply_and_plain_message_are_not_card_replies(self, monkeypatch):
        adapter = _adapter(monkeypatch)
        entry = _register()
        await _send_card(adapter)
        relates = _reply("2", rel_type="m.thread", event_id="$root", is_falling_back=True)["m.relates_to"]
        assert await adapter._answer_clarify_reply(ROOM, OWNER, "$m", "2", relates) is False
        assert await adapter._answer_clarify_reply(ROOM, OWNER, "$m", "2", {}) is False
        assert not entry.event.is_set()

    @pytest.mark.asyncio
    async def test_reply_with_command_is_not_an_answer(self, monkeypatch):
        adapter = _adapter(monkeypatch)
        entry = _register()
        await _send_card(adapter)
        assert await adapter._answer_clarify_reply(ROOM, OWNER, "$m", "!stop", _reply("!stop")["m.relates_to"]) is False
        assert not entry.event.is_set()


class TestRetire:
    @pytest.mark.asyncio
    async def test_timeout_settles_as_expired(self, monkeypatch):
        from agent.i18n import t
        adapter = _adapter(monkeypatch)
        entry = _register()
        await _send_card(adapter)
        entry.event.set()  # wait_for_response timed out: no response
        await adapter.retire_clarify_card("cid1", t("gateway.clarify.expired"))
        new = _last_edit(adapter)
        assert new["body"] == f"❓ Deploy where?\n{t('platform.matrix.clarify.expired')}"
        assert new["org.hermes.clarify"]["state"] == "expired"
        assert new["org.hermes.clarify"]["answer"] is None
        assert adapter._schedule_reaction_redaction.call_count == 4

    @pytest.mark.asyncio
    async def test_cancellation_settles_as_cancelled(self, monkeypatch):
        from agent.i18n import t
        from tools import clarify_gateway as cg
        adapter = _adapter(monkeypatch)
        _register()
        await _send_card(adapter)
        cg.resolve_gateway_clarify("cid1", cg.CANCELLED)
        await adapter.retire_clarify_card("cid1", t("gateway.clarify.cancelled"))
        new = _last_edit(adapter)
        assert new["body"] == f"❓ Deploy where?\n{t('platform.matrix.clarify.cancelled')}"
        assert new["org.hermes.clarify"]["state"] == "cancelled"

    @pytest.mark.asyncio
    async def test_typed_plain_answer_settles_as_answered(self, monkeypatch):
        """The gateway text intercept resolves a plain message, then retires the card."""
        from agent.i18n import t
        from tools import clarify_gateway as cg
        adapter = _adapter(monkeypatch)
        _register()
        await _send_card(adapter)
        assert cg.attempt_text_response_for_session("sess", "3") == cg.TEXT_RESOLVED
        await adapter.retire_clarify_card("cid1", t("gateway.clarify.answered", response="Both"))
        assert _last_edit(adapter)["org.hermes.clarify"]["answer"] == "Both"

    @pytest.mark.asyncio
    async def test_retire_is_idempotent_and_ignores_unknown_ids(self, monkeypatch):
        adapter = _adapter(monkeypatch)
        entry = _register()
        await _send_card(adapter)
        await adapter._handle_clarify_reaction(ROOM, "$card1", "1️⃣", OWNER)
        count = len(adapter.sent)
        await adapter.retire_clarify_card("cid1", "whatever")
        await adapter.retire_clarify_card("nope", "whatever")
        assert len(adapter.sent) == count and entry.response

    @pytest.mark.asyncio
    async def test_answer_during_send_settles_on_arrival(self, monkeypatch):
        from tools import clarify_gateway as cg
        adapter = _adapter(monkeypatch)
        _register()
        real_send = adapter._send_room_message

        async def _answer_then_send(chat_id, content):
            cg.resolve_gateway_clarify("cid1", "Both")
            return await real_send(chat_id, content)
        adapter._send_room_message = _answer_then_send
        await _send_card(adapter)
        assert _last_edit(adapter)["org.hermes.clarify"]["answer"] == "Both"


class TestExtraContentPlumbing:
    @pytest.mark.asyncio
    async def test_extra_content_never_overrides_standard_keys(self, monkeypatch):
        adapter = _adapter(monkeypatch)
        await adapter.send(ROOM, "hello", metadata={"matrix_extra_content": {"body": "x", "org.x": {"a": 1}}})
        assert adapter.sent[0]["body"] == "hello" and adapter.sent[0]["org.x"] == {"a": 1}

    @pytest.mark.asyncio
    async def test_plain_send_has_no_clarify_key(self, monkeypatch):
        adapter = _adapter(monkeypatch)
        await adapter.send(ROOM, "hello")
        assert "org.hermes.clarify" not in adapter.sent[0]

    @pytest.mark.asyncio
    async def test_extra_content_ignores_spec_keys(self, monkeypatch):
        """``m.*`` keys (relations, replacements, mentions) never ride in through extra content."""
        adapter = _adapter(monkeypatch)
        await adapter.send(ROOM, "hello", metadata={"matrix_extra_content": {
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$victim"},
            "m.new_content": {"body": "x"}, "m.mentions": {"room": True}, "org.x": 1}})
        sent = adapter.sent[0]
        assert "m.new_content" not in sent and sent.get("m.relates_to") is None
        assert sent.get("m.mentions") != {"room": True} and sent["org.x"] == 1


class TestSettleRobustness:
    """Approval-card parity: the card stays answerable-looking only while it is registered."""

    @pytest.mark.asyncio
    async def test_failed_terminal_edit_keeps_card_and_a_later_retire_retries(self, monkeypatch):
        from tools import clarify_gateway as cg
        adapter = _adapter(monkeypatch)
        _register()
        await _send_card(adapter)
        cg.resolve_gateway_clarify("cid1", "Production")
        monkeypatch.setattr("asyncio.sleep", AsyncMock())
        failing = AsyncMock(return_value=types.SimpleNamespace(success=False, error="boom", message_id=None))
        real_edit = adapter.edit_message
        adapter.edit_message = failing
        await adapter.retire_clarify_card("cid1", "")
        assert failing.await_count == 3
        assert "$card1" in adapter._clarify_cards_by_event and "cid1" in adapter._clarify_cards_by_id
        assert adapter._schedule_reaction_redaction.call_count == 0  # controls stay until the edit lands
        adapter.edit_message = real_edit
        await adapter.retire_clarify_card("cid1", "")
        assert _last_edit(adapter)["org.hermes.clarify"]["answer"] == "Production"
        assert "$card1" not in adapter._clarify_cards_by_event
        assert adapter._schedule_reaction_redaction.call_count == 4

    @pytest.mark.asyncio
    async def test_raising_edit_is_logged_and_retryable(self, monkeypatch):
        from tools import clarify_gateway as cg
        adapter = _adapter(monkeypatch)
        _register()
        await _send_card(adapter)
        cg.resolve_gateway_clarify("cid1", cg.SKIPPED)
        monkeypatch.setattr("asyncio.sleep", AsyncMock())
        adapter.edit_message = AsyncMock(side_effect=RuntimeError("network"))
        assert await adapter._settle_clarify_card(adapter._clarify_cards_by_id["cid1"]) is False
        assert adapter._clarify_cards_by_id["cid1"].state == "pending"
        assert adapter._clarify_cards_by_id["cid1"].terminal == ("skipped", None)

    @pytest.mark.asyncio
    async def test_reaction_on_card_with_failed_edit_retries_the_edit(self, monkeypatch):
        from tools import clarify_gateway as cg
        adapter = _adapter(monkeypatch)
        _register()
        await _send_card(adapter)
        cg.resolve_gateway_clarify("cid1", "Both")
        monkeypatch.setattr("asyncio.sleep", AsyncMock())
        real_edit = adapter.edit_message
        adapter.edit_message = AsyncMock(return_value=types.SimpleNamespace(success=False, error="x", message_id=None))
        await adapter.retire_clarify_card("cid1", "")
        adapter.edit_message = real_edit
        assert await adapter._handle_clarify_reaction(ROOM, "$card1", "1️⃣", OWNER) is True
        assert _last_edit(adapter)["org.hermes.clarify"]["answer"] == "Both"  # the decided outcome, not 1️⃣

    @pytest.mark.asyncio
    async def test_reply_to_card_with_failed_edit_retries_and_routes(self, monkeypatch):
        from tools import clarify_gateway as cg
        adapter = _adapter(monkeypatch)
        _register()
        await _send_card(adapter)
        cg.resolve_gateway_clarify("cid1", "Both")
        monkeypatch.setattr("asyncio.sleep", AsyncMock())
        real_edit = adapter.edit_message
        adapter.edit_message = AsyncMock(return_value=types.SimpleNamespace(success=False, error="x", message_id=None))
        await adapter.retire_clarify_card("cid1", "")
        adapter.edit_message = real_edit
        handled = await adapter._answer_clarify_reply(
            ROOM, OWNER, "$reply", "2", {"m.in_reply_to": {"event_id": "$card1"}})
        assert handled is False  # not an answer any more: routes as an ordinary message
        assert _last_edit(adapter)["org.hermes.clarify"]["answer"] == "Both"

    @pytest.mark.asyncio
    async def test_seeded_reaction_on_settled_card_gets_expired_notice(self, monkeypatch):
        from agent.i18n import t
        adapter = _adapter(monkeypatch)
        _register()
        await _send_card(adapter)
        await adapter._handle_clarify_reaction(ROOM, "$card1", "2️⃣", OWNER)
        adapter._send_invalid_reaction_feedback.reset_mock()
        assert await adapter._handle_clarify_reaction(ROOM, "$card1", "1⃣", OWNER) is True  # no VS16
        adapter._send_invalid_reaction_feedback.assert_awaited_once_with(ROOM, "$card1", t("gateway.clarify.expired"))

    @pytest.mark.asyncio
    async def test_other_emoji_on_settled_card_stays_quiet(self, monkeypatch):
        adapter = _adapter(monkeypatch)
        _register()
        await _send_card(adapter)
        await adapter._handle_clarify_reaction(ROOM, "$card1", "2️⃣", OWNER)
        adapter._send_invalid_reaction_feedback.reset_mock()
        assert await adapter._handle_clarify_reaction(ROOM, "$card1", "👍", OWNER) is False
        adapter._send_invalid_reaction_feedback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_long_free_text_answer_is_capped_and_settles(self, monkeypatch):
        from plugins.platforms.matrix import clarify_cards as cc
        from tools import clarify_gateway as cg
        adapter = _adapter(monkeypatch)
        _register(choices=None)
        await _send_card(adapter, choices=None)
        long_answer = "x" * (adapter.max_message_length + 10)
        cg.resolve_gateway_clarify("cid1", long_answer)
        await adapter.retire_clarify_card("cid1", "")
        new = _last_edit(adapter)
        payload = new["org.hermes.clarify"]
        assert len(payload["answer"]) == cc.ANSWER_DISPLAY_MAX and payload["answer"].endswith("…")
        assert payload["answer_truncated"] is True
        assert len(new["body"]) < adapter.max_message_length
        assert "$card1" not in adapter._clarify_cards_by_event

    @pytest.mark.asyncio
    async def test_cancelled_outcome_comes_from_the_entry_not_the_notice(self, monkeypatch):
        from tools import clarify_gateway as cg
        adapter = _adapter(monkeypatch)
        _register()
        await _send_card(adapter)
        assert cg.clear_session("sess") == 1
        await adapter.retire_clarify_card("cid1", "any notice text")
        assert _last_edit(adapter)["org.hermes.clarify"]["state"] == "cancelled"


class TestClarifyGatewaySeams:
    def test_get_entry_returns_answered_entries(self):
        from tools import clarify_gateway as cg
        entry = _register()
        assert cg.get_entry("cid1") is entry and cg.get_pending("cid1") is entry
        cg.resolve_gateway_clarify("cid1", "Both")
        assert cg.get_entry("cid1") is entry and cg.get_pending("cid1") is None
        assert cg.get_entry("nope") is None

    def test_coerce_text_response_matches_typed_answers(self):
        from tools import clarify_gateway as cg
        entry = _register()
        assert cg.coerce_text_response(entry, "2") == "Production"
        assert cg.coerce_text_response(entry, "skip") == cg.SKIPPED
        assert cg.coerce_text_response(entry, "9") is None
