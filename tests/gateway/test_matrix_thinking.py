"""Tests for Matrix thinking fields (collapsible <details> blocks).

Tests cover:
  - ThinkingManager lifecycle (start, update, finalize, abort)
  - HTML generation and sanitization
  - Rate limiting
  - Edit content structure (m.replace / m.new_content)
  - Config toggle (MATRIX_THINKING_FIELDS_ENABLED)
  - MatrixAdapter integration
  - Stale session cleanup
  - Error handling / edge cases
"""

import asyncio
import html
import time

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.config import Platform, PlatformConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter(thinking_enabled=True):
    """Create a MatrixAdapter with mock nio client for thinking tests."""
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

    # Mock the nio client
    fake_nio = MagicMock()
    RoomSendResponse = type("RoomSendResponse", (), {"event_id": "$evt_123"})
    fake_nio.RoomSendResponse = RoomSendResponse

    mock_client = AsyncMock()
    mock_resp = RoomSendResponse()
    mock_resp.event_id = "$evt_123"
    mock_client.room_send = AsyncMock(return_value=mock_resp)

    adapter._client = mock_client
    return adapter, mock_client, RoomSendResponse


def _make_manager(adapter=None):
    """Create a ThinkingManager with mock adapter."""
    from gateway.platforms.matrix_thinking import ThinkingManager

    if adapter is None:
        adapter, _, _ = _make_adapter()
    return ThinkingManager(adapter)


# ---------------------------------------------------------------------------
# ThinkingSession dataclass
# ---------------------------------------------------------------------------

class TestThinkingSession:
    def test_session_defaults(self):
        from gateway.platforms.matrix_thinking import ThinkingSession

        session = ThinkingSession(
            room_id="!room:example.org",
            event_id="$evt_1",
            task_id="task_1",
            started_at=time.time(),
            last_update=time.time(),
        )
        assert session.step_count == 0
        assert session.content_lines == []
        assert session.finalized is False

    def test_session_mutable_content(self):
        from gateway.platforms.matrix_thinking import ThinkingSession

        session = ThinkingSession(
            room_id="!room:example.org",
            event_id="$evt_1",
            task_id="task_1",
            started_at=time.time(),
            last_update=time.time(),
        )
        session.content_lines.append("Line 1")
        session.step_count += 1
        assert session.step_count == 1
        assert len(session.content_lines) == 1


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

class TestThinkingHtmlGeneration:
    def test_build_html_basic(self):
        mgr = _make_manager()
        result = mgr._build_html(
            summary="Test summary",
            step=1,
            ts=time.time(),
            content_html="Hello",
            open_tag=True,
        )
        assert "<details open>" in result
        assert "<summary>" in result
        assert "Test summary" in result
        assert "Hello" in result
        assert "</details>" in result

    def test_build_html_collapsed(self):
        mgr = _make_manager()
        result = mgr._build_html(
            summary="Done",
            step=3,
            ts=time.time(),
            content_html="Content",
            open_tag=False,
        )
        assert "<details>" in result
        assert "<details open>" not in result

    def test_build_html_escapes_summary(self):
        mgr = _make_manager()
        result = mgr._build_html(
            summary='<script>alert("xss")</script>',
            step=1,
            ts=time.time(),
            content_html="safe",
        )
        assert "<script>" not in result
        assert html.escape('<script>alert("xss")</script>') in result

    def test_build_html_with_elapsed(self):
        mgr = _make_manager()
        result = mgr._build_html(
            summary="Working",
            step=5,
            ts=time.time(),
            content_html="data",
            elapsed="2m30s",
        )
        assert "2m30s" in result

    def test_build_html_step_zero_shows_starting(self):
        mgr = _make_manager()
        result = mgr._build_html(
            summary="Init",
            step=0,
            ts=time.time(),
            content_html="",
        )
        assert "Starting" in result

    def test_lines_to_html_escapes(self):
        mgr = _make_manager()
        lines = ["Normal line", '<img src="x" onerror="alert(1)">', "Another & line"]
        result = mgr._lines_to_html(lines)
        # HTML tags are escaped — no raw <img> element
        assert "<img" not in result
        assert "&lt;img" in result
        # Ampersand is escaped
        assert "&amp;" in result

    def test_lines_to_html_empty(self):
        mgr = _make_manager()
        assert mgr._lines_to_html([]) == ""

    def test_elapsed_str_seconds(self):
        from gateway.platforms.matrix_thinking import ThinkingManager
        # Monkey-patch time for predictable output
        now = time.time()
        result = ThinkingManager._elapsed_str(now - 45)
        assert result == "45s"

    def test_elapsed_str_minutes(self):
        from gateway.platforms.matrix_thinking import ThinkingManager
        now = time.time()
        result = ThinkingManager._elapsed_str(now - 125)
        assert result == "2m5s"


