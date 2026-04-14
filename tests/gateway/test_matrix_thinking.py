"""Tests for Matrix thinking/acting panes."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig


def _make_adapter(thinking_enabled=True):
    from gateway.platforms.matrix import MatrixAdapter

    config = PlatformConfig(
        enabled=True,
        token="syt_test123",
        extra={
            "homeserver": "https://matrix.example.org",
            "user_id": "@bot:example.org",
            "thinking_fields_enabled": thinking_enabled,
        },
    )
    adapter = MatrixAdapter(config)
    adapter._client = AsyncMock()
    return adapter


class TestThinkingManagerTitles:
    def test_field_titles_match_branch_c_requirement_with_friendly_emoji(self):
        from gateway.platforms.matrix_thinking import ThinkingManager

        assert ThinkingManager._field_title("thinking", "gpt-5.4 via openai-codex") == "🤔 Agent Thinking: Hermes via gpt-5.4 via openai-codex"
        assert ThinkingManager._field_title("tools", "gpt-5.4 via openai-codex") == "⚡ Agent Acting:"

    def test_plaintext_summary_includes_required_heading(self):
        from gateway.platforms.matrix_thinking import ThinkingManager

        text = ThinkingManager._plaintext_summary(
            "Agent Thinking: Hermes via gpt-5.4 via openai-codex",
            "Processing request...",
        )
        assert "Agent Thinking: Hermes via gpt-5.4 via openai-codex" in text
        assert "Processing request..." in text


class TestThinkingManagerLifecycle:
    @pytest.mark.asyncio
    async def test_start_thinking_uses_branch_c_heading(self):
        from gateway.platforms.matrix_thinking import ThinkingManager

        adapter = _make_adapter()
        send_message_event = AsyncMock(return_value="$evt_123")
        adapter._client.send_message_event = send_message_event

        mgr = ThinkingManager(adapter)
        event_id = await mgr.start(
            "!room:example.org",
            "task-1",
            "Processing request...",
            field_kind="thinking",
            model_label="gpt-5.4 via openai-codex",
            initial_content_md="first delta",
        )

        assert event_id == "$evt_123"
        content = send_message_event.call_args.args[2]
        assert "Agent Thinking: Hermes via gpt-5.4 via openai-codex" in content["formatted_body"]
        assert "first delta" in content["formatted_body"]

    @pytest.mark.asyncio
    async def test_start_tool_activity_uses_agent_acting_heading(self):
        from gateway.platforms.matrix_thinking import ThinkingManager

        adapter = _make_adapter()
        send_message_event = AsyncMock(return_value="$evt_456")
        adapter._client.send_message_event = send_message_event

        mgr = ThinkingManager(adapter)
        event_id = await mgr.start(
            "!room:example.org",
            "task-2",
            "Tool activity",
            field_kind="tools",
            model_label="gpt-5.4 via openai-codex",
            initial_content_md='💻 terminal: "pwd"',
        )

        assert event_id == "$evt_456"
        content = send_message_event.call_args.args[2]
        assert "⚡ Agent Acting:" in content["formatted_body"]
        assert '💻 terminal: &quot;pwd&quot;' in content["formatted_body"]
        assert "<pre" in content["formatted_body"]

    @pytest.mark.asyncio
    async def test_thinking_delta_updates_append_as_flowing_text_not_single_word_lines(self):
        from gateway.platforms.matrix_thinking import ThinkingManager

        adapter = _make_adapter()
        adapter._client.send_message_event = AsyncMock(return_value="$evt_flow")

        mgr = ThinkingManager(adapter)
        await mgr.start(
            "!room:example.org",
            "task-flow",
            "Reasoning...",
            field_kind="thinking",
            model_label="gpt-5.4 via openai-codex",
            initial_content_md="I am",
        )
        await mgr.update(
            "task-flow",
            "Reasoning...",
            " considering the issue",
            field_kind="thinking",
            append_line=False,
        )
        await mgr.finalize("task-flow", "done", field_kind="thinking")

        final_payload = adapter._client.send_message_event.await_args_list[-1].args[2]
        formatted = final_payload["formatted_body"]
        assert "I am considering the issue" in formatted
        assert "I am<br/> considering the issue" not in formatted

    @pytest.mark.asyncio
    async def test_tools_finalize_stays_expanded_by_default_while_thinking_collapses(self):
        from gateway.platforms.matrix_thinking import ThinkingManager

        adapter = _make_adapter()
        adapter._client.send_message_event = AsyncMock(return_value="$evt_expand")

        mgr = ThinkingManager(adapter)
        await mgr.start("!room:example.org", "task-think", "Thinking...", field_kind="thinking")
        await mgr.finalize("task-think", "done", field_kind="thinking")
        thinking_payload = adapter._client.send_message_event.await_args_list[-1].args[2]
        assert "<details><summary>" in thinking_payload["formatted_body"]

        await mgr.start("!room:example.org", "task-tools", "Tool activity", field_kind="tools")
        await mgr.finalize("task-tools", "done", field_kind="tools")
        tools_payload = adapter._client.send_message_event.await_args_list[-1].args[2]
        assert "<details open><summary>" in tools_payload["formatted_body"]

    def test_elapsed_str_still_formats_duration(self):
        from gateway.platforms.matrix_thinking import ThinkingManager

        now = time.time()
        assert ThinkingManager._elapsed_str(now - 45) == "45s"
        assert ThinkingManager._elapsed_str(now - 125) == "2m5s"
