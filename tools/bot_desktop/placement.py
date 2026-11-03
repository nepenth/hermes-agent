"""Where this profile's desktop lives: the gateway host, inside the terminal backend, or on a dedicated SSH host.

``bot_desktop.placement``:
  ``auto``      (default) follow the terminal backend when it is a sandbox that can host a stream
                (docker / ssh, or a plugin backend declaring ``supports_streams``); the
                gateway host when ``terminal.backend`` is local. A
                sandbox backend that CANNOT host one (modal, daytona, vercel) resolves to ``refused``:
                the user chose a sandbox for the agent's actions, so quietly running the screen, cua-driver
                and the browser on the host beside it would hand the agent a desktop outside that sandbox.
  ``terminal``  always the terminal backend; error when it cannot host one.
  ``gateway``   always the gateway host (the pre-#108914 behaviour for sandboxed users, now an explicit
                opt-in because it is the boundary-crossing shape).
  ``ssh``       a desktop host of its own, reached over SSH as ``bot_desktop.ssh`` says, whatever
                ``terminal.backend`` is: the agent's shell stays where it is (often the gateway host) while
                its screen, cua-driver and browser run on a separate machine or VM. An incomplete
                ``bot_desktop.ssh`` block resolves to ``refused``, never to the gateway host.

``resolve()`` is pure config + backend-class reasoning: it never starts a sandbox or dials a host.
``desktop_environment()`` does acquire the environment hosting the screen (creating the terminal container, or
opening the SSH connection, if needed) because a screen cannot exist before the machine it runs on does.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

GATEWAY = "gateway"
TERMINAL = "terminal"
SSH = "ssh"
REFUSED = "refused"
_STREAM_BACKENDS = ("docker", "ssh")
# X display numbers a dedicated host may assign. Hosts shared by several agents give each its own login
# and its own number: /tmp/.X<n>-lock is machine-wide, and the launcher never takes a number whose
# lock belongs to a live server.
_DISPLAY_RANGE = range(1, 1000)


@dataclass(frozen=True)
class SshTarget:
    """``bot_desktop.ssh``: the desktop host and the login the profile's screen runs as there."""
    host: str
    user: str
    port: int = 22
    key: str = ""
    display: int = 20

    def label(self) -> str:
        return f"{self.user}@{self.host}" + (f":{self.port}" if self.port != 22 else "")

    def to_marker(self) -> dict[str, Any]:
        return {"host": self.host, "user": self.user, "port": self.port, "key": self.key, "display": self.display}

    @classmethod
    def from_marker(cls, marker: dict[str, Any]) -> Optional[SshTarget]:
        """The target a host-side screen marker records, or None for a terminal-sandbox marker (no
        ``ssh_target``). Raises ``ValueError`` for an ``ssh_target`` that does not parse: a corrupt record is a
        broken marker, never a terminal-sandbox screen looked for (and stopped) on the wrong machine."""
        if "ssh_target" not in marker:
            return None
        raw = marker["ssh_target"]
        target, problem = _parse_ssh_target(raw) if isinstance(raw, dict) else (None, "not a mapping")
        if target is None:
            raise ValueError(f"the screen marker's ssh_target is unreadable ({problem})")
        return target


@dataclass(frozen=True)
class Placement:
    where: str            # GATEWAY | TERMINAL | SSH | REFUSED
    backend: str          # terminal.backend as configured
    reason: str = ""      # human sentence when REFUSED (or why TERMINAL was not possible)
    ssh: Optional[SshTarget] = None  # set when where == SSH

    @property
    def off_gateway(self) -> bool:
        """The screen runs on another machine than the gateway (terminal sandbox or SSH desktop host), so
        everything that acts on it goes through that machine's exec channel."""
        return self.where in (TERMINAL, SSH)


def _bot_desktop_cfg() -> dict[str, Any]:
    from hermes_cli.config import load_config_readonly
    cfg = load_config_readonly().get("bot_desktop") or {}
    return cfg if isinstance(cfg, dict) else {}


def _setting() -> str:
    value = str(_bot_desktop_cfg().get("placement") or "auto").strip().lower()
    return value if value in ("auto", TERMINAL, GATEWAY, SSH) else "auto"


def _ssh_cfg() -> dict[str, Any]:
    raw = _bot_desktop_cfg().get("ssh") or {}
    return raw if isinstance(raw, dict) else {}


def _parse_ssh_target(raw: dict[str, Any]) -> tuple[Optional[SshTarget], str]:
    """``(target, "")`` or ``(None, what is wrong)``. Strict on purpose: a typo here must refuse, not
    land the screen somewhere the user did not name."""
    host = str(raw.get("host") or "").strip()
    user = str(raw.get("user") or "").strip()
    missing = [name for name, value in (("host", host), ("user", user)) if not value]
    if missing:
        names = " and ".join(f"bot_desktop.ssh.{name}" for name in missing)
        return None, f"{names} {'is' if len(missing) == 1 else 'are'} not set"
    if host.startswith("-") or user.startswith("-") or any(c.isspace() or c == "@" for c in host + user):
        return None, "bot_desktop.ssh.host / user must be a plain host name and login (no spaces, '@' or leading '-')"
    if ":" in host:
        # ``user@<v6 literal>`` is not a destination OpenSSH parses; brackets only work in ssh:// URIs.
        return None, "bot_desktop.ssh.host must be a DNS name or IPv4 address (IPv6 literals are not supported)"
    numbers: dict[str, int] = {}
    for name, default, valid in (("port", 22, range(1, 65536)), ("display", 20, _DISPLAY_RANGE)):
        value = raw.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).strip().isdigit() \
                or int(value) not in valid:
            return None, f"bot_desktop.ssh.{name} must be a whole number in {valid.start}-{valid.stop - 1}"
        numbers[name] = int(value)
    key = str(raw.get("key") or "").strip()
    if key.startswith("-") or any(c.isspace() for c in key):
        # Its own argv word after -i today; rejected anyway so no later shell join can turn it into an option.
        return None, "bot_desktop.ssh.key must be a plain file path (no whitespace or leading '-')"
    return SshTarget(host=host, user=user, port=numbers["port"], key=key, display=numbers["display"]), ""