# ---------------------------------------------------------------------------
# Message content builders
# ---------------------------------------------------------------------------

class TestMessageContentBuilders:
    def test_msg_content_structure(self):
        from gateway.platforms.matrix_thinking import ThinkingManager

        content = ThinkingManager._msg_content("<b>hi</b>", "hi")
        assert content["msgtype"] == "m.text"
        assert content["body"] == "hi"
        assert content["format"] == "org.matrix.custom.html"
        assert content["formatted_body"] == "<b>hi</b>"

    def test_edit_content_structure(self):
        from gateway.platforms.matrix_thinking import ThinkingManager

        content = ThinkingManager._edit_content("$orig_evt", "<b>updated</b>", "updated")

        # Top-level has the "* " prefix per Matrix spec
        assert content["body"].startswith("* ")
        assert content["formatted_body"].startswith("* ")

        # m.new_content has the clean content
        new = content["m.new_content"]
        assert new["body"] == "updated"
        assert new["formatted_body"] == "<b>updated</b>"
        assert new["format"] == "org.matrix.custom.html"

        # m.relates_to is correct
        rel = content["m.relates_to"]
        assert rel["rel_type"] == "m.replace"
        assert rel["event_id"] == "$orig_evt"


# ---------------------------------------------------------------------------
# ThinkingManager lifecycle
# ---------------------------------------------------------------------------

