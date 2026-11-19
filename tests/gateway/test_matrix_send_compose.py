"""Matrix send(): sticky panes, pre-rendered cards and ``reply_to_mode`` share one chunk loop.

Panes (``matrix_formatted_body_unprefixed``) keep ``m.thread`` but never take the reply quote,
and still split. Pre-rendered cards are one event and fail closed over the transport limit.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig


def _adapter(mode="all"):
    from plugins.platforms.matrix.adapter import MatrixAdapter
    adapter = MatrixAdapter(PlatformConfig.from_dict({
        "enabled": True, "token": "syt_test_token", "reply_to_mode": mode,
        "extra": {"homeserver": "https://matrix.example.org", "user_id": "@bot:example.org"},
    }))
    adapter._client = MagicMock()
    adapter._client.send_message_event = AsyncMock(return_value="$evt")
    return adapter


def _sent(adapter):
    return [call.args[2] if len(call.args) > 2 else call.kwargs["content"]
            for call in adapter._client.send_message_event.await_args_list]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["first", "all"])
async def test_pane_send_keeps_thread_and_omits_reply_fallback(mode):
    adapter = _adapter(mode)
    text = "line\n" * (adapter.max_message_length // len("line\n") + 200)
    meta = {"thread_id": "$root", "matrix_formatted_body": "<ul><li>tool</li></ul>",
            "matrix_formatted_body_unprefixed": True}
    result = await adapter.send("!room:example.org", text, reply_to="$parent", metadata=meta)
    assert result.success
    contents = _sent(adapter)
    assert len(contents) == 2  # a pane still splits
    assert contents[0]["formatted_body"] == "<ul><li>tool</li></ul>"
    assert "<li>tool</li>" not in contents[1].get("formatted_body", "")  # pane HTML on chunk 0 only
    for content in contents:
        relation = content["m.relates_to"]
        assert relation["rel_type"] == "m.thread" and relation["event_id"] == "$root"
        assert "m.in_reply_to" not in relation


@pytest.mark.asyncio
async def test_answer_send_still_follows_reply_to_mode():
    adapter = _adapter("all")
    text = "line\n" * (adapter.max_message_length // len("line\n") + 200)
    await adapter.send("!room:example.org", text, reply_to="$parent", metadata={"thread_id": "$root"})
    assert all("m.in_reply_to" in c["m.relates_to"] for c in _sent(adapter))


@pytest.mark.asyncio
async def test_oversized_pre_rendered_card_fails_closed_as_one_event():
    adapter = _adapter("first")
    text = "x" * (adapter.max_message_length + 10)
    result = await adapter.send("!room:example.org", text, reply_to="$parent",
                                metadata={"matrix_formatted_body": "<p>card</p>"})
    assert not result.success
    assert _sent(adapter) == []
