"""Regression tests for Matrix thinking delta coalescing."""

import importlib
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_fake_nio():
    mod = types.ModuleType("nio")

    class RoomSendResponse:
        def __init__(self, event_id="$evt"):
            self.event_id = event_id

    mod.RoomSendResponse = RoomSendResponse
    return mod


class _Adapter:
    def __init__(self, nio_mod):
        self._client = MagicMock()
        self._client.room_send = AsyncMock(return_value=nio_mod.RoomSendResponse())


@pytest.mark.asyncio
async def test_update_with_append_line_false_merges_into_last_line():
    nio = _make_fake_nio()
    with patch.dict(sys.modules, {"nio": nio}):
        if "gateway.platforms.matrix_thinking" in sys.modules:
            importlib.reload(sys.modules["gateway.platforms.matrix_thinking"])
        from gateway.platforms.matrix_thinking import ThinkingManager

        adapter = _Adapter(nio)
        mgr = ThinkingManager(adapter)

        await mgr.start("!room:example", "task1", initial_content_md="Hello")
        await mgr.update("task1", "Reasoning…", " world", append_line=False)
        await mgr.update("task1", "Reasoning…", "!", append_line=False)

        session = mgr._sessions[mgr._session_key("task1", "thinking")]
        assert session.content_lines == ["Hello world!"]
