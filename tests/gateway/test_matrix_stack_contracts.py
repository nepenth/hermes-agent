"""Private integration contracts across independently maintained Matrix PRs."""
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from plugins.platforms.matrix.adapter import MatrixAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "first", "all"])
async def test_split_relations_and_authoritative_html_coexist(mode):
    adapter = MatrixAdapter(PlatformConfig(enabled=True, reply_to_mode=mode))
    adapter.format_message = lambda text: text
    adapter.truncate_message = lambda text, limit: ["one", "two", "three"]
    adapter._send_room_message = AsyncMock(return_value="$root")
    metadata = {"thread_id": "$thread"}
    result = await adapter.send("!room:example.org", "split text", reply_to="$parent", metadata=metadata)
    assert result.success
    for index, call in enumerate(adapter._send_room_message.await_args_list):
        relation = call.args[1]["m.relates_to"]
        assert relation["rel_type"] == "m.thread"
        assert relation["event_id"] == "$thread"
        expected = mode == "all" or (mode == "first" and index == 0)
        assert ("m.in_reply_to" in relation) is expected
        assert ("is_falling_back" in relation) is expected

    adapter._send_room_message.reset_mock()
    html = "<details><summary>Command</summary><pre>safe</pre></details><script>unsafe()</script>"
    result = await adapter.send("!room:example.org", "full audit fallback", reply_to="$parent",
                                metadata={**metadata, "matrix_formatted_body": html})
    assert result.success
    adapter._send_room_message.assert_awaited_once()
    payload = adapter._send_room_message.await_args.args[1]
    assert payload["body"] == "full audit fallback"
    assert "<details>" in payload["formatted_body"]
    assert "<summary>" in payload["formatted_body"]
    assert "unsafe" not in payload["formatted_body"]


@pytest.mark.asyncio
@pytest.mark.parametrize("unprefixed", [False, True])
async def test_edit_prefix_is_explicit_and_original_root_survives(unprefixed):
    adapter = MatrixAdapter(PlatformConfig(enabled=True))
    adapter._send_content_event = AsyncMock(return_value=SendResult(success=True, message_id="$replacement"))
    html = "<p><strong>Current status</strong></p>"
    result = await adapter.edit_message("!room:example.org", "$root", "Current status",
                                        metadata={"matrix_formatted_body": html,
                                                  "matrix_formatted_body_unprefixed": unprefixed})
    assert result.success and result.message_id == "$root"
    payload = adapter._send_content_event.await_args.args[1]
    assert payload["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$root"}
    assert payload["m.new_content"]["formatted_body"] == html
    assert payload["formatted_body"] == ("" if unprefixed else "* ") + html


@pytest.mark.asyncio
async def test_oversized_authoritative_card_never_sends_a_partial_audit():
    adapter = MatrixAdapter(PlatformConfig(enabled=True))
    adapter.max_message_length = 64
    adapter._send_room_message = AsyncMock()
    result = await adapter.send("!room:example.org", "audit " * 100,
                                metadata={"matrix_formatted_body": "<p>short summary</p>"})
    assert not result.success
    adapter._send_room_message.assert_not_awaited()
