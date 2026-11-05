"""A dedicated SSH desktop host keeps Chromium's own sandbox when it can start there; containers keep the
``--no-sandbox`` bypass. And ``computer-use screen status`` says where an off-gateway screen runs instead of
printing ``pid None`` / ``rfb socket: None``."""

import argparse
import subprocess

import pytest

from hermes_cli.subcommands import computer_use_screen
from tools.bot_desktop import lease, runtime, sandbox_host
from tools.bot_desktop.placement import SshTarget
from tools.bot_desktop.ssh_host import DesktopSSHEnvironment


def _ssh_env():
    env = object.__new__(DesktopSSHEnvironment)  # no connection: only the type and target matter here
    env.target = SshTarget(host="desktops.example.lan", user="agent-a", port=2222, display=21)
    env._bd_desktop_user = None
    return env


def _fake_run_in(results, calls):
    def run_in(env, argv, **kwargs):
        calls.append(argv)
        out = results.pop(0)
        if isinstance(out, Exception):
            raise out
        return subprocess.CompletedProcess(argv, out[0], stdout=out[1], stderr=b"")
    return run_in


def test_container_env_always_bypasses(monkeypatch):
    calls = []
    monkeypatch.setattr(sandbox_host.streams, "run_in", _fake_run_in([], calls))
    assert sandbox_host.chromium_needs_sandbox_bypass(object()) is True
    assert calls == [], "a container is never probed: it has no user namespaces for Chromium's sandbox"


def test_ssh_host_with_userns_keeps_the_sandbox_and_probes_once(monkeypatch):
    calls = []
    monkeypatch.setattr(sandbox_host.streams, "run_in", _fake_run_in([(0, b"userns-ok\n")], calls))
    env = _ssh_env()
    assert sandbox_host.chromium_needs_sandbox_bypass(env) is False
    assert sandbox_host.chromium_needs_sandbox_bypass(env) is False
    assert len(calls) == 1 and "unshare -Ur" in calls[0][-1]


@pytest.mark.parametrize("result", [(1, b""), (0, b""), RuntimeError("unreachable"),
                                    subprocess.TimeoutExpired("ssh", 20)])
def test_ssh_host_without_a_usable_sandbox_falls_back_to_the_bypass(monkeypatch, result):
    monkeypatch.setattr(sandbox_host.streams, "run_in", _fake_run_in([result], []))
    assert sandbox_host.chromium_needs_sandbox_bypass(_ssh_env()) is True


def test_dock_icon_follows_the_probe(monkeypatch):
    from tools.bot_desktop.browser import dock_exec_line
    assert "--no-sandbox" not in dock_exec_line("/opt/c/chrome", "/home/agent-a/p", sandbox_bypass=False)
    assert "--no-sandbox" in dock_exec_line("/opt/c/chrome", "/home/agent-a/p", sandbox_bypass=True)


def _parse(argv):
    parser = argparse.ArgumentParser()
    computer_use_screen.build_screen_parser(
        parser.add_subparsers(), lambda sub, help_text: sub.add_argument("--json", action="store_true"))
    return parser.parse_args(argv)


@pytest.mark.parametrize("placement,expect,absent", [
    ("ssh:agent-a@desktops.example.lan:2222", "in ssh:agent-a@desktops.example.lan:2222", "None"),
    ("gateway", "pid 4242", None),
])
def test_status_names_where_an_off_gateway_screen_runs(monkeypatch, capsys, placement, expect, absent):
    off = placement != "gateway"
    monkeypatch.setattr(runtime, "status", lambda profile=None: runtime.DesktopStatus(
        profile="agent-a", supported=True, installed=True, missing=[], running=True,
        pid=None if off else 4242, display=":21", socket=None if off else "/x/rfb.sock",
        geometry="1440x900", install_command=None, browser=None, placement=placement))
    try:
        assert computer_use_screen.SCREEN_ACTIONS["status"](_parse(["screen", "status"])) == 0
    finally:
        lease._reset_for_tests()
    out = capsys.readouterr().out
    assert expect in out
    if absent:
        assert absent not in out
