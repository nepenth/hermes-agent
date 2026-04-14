"""Regression tests for gateway /model support of config.yaml custom_providers."""

import yaml
import pytest

from unittest.mock import AsyncMock

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType, SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _make_runner():
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._session_model_overrides = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._pending_model_notes = {}
    runner._evict_cached_agent = lambda session_key: None
    return runner


def _make_event(text="/model", *, platform=Platform.TELEGRAM, user_id="user-1", thread_id=None):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(platform=platform, chat_id="12345", chat_type="dm", user_id=user_id, thread_id=thread_id),
    )


@pytest.mark.asyncio
async def test_handle_model_command_lists_saved_custom_provider(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {
                    "default": "gpt-5.4",
                    "provider": "openai-codex",
                    "base_url": "https://chatgpt.com/backend-api/codex",
                },
                "providers": {},
                "custom_providers": [
                    {
                        "name": "Local (127.0.0.1:4141)",
                        "base_url": "http://127.0.0.1:4141/v1",
                        "model": "rotator-openrouter-coding",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})

    result = await _make_runner()._handle_model_command(_make_event())

    assert result is not None
    assert "Local (127.0.0.1:4141)" in result
    assert "custom:local-(127.0.0.1:4141)" in result
    assert "rotator-openrouter-coding" in result


@pytest.mark.asyncio
async def test_handle_model_command_matrix_picker_passes_sender_identity(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {
                    "default": "gpt-5.4",
                    "provider": "openai-codex",
                    "base_url": "https://chatgpt.com/backend-api/codex",
                },
                "providers": {},
            }
        ),
        encoding="utf-8",
    )

    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.setattr(
        "agent.models_dev.fetch_models_dev",
        lambda: {
            "openai-codex": {
                "label": "OpenAI Codex",
                "models": {"gpt-5.4": {"label": "gpt-5.4"}},
            }
        },
    )

    class MatrixPickerAdapter:
        def __init__(self):
            self.mock = AsyncMock(return_value=SendResult(success=True, message_id="$picker"))

        async def send_model_picker(self, **kwargs):
            return await self.mock(**kwargs)

    adapter = MatrixPickerAdapter()

    runner = _make_runner()
    runner.adapters = {Platform.MATRIX: adapter}

    result = await runner._handle_model_command(
        _make_event(
            "/model",
            platform=Platform.MATRIX,
            user_id="@chris:matrix.whyland.com",
            thread_id="$thread-1",
        )
    )

    assert result is None
    adapter.mock.assert_awaited_once()
    metadata = adapter.mock.await_args.kwargs["metadata"]
    assert metadata["thread_id"] == "$thread-1"
    assert metadata["sender_id"] == "@chris:matrix.whyland.com"
