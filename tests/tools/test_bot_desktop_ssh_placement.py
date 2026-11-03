"""``bot_desktop.placement: ssh``: the screen on a desktop host of its own, the terminal wherever it is.

Invariants:
  1. ``ssh`` wins over the terminal backend (a local terminal is the common case) and an incomplete
     ``bot_desktop.ssh`` block refuses; it never resolves to the gateway host.
  2. Every desktop channel (screen, cua-driver MCP, the browser) goes to the ``bot_desktop.ssh`` login, and that
     connection carries nothing from the gateway's home: no file sync, no passthrough secrets.
  3. The recorded owner wins: a screen started on the desktop host is found and stopped there after the
     placement or the target changed.
  4. A cua-driver mode that runs a private daemon on the gateway host is refused for any off-gateway screen.
  5. Nothing of the desktop host's session reaches a process on the gateway host (local terminal, local browser
     harness), and the desktop connection never shares a ControlMaster with another profile, key or the
     terminal backend.
  6. A desktop host that does not answer is never forgotten (stop keeps the marker) and not re-dialled on
     every status call.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from tools.bot_desktop import placement, runtime, sandbox_host, ssh_host
from tools.environments import ssh as ssh_env
from tools.environments import streams

TARGET = {"host": "desktops.example", "user": "agent-a", "port": 2222, "key": "", "display": 31}
OTHER = {**TARGET, "host": "desktops-2.example"}
REMOTE_SESSION = {"DISPLAY": ":31", "XAUTHORITY": "/home/agent-a/.cache/hermes-bot-desktop/p1/Xauthority",
                  "DBUS_SESSION_BUS_ADDRESS": "unix:path=/home/agent-a/.cache/bus",
                  "XDG_CONFIG_HOME": "/home/agent-a/.cache/xdg", "XDG_CACHE_HOME": "/home/agent-a/.cache/xdg-cache",
                  "XDG_DATA_HOME": "/home/agent-a/.cache/xdg-data"}


def _use(monkeypatch, *, setting="ssh", backend="local", ssh=None):
    monkeypatch.setattr(placement, "_setting", lambda: setting)
    monkeypatch.setattr(placement, "_terminal_backend", lambda: backend)
    monkeypatch.setattr(placement, "_ssh_cfg", lambda: dict(TARGET if ssh is None else ssh))


@pytest.fixture
def offline_ssh(monkeypatch):
    """SSHEnvironment without a network: connection and home probe answer locally, every ssh argv is recorded."""
    runs: list[list[str]] = []

    def _run_capture(argv, timeout=None, **_):
        runs.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="/home/agent-a\n", stderr="")

    monkeypatch.setattr(ssh_env, "_ensure_ssh_available", lambda: None)
    monkeypatch.setattr(ssh_env, "run_capture", _run_capture)
    monkeypatch.setattr(ssh_host, "_envs", {})
    monkeypatch.setattr(ssh_host, "_unreachable", {})
    return runs


def _write_marker(target=TARGET, **extra):
    sandbox_host._marker().parent.mkdir(parents=True, exist_ok=True)
    sandbox_host._marker().write_text(json.dumps({"display": ":31", "backend": "DesktopSSHEnvironment",
                                                  "ssh_target": target, **extra}), encoding="utf-8")


def _dials(runs):
    return [argv for argv in runs if argv[-1] == "echo 'SSH connection established'"]


def _control_path(env):
    return next(a.split("=", 1)[1] for a in env._build_ssh_command() if a.startswith("ControlPath="))


@pytest.mark.parametrize("backend", ["local", "docker", "ssh", "modal"])
def test_ssh_placement_is_independent_of_the_terminal_backend(monkeypatch, backend):
    _use(monkeypatch, backend=backend)
    where = placement.resolve()
    assert where.where == placement.SSH and where.off_gateway
    assert where.ssh == placement.SshTarget("desktops.example", "agent-a", 2222, "", 31)


@pytest.mark.parametrize(("ssh", "names"), [
    ({}, "bot_desktop.ssh.host and bot_desktop.ssh.user"),
    ({"host": "desktops.example"}, "bot_desktop.ssh.user"),
    ({**TARGET, "port": 0}, "bot_desktop.ssh.port"),
    ({**TARGET, "display": "twenty"}, "bot_desktop.ssh.display"),
    ({**TARGET, "display": True}, "bot_desktop.ssh.display"),
    ({**TARGET, "user": "-oProxyCommand=x"}, "host / user"),
    ({**TARGET, "host": "a b"}, "host / user"),
    ({**TARGET, "host": "fd00::1"}, "IPv6 literals are not supported"),
    ({**TARGET, "key": "-oProxyCommand=x"}, "bot_desktop.ssh.key"),
    ({**TARGET, "key": "~/keys/id a"}, "bot_desktop.ssh.key"),
    ({**TARGET, "key": "~/keys/id\n-oProxyCommand=x"}, "bot_desktop.ssh.key"),
])
def test_incomplete_ssh_block_refuses_and_never_falls_back_to_the_gateway(monkeypatch, ssh, names):
    _use(monkeypatch, ssh=ssh)
    where = placement.resolve()
    assert where.where == placement.REFUSED and not where.off_gateway
    assert names in where.reason and "placement: gateway" in where.reason
    with pytest.raises(RuntimeError, match="bot_desktop.ssh"):
        runtime.tool_placement()


def test_ssh_placement_reads_the_profile_config_file(monkeypatch):
    """Through the real loader: the keys the docs name are the keys the resolver reads."""
    from hermes_constants import get_hermes_home
    (get_hermes_home() / "config.yaml").write_text(
        "bot_desktop:\n  placement: ssh\n  ssh:\n    host: desktops.example\n    user: agent-a\n    display: 31\n",
        encoding="utf-8")
    monkeypatch.setattr(placement, "_terminal_backend", lambda: "local")
    where = placement.resolve()
    assert where.where == placement.SSH and where.ssh is not None
    assert (where.ssh.host, where.ssh.user, where.ssh.port, where.ssh.display) == ("desktops.example", "agent-a", 22, 31)


def test_desktop_connection_carries_nothing_from_the_gateway_home(monkeypatch, offline_ssh):
    """The ssh terminal backend syncs ~/.hermes and forwards passthrough secrets; the desktop host gets neither."""
    def _forbidden(*_a, **_k):
        raise AssertionError("the desktop host must not receive the gateway's files or passthrough env")

    monkeypatch.setattr(ssh_env, "FileSyncManager", _forbidden)
    monkeypatch.setattr(ssh_env, "resolve_passthrough_env", _forbidden)
    _use(monkeypatch)
    monkeypatch.setattr(placement, "terminal_environment",
                        lambda **_: pytest.fail("ssh placement must not touch the terminal backend"))
    assert runtime._sandbox_env(create=False) is None  # status probes never dial
    env = runtime._sandbox_env(create=True)
    assert isinstance(env, ssh_host.DesktopSSHEnvironment)
    assert runtime._sandbox_env(create=False) is env  # one connection per profile + target
    assert streams.sandbox_temp_dir(env) == "/home/agent-a/.cache"  # per login, not a shared /tmp
    assert all(argv[-2] == "agent-a@desktops.example" and "2222" in argv for argv in offline_ssh)

    command, args = sandbox_host.cua_mcp_invocation(env, "p1", {"DISPLAY": ":31"})
    argv = [command, *args]
    assert argv[0] == "ssh" and "agent-a@desktops.example" in argv and "BatchMode=yes" in argv
    assert "cua-driver mcp" in argv[-1] and "export DISPLAY=:31" in argv[-1]


def test_screen_starts_on_the_configured_display_and_records_its_host(monkeypatch, offline_ssh):
    _use(monkeypatch)
    env = runtime._sandbox_env(create=True)
    seen: list[dict] = []
    monkeypatch.setattr(sandbox_host, "_published", lambda e, rdir: {"DISPLAY": ":31"} if seen else {})
    monkeypatch.setattr(sandbox_host, "stop", lambda e, profile: False)
    monkeypatch.setattr(sandbox_host, "missing_binaries", lambda e: [])
    monkeypatch.setattr(sandbox_host, "_user_for", lambda e: None)

    def _run_in(e, argv, *, child_env=None, user=None, timeout=30.0, stdin=None):
        if child_env and "HERMES_BD_DISPLAY_NUM" in child_env:
            seen.append(child_env)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(streams, "run_in", _run_in)
    sandbox_host.start(env, "p1", geometry="1440x900", wait_seconds=1, display_num=31)
    assert seen[0]["HERMES_BD_DISPLAY_NUM"] == "31"
    assert seen[0]["HERMES_BD_SOCKET"] == "/home/agent-a/.cache/hermes-bot-desktop/p1/rfb.sock"
    marker = json.loads(sandbox_host._marker().read_text(encoding="utf-8"))
    assert marker["ssh_target"] == TARGET


def test_recorded_desktop_host_wins_over_the_current_placement(monkeypatch, offline_ssh):
    """Placement moved back to gateway after a screen started on the desktop host: stop goes to that host."""
    sandbox_host._marker().parent.mkdir(parents=True, exist_ok=True)
    sandbox_host._marker().write_text(json.dumps({"display": ":31", "backend": "DesktopSSHEnvironment",
                                                  "ssh_target": TARGET}), encoding="utf-8")
    _use(monkeypatch, setting="gateway")
    monkeypatch.setattr(placement, "terminal_environment",
                        lambda **_: pytest.fail("a desktop-host marker must not resolve to the terminal backend"))
    stopped: list[tuple[str, str]] = []
    monkeypatch.setattr(sandbox_host, "stop", lambda e, profile: stopped.append((e.target.host, e.target.user)) or True)
    assert runtime.stop() is True
    assert stopped == [("desktops.example", "agent-a")]
    assert not sandbox_host._marker().exists()


def _host_down(monkeypatch):
    runs: list[list[str]] = []

    def _down(argv, timeout=None, **_):
        runs.append(argv)
        return subprocess.CompletedProcess(argv, 255, "", "No route to host")

    monkeypatch.setattr(ssh_env, "run_capture", _down)
    return runs


def test_unreachable_desktop_host_is_dialled_once_then_negative_cached(monkeypatch, offline_ssh):
    _write_marker()
    _use(monkeypatch)
    runs = _host_down(monkeypatch)
    st = runtime.status()
    assert not st.running and st.placement == "ssh:agent-a@desktops.example:2222"
    assert len(_dials(runs)) == 1  # status really tried the host
    assert not runtime.status().running
    assert runtime._marker_env(sandbox_host._read_marker(), create=False) is None
    assert len(_dials(runs)) == 1  # ...and the second status did not wait out ConnectTimeout again
    monkeypatch.setattr(ssh_host.time, "monotonic", lambda: 10**9)  # the retry window has passed
    runtime.status()
    assert len(_dials(runs)) == 2


def test_label_follows_the_recorded_host_when_it_is_down_and_placement_moved(monkeypatch, offline_ssh):
    _write_marker()
    _use(monkeypatch, setting="terminal", backend="docker")
    monkeypatch.setattr(placement, "terminal_environment",
                        lambda **_: pytest.fail("a desktop-host marker must not resolve to the terminal backend"))
    _host_down(monkeypatch)
    assert runtime.status().placement == "ssh:agent-a@desktops.example:2222"


def test_stop_while_the_desktop_host_is_down_keeps_the_marker(monkeypatch, offline_ssh):
    from tools.environments.base import EnvironmentConnectionError
    _write_marker()
    _use(monkeypatch)
    _host_down(monkeypatch)
    with pytest.raises(EnvironmentConnectionError):
        runtime.stop()
    assert sandbox_host._read_marker()["ssh_target"] == TARGET  # still the only record of where the screen is


def test_stop_whose_ssh_drops_mid_command_keeps_the_marker(monkeypatch, offline_ssh):
    """Connected, then the stop exec itself fails in ssh (rc 255): the screen was not reached."""
    from tools.environments.base import EnvironmentConnectionError
    _write_marker()
    _use(monkeypatch)
    monkeypatch.setattr(sandbox_host, "_user_for", lambda e: None)
    monkeypatch.setattr(streams, "run_in", lambda e, argv, **_: subprocess.CompletedProcess(argv, 255, b"", b"broken pipe"))
    with pytest.raises(EnvironmentConnectionError, match="did not answer"):
        runtime.stop()
    assert sandbox_host._marker().exists()


def test_corrupt_ssh_target_is_a_broken_marker_not_a_terminal_screen(monkeypatch, offline_ssh):
    _write_marker({"host": "", "user": "agent-a"})
    _use(monkeypatch, setting="terminal", backend="docker")
    monkeypatch.setattr(placement, "terminal_environment",
                        lambda **_: pytest.fail("a broken desktop-host marker must not resolve to the terminal backend"))
    monkeypatch.setattr(runtime, "_sandbox_env", lambda **_: pytest.fail("nor to the current placement"))
    with pytest.raises(runtime.BrokenMarker, match="ssh_target is unreadable"):
        runtime.stop()
    assert sandbox_host._marker().exists()
    st = runtime.status()
    assert not st.running and "broken screen record" in (st.blocker or "")
    assert runtime.published_env() == {} and runtime.gateway_host_env() == {}
    assert runtime.sandbox_screen_running()  # kept and reported, never dropped as gone


def test_local_terminal_and_browser_harness_never_get_the_desktop_hosts_session(monkeypatch, offline_ssh):
    """The advertised shape: terminal.backend local, screen on the desktop host. Its DISPLAY, bus and XDG_* paths
    name another machine; a local ``gsettings`` / ``systemctl --user`` must not follow them."""
    from tools import browser_session
    from tools.environments import local
    _use(monkeypatch)
    _write_marker()
    monkeypatch.setattr(sandbox_host, "published_env", lambda e, profile: dict(REMOTE_SESSION))
    runtime._sandbox_env(create=True)  # the screen is up and its connection cached
    assert runtime.published_env() == REMOTE_SESSION  # cua-driver / thumbnail still see it
    from tools.computer_use import cua_backend
    run_env = local._make_run_env({})
    browser_env = browser_session.build_browser_env()
    cua_env = cua_backend.cua_driver_child_env({})  # local cua-driver children: status, doctor, install probes
    for key, value in REMOTE_SESSION.items():
        assert run_env.get(key) != value and browser_env.get(key) != value and cua_env.get(key) != value, key
    # A leftover terminal-sandbox marker under placement: ssh is not this machine's session either.
    sandbox_host._marker().write_text(json.dumps({"display": ":20", "backend": "DockerEnvironment"}), encoding="utf-8")
    assert runtime.gateway_host_env() == {}


def test_gateway_hosted_screen_still_reaches_the_local_terminal(monkeypatch):
    monkeypatch.setattr(runtime, "published_env", lambda: {"DISPLAY": ":55"})
    assert runtime.gateway_host_env() == {"DISPLAY": ":55"}


def test_desktop_connection_has_its_own_control_master(monkeypatch, offline_ssh, tmp_path):
    """Two profiles (or two keys) naming one login never ride one authenticated master, and the terminal
    backend's shared user@host:port socket is not used."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    monkeypatch.setattr(ssh_host, "_CONTROL_PATH_MAX", 10_000)  # pytest's tmp_path is deeper than a real home
    target = placement.SshTarget("desktops.example", "agent-a", 2222, "", 31)
    paths = {}
    for name, key in (("a", ""), ("b", ""), ("a-key", "~/keys/agent-a")):
        home = tmp_path / name.split("-")[0]
        home.mkdir(exist_ok=True)
        token = set_hermes_home_override(home)
        try:
            env = ssh_host.DesktopSSHEnvironment(placement.SshTarget(target.host, target.user, target.port, key, 31))
        finally:
            reset_hermes_home_override(token)
        paths[name] = _control_path(env)
        directory = os.path.dirname(paths[name])
        assert directory == str(home / "bot-desktop" / "ssh")
        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
        argv = env._build_ssh_command()
        assert {"ForwardAgent=no", "ForwardX11=no", "ClearAllForwardings=yes"} <= set(argv)
        assert ("IdentitiesOnly=yes" in argv) == bool(key)
    assert len(set(paths.values())) == 3
    terminal = ssh_env.SSHEnvironment(host="desktops.example", user="agent-a", port=2222, sync_files=False)
    assert _control_path(terminal) not in paths.values()
    assert "ClearAllForwardings=yes" not in terminal._build_ssh_command()  # the terminal backend is unchanged


