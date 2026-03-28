"""Persistent shell mixin: file-based IPC protocol for long-lived bash shells."""

import logging
import shlex
import subprocess
import threading
import time
import uuid
from abc import abstractmethod

from tools.interrupt import is_interrupted

logger = logging.getLogger(__name__)

_SENTINEL_PREFIX = "__HERMES_DONE_"
_SENTINEL_SUFFIX = "__"


class PersistentShellMixin:
    """Mixin that adds persistent shell capability to any BaseEnvironment.

    Subclasses must implement ``_spawn_shell_process()``, ``_read_temp_files()``,
    ``_kill_shell_children()``, ``_execute_oneshot()``, and ``_cleanup_temp_files()``.
    """

    persistent: bool

    @abstractmethod
    def _spawn_shell_process(self) -> subprocess.Popen: ...

    @abstractmethod
    def _read_temp_files(self, *paths: str) -> list[str]: ...

    @abstractmethod
    def _kill_shell_children(self): ...

    @abstractmethod
    def _execute_oneshot(self, command: str, cwd: str, *,
                         timeout: int | None = None,
                         stdin_data: str | None = None) -> dict: ...

    @abstractmethod
    def _cleanup_temp_files(self): ...

    _session_id: str = ""

    @property
    def _temp_prefix(self) -> str:
        return f"/tmp/hermes-persistent-{self._session_id}"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _init_persistent_shell(self):
        self._shell_lock = threading.Lock()
        self._shell_proc: subprocess.Popen | None = None
        self._shell_alive: bool = False
        self._shell_pid: int | None = None
        self._sentinel_event = threading.Event()
        self._sentinel_cmd_id: str | None = None

        self._session_id = uuid.uuid4().hex[:12]
        p = self._temp_prefix
        self._pshell_stdout = f"{p}-stdout"
        self._pshell_stderr = f"{p}-stderr"
        self._pshell_status = f"{p}-status"
        self._pshell_cwd = f"{p}-cwd"
        self._pshell_pid_file = f"{p}-pid"

        self._shell_proc = self._spawn_shell_process()
        self._shell_alive = True

        self._drain_thread = threading.Thread(
            target=self._drain_shell_output, daemon=True,
        )
        self._drain_thread.start()

        init_cmd_id = "init"
        init_script = (
            # Disable echo so sentinel markers aren't duplicated in stdout
            f"stty -echo 2>/dev/null\n"
            f"export TERM=${{TERM:-dumb}}\n"
            f"touch {self._pshell_stdout} {self._pshell_stderr} "
            f"{self._pshell_status} {self._pshell_cwd} {self._pshell_pid_file}\n"
            f"echo $$ > {self._pshell_pid_file}\n"
            f"pwd > {self._pshell_cwd}\n"
            f"echo '{_SENTINEL_PREFIX}{init_cmd_id}{_SENTINEL_SUFFIX}'\n"
        )
        self._sentinel_event.clear()
        self._sentinel_cmd_id = init_cmd_id
        self._send_to_shell(init_script)

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if self._sentinel_event.wait(timeout=min(remaining, 0.5)):
                break
        else:
            logger.warning("Persistent shell init sentinel not received")

        pid_str, reported_cwd = self._read_temp_files(
            self._pshell_pid_file, self._pshell_cwd,
        )
        pid_str = pid_str.strip()
        if pid_str.isdigit():
            self._shell_pid = int(pid_str)
            logger.info(
                "Persistent shell started (session=%s, pid=%d)",
                self._session_id, self._shell_pid,
            )
        else:
            logger.warning("Could not read persistent shell PID")
            self._shell_pid = None

        reported_cwd = reported_cwd.strip()
        if reported_cwd:
            self.cwd = reported_cwd

    def _cleanup_persistent_shell(self):
        if self._shell_proc is None:
            return

        if self._session_id:
            self._cleanup_temp_files()

        try:
            self._shell_proc.stdin.close()
        except Exception:
            pass
        try:
            self._shell_proc.terminate()
            self._shell_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._shell_proc.kill()

        self._shell_alive = False
        self._shell_proc = None

        if hasattr(self, "_drain_thread") and self._drain_thread.is_alive():
            self._drain_thread.join(timeout=1.0)

    # ------------------------------------------------------------------
    # execute() / cleanup() — shared dispatcher, subclasses inherit
    # ------------------------------------------------------------------

    def execute(self, command: str, cwd: str = "", *,
                timeout: int | None = None,
                stdin_data: str | None = None) -> dict:
        if self.persistent:
            return self._execute_persistent(
                command, cwd, timeout=timeout, stdin_data=stdin_data,
            )
        return self._execute_oneshot(
            command, cwd, timeout=timeout, stdin_data=stdin_data,
        )

    def cleanup(self):
        if self.persistent:
            self._cleanup_persistent_shell()

    # ------------------------------------------------------------------
    # Shell I/O
    # ------------------------------------------------------------------

    def _drain_shell_output(self):
        try:
            for line in self._shell_proc.stdout:
                stripped = line.rstrip('\r\n')
                if (
                    stripped.startswith(_SENTINEL_PREFIX)
                    and stripped.endswith(_SENTINEL_SUFFIX)
                ):
                    inner = stripped[len(_SENTINEL_PREFIX):-len(_SENTINEL_SUFFIX)]
                    if inner == self._sentinel_cmd_id:
                        self._sentinel_event.set()
        except Exception:
            pass
        self._shell_alive = False
        self._sentinel_event.set()

    def _send_to_shell(self, text: str):
        if not self._shell_alive or self._shell_proc is None:
            return
        try:
            self._shell_proc.stdin.write(text)
            self._shell_proc.stdin.flush()
        except (BrokenPipeError, OSError):
            self._shell_alive = False

    def _read_persistent_output(self) -> tuple[str, int, str]:
        stdout, stderr, status_raw, cwd = self._read_temp_files(
            self._pshell_stdout, self._pshell_stderr,
            self._pshell_status, self._pshell_cwd,
        )
        output = self._merge_output(stdout, stderr)
        status = status_raw.strip()
        if ":" in status:
            status = status.split(":", 1)[1]
        try:
            exit_code = int(status.strip())
        except ValueError:
            exit_code = 1
        return output, exit_code, cwd.strip()

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _execute_persistent(self, command: str, cwd: str, *,
                            timeout: int | None = None,
                            stdin_data: str | None = None) -> dict:
        if not self._shell_alive:
            logger.info("Persistent shell died, restarting...")
            self._init_persistent_shell()

        exec_command, sudo_stdin = self._prepare_command(command)
        effective_timeout = timeout or self.timeout
        if stdin_data or sudo_stdin:
            return self._execute_oneshot(
                command, cwd, timeout=timeout, stdin_data=stdin_data,
            )

        with self._shell_lock:
            return self._execute_persistent_locked(
                exec_command, cwd, effective_timeout,
            )

    def _execute_persistent_locked(self, command: str, cwd: str,
                                   timeout: int) -> dict:
        work_dir = cwd or self.cwd
        cmd_id = uuid.uuid4().hex[:8]
        truncate = (
            f": > {self._pshell_stdout}\n"
            f": > {self._pshell_stderr}\n"
            f": > {self._pshell_status}\n"
        )
        self._send_to_shell(truncate)
        escaped = command.replace("'", "'\\''")

        ipc_script = (
            f"cd {shlex.quote(work_dir)}\n"
            f"eval '{escaped}' < /dev/null > {self._pshell_stdout} 2> {self._pshell_stderr}\n"
            f"__EC=$?\n"
            f"pwd > {self._pshell_cwd}\n"
            f"echo {cmd_id}:$__EC > {self._pshell_status}\n"
            f"echo '{_SENTINEL_PREFIX}{cmd_id}{_SENTINEL_SUFFIX}'\n"
        )
        self._sentinel_event.clear()
        self._sentinel_cmd_id = cmd_id
        self._send_to_shell(ipc_script)
        deadline = time.monotonic() + timeout

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._kill_shell_children()
                output, _, _ = self._read_persistent_output()
                if output:
                    return {
                        "output": output + f"\n[Command timed out after {timeout}s]",
                        "returncode": 124,
                    }
                return self._timeout_result(timeout)

            if is_interrupted():
                self._kill_shell_children()
                output, _, _ = self._read_persistent_output()
                return {
                    "output": output + "\n[Command interrupted]",
                    "returncode": 130,
                }

            if not self._shell_alive:
                return {
                    "output": "Persistent shell died during execution",
                    "returncode": 1,
                }

            if self._sentinel_event.wait(timeout=min(remaining, 0.5)):
                break

        output, exit_code, new_cwd = self._read_persistent_output()
        if new_cwd:
            self.cwd = new_cwd
        return {"output": output, "returncode": exit_code}

    @staticmethod
    def _merge_output(stdout: str, stderr: str) -> str:
        parts = []
        if stdout.strip():
            parts.append(stdout.rstrip("\n"))
        if stderr.strip():
            parts.append(stderr.rstrip("\n"))
        return "\n".join(parts)
