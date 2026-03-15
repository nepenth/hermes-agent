"""Tests for the /plan CLI slash command."""

from unittest.mock import MagicMock

from cli import HermesCLI


def _make_cli():
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.config = {}
    cli_obj.console = MagicMock()
    cli_obj.agent = None
    cli_obj.conversation_history = []
    cli_obj.session_id = "sess-123"
    cli_obj._pending_input = MagicMock()
    return cli_obj


class TestCLIPlanCommand:
    def test_plan_command_queues_plan_mode_message(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        cli_obj = _make_cli()

        result = cli_obj.process_command("/plan Add OAuth login")

        assert result is True
        cli_obj._pending_input.put.assert_called_once()
        queued = cli_obj._pending_input.put.call_args[0][0]
        assert '"/plan" command' in queued
        assert "Add OAuth login" in queued
        assert str(tmp_path / "plans") in queued

    def test_plan_without_args_uses_conversation_context(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        cli_obj = _make_cli()

        cli_obj.process_command("/plan")

        queued = cli_obj._pending_input.put.call_args[0][0]
        assert "current conversation context" in queued
        assert "conversation-plan" in queued