def test_deep_home_falls_back_to_a_private_temp_dir_still_named_per_profile(monkeypatch, offline_ssh, tmp_path):
    """sun_path: OpenSSH refuses a ControlPath that, with its 17-byte bind suffix, overflows 104 bytes."""
    monkeypatch.setattr(ssh_host, "_CONTROL_PATH_MAX", 1)
    monkeypatch.setattr(ssh_host.tempfile, "gettempdir", lambda: str(tmp_path))
    env = ssh_host.DesktopSSHEnvironment(placement.SshTarget("desktops.example", "agent-a", 2222, "", 31))
    directory = os.path.dirname(_control_path(env))
    assert directory.startswith(str(tmp_path / "hermes-bd-ssh-"))
    assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
    assert _control_path(env) != _control_path(ssh_env.SSHEnvironment(host="desktops.example", user="agent-a",
                                                                       port=2222, sync_files=False))


def test_desktop_connection_never_builds_the_session_snapshot(monkeypatch, offline_ssh):
    """The snapshot goes through _run_bash, which SendEnv-forwards passthrough secrets."""
    monkeypatch.setattr(ssh_host.DesktopSSHEnvironment, "_run_bash",
                        lambda *a, **k: pytest.fail("the desktop connection ran the snapshot path"))
    env = ssh_host.DesktopSSHEnvironment(placement.SshTarget("desktops.example", "agent-a", 2222, "", 31))
    assert env._snapshot_ready is False


