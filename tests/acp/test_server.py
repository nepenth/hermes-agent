"""Tests for acp_adapter.server — HermesACPAgent ACP server."""

import os
from unittest.mock import MagicMock

import pytest

import acp
from acp.schema import (
    AgentCapabilities,
    AuthenticateResponse,
    Implementation,
    InitializeResponse,
    NewSessionResponse,
    SessionInfo,
)
from acp_adapter.server import HermesACPAgent, HERMES_VERSION
from acp_adapter.session import SessionManager


@pytest.fixture()
def mock_manager():
    """SessionManager with a mock agent factory."""
    return SessionManager(agent_factory=lambda: MagicMock(name="MockAIAgent"))


@pytest.fixture()
def agent(mock_manager):
    """HermesACPAgent backed by a mock session manager."""
    return HermesACPAgent(session_manager=mock_manager)


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------


class TestInitialize:
    def test_initialize_returns_correct_protocol_version(self, agent):
        resp = agent.initialize(protocol_version=1)
        assert isinstance(resp, InitializeResponse)
        assert resp.protocol_version == acp.PROTOCOL_VERSION

    def test_initialize_returns_agent_info(self, agent):
        resp = agent.initialize(protocol_version=1)
        assert resp.agent_info is not None
        assert isinstance(resp.agent_info, Implementation)
        assert resp.agent_info.name == "hermes-agent"
        assert resp.agent_info.version == HERMES_VERSION

    def test_initialize_returns_capabilities(self, agent):
        resp = agent.initialize(protocol_version=1)
        caps = resp.agent_capabilities
        assert isinstance(caps, AgentCapabilities)
        assert caps.session_capabilities is not None
        assert caps.session_capabilities.fork is not None
        assert caps.session_capabilities.list is not None


# ---------------------------------------------------------------------------
# authenticate
# ---------------------------------------------------------------------------


class TestAuthenticate:
    def test_authenticate_with_provider_configured(self, agent, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-123")
        resp = agent.authenticate(method_id="openrouter")
        assert isinstance(resp, AuthenticateResponse)

    def test_authenticate_without_provider(self, agent, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        resp = agent.authenticate(method_id="openrouter")
        assert resp is None


# ---------------------------------------------------------------------------
# new_session / cancel
# ---------------------------------------------------------------------------


class TestSessionOps:
    def test_new_session_creates_session(self, agent):
        resp = agent.new_session(cwd="/home/user/project")
        assert isinstance(resp, NewSessionResponse)
        assert resp.session_id
        # Session should be retrievable from the manager
        state = agent.session_manager.get_session(resp.session_id)
        assert state is not None
        assert state.cwd == "/home/user/project"

    def test_cancel_sets_event(self, agent):
        resp = agent.new_session(cwd=".")
        state = agent.session_manager.get_session(resp.session_id)
        assert not state.cancel_event.is_set()
        agent.cancel(session_id=resp.session_id)
        assert state.cancel_event.is_set()

    def test_cancel_nonexistent_session_is_noop(self, agent):
        # Should not raise
        agent.cancel(session_id="does-not-exist")