class TestThinkingManagerLifecycle:
    @pytest.mark.asyncio
    async def test_start_creates_session(self):
        adapter, mock_client, _ = _make_adapter()

        # Create a proper RoomSendResponse subclass so isinstance() works
        import sys
        RoomSendResponse = type("RoomSendResponse", (), {})
        fake_nio = MagicMock()
        fake_nio.RoomSendResponse = RoomSendResponse
        mock_resp = RoomSendResponse()
        mock_resp.event_id = "$think_evt_1"
        mock_client.room_send = AsyncMock(return_value=mock_resp)

        with patch.dict(sys.modules, {"nio": fake_nio}):
            event_id = await adapter.start_thinking(
                "!room:example.org", "task_1", "Starting research"
            )

        assert event_id == "$think_evt_1"
        mock_client.room_send.assert_called_once()

        # Verify the content sent
        call_args = mock_client.room_send.call_args
        assert call_args[0][0] == "!room:example.org"
        assert call_args[0][1] == "m.room.message"
        content = call_args[0][2]
        assert content["format"] == "org.matrix.custom.html"
        assert "<details open>" in content["formatted_body"]

    @pytest.mark.asyncio
    async def test_start_returns_none_when_disabled(self):
        adapter, _, _ = _make_adapter(thinking_enabled=False)
        result = await adapter.start_thinking("!room:example.org", "task_1")
        assert result is None

    @pytest.mark.asyncio
    async def test_start_returns_none_without_client(self):
        adapter, _, _ = _make_adapter()
        adapter._client = None
        result = await adapter.start_thinking("!room:example.org", "task_1")
        assert result is None

    @pytest.mark.asyncio
    async def test_update_rate_limited(self):
        adapter, mock_client, _ = _make_adapter()

        import sys
        fake_nio = MagicMock()
        fake_nio.RoomSendResponse = type("RoomSendResponse", (), {})
        mock_resp = fake_nio.RoomSendResponse()
        mock_resp.event_id = "$think_evt_1"
        mock_client.room_send = AsyncMock(return_value=mock_resp)

        with patch.dict(sys.modules, {"nio": fake_nio}):
            mgr = adapter._get_thinking_manager()

            # Manually create a session with recent last_update
            from gateway.platforms.matrix_thinking import ThinkingSession
            mgr._sessions["task_1"] = ThinkingSession(
                room_id="!room:example.org",
                event_id="$evt_1",
                task_id="task_1",
                started_at=time.time(),
                last_update=time.time(),  # just now
            )

            # Should be rate-limited (no room_send call)
            mock_client.room_send.reset_mock()
            await mgr.update("task_1", "Step 1", "Some reasoning")
            mock_client.room_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_after_rate_limit_expires(self):
        adapter, mock_client, _ = _make_adapter()

        import sys
        fake_nio = MagicMock()
        fake_nio.RoomSendResponse = type("RoomSendResponse", (), {})
        mock_resp = fake_nio.RoomSendResponse()
        mock_resp.event_id = "$evt_update"
        mock_client.room_send = AsyncMock(return_value=mock_resp)

        with patch.dict(sys.modules, {"nio": fake_nio}):
            mgr = adapter._get_thinking_manager()

            from gateway.platforms.matrix_thinking import ThinkingSession
            mgr._sessions["task_1"] = ThinkingSession(
                room_id="!room:example.org",
                event_id="$evt_1",
                task_id="task_1",
                started_at=time.time() - 10,
                last_update=time.time() - 5,  # 5 seconds ago, past rate limit
            )

            await mgr.update("task_1", "Step 1", "Reasoning trace")
            mock_client.room_send.assert_called_once()

            # Verify it's an edit (m.replace)
            content = mock_client.room_send.call_args[0][2]
            assert "m.relates_to" in content
            assert content["m.relates_to"]["rel_type"] == "m.replace"
            assert content["m.relates_to"]["event_id"] == "$evt_1"

    @pytest.mark.asyncio
    async def test_finalize_removes_session(self):
        adapter, mock_client, _ = _make_adapter()

        import sys
        fake_nio = MagicMock()
        fake_nio.RoomSendResponse = type("RoomSendResponse", (), {})
        mock_resp = fake_nio.RoomSendResponse()
        mock_resp.event_id = "$evt_final"
        mock_client.room_send = AsyncMock(return_value=mock_resp)

        with patch.dict(sys.modules, {"nio": fake_nio}):
            mgr = adapter._get_thinking_manager()

            from gateway.platforms.matrix_thinking import ThinkingSession
            mgr._sessions["task_1"] = ThinkingSession(
                room_id="!room:example.org",
                event_id="$evt_1",
                task_id="task_1",
                started_at=time.time() - 30,
                last_update=time.time() - 5,
            )

            await mgr.finalize("task_1", "All done")
            assert "task_1" not in mgr._sessions

            # Verify final edit has collapsed details
            content = mock_client.room_send.call_args[0][2]
            new_html = content["m.new_content"]["formatted_body"]
            assert "<details>" in new_html  # no "open" attr = collapsed
            assert "✅" in new_html

    @pytest.mark.asyncio
    async def test_finalize_nonexistent_session_is_noop(self):
        adapter, mock_client, _ = _make_adapter()
        mgr = adapter._get_thinking_manager()

        await mgr.finalize("nonexistent_task", "Done")
        mock_client.room_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_abort_marks_warning(self):
        adapter, mock_client, _ = _make_adapter()

        import sys
        fake_nio = MagicMock()
        fake_nio.RoomSendResponse = type("RoomSendResponse", (), {})
        mock_resp = fake_nio.RoomSendResponse()
        mock_resp.event_id = "$evt_abort"
        mock_client.room_send = AsyncMock(return_value=mock_resp)

        with patch.dict(sys.modules, {"nio": fake_nio}):
            mgr = adapter._get_thinking_manager()

            from gateway.platforms.matrix_thinking import ThinkingSession
            mgr._sessions["task_err"] = ThinkingSession(
                room_id="!room:example.org",
                event_id="$evt_1",
                task_id="task_err",
                started_at=time.time() - 10,
                last_update=time.time() - 5,
            )

            await mgr.abort("task_err", "Rate limited")
            assert "task_err" not in mgr._sessions

            content = mock_client.room_send.call_args[0][2]
            new_html = content["m.new_content"]["formatted_body"]
            assert "⚠️" in new_html
            assert "Rate limited" in new_html

    @pytest.mark.asyncio
    async def test_update_finalized_session_is_noop(self):
        adapter, mock_client, _ = _make_adapter()
        mgr = adapter._get_thinking_manager()

        from gateway.platforms.matrix_thinking import ThinkingSession
        mgr._sessions["task_done"] = ThinkingSession(
            room_id="!room:example.org",
            event_id="$evt_1",
            task_id="task_done",
            started_at=time.time() - 10,
            last_update=time.time() - 10,
            finalized=True,
        )

        await mgr.update("task_done", "Should not send", "data")
        mock_client.room_send.assert_not_called()