def test_recorded_host_wins_for_viewer_and_published_env_after_the_target_moved(monkeypatch, offline_ssh):
    _write_marker()
    _use(monkeypatch, ssh=OTHER)
    recorded = ssh_host.environment(placement.SshTarget.from_marker({"ssh_target": TARGET}))
    seen: list[str] = []
    monkeypatch.setattr(sandbox_host, "published_env", lambda e, profile: seen.append(e.target.host) or {"DISPLAY": ":31"})
    monkeypatch.setattr(sandbox_host, "open_rfb_stream", lambda e, profile: seen.append(e.target.host) or "relay")
    assert runtime.published_env() == {"DISPLAY": ":31"}
    assert runtime.open_rfb_stream() == "relay"
    assert runtime._screen_env(create=True) is recorded  # cua-driver and the browser spawn there too
    assert seen == ["desktops.example", "desktops.example"]
    assert len(_dials(offline_ssh)) == 1  # nothing dialled the new target


def test_viewer_of_an_unattached_screen_closes_as_desktop_gone(monkeypatch, offline_ssh):
    """After a restart nothing attached the recorded host yet: the relay refuses with RuntimeError, which the
    display websocket must treat like a missing socket."""
    _write_marker()
    _use(monkeypatch)
    with pytest.raises(RuntimeError, match="not running"):
        runtime.open_rfb_stream()
    assert offline_ssh == []  # the event-loop path never dials