def _terminal_backend() -> str:
    from tools.terminal_tool import _get_env_config
    return str(_get_env_config().get("env_type") or "local")


def _plugin_streams(backend: str) -> bool:
    from tools.terminal_tool_config import _plugin_env_flag
    return _plugin_env_flag(backend, "supports_streams") is True


def _plugin_host_isolated() -> bool:
    # Under host isolation a plugin's environment object reaches Hermes as an opaque placeholder, so its
    # exec_prefix() is unreachable. (No bundled plugin backend declares supports_streams, so bundled
    # plugins, which stay in-process, are not a case here.)
    from hermes_cli.plugin_isolation import ISOLATION_HOST, isolation_mode
    return isolation_mode() == ISOLATION_HOST


def resolve() -> Placement:
    backend = _terminal_backend()
    try:
        # A cached config read (this runs on every browser / cua-driver spawn); needed even for a local
        # terminal now, because only the setting can say the screen lives on an SSH desktop host.
        setting = _setting()
    except Exception:
        if backend != "local":
            raise
        from tools.bot_desktop import sandbox_host
        if sandbox_host._read_marker().get("ssh_target"):
            # A screen recorded on a desktop host: falling back to the gateway would retire it (and run the
            # browser / computer_use here) over a config that merely could not be read. Fail instead.
            raise
        # A local terminal IS the gateway host, which is where the screen ran before the setting was read at
        # all: a config that cannot be loaded (an unreadable home) must not turn that into a failure.
        logger.debug("bot_desktop placement: config unreadable, local terminal resolves to the gateway host",
                     exc_info=True)
        return Placement(GATEWAY, backend, "terminal.backend is local, so the terminal IS the gateway host")
    if setting == SSH:
        # Independent of terminal.backend by design: the common shape is a local terminal with the screen on
        # a desktop VM. Checked before the local shortcut below, which would otherwise put it on the gateway.
        target, problem = _parse_ssh_target(_ssh_cfg())
        if target is None:
            return Placement(REFUSED, backend, (
                f"bot_desktop.placement is ssh but {problem}, so there is no desktop host to put the screen on. "
                f"Complete bot_desktop.ssh, or set bot_desktop.placement: gateway to run the desktop on the "
                f"gateway host."))
        return Placement(SSH, backend, ssh=target)
    if backend == "local":
        return Placement(GATEWAY, backend, "terminal.backend is local, so the terminal IS the gateway host")
    if setting == GATEWAY:
        return Placement(GATEWAY, backend)
    if backend in _STREAM_BACKENDS:
        return Placement(TERMINAL, backend)
    if _plugin_streams(backend):
        if not _plugin_host_isolated():
            return Placement(TERMINAL, backend)
        return Placement(REFUSED, backend, (
            f"terminal.backend is {backend}, a plugin that runs in the plugin host (plugins.isolation: host), "
            f"and the host cannot hand its sandbox's exec channel to Hermes, so the screen cannot follow it. "
            f"Set plugins.isolation: in_process, or bot_desktop.placement: gateway to run the desktop on the "
            f"gateway host."))
    reason = (f"terminal.backend is {backend}, which cannot host a screen yet, and running the desktop on the "
              f"gateway host would put the agent's screen, browser and computer_use outside the sandbox you chose "
              f"for it. Set bot_desktop.placement: gateway to allow that explicitly.")
    return Placement(REFUSED, backend, reason)


def terminal_environment(*, create: bool = True) -> Optional[Any]:
    """This profile's terminal environment object (the one ``terminal`` runs commands in). ``create=False``
    only returns an already-running one."""
    from tools import terminal_tool as tt
    task_id = tt._resolve_container_task_id(None)
    with tt._env_lock:
        env = tt._lookup_active_env(task_id, None)
    if env is not None or not create:
        return env
    # Acquire through the same planner the terminal tool uses so the container carries the same mounts,
    # limits and identity label; a trivial command warms it.
    tt.terminal_tool("true", task_id=None)
    with tt._env_lock:
        return tt._lookup_active_env(task_id, None)


def desktop_environment(*, create: bool = True) -> Optional[Any]:
    """The environment the current placement puts this profile's screen in: the SSH desktop host under
    ``ssh``, the terminal backend's otherwise. ``create=False`` never connects or builds anything."""
    where = resolve()
    if where.ssh is not None:
        from tools.bot_desktop import ssh_host
        return ssh_host.environment(where.ssh, create=create)
    return terminal_environment(create=create)