# ---------------------------------------------------------------------------
# Cleanup & edge cases
# ---------------------------------------------------------------------------

class TestThinkingManagerCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_stale_sessions(self):
        mgr = _make_manager()

        from gateway.platforms.matrix_thinking import ThinkingSession
        mgr._sessions["stale_task"] = ThinkingSession(
            room_id="!room:example.org",
            event_id="$evt_1",
            task_id="stale_task",
            started_at=time.time() - 3600,  # 1 hour ago
            last_update=time.time() - 3600,
        )
        mgr._sessions["fresh_task"] = ThinkingSession(
            room_id="!room:example.org",
            event_id="$evt_2",
            task_id="fresh_task",
            started_at=time.time() - 10,
            last_update=time.time() - 5,
        )

        await mgr.cleanup_stale(max_age=1800)  # 30 min
        assert "stale_task" not in mgr._sessions
        assert "fresh_task" in mgr._sessions

    def test_has_session(self):
        mgr = _make_manager()

        from gateway.platforms.matrix_thinking import ThinkingSession
        mgr._sessions["task_1"] = ThinkingSession(
            room_id="!room:example.org",
            event_id="$evt_1",
            task_id="task_1",
            started_at=time.time(),
            last_update=time.time(),
        )

        assert mgr.has_session("task_1") is True
        assert mgr.has_session("task_nonexistent") is False

    @pytest.mark.asyncio
    async def test_abort_all_finalizes_active_sessions(self):
        adapter, mock_client, _ = _make_adapter()
        mgr = adapter._get_thinking_manager()

        from gateway.platforms.matrix_thinking import ThinkingSession
        mgr._sessions["task_1:thinking"] = ThinkingSession(
            room_id="!room:example.org",
            event_id="$evt_1",
            task_id="task_1",
            started_at=time.time() - 5,
            last_update=time.time() - 5,
            field_kind="thinking",
        )
        mgr._sessions["task_1:tools"] = ThinkingSession(
            room_id="!room:example.org",
            event_id="$evt_2",
            task_id="task_1",
            started_at=time.time() - 5,
            last_update=time.time() - 5,
            field_kind="tools",
        )

        await mgr.abort_all("Gateway restarting")

        assert mgr._sessions == {}
        assert mock_client.room_send.call_count == 2


# ---------------------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------------------