def test_start_on_a_new_target_stops_the_old_hosts_screen_first(monkeypatch, offline_ssh):
    _write_marker()
    _use(monkeypatch, ssh=OTHER)
    order: list[str] = []
    monkeypatch.setattr(sandbox_host, "stop", lambda e, profile: order.append(f"stop {e.target.host}") or True)
    monkeypatch.setattr(runtime, "_start_in_sandbox",
                        lambda wait, *, display_num: order.append(f"start {runtime.placement.resolve().ssh.host}"))
    runtime.start()
    assert order == ["stop desktops.example", "start desktops-2.example"]
    assert not sandbox_host._marker().exists()


@pytest.mark.parametrize("setting", ["gateway", "ssh-other"])
def test_tools_on_a_moved_placement_retire_the_recorded_screen(monkeypatch, offline_ssh, setting):
    _write_marker()
    _use(monkeypatch, setting="gateway" if setting == "gateway" else "ssh", ssh=OTHER)
    stopped: list[str] = []
    monkeypatch.setattr(sandbox_host, "stop", lambda e, profile: stopped.append(e.target.host) or True)
    monkeypatch.setattr(runtime, "start", lambda **_: None)
    runtime.tool_placement()
    assert stopped == ["desktops.example"] and not sandbox_host._marker().exists()


