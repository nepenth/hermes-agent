"""grok-4.7 takes reasoning effort up to xhigh, without the 4.6-only Priority service tier."""
import pytest

from agent.model_metadata import grok_supports_reasoning_effort


@pytest.fixture
def transport():
    from agent.transports.codex import ResponsesApiTransport
    return ResponsesApiTransport()


def _kwargs(transport, **extra):
    return transport.build_kwargs(
        model="grok-4.7", messages=[{"role": "user", "content": "hi"}], tools=[], is_xai_responses=True, **extra)


def test_grok_47_is_effort_capable():
    assert grok_supports_reasoning_effort("grok-4.7")


def test_grok_47_preserves_xhigh(transport):
    assert _kwargs(transport, reasoning_config={"effort": "xhigh"})["reasoning"]["effort"] == "xhigh"


def test_grok_47_strips_priority_service_tier(transport):
    assert "service_tier" not in _kwargs(transport, request_overrides={"service_tier": "priority"})