class TestThinkingConfig:
    def test_thinking_enabled_by_default(self, monkeypatch):
        monkeypatch.setenv("MATRIX_ACCESS_TOKEN", "syt_abc123")
        monkeypatch.setenv("MATRIX_HOMESERVER", "https://matrix.example.org")
        monkeypatch.delenv("MATRIX_THINKING_FIELDS_ENABLED", raising=False)

        from gateway.config import GatewayConfig, _apply_env_overrides
        config = GatewayConfig()
        _apply_env_overrides(config)

        mc = config.platforms[Platform.MATRIX]
        assert mc.extra.get("thinking_fields_enabled") is True

    def test_thinking_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("MATRIX_ACCESS_TOKEN", "syt_abc123")
        monkeypatch.setenv("MATRIX_HOMESERVER", "https://matrix.example.org")
        monkeypatch.setenv("MATRIX_THINKING_FIELDS_ENABLED", "false")

        from gateway.config import GatewayConfig, _apply_env_overrides
        config = GatewayConfig()
        _apply_env_overrides(config)

        mc = config.platforms[Platform.MATRIX]
        assert mc.extra.get("thinking_fields_enabled") is False

    def test_adapter_respects_config_disabled(self):
        adapter, _, _ = _make_adapter(thinking_enabled=False)
        assert adapter._thinking_enabled is False

    def test_adapter_thinking_enabled_default(self):
        adapter, _, _ = _make_adapter(thinking_enabled=True)
        assert adapter._thinking_enabled is True


# ---------------------------------------------------------------------------
# MatrixAdapter integration
# ---------------------------------------------------------------------------

class TestMatrixAdapterThinkingIntegration:
    def test_lazy_thinking_manager_init(self):
        adapter, _, _ = _make_adapter()
        assert adapter._thinking_manager is None

        mgr = adapter._get_thinking_manager()
        assert mgr is not None
        assert adapter._thinking_manager is mgr

        # Second call returns same instance
        assert adapter._get_thinking_manager() is mgr

    def test_thinking_manager_not_created_without_client(self):
        adapter, _, _ = _make_adapter()
        adapter._client = None
        assert adapter._get_thinking_manager() is None

    @pytest.mark.asyncio
    async def test_update_noop_when_disabled(self):
        adapter, mock_client, _ = _make_adapter(thinking_enabled=False)
        await adapter.update_thinking("task_1", "Step 1", "data")
        mock_client.room_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_finalize_noop_when_disabled(self):
        adapter, mock_client, _ = _make_adapter(thinking_enabled=False)
        await adapter.finalize_thinking("task_1", "Done")
        mock_client.room_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_abort_noop_without_manager(self):
        adapter, mock_client, _ = _make_adapter()
        # Manager not initialized yet
        await adapter.abort_thinking("task_1", "Error")
        mock_client.room_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_disconnect_aborts_active_introspection_fields(self):
        adapter, mock_client, _ = _make_adapter()
        mgr = adapter._get_thinking_manager()

        from gateway.platforms.matrix_thinking import ThinkingSession
        mgr._sessions["task_disc:thinking"] = ThinkingSession(
            room_id="!room:example.org",
            event_id="$evt_1",
            task_id="task_disc",
            started_at=time.time() - 5,
            last_update=time.time() - 5,
            field_kind="thinking",
        )

        await adapter.disconnect()

        assert mock_client.room_send.called


# ---------------------------------------------------------------------------
# Content accumulation
# ---------------------------------------------------------------------------