def test_start_elsewhere_refuses_while_the_recorded_host_is_down(monkeypatch, offline_ssh):
    _write_marker()
    _use(monkeypatch, ssh=OTHER)
    _host_down(monkeypatch)
    monkeypatch.setattr(runtime, "_start_in_sandbox", lambda *a, **k: pytest.fail("started over an unstopped screen"))
    with pytest.raises(RuntimeError, match="did not answer"):
        runtime.start()
    assert sandbox_host._read_marker()["ssh_target"] == TARGET


def test_terminal_sandbox_screen_is_stopped_when_placement_moves_to_ssh(monkeypatch, offline_ssh):
    sandbox_host._marker().parent.mkdir(parents=True, exist_ok=True)
    sandbox_host._marker().write_text(json.dumps({"display": ":20", "backend": "DockerEnvironment"}), encoding="utf-8")
    _use(monkeypatch)
    sandbox_env = object()
    monkeypatch.setattr(placement, "terminal_environment", lambda **_: sandbox_env)
    stopped: list[object] = []
    monkeypatch.setattr(sandbox_host, "stop", lambda e, profile: stopped.append(e) or True)
    monkeypatch.setattr(runtime, "_start_in_sandbox", lambda *a, **k: None)
    runtime.start()
    assert stopped == [sandbox_env]


