"""Regression tests for gateway startup PID-file timing."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import gateway.run as gateway_run


class _FakeRunner:
    should_exit_cleanly = False
    should_exit_with_failure = False
    exit_reason = None
    adapters = {}

    def __init__(self, config):
        self.config = config

    async def start(self):
        return True

    async def wait_for_shutdown(self):
        return None

    async def stop(self):
        return None


@pytest.mark.asyncio
async def test_start_gateway_writes_pid_before_runner_start(monkeypatch):
    order = []

    class Runner(_FakeRunner):
        async def start(self):
            order.append("start")
            assert order and order[0] == "pid"
            return True

    monkeypatch.setattr(gateway_run, "GatewayRunner", Runner)

    with patch("gateway.status.get_running_pid", return_value=None), \
         patch("gateway.status.write_pid_file", side_effect=lambda: order.append("pid")), \
         patch("gateway.status.remove_pid_file"), \
         patch("hermes_logging.setup_logging", return_value=gateway_run._hermes_home / "logs"), \
         patch("hermes_logging._add_rotating_handler"), \
         patch("tools.skills_sync.sync_skills"), \
         patch.object(gateway_run, "_start_cron_ticker"), \
         patch.object(gateway_run.logging.getLogger(), "addHandler"):
        result = await gateway_run.start_gateway(config=SimpleNamespace())

    await asyncio.sleep(0)
    assert result is True
    assert order[0] == "pid"
    assert "start" in order


@pytest.mark.asyncio
async def test_start_gateway_removes_pid_on_clean_exit(monkeypatch):
    class Runner(_FakeRunner):
        should_exit_cleanly = True
        exit_reason = "test clean exit"

        async def start(self):
            return True

    monkeypatch.setattr(gateway_run, "GatewayRunner", Runner)
    removed = []

    with patch("gateway.status.get_running_pid", return_value=None), \
         patch("gateway.status.write_pid_file"), \
         patch("gateway.status.remove_pid_file", side_effect=lambda: removed.append(True)), \
         patch("hermes_logging.setup_logging", return_value=gateway_run._hermes_home / "logs"), \
         patch("hermes_logging._add_rotating_handler"), \
         patch("tools.skills_sync.sync_skills"), \
         patch.object(gateway_run.logging.getLogger(), "addHandler"):
        result = await gateway_run.start_gateway(config=SimpleNamespace())

    assert result is True
    assert removed
