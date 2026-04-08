"""Tests for Matrix LLM-callable tools (tools/matrix_tools.py).

Covers all 6 tool handlers, input validation, adapter availability checks,
toolset registration, and platform hint configuration.
"""

import asyncio
import json
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.config import Platform, PlatformConfig


# ===========================================================================
# Tool handler tests
# ===========================================================================

class TestMatrixToolsSendReaction:
    """Tests for the matrix_send_reaction tool handler."""

    def setup_method(self):
        from tools.matrix_tools import set_matrix_adapter, _handle_send_reaction
        self.handler = _handle_send_reaction
        self.adapter = MagicMock()
        self.adapter._send_reaction = AsyncMock(return_value=True)
        self.adapter._loop = asyncio.new_event_loop()
        set_matrix_adapter(self.adapter)

    def teardown_method(self):
        from tools.matrix_tools import set_matrix_adapter
        set_matrix_adapter(None)
        if hasattr(self, "adapter") and hasattr(self.adapter, "_loop"):
            self.adapter._loop.close()

    def test_valid_reaction(self):
        result = json.loads(self.handler({
            "room_id": "!room:example.org",
            "event_id": "$evt123",
            "emoji": "👍",
        }))
        assert result["success"] is True

    def test_missing_room_id(self):
        result = json.loads(self.handler({
            "room_id": "",
            "event_id": "$evt123",
            "emoji": "👍",
        }))
        assert "error" in result

    def test_invalid_room_id_prefix(self):
        result = json.loads(self.handler({
            "room_id": "bad_room",
            "event_id": "$evt123",
            "emoji": "👍",
        }))
        assert "error" in result
        assert "!" in result["error"]

    def test_missing_event_id(self):
        result = json.loads(self.handler({
            "room_id": "!room:example.org",
            "event_id": "",
            "emoji": "👍",
        }))
        assert "error" in result

    def test_invalid_event_id_prefix(self):
        result = json.loads(self.handler({
            "room_id": "!room:example.org",
            "event_id": "not_an_event",
            "emoji": "👍",
        }))
        assert "error" in result

    def test_missing_emoji(self):
        result = json.loads(self.handler({
            "room_id": "!room:example.org",
            "event_id": "$evt123",
            "emoji": "",
        }))
        assert "error" in result

    def test_emoji_too_long(self):
        result = json.loads(self.handler({
            "room_id": "!room:example.org",
            "event_id": "$evt123",
            "emoji": "x" * 33,
        }))
        assert "error" in result

    def test_adapter_error_handled(self):
        self.adapter._send_reaction = AsyncMock(side_effect=RuntimeError("network"))
        result = json.loads(self.handler({
            "room_id": "!room:example.org",
            "event_id": "$evt123",
            "emoji": "👍",
        }))
        assert "error" in result


class TestMatrixToolsRedactMessage:
    def setup_method(self):
        from tools.matrix_tools import set_matrix_adapter, _handle_redact_message
        self.handler = _handle_redact_message
        self.adapter = MagicMock()
        self.adapter.redact_message = AsyncMock(return_value=True)
        self.adapter._loop = asyncio.new_event_loop()
        set_matrix_adapter(self.adapter)

    def teardown_method(self):
        from tools.matrix_tools import set_matrix_adapter
        set_matrix_adapter(None)
        if hasattr(self, "adapter") and hasattr(self.adapter, "_loop"):
            self.adapter._loop.close()

    def test_valid_redact(self):
        result = json.loads(self.handler({
            "room_id": "!room:example.org",
            "event_id": "$evt123",
        }))
        assert result["success"] is True

    def test_with_reason(self):
        result = json.loads(self.handler({
            "room_id": "!room:example.org",
            "event_id": "$evt123",
            "reason": "spam",
        }))
        assert result["success"] is True

    def test_long_reason_truncated(self):
        self.handler({
            "room_id": "!room:example.org",
            "event_id": "$evt123",
            "reason": "x" * 600,
        })
        call_args = self.adapter.redact_message.call_args
        assert len(call_args.kwargs.get("reason", "")) <= 500


