"""The SSH desktop host of ``bot_desktop.placement: ssh``.

The screen, cua-driver and the bot's browser run on a machine of their own while the agent's terminal stays
wherever ``terminal.backend`` puts it. Everything downstream (``sandbox_host``, the RFB relay, the cua MCP
spawn, the sandboxed browser's port forward) already speaks to "an environment with an exec prefix", so this
module only has to produce one: an :class:`SSHEnvironment` reduced to the exec channel.

What it deliberately does NOT inherit from the ssh terminal backend:

- **No file sync.** The terminal backend mirrors ``~/.hermes`` (skills, credentials) to its host so commands
  there can use them. A desktop host only runs the screen; it gets nothing from the gateway's home.
- **No session snapshot.** The snapshot runs through ``_run_bash``, which forwards the profile's passthrough
  secrets with ``SendEnv``. Nothing here runs terminal commands, so the desktop host never receives them.
- **Runtime state in the login's home, not ``/tmp``.** One desktop host may serve several agents, each logged
  in as its own user; a shared ``/tmp/hermes-bot-desktop`` would belong to whichever login created it first.
- **No shared ControlMaster.** The terminal backend's socket is keyed by ``user@host:port`` in a shared temp
  dir, and ``ControlMaster=auto`` attaches to an existing master without re-checking ``-i``: two profiles naming
  the same login with different keys would ride one authenticated session, and either's ``-O exit`` would cut
  the other. This connection's socket lives in a ``0700`` directory under the profile's ``HERMES_HOME``, named
  by a hash over that home and the whole target (key included); a home too deep for a Unix socket path
  falls back to a per-uid ``0700`` directory in the temp dir, still named per profile. Agent and X11 forwarding are off and
  configured port forwards cleared: nothing here needs them (the browser's DevTools port rides the exec
  channel, ``streams.forward_port``), and a desktop host shared by several agents must not reach back.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from tools.bot_desktop.placement import SshTarget
from tools.environments.base import EnvironmentConnectionError
from tools.environments.ssh import SSHEnvironment

# OpenSSH binds the master at ControlPath plus a 17-byte random suffix and refuses a path that, so suffixed,
# overflows sun_path (104 bytes on macOS, 108 on Linux). A deep HERMES_HOME falls back to a per-uid 0700
# directory in the temp dir; the socket name still hashes the profile home, so profiles never meet there.
_CONTROL_PATH_MAX = 104 - 17 - 1
# An unreachable target is not re-dialled for this long: status, thumbnail and the idle check each run the
# full ConnectTimeout otherwise, every call.
UNREACHABLE_RETRY_S = 30.0


class DesktopSSHEnvironment(SSHEnvironment):
    """Exec channel to ``target`` over a ControlMaster of its own (see the module doc)."""

    def __init__(self, target: SshTarget):
        self.target = target
        super().__init__(host=target.host, user=target.user, port=target.port,
                         key_path=os.path.expanduser(target.key) if target.key else "",
                         cwd="~", sync_files=False, persistent=False)

    def _control_socket_path(self, socket_key: str) -> Path:
        from hermes_constants import get_hermes_home, hermes_home_key
        ident = "\0".join((hermes_home_key(), socket_key, self.key_path))
        name = f"{hashlib.sha256(ident.encode()).hexdigest()[:16]}.sock"
        directory = get_hermes_home() / "bot-desktop" / "ssh"
        if len(os.fsencode(str(directory / name))) > _CONTROL_PATH_MAX:
            directory = Path(tempfile.gettempdir()) / f"hermes-bd-ssh-{os.getuid() if hasattr(os, 'getuid') else 0}"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink() or not directory.is_dir():
            # The temp-dir fallback is a predictable path: never chmod (or bind a socket) through a planted link.
            raise EnvironmentConnectionError(f"Bot Desktop SSH control directory {directory} is not a plain directory")
        os.chmod(directory, 0o700)  # mkdir's mode is umask-filtered and skipped for an existing directory
        self.control_dir = directory
        return directory / name

    def _build_ssh_command(self, extra_args: list | None = None, send_env=()) -> list:
        hardening = ["-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-o", "ClearAllForwardings=yes"]
        if self.key_path:
            hardening += ["-o", "IdentitiesOnly=yes"]  # the configured key, not whatever the agent offers first
        return super()._build_ssh_command([*hardening, *(extra_args or [])], send_env)

    def cleanup(self):
        # A racing creator that lost the cache binds the winner's ControlPath (same home, same target): its
        # ``ssh -O exit`` would cut the connection the winner is using.
        if getattr(self, "_shares_master", False):
            return
        super().cleanup()

    def init_session(self):
        # The snapshot is for terminal commands, and building it forwards passthrough secrets (see module doc).
        self._snapshot_ready = False

    def get_temp_dir(self) -> str:
        # BaseEnvironment.__init__ asks before the home probe ran, for snapshot/cwd scratch paths this exec-only
        # environment never uses; every desktop path is derived after construction.
        home = getattr(self, "_remote_home", "")
        return f"{home.rstrip('/')}/.cache" if home else super().get_temp_dir()


# One connection object per (profile home, target): profiles served by one process must never share a
# desktop login by accident, and a changed target gets a fresh connection instead of the old host's.
_envs: dict[tuple[str, SshTarget], DesktopSSHEnvironment] = {}
# (profile home, target) -> (when it failed, the failure): re-raised without dialling until UNREACHABLE_RETRY_S.
_unreachable: dict[tuple[str, SshTarget], tuple[float, EnvironmentConnectionError]] = {}
_lock = threading.Lock()


def environment(target: SshTarget, *, create: bool = True) -> Optional[DesktopSSHEnvironment]:
    """The connection to ``target`` for the active profile. ``create=False`` only returns an open one; creating
    raises ``EnvironmentConnectionError`` (a ``RuntimeError``) when the host cannot be reached, and again,
    without dialling, for ``UNREACHABLE_RETRY_S`` after that."""
    from hermes_constants import hermes_home_key
    key = (hermes_home_key(), target)
    with _lock:
        env = _envs.get(key)
        if env is not None or not create:
            return env
        failed = _unreachable.get(key)
        if failed is not None and time.monotonic() - failed[0] < UNREACHABLE_RETRY_S:
            raise EnvironmentConnectionError(failed[1].reason, retry_hint=failed[1].retry_hint)
    # Connect outside the lock: an unreachable host costs ConnectTimeout, and other profiles must not wait it out.
    # A racing creator for the same key binds the same ControlPath (it hashes the same home and target), so the
    # loser is dropped without closing that master.
    try:
        env = DesktopSSHEnvironment(target)
    except EnvironmentConnectionError as exc:
        with _lock:
            _unreachable[key] = (time.monotonic(), exc)
        raise
    with _lock:
        _unreachable.pop(key, None)
        winner = _envs.setdefault(key, env)
    if winner is not env:
        env._shares_master = True  # set while still referenced: its __del__ -> cleanup() must not exit the winner's master
    return winner