def test_idle_stop_reaches_a_desktop_host_left_by_a_gateway_that_restarted(monkeypatch, offline_ssh):
    """Empty connection cache (a new gateway process): the idle check re-attaches to the recorded host when the
    local idle clock says the screen is due, and stops it there."""
    from tools.bot_desktop import lease
    _write_marker()
    _use(monkeypatch)
    monkeypatch.setattr(runtime, "idle_stop_seconds", lambda: 60.0)
    monkeypatch.setattr(runtime, "idle_seconds", lambda: 600.0)
    monkeypatch.setattr(lease, "get", lambda: lease.Lease(holder=lease.AGENT))
    monkeypatch.setattr(sandbox_host, "published_env", lambda e, profile: {"DISPLAY": ":31"})
    stopped: list[str] = []
    monkeypatch.setattr(sandbox_host, "stop", lambda e, profile: stopped.append(e.target.host) or True)
    assert runtime.stop_if_idle() is True
    assert stopped == ["desktops.example"] and len(_dials(offline_ssh)) == 1


def test_idle_check_of_a_screen_not_yet_due_never_dials(monkeypatch, offline_ssh):
    _write_marker()
    _use(monkeypatch)
    monkeypatch.setattr(runtime, "idle_stop_seconds", lambda: 600.0)
    monkeypatch.setattr(runtime, "idle_seconds", lambda: 60.0)
    assert runtime.stop_if_idle() is False
    assert offline_ssh == []


