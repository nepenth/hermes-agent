"""Tests for /reasoning slash command in HermesCLI."""

from unittest.mock import patch

import pytest


def _make_cli(**kwargs):
    """Create a HermesCLI instance with minimal mocking."""
    import cli as _cli_mod
    from cli import HermesCLI

    clean_config = {
        "model": {
            "default": "anthropic/claude-opus-4.6",
            "base_url": "https://openrouter.ai/api/v1",
            "provider": "auto",
        },
        "display": {"compact": False, "tool_progress": "all"},
        "agent": {"reasoning_effort": "medium"},
        "terminal": {"env_type": "local"},
    }

    clean_env = {"LLM_MODEL": "", "HERMES_MAX_ITERATIONS": ""}

    with (
        patch("cli.get_tool_definitions", return_value=[]),
        patch.dict("os.environ", clean_env, clear=False),
        patch.dict(_cli_mod.__dict__, {"CLI_CONFIG": clean_config}),
    ):
        return HermesCLI(**kwargs)


# -- setting valid effort levels -------------------------------------------


@pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh"])
def test_reasoning_command_sets_effort_and_persists(level):
    cli_obj = _make_cli()
    cli_obj.agent = object()  # ensure command forces re-init

    with patch("cli.save_config_value", return_value=True) as mock_save:
        keep_running = cli_obj.process_command(f"/reasoning {level}")

    assert keep_running is True
    assert cli_obj.reasoning_config == {"enabled": True, "effort": level}
    assert cli_obj.agent is None
    mock_save.assert_called_once_with("agent.reasoning_effort", level)


def test_reasoning_command_sets_none_disables_reasoning():
    cli_obj = _make_cli()
    cli_obj.agent = object()

    with patch("cli.save_config_value", return_value=True) as mock_save:
        keep_running = cli_obj.process_command("/reasoning none")

    assert keep_running is True
    assert cli_obj.reasoning_config == {"enabled": False}
    assert cli_obj.agent is None
    mock_save.assert_called_once_with("agent.reasoning_effort", "none")


# -- rejecting invalid levels ---------------------------------------------


@pytest.mark.parametrize("bad_level", ["ultra", "minimal", "max", "off", "0", "150"])
def test_reasoning_command_rejects_invalid_level(capsys, bad_level):
    cli_obj = _make_cli()
    before = cli_obj.reasoning_config

    with patch("cli.save_config_value", return_value=True) as mock_save:
        keep_running = cli_obj.process_command(f"/reasoning {bad_level}")

    out = capsys.readouterr().out
    assert keep_running is True
    assert "Invalid reasoning level" in out
    assert cli_obj.reasoning_config == before
    mock_save.assert_not_called()


# -- display current level -------------------------------------------------


def test_reasoning_shows_current_effort(capsys):
    cli_obj = _make_cli()
    cli_obj.reasoning_config = {"enabled": True, "effort": "high"}

    cli_obj.process_command("/reasoning")

    out = capsys.readouterr().out
    assert "Reasoning effort: high" in out


def test_reasoning_shows_default_when_none_set(capsys):
    cli_obj = _make_cli()
    cli_obj.reasoning_config = None

    cli_obj.process_command("/reasoning")

    out = capsys.readouterr().out
    assert "medium (default)" in out


def test_reasoning_shows_disabled_when_none_effort(capsys):
    cli_obj = _make_cli()
    cli_obj.reasoning_config = {"enabled": False}

    cli_obj.process_command("/reasoning")

    out = capsys.readouterr().out
    assert "none (reasoning disabled)" in out


# -- case insensitivity ----------------------------------------------------


def test_reasoning_command_is_case_insensitive(capsys):
    cli_obj = _make_cli()

    with patch("cli.save_config_value", return_value=True):
        cli_obj.process_command("/reasoning HIGH")

    assert cli_obj.reasoning_config == {"enabled": True, "effort": "high"}


# -- config save failure ---------------------------------------------------


def test_reasoning_shows_session_only_on_save_failure(capsys):
    cli_obj = _make_cli()

    with patch("cli.save_config_value", return_value=False):
        cli_obj.process_command("/reasoning high")

    out = capsys.readouterr().out
    assert "session only" in out