class TestMatrixToolsCreateRoom:
    def setup_method(self):
        from tools.matrix_tools import set_matrix_adapter, _handle_create_room
        self.handler = _handle_create_room
        self.adapter = MagicMock()
        self.adapter.create_room = AsyncMock(return_value="!new:example.org")
        self.adapter._loop = asyncio.new_event_loop()
        set_matrix_adapter(self.adapter)

    def teardown_method(self):
        from tools.matrix_tools import set_matrix_adapter
        set_matrix_adapter(None)
        if hasattr(self, "adapter") and hasattr(self.adapter, "_loop"):
            self.adapter._loop.close()

    def test_create_private_room(self):
        result = json.loads(self.handler({
            "name": "Test Room",
            "preset": "private_chat",
        }))
        assert result["success"] is True
        assert result["room_id"] == "!new:example.org"

    def test_invalid_preset(self):
        result = json.loads(self.handler({
            "name": "Test",
            "preset": "bad_preset",
        }))
        assert "error" in result

    @patch.dict("os.environ", {"MATRIX_ALLOW_PUBLIC_ROOMS": "false"})
    def test_public_room_blocked_by_default(self):
        result = json.loads(self.handler({
            "name": "Public",
            "preset": "public_chat",
        }))
        assert "error" in result
        assert "disabled" in result["error"].lower()

    @patch.dict("os.environ", {"MATRIX_ALLOW_PUBLIC_ROOMS": "true"})
    def test_public_room_allowed_with_env(self):
        result = json.loads(self.handler({
            "name": "Public",
            "preset": "public_chat",
        }))
        assert result["success"] is True

    def test_invalid_invite_list_entry(self):
        result = json.loads(self.handler({
            "invite": ["not_a_user"],
        }))
        assert "error" in result

    def test_valid_invite_list(self):
        result = json.loads(self.handler({
            "invite": ["@user:example.org"],
        }))
        assert result["success"] is True

    def test_room_creation_failure(self):
        self.adapter.create_room = AsyncMock(return_value=None)
        result = json.loads(self.handler({"name": "fail"}))
        assert result["success"] is False


class TestMatrixToolsInviteUser:
    def setup_method(self):
        from tools.matrix_tools import set_matrix_adapter, _handle_invite_user
        self.handler = _handle_invite_user
        self.adapter = MagicMock()
        self.adapter.invite_user = AsyncMock(return_value=True)
        self.adapter._loop = asyncio.new_event_loop()
        set_matrix_adapter(self.adapter)

    def teardown_method(self):
        from tools.matrix_tools import set_matrix_adapter
        set_matrix_adapter(None)
        if hasattr(self, "adapter") and hasattr(self.adapter, "_loop"):
            self.adapter._loop.close()

    def test_valid_invite(self):
        result = json.loads(self.handler({
            "room_id": "!room:example.org",
            "user_id": "@user:example.org",
        }))
        assert result["success"] is True

    def test_invalid_user_id(self):
        result = json.loads(self.handler({
            "room_id": "!room:example.org",
            "user_id": "bad_user",
        }))
        assert "error" in result

    def test_missing_room_id(self):
        result = json.loads(self.handler({
            "room_id": "",
            "user_id": "@user:example.org",
        }))
        assert "error" in result


class TestMatrixToolsFetchHistory:
    def setup_method(self):
        from tools.matrix_tools import set_matrix_adapter, _handle_fetch_history
        self.handler = _handle_fetch_history
        self.adapter = MagicMock()
        self.adapter.fetch_room_history = AsyncMock(return_value=[
            {"sender": "@alice:ex.org", "body": "hello", "timestamp": 12345},
        ])
        self.adapter._loop = asyncio.new_event_loop()
        set_matrix_adapter(self.adapter)

    def teardown_method(self):
        from tools.matrix_tools import set_matrix_adapter
        set_matrix_adapter(None)
        if hasattr(self, "adapter") and hasattr(self.adapter, "_loop"):
            self.adapter._loop.close()

    def test_fetch_history(self):
        result = json.loads(self.handler({
            "room_id": "!room:example.org",
            "limit": 10,
        }))
        assert result["count"] == 1
        assert len(result["messages"]) == 1

    def test_limit_clamped_to_200(self):
        self.handler({"room_id": "!room:example.org", "limit": 500})
        call_args = self.adapter.fetch_room_history.call_args
        assert call_args.kwargs.get("limit", 200) <= 200

    def test_negative_limit_defaults(self):
        self.handler({"room_id": "!room:example.org", "limit": -1})
        call_args = self.adapter.fetch_room_history.call_args
        assert call_args.kwargs.get("limit", 50) == 50