def test_browser_profile_on_the_desktop_host_is_owner_only(monkeypatch, offline_ssh):
    from tools import browser_session_sandbox
    env = ssh_host.DesktopSSHEnvironment(placement.SshTarget("desktops.example", "agent-a", 2222, "", 31))
    scripts: list[str] = []

    def _run_in(e, argv, **_):
        scripts.append(argv[-1])
        return subprocess.CompletedProcess(argv, 0, b"4242 777\n", b"")

    monkeypatch.setattr(streams, "run_in", _run_in)
    udd = "/home/agent-a/.hermes/bot-desktop/browser-profile"
    browser_session_sandbox._start_chromium(env, None, "/opt/chrome", udd, {"DISPLAY": ":31"})
    script = scripts[0]
    assert f"(umask 077 && mkdir -p {udd})" in script
    assert f"chmod go-rwx {udd} /home/agent-a/.hermes/bot-desktop" in script


def test_profile_delete_on_a_non_linux_gateway_stops_the_desktop_hosts_screen(monkeypatch, tmp_path):
    from hermes_cli import profiles
    profile_dir = tmp_path / "profiles" / "agent-a"
    (profile_dir / "bot-desktop").mkdir(parents=True)
    (profile_dir / "bot-desktop" / "sandbox.json").write_text(json.dumps({"ssh_target": TARGET}), encoding="utf-8")
    monkeypatch.setattr(runtime, "is_supported_host", lambda: False)
    calls: list[str] = []
    monkeypatch.setattr(runtime, "stop", lambda: calls.append(str(sandbox_host._marker())) or False)
    profiles._stop_bot_desktop(profile_dir)
    assert calls == [str(profile_dir / "bot-desktop" / "sandbox.json")]
    calls.clear()
    profiles._stop_bot_desktop(tmp_path / "profiles" / "no-screen")
    assert calls == []  # a gateway-hosted screen still needs a Linux gateway


def test_display_install_reports_a_refused_placement_instead_of_offering_apt(monkeypatch):
    from tui_gateway import server
    _use(monkeypatch, ssh={"host": "desktops.example"})
    monkeypatch.setattr(runtime, "install_command", lambda: pytest.fail("offered a gateway install"))
    resp = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "display.install", "params": {}})
    assert "bot_desktop.ssh.user" in resp["error"]["message"]
    _use(monkeypatch)
    monkeypatch.setattr(runtime, "is_supported_host", lambda: False)  # a macOS gateway with a desktop host
    resp = server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "display.install", "params": {}})
    assert "desktop host agent-a@desktops.example:2222" in resp["error"]["message"]


def test_browser_follows_the_screen_to_the_desktop_host(monkeypatch):
    from tools import browser_session, browser_session_sandbox
    monkeypatch.setattr(runtime, "tool_placement", lambda: placement.SSH)
    monkeypatch.setattr(browser_session_sandbox, "create_sandbox_session", lambda key: {"sandbox": key})
    monkeypatch.setattr("tools.browser_launcher.launch_chromium",
                        lambda *a, **k: pytest.fail("the gateway host's Chromium must not start"))
    assert browser_session._create_chromium_session("k1") == {"sandbox": "k1"}


@pytest.mark.parametrize("where", [placement.SSH, placement.TERMINAL])
def test_host_daemon_modes_are_refused_for_an_off_gateway_screen(monkeypatch, where):
    """``unrestricted`` (approval bypass) spawns ``cua-driver serve`` on the gateway host; with the screen elsewhere
    that daemon would drive the gateway's display under the remote DISPLAY number."""
    from tools.computer_use import cua_backend, cua_backend_daemon
    monkeypatch.setattr(placement, "resolve", lambda: placement.Placement(where, "local"))
    monkeypatch.setattr(cua_backend, "sandbox_mcp_invocation", lambda: (("ssh", ["cua-driver", "mcp"]), {}))
    monkeypatch.setattr("pm.ensure_import", lambda *_a, **_k: None)
    monkeypatch.setattr(cua_backend_daemon._EmbeddedCuaDaemon, "start",
                        lambda self: pytest.fail("a host daemon was started for an off-gateway screen"))
    backend = cua_backend.CuaDriverBackend("unrestricted")
    with pytest.raises(RuntimeError, match="permission mode 'unrestricted'.*off it"):
        backend.start()


