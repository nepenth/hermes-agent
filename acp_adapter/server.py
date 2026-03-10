"""ACP agent server — exposes hermes-agent via the Agent Communication Protocol."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional, Sequence

import acp
from acp.schema import (
    AgentCapabilities,
    AuthenticateResponse,
    AuthMethod,
    ClientCapabilities,
    ForkSessionResponse,
    Implementation,
    InitializeResponse,
    ListSessionsResponse,
    NewSessionResponse,
    PromptResponse,
    SessionCapabilities,
    SessionForkCapabilities,
    SessionListCapabilities,
    SessionInfo,
    TextContentBlock,
    ImageContentBlock,
    AudioContentBlock,
    ResourceContentBlock,
    EmbeddedResourceContentBlock,
    HttpMcpServer,
    SseMcpServer,
    McpServerStdio,
)

from acp_adapter.auth import detect_provider, has_provider
from acp_adapter.session import SessionManager

logger = logging.getLogger(__name__)

HERMES_VERSION = "0.1.0"


class HermesACPAgent(acp.Agent):
    """ACP Agent implementation wrapping hermes-agent."""

    def __init__(self, session_manager: SessionManager | None = None):
        super().__init__()
        self.session_manager = session_manager or SessionManager()

    # ---- ACP lifecycle ------------------------------------------------------

    def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        provider = detect_provider()
        auth_methods = []
        if provider:
            auth_methods.append(
                AuthMethod(
                    id=provider,
                    name=f"{provider} API key",
                    description=f"Authenticate via {provider}",
                )
            )

        return InitializeResponse(
            protocol_version=acp.PROTOCOL_VERSION,
            agent_info=Implementation(name="hermes-agent", version=HERMES_VERSION),
            agent_capabilities=AgentCapabilities(
                session_capabilities=SessionCapabilities(
                    fork=SessionForkCapabilities(),
                    list=SessionListCapabilities(),
                ),
            ),
            auth_methods=auth_methods if auth_methods else None,
        )

    def authenticate(self, method_id: str, **kwargs: Any) -> AuthenticateResponse | None:
        if has_provider():
            return AuthenticateResponse()
        return None

    # ---- Session management -------------------------------------------------

    def new_session(
        self,
        cwd: str,
        mcp_servers: list | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        state = self.session_manager.create_session(cwd=cwd)
        return NewSessionResponse(session_id=state.session_id)

    def cancel(self, session_id: str, **kwargs: Any) -> None:
        state = self.session_manager.get_session(session_id)
        if state and state.cancel_event:
            state.cancel_event.set()

    def fork_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list | None = None,
        **kwargs: Any,
    ) -> ForkSessionResponse:
        state = self.session_manager.fork_session(session_id, cwd=cwd)
        return ForkSessionResponse(session_id=state.session_id if state else "")

    def list_sessions(
        self,
        cursor: str | None = None,
        cwd: str | None = None,
        **kwargs: Any,
    ) -> ListSessionsResponse:
        infos = self.session_manager.list_sessions()
        sessions = [
            SessionInfo(session_id=s["session_id"], cwd=s["cwd"])
            for s in infos
        ]
        return ListSessionsResponse(sessions=sessions)

    # ---- Prompt (placeholder) -----------------------------------------------

    def prompt(
        self,
        prompt: list,
        session_id: str,
        **kwargs: Any,
    ) -> PromptResponse:
        # Full implementation would run AIAgent here.
        return PromptResponse(stop_reason="end_turn")
