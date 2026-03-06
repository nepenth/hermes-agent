"""Tests for the serve layer (serve.py) and event_queue integration.

Covers:
- _emit_event: queue attached, no queue, queue full
- extra_tags merging in _build_api_kwargs for Nous API
- FastAPI /health endpoint
- FastAPI /v1/agent/stream SSE endpoint (mocked AIAgent)

Run with: python -m pytest tests/test_serve.py -v
"""

import json
import queue
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from run_agent import AIAgent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


@pytest.fixture()
def agent_no_queue():
    """AIAgent without an event_queue (CLI/gateway mode)."""
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.client = MagicMock()
        return a


@pytest.fixture()
def agent_with_queue():
    """AIAgent with an event_queue attached (serve mode)."""
    eq = queue.Queue(maxsize=128)
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            event_queue=eq,
        )
        a.client = MagicMock()
        return a, eq


@pytest.fixture()
def nous_agent():
    """AIAgent pointing at a Nous inference URL with extra_tags."""
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            base_url="https://stg-inference-api.nousresearch.com/v1",
            api_key="test-key-1234567890",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            extra_tags=["user=test-user", "tier=paid"],
        )
        a.client = MagicMock()
        return a


# ===========================================================================
# Group 1: _emit_event
# ===========================================================================


class TestEmitEvent:
    def test_no_queue_is_noop(self, agent_no_queue):
        """_emit_event should silently do nothing when no queue is attached."""
        agent_no_queue._emit_event({"type": "text", "text": "hello"})

    def test_event_pushed_to_queue(self, agent_with_queue):
        agent, eq = agent_with_queue
        event = {"type": "text", "text": "hello"}
        agent._emit_event(event)
        assert not eq.empty()
        assert eq.get_nowait() == event

    def test_multiple_events_ordered(self, agent_with_queue):
        agent, eq = agent_with_queue
        events = [
            {"type": "tool-call", "name": "terminal", "status": "calling"},
            {"type": "tool-result", "name": "terminal", "status": "complete"},
            {"type": "text", "text": "done"},
            {"type": "done"},
        ]
        for e in events:
            agent._emit_event(e)
        received = []
        while not eq.empty():
            received.append(eq.get_nowait())
        assert received == events

    def test_full_queue_does_not_raise(self):
        """When the queue is full, _emit_event should silently drop the event."""
        eq = queue.Queue(maxsize=1)
        with (
            patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
        ):
            a = AIAgent(
                api_key="test-key-1234567890",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                event_queue=eq,
            )
        eq.put({"type": "filler"})
        assert eq.full()
        a._emit_event({"type": "text", "text": "overflow"})
        assert eq.qsize() == 1
        assert eq.get_nowait()["type"] == "filler"


# ===========================================================================
# Group 2: extra_tags in _build_api_kwargs
# ===========================================================================


class TestExtraTags:
    def test_no_tags_on_openrouter(self, agent_no_queue):
        """OpenRouter requests should NOT include Nous product tags."""
        messages = [{"role": "user", "content": "hi"}]
        kwargs = agent_no_queue._build_api_kwargs(messages)
        extra = kwargs.get("extra_body", {})
        assert "tags" not in extra

    def test_default_product_tag_on_nous(self, nous_agent):
        """Nous API requests should always include product=hermes-agent."""
        messages = [{"role": "user", "content": "hi"}]
        kwargs = nous_agent._build_api_kwargs(messages)
        tags = kwargs["extra_body"]["tags"]
        assert "product=hermes-agent" in tags

    def test_extra_tags_merged(self, nous_agent):
        """Caller-supplied tags should appear alongside the product tag."""
        messages = [{"role": "user", "content": "hi"}]
        kwargs = nous_agent._build_api_kwargs(messages)
        tags = kwargs["extra_body"]["tags"]
        assert "user=test-user" in tags
        assert "tier=paid" in tags
        assert "product=hermes-agent" in tags

    def test_extra_tags_empty_by_default(self, agent_no_queue):
        """Agent without extra_tags should have an empty list."""
        assert agent_no_queue._extra_tags == []

    def test_extra_tags_does_not_mutate_original(self, nous_agent):
        """Calling _build_api_kwargs should not grow _extra_tags each time."""
        messages = [{"role": "user", "content": "hi"}]
        nous_agent._build_api_kwargs(messages)
        nous_agent._build_api_kwargs(messages)
        assert nous_agent._extra_tags.count("product=hermes-agent") == 0
        assert len(nous_agent._extra_tags) == 2


