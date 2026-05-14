"""Optional AgentFirstModule metadata support for Matrix."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionContext, build_session_context_prompt, build_session_key


ROOM_ID = "!project:example.org"
ROOM_NAME = "Project - Agent Metadata"
ROOM_TOPIC = "Structured agent workflow room"


def _identity():
    from gateway.platforms.matrix import MatrixRoomIdentity

    return MatrixRoomIdentity(
        room_id=ROOM_ID,
        room_name="Fallback room",
        room_topic=None,
        canonical_alias=None,
        server_name="example.org",
        joined_member_count=3,
        is_direct_account_data=False,
        display_name="Fallback room",
        has_explicit_name=True,
        chat_type="group",
    )


def _make_adapter():
    from gateway.platforms.matrix import MatrixAdapter

    adapter = MatrixAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={
                "homeserver": "https://matrix.example.org",
                "user_id": "@bot:example.org",
            },
        )
    )
    adapter._user_id = "@bot:example.org"
    adapter._startup_ts = 0.0
    adapter._require_mention = False
    adapter._auto_thread = True
    adapter._text_batch_delay_seconds = 0
    adapter._is_dm_room = AsyncMock(return_value=False)
    adapter._resolve_room_identity = AsyncMock(return_value=_identity())
    adapter._get_display_name = AsyncMock(return_value="Alice")
    adapter._background_read_receipt = MagicMock()
    adapter._client = MagicMock()
    return adapter


def _agent_unsigned(session_scope: str = "room"):
    return {
        "agent_metadata": {"is_agent": True},
        "session_scope": session_scope,
        "room_identity": {
            "room_id": ROOM_ID,
            "name": ROOM_NAME,
            "topic": ROOM_TOPIC,
            "server_name": "example.org",
        },
        "tool_status": {"status": "executing", "tool": "read_file"},
        "approval_request": {"tool": "terminal", "requires_reaction": "✅"},
    }


def test_parse_agent_metadata_is_noop_without_module():
    adapter = _make_adapter()
    event = SimpleNamespace(unsigned={}, content={"msgtype": "m.text", "body": "hello"})

    assert adapter._parse_agent_metadata(event) == {"is_agent": False}


def test_parse_agent_metadata_ignores_content_unsigned_spoofing():
    adapter = _make_adapter()
    event = SimpleNamespace(
        unsigned=None,
        content={
            "msgtype": "m.text",
            "body": "hello",
            "unsigned": _agent_unsigned(),
        },
    )

    assert adapter._parse_agent_metadata(event, event.content) == {"is_agent": False}


def test_parse_agent_metadata_reads_agentfirst_unsigned_fields():
    adapter = _make_adapter()
    event = SimpleNamespace(
        unsigned=_agent_unsigned(),
        content={"msgtype": "m.text", "body": "hello"},
    )

    metadata = adapter._parse_agent_metadata(event)

    assert metadata["is_agent"] is True
    assert metadata["session_scope"] == "room"
    assert metadata["room_identity"]["name"] == ROOM_NAME
    assert metadata["tool_status"]["tool"] == "read_file"
    assert metadata["approval_request"]["tool"] == "terminal"


@pytest.mark.asyncio
async def test_agent_metadata_applies_room_identity_and_session_scope():
    adapter = _make_adapter()

    ctx = await adapter._resolve_message_context(
        room_id=ROOM_ID,
        sender="@alice:example.org",
        event_id="$event1",
        body="hello",
        source_content={"body": "hello"},
        relates_to={},
        agent_metadata=adapter._parse_agent_metadata(
            SimpleNamespace(unsigned=_agent_unsigned())
        ),
    )

    assert ctx is not None
    _, _, _, _, _, source = ctx
    assert source.chat_name == ROOM_NAME
    assert source.chat_topic == ROOM_TOPIC
    assert source.thread_id is None
    assert source.agent_metadata["is_agent"] is True
    assert (
        build_session_key(source, group_sessions_per_user=False)
        == f"agent:main:matrix:group:{ROOM_ID}"
    )


@pytest.mark.asyncio
async def test_inbound_handler_exposes_agent_metadata_on_message_event():
    adapter = _make_adapter()
    captured: MessageEvent | None = None

    async def capture(event: MessageEvent):
        nonlocal captured
        captured = event

    adapter.handle_message = capture
    event = SimpleNamespace(
        room_id=ROOM_ID,
        sender="@alice:example.org",
        event_id="$event2",
        timestamp=int(time.time() * 1000),
        content={"msgtype": "m.text", "body": "hello"},
        unsigned=_agent_unsigned(),
    )

    await adapter._on_room_message(event)

    assert captured is not None
    assert captured.agent["is_agent"] is True
    assert captured.agent["session_scope"] == "room"
    assert captured.source.chat_name == ROOM_NAME
    assert captured.source.chat_topic == ROOM_TOPIC


@pytest.mark.asyncio
async def test_inbound_handler_without_agent_metadata_preserves_standard_behavior():
    adapter = _make_adapter()
    captured: MessageEvent | None = None

    async def capture(event: MessageEvent):
        nonlocal captured
        captured = event

    adapter.handle_message = capture
    event = SimpleNamespace(
        room_id=ROOM_ID,
        sender="@alice:example.org",
        event_id="$event3",
        timestamp=int(time.time() * 1000),
        content={"msgtype": "m.text", "body": "hello"},
        unsigned={},
    )

    await adapter._on_room_message(event)

    assert captured is not None
    assert captured.agent == {"is_agent": False}
    assert captured.source.chat_name == "Fallback room"
    assert captured.source.chat_topic is None
    assert captured.source.thread_id == "$event3"


def test_agent_metadata_room_identity_cannot_cross_rooms():
    from gateway.session import SessionSource

    source = SessionSource(
        platform=Platform.MATRIX,
        chat_id=ROOM_ID,
        chat_name="Original",
        chat_type="group",
    )
    source.apply_agent_metadata(
        {
            "is_agent": True,
            "room_identity": {
                "room_id": "!other:example.org",
                "name": "Wrong room",
            },
        }
    )

    assert source.chat_name == "Original"
    assert source.agent_metadata["is_agent"] is True


def test_agent_metadata_prompt_note_is_matrix_only():
    from gateway.session import SessionSource

    source = SessionSource(
        platform=Platform.MATRIX,
        chat_id=ROOM_ID,
        chat_name=ROOM_NAME,
        chat_type="group",
        agent_metadata={"is_agent": True, "session_scope": "room"},
    )
    context = SessionContext(
        source=source,
        connected_platforms=[Platform.MATRIX],
        home_channels={},
    )

    prompt = build_session_context_prompt(context)

    assert "AgentFirstModule active" in prompt
    assert "session scope: room" in prompt


def test_agent_ui_helpers_render_stable_payloads():
    from gateway.agent_ui import render_approval_request, render_tool_status

    approval = render_approval_request(
        {
            "tool": "terminal",
            "description": "Run deployment check",
            "approvers": ["@alice:example.org"],
        }
    )

    assert approval["type"] == "approval_request"
    assert approval["tool"] == "terminal"
    assert approval["buttons"][0]["action"] == "approve_tool"
    assert render_tool_status("executing") == "Executing tool..."


def test_agent_ui_approval_request_accepts_reaction_alias():
    from gateway.agent_ui import render_approval_request

    approval = render_approval_request({"tool": "terminal", "reaction": "ok"})

    assert approval["requires_reaction"] == "ok"