def test_unreadable_config_keeps_a_local_terminal_on_the_gateway_host(monkeypatch):
    """Before the ssh placement a local terminal never read the config here; an unreadable home must still not
    break every browser / cua-driver spawn. Any other backend still surfaces the error."""
    def _broken():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(placement, "_bot_desktop_cfg", _broken)
    monkeypatch.setattr(placement, "_terminal_backend", lambda: "local")
    assert placement.resolve().where == placement.GATEWAY
    monkeypatch.setattr(placement, "_terminal_backend", lambda: "docker")
    with pytest.raises(RuntimeError, match="config unreadable"):
        placement.resolve()


def test_unreadable_config_never_retires_a_screen_recorded_on_a_desktop_host(monkeypatch, offline_ssh):
    """The local-terminal fallback above must not read as "placement moved to gateway" for a recorded SSH screen:
    that would stop the remote screen and run the browser / computer_use here over a config that failed to load."""
    def _broken():
        raise RuntimeError("config unreadable")

    _write_marker()
    monkeypatch.setattr(placement, "_bot_desktop_cfg", _broken)
    monkeypatch.setattr(placement, "_terminal_backend", lambda: "local")
    monkeypatch.setattr(sandbox_host, "stop", lambda e, profile: pytest.fail("the recorded screen was stopped"))
    with pytest.raises(RuntimeError, match="config unreadable"):
        runtime.tool_placement()
    assert sandbox_host._marker().exists()


def test_racing_connection_that_loses_the_cache_leaves_the_shared_master_open(monkeypatch, offline_ssh):
    """Two creators for one (profile, target) bind the same private ControlPath; the dropped one must not
    ``ssh -O exit`` the master the cached one uses."""
    from hermes_constants import hermes_home_key
    _use(monkeypatch)
    target = placement.resolve().ssh
    real = ssh_host.DesktopSSHEnvironment
    winner, loser = real(target), real(target)

    def _construct_while_another_caller_wins(t):
        ssh_host._envs[(hermes_home_key(), t)] = winner  # the other creator finished its dial first
        return loser

    monkeypatch.setattr(ssh_host, "DesktopSSHEnvironment", _construct_while_another_caller_wins)
    exits: list[list[str]] = []
    monkeypatch.setattr(ssh_env.subprocess, "run", lambda cmd, **_: exits.append(cmd))
    Path(_control_path(winner)).touch()
    assert ssh_host.environment(target, create=True) is winner
    loser.cleanup()
    assert exits == [] and Path(_control_path(winner)).exists()
    winner.cleanup()  # the cached one still closes its own master
    assert exits and "-O" in exits[0]


def test_cached_local_cua_backend_is_replaced_once_the_screen_moves_to_the_desktop_host(monkeypatch, offline_ssh):
    """The local children's env never carries the remote DISPLAY, so the cache identity must still change when
    placement moves to ssh; otherwise a long-lived process keeps driving the local seat (or no display)."""
    from tools.computer_use import cua_backend
    _use(monkeypatch, setting="gateway")
    monkeypatch.delenv("DISPLAY", raising=False)
    before = cua_backend.desktop_identity()
    _use(monkeypatch)
    _write_marker()
    monkeypatch.setattr(sandbox_host, "published_env", lambda e, profile: dict(REMOTE_SESSION))
    runtime._sandbox_env(create=True)
    after = cua_backend.desktop_identity()
    assert cua_backend.backend_display_stale(before, after)
    monkeypatch.setattr(runtime, "published_env", lambda: pytest.fail("identity must not exec on the hot path"))
    assert cua_backend.desktop_identity() == after  # stable: the cached backend is kept call after call
    _use(monkeypatch, ssh={**TARGET, "host": "desktops2.example"})
    assert cua_backend.backend_display_stale(after, cua_backend.desktop_identity())  # another desktop host
    _use(monkeypatch, setting="terminal", backend="docker")
    assert cua_backend.backend_display_stale(after, cua_backend.desktop_identity())  # back into the sandbox