# ===========================================================================
# Group 3: FastAPI endpoints (serve.py)
# ===========================================================================


@pytest.fixture()
def fastapi_app():
    """Import the FastAPI app from serve.py."""
    from serve import app
    return app


@pytest.mark.asyncio
class TestHealthEndpoint:
    async def test_health_returns_ok(self, fastapi_app):
        transport = ASGITransport(app=fastapi_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"


@pytest.mark.asyncio
class TestAgentStreamEndpoint:
    async def test_stream_returns_sse_events(self, fastapi_app):
        """Mock AIAgent to emit known events and verify SSE output."""
        mock_result = {
            "final_response": "Hello!",
            "messages": [],
            "api_calls": 1,
            "completed": True,
        }

        def fake_run_conversation(user_message, conversation_history=None):
            agent_instance = fake_init.agent_ref
            if agent_instance and agent_instance.event_queue:
                eq = agent_instance.event_queue
                eq.put({"type": "tool-call", "name": "terminal", "args": "echo hi", "status": "calling"})
                eq.put({"type": "tool-result", "name": "terminal", "output": "hi", "status": "complete", "duration": 0.1})
                eq.put({"type": "text", "text": "Hello!"})
                eq.put({"type": "done"})
            return mock_result

        class fake_init:
            agent_ref = None

        original_init = AIAgent.__init__

        def patched_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            self.run_conversation = fake_run_conversation
            fake_init.agent_ref = self

        with (
            patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
            patch.object(AIAgent, "__init__", patched_init),
        ):
            transport = ASGITransport(app=fastapi_app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/v1/agent/stream",
                    json={
                        "messages": [{"role": "user", "content": "Say hello"}],
                        "model": "test/model",
                    },
                    timeout=30,
                )

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

        lines = resp.text.strip().split("\n")
        events = []
        for line in lines:
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))

        types = [e["type"] for e in events]
        assert "tool-call" in types
        assert "tool-result" in types
        assert "text" in types
        assert types[-1] == "done"

        text_event = next(e for e in events if e["type"] == "text")
        assert text_event["text"] == "Hello!"

        tool_call = next(e for e in events if e["type"] == "tool-call")
        assert tool_call["name"] == "terminal"

    async def test_stream_error_propagated(self, fastapi_app):
        """When AIAgent raises, an error event should be streamed."""
        original_init = AIAgent.__init__

        def patched_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)

            def exploding_run(user_message, conversation_history=None):
                raise RuntimeError("kaboom")

            self.run_conversation = exploding_run

        with (
            patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
            patch.object(AIAgent, "__init__", patched_init),
        ):
            transport = ASGITransport(app=fastapi_app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/v1/agent/stream",
                    json={
                        "messages": [{"role": "user", "content": "fail"}],
                        "model": "test/model",
                    },
                    timeout=30,
                )

        assert resp.status_code == 200
        events = []
        for line in resp.text.strip().split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))

        error_events = [e for e in events if e["type"] == "error"]
        assert len(error_events) >= 1
        assert "kaboom" in error_events[0]["error"]
        assert events[-1]["type"] == "done"

    async def test_stream_passes_base_url_and_tags(self, fastapi_app):
        """Verify base_url, api_key, and tags from the request body reach AIAgent."""
        captured = {}
        original_init = AIAgent.__init__

        def patched_init(self, *args, **kwargs):
            captured["base_url"] = kwargs.get("base_url")
            captured["api_key"] = kwargs.get("api_key")
            captured["extra_tags"] = kwargs.get("extra_tags")
            original_init(self, *args, **kwargs)
            self.run_conversation = lambda **kw: (
                self.event_queue.put({"type": "text", "text": "ok"}) if self.event_queue else None,
                self.event_queue.put({"type": "done"}) if self.event_queue else None,
                {"final_response": "ok", "messages": [], "api_calls": 1, "completed": True},
            )[-1]

        with (
            patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
            patch.object(AIAgent, "__init__", patched_init),
        ):
            transport = ASGITransport(app=fastapi_app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                await client.post(
                    "/v1/agent/stream",
                    json={
                        "messages": [{"role": "user", "content": "hi"}],
                        "model": "test/model",
                        "base_url": "https://my-api.example.com/v1",
                        "api_key": "sk-test-key",
                        "tags": ["user=alice", "tier=free"],
                    },
                    timeout=30,
                )

        assert captured["base_url"] == "https://my-api.example.com/v1"
        assert captured["api_key"] == "sk-test-key"
        assert captured["extra_tags"] == ["user=alice", "tier=free"]
