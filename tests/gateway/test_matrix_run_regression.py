"""Regression tests for Matrix gateway run-loop wiring."""

import importlib
import sys
import threading
import types
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.session import SessionSource


class MatrixThinkingCaptureAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.MATRIX)
        self._thinking_enabled = True
        self.started = []
        self.finalized = []

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        return SendResult(success=True, message_id="$msg")

    async def edit_message(self, chat_id, message_id, content) -> SendResult:
        return SendResult(success=True, message_id=message_id)

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}

    async def start_thinking(self, chat_id, task_id, summary="Processing…", model_label="", initial_content_md=""):
        self.started.append({
            "chat_id": chat_id,
            "task_id": task_id,
            "summary": summary,
            "model_label": model_label,
            "initial_content_md": initial_content_md,
        })
        return "$thinking"

    async def update_thinking(self, task_id, summary, content_md, model_label=None, append_line=True):
        return None

    async def finalize_thinking(self, task_id, summary, collapse=True, model_label=None):
        self.finalized.append({
            "task_id": task_id,
            "summary": summary,
            "collapse": collapse,
            "model_label": model_label,
        })
        return None

    async def start_tool_activity(self, chat_id, task_id, summary="Tool activity", model_label="", initial_content_md=""):
        return "$tools"

    async def update_tool_activity(self, task_id, tool_name, content_md, model_label=None, append_line=True):
        return None

    async def finalize_tool_activity(self, task_id, summary, collapse=True, model_label=None):
        return None


class FakeMatrixAgent:
    def __init__(self, **kwargs):
        self.status_callback = kwargs.get("status_callback")
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.thinking_callback = None
        self.reasoning_callback = None
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        if self.status_callback:
            self.status_callback("status", "Preparing response")
        return {
            "final_response": "ok",
            "messages": [],
            "api_calls": 1,
        }


def _make_runner(adapter):
    gateway_run = importlib.import_module("gateway.run")
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {Platform.MATRIX: adapter}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner._running_agent_started_at = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._effective_model = None
    runner._effective_provider = None
    runner._get_or_create_gateway_honcho = lambda sk: (None, {})
    runner.hooks = SimpleNamespace(loaded_hooks=False, emit=None)
    return runner


@pytest.mark.asyncio
async def test_run_agent_matrix_thinking_enabled_does_not_raise_nameerror(monkeypatch, tmp_path):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeMatrixAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    adapter = MatrixThinkingCaptureAdapter()
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_env_path", tmp_path / ".env")
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    runner._load_reasoning_config = lambda: {"enabled": False}

    source = SessionSource(
        platform=Platform.MATRIX,
        chat_id="!room:example.org",
        chat_type="room",
        user_id="@chris:example.org",
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-matrix-1",
        session_key="agent:main:matrix:room:!room:example.org",
    )

    assert result["final_response"] == "ok"
    assert adapter.started
    assert adapter.finalized
    assert adapter.started[0]["task_id"] == "agent:main:matrix:room:!room:example.org"
    assert adapter.finalized[0]["summary"] == "Complete (1 API calls)"