class TestContentAccumulation:
    @pytest.mark.asyncio
    async def test_content_accumulates_across_updates(self):
        adapter, mock_client, _ = _make_adapter()

        import sys
        fake_nio = MagicMock()
        fake_nio.RoomSendResponse = type("RoomSendResponse", (), {})
        mock_resp = fake_nio.RoomSendResponse()
        mock_resp.event_id = "$evt_update"
        mock_client.room_send = AsyncMock(return_value=mock_resp)

        with patch.dict(sys.modules, {"nio": fake_nio}):
            mgr = adapter._get_thinking_manager()

            from gateway.platforms.matrix_thinking import ThinkingSession
            mgr._sessions["task_acc"] = ThinkingSession(
                room_id="!room:example.org",
                event_id="$evt_1",
                task_id="task_acc",
                started_at=time.time() - 20,
                last_update=time.time() - 10,  # past rate limit
            )

            await mgr.update("task_acc", "Step 1", "First reasoning line")

            session = mgr._sessions["task_acc"]
            assert len(session.content_lines) == 1
            assert session.content_lines[0] == "First reasoning line"
            assert session.step_count == 1

    @pytest.mark.asyncio
    async def test_rapid_updates_are_buffered_and_flushed_losslessly(self):
        adapter, mock_client, _ = _make_adapter()

        import sys
        fake_nio = MagicMock()
        fake_nio.RoomSendResponse = type("RoomSendResponse", (), {})
        mock_resp = fake_nio.RoomSendResponse()
        mock_resp.event_id = "$evt_update"
        mock_client.room_send = AsyncMock(return_value=mock_resp)

        with patch.dict(sys.modules, {"nio": fake_nio}):
            import gateway.platforms.matrix_thinking as mt
            mgr = adapter._get_thinking_manager()

            with patch.object(mt, "_MIN_EDIT_INTERVAL", 0.05):
                await mgr.start("!room:example.org", "task_buffer", "Starting")
                mock_client.room_send.reset_mock()

                await mgr.update("task_buffer", "Reasoning…", "first line")
                await mgr.update("task_buffer", "Reasoning…", "second line")
                await asyncio.sleep(0.08)

                assert mock_client.room_send.called
                payload = mock_client.room_send.call_args[0][2]
                rendered = payload["m.new_content"]["formatted_body"]
                assert "first line" in rendered
                assert "second line" in rendered

    def test_tool_field_html_includes_model_label(self):
        mgr = _make_manager()
        result = mgr._build_html(
            summary="Running tools",
            step=2,
            ts=time.time(),
            content_html="tool line",
            model_label="gpt-5.4 via openai-codex",
            field_kind="tools",
        )
        assert "Tool Activity" in result
        assert "gpt-5.4 via openai-codex" in result


# ---------------------------------------------------------------------------
# HTML truncation
# ---------------------------------------------------------------------------

class TestHtmlTruncation:
    def test_large_content_truncated(self):
        from gateway.platforms.matrix_thinking import _MAX_BODY_SIZE

        mgr = _make_manager()
        huge_content = "x" * (_MAX_BODY_SIZE + 1000)
        result = mgr._build_html(
            summary="Big",
            step=1,
            ts=time.time(),
            content_html=huge_content,
        )
        # Should be smaller than original + wrapper
        assert len(result.encode("utf-8")) < len(huge_content.encode("utf-8")) + 500
        assert "truncated" in result


# ---------------------------------------------------------------------------
# Error resilience
# ---------------------------------------------------------------------------

class TestErrorResilience:
    @pytest.mark.asyncio
    async def test_start_handles_send_failure(self):
        adapter, mock_client, _ = _make_adapter()

        import sys
        fake_nio = MagicMock()
        fake_nio.RoomSendResponse = type("RoomSendResponse", (), {})
        mock_client.room_send = AsyncMock(side_effect=Exception("Connection lost"))

        with patch.dict(sys.modules, {"nio": fake_nio}):
            result = await adapter.start_thinking("!room:example.org", "task_fail")

        assert result is None

    @pytest.mark.asyncio
    async def test_update_handles_send_failure_gracefully(self):
        adapter, mock_client, _ = _make_adapter()

        mgr = adapter._get_thinking_manager()
        from gateway.platforms.matrix_thinking import ThinkingSession

        mgr._sessions["task_err"] = ThinkingSession(
            room_id="!room:example.org",
            event_id="$evt_1",
            task_id="task_err",
            started_at=time.time() - 20,
            last_update=time.time() - 10,
        )

        mock_client.room_send = AsyncMock(side_effect=Exception("Network error"))

        # Should not raise
        await mgr.update("task_err", "Step 1", "data")
        # Session should still exist
        assert "task_err" in mgr._sessions