class TestMatrixToolsSetPresence:
    def setup_method(self):
        from tools.matrix_tools import set_matrix_adapter, _handle_set_presence
        self.handler = _handle_set_presence
        self.adapter = MagicMock()
        self.adapter.set_presence = AsyncMock(return_value=True)
        self.adapter._loop = asyncio.new_event_loop()
        set_matrix_adapter(self.adapter)

    def teardown_method(self):
        from tools.matrix_tools import set_matrix_adapter
        set_matrix_adapter(None)
        if hasattr(self, "adapter") and hasattr(self.adapter, "_loop"):
            self.adapter._loop.close()

    def test_set_online(self):
        result = json.loads(self.handler({"state": "online"}))
        assert result["success"] is True

    def test_set_offline(self):
        result = json.loads(self.handler({"state": "offline"}))
        assert result["success"] is True

    def test_set_unavailable(self):
        result = json.loads(self.handler({"state": "unavailable"}))
        assert result["success"] is True

    def test_invalid_state(self):
        result = json.loads(self.handler({"state": "busy"}))
        assert "error" in result

    def test_status_msg_truncated(self):
        self.handler({"state": "online", "status_msg": "x" * 300})
        call_args = self.adapter.set_presence.call_args
        assert len(call_args.kwargs.get("status_msg", "")) <= 255


class TestMatrixToolsAvailabilityCheck:
    """Test that tools are unavailable when no adapter is connected."""

    def test_unavailable_when_no_adapter(self):
        from tools.matrix_tools import set_matrix_adapter, _check_matrix_available
        set_matrix_adapter(None)
        assert _check_matrix_available() is False

    def test_available_when_adapter_set(self):
        from tools.matrix_tools import set_matrix_adapter, _check_matrix_available
        set_matrix_adapter(MagicMock())
        assert _check_matrix_available() is True
        set_matrix_adapter(None)

    def test_adapter_not_connected_raises(self):
        from tools.matrix_tools import set_matrix_adapter, _ensure_adapter
        set_matrix_adapter(None)
        with pytest.raises(RuntimeError, match="not connected"):
            _ensure_adapter()


# ===========================================================================
# Registration tests
# ===========================================================================

class TestToolRegistration:
    """Verify matrix tools are registered in the correct toolsets."""

    def test_matrix_toolset_exists(self):
        from toolsets import TOOLSETS
        assert "matrix" in TOOLSETS

    def test_matrix_toolset_has_all_tools(self):
        from toolsets import TOOLSETS
        tools = TOOLSETS["matrix"]["tools"]
        expected = [
            "matrix_send_reaction", "matrix_redact_message", "matrix_create_room",
            "matrix_invite_user", "matrix_fetch_history", "matrix_set_presence",
        ]
        for name in expected:
            assert name in tools, f"Missing tool: {name}"

    def test_hermes_matrix_includes_matrix(self):
        from toolsets import TOOLSETS
        includes = TOOLSETS["hermes-matrix"]["includes"]
        assert "matrix" in includes

    def test_matrix_platform_hint_exists(self):
        from agent.prompt_builder import PLATFORM_HINTS
        assert "matrix" in PLATFORM_HINTS
        hint = PLATFORM_HINTS["matrix"]
        assert "matrix_send_reaction" in hint
        assert "HTML" in hint

    def test_matrix_tools_module_importable(self):
        """tools.matrix_tools should be importable."""
        import importlib
        spec = importlib.util.find_spec("tools.matrix_tools")
        assert spec is not None


class TestAdapterWiring:
    """Verify set_matrix_adapter is called in connect/disconnect."""

    def test_connect_wires_adapter(self):
        """connect() should call set_matrix_adapter(self)."""
        from gateway.platforms.matrix import MatrixAdapter
        config = PlatformConfig(
            enabled=True, token="***",
            extra={"homeserver": "https://matrix.example.org", "user_id": "@bot:example.org"},
        )
        adapter = MatrixAdapter(config)
        # The actual connect() wiring is tested by checking the import
        # Try/except pattern — just verify the code path exists
        import inspect
        source = inspect.getsource(adapter.connect)
        assert "set_matrix_adapter" in source
