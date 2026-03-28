"""
Capability verification test for pwn-dojo RL infrastructure.

Verifies that RL containers are provisioned with the correct Linux capabilities,
resource limits, and host configuration for each challenge type.

Usage:
    python environments/pwncollege_env/stress_test.py -y
    python environments/pwncollege_env/stress_test.py -y -o report.json --verbose
"""

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

_repo_root = Path(__file__).resolve().parent.parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from environments.pwncollege_env.sdk import DojoRLClient


@dataclass
class SSHConfig:
    host: str
    port: int
    key: str


@dataclass
class CheckResult:
    name: str
    passed: bool
    message: str
    duration: float = 0.0


@dataclass
class TestResult:
    name: str
    challenge: str
    checks: list[CheckResult] = field(default_factory=list)
    passed: bool = False
    skipped: bool = False
    error: str | None = None
    duration: float = 0.0


@dataclass
class TestCase:
    name: str
    challenge: str
    checks: list


async def ssh_run(
    cfg: SSHConfig, user: str, command: str, timeout: float = 30.0
) -> tuple[int, str]:
    """Run a command over SSH via subprocess. Returns (returncode, output)."""
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "LogLevel=ERROR",
        "-p",
        str(cfg.port),
        "-i",
        cfg.key,
        f"{user}@{cfg.host}",
        command,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, stdout.decode(errors="replace")
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, f"[SSH timeout after {timeout}s]"


async def wait_ssh_ready(cfg: SSHConfig, user: str, retries: int = 10) -> bool:
    for i in range(retries):
        rc, out = await ssh_run(cfg, user, "echo ready", timeout=10)
        if rc == 0 and "ready" in out:
            return True
        await asyncio.sleep(1)
    return False


# ── Check functions ──────────────────────────────────────────────────────────


async def check_ssh_echo(cfg: SSHConfig, user: str) -> CheckResult:
    t0 = time.monotonic()
    rc, out = await ssh_run(cfg, user, "echo ok")
    dur = time.monotonic() - t0
    if rc == 0 and "ok" in out:
        return CheckResult("ssh_echo", True, "connected", dur)
    return CheckResult("ssh_echo", False, f"rc={rc}: {out.strip()[:100]}", dur)


async def check_unshare_net(cfg: SSHConfig, user: str) -> CheckResult:
    t0 = time.monotonic()
    rc, out = await ssh_run(cfg, user, "unshare --net echo ok")
    dur = time.monotonic() - t0
    if rc == 0 and "ok" in out:
        return CheckResult("unshare_net", True, "namespace creation works", dur)
    return CheckResult("unshare_net", False, f"rc={rc}: {out.strip()[:120]}", dur)


async def check_unshare_user(cfg: SSHConfig, user: str) -> CheckResult:
    t0 = time.monotonic()
    rc, out = await ssh_run(cfg, user, "unshare --user --map-root-user bash -c 'id'")
    dur = time.monotonic() - t0
    if rc == 0 and "uid=0" in out:
        return CheckResult("unshare_user", True, "user namespace works", dur)
    return CheckResult("unshare_user", False, f"rc={rc}: {out.strip()[:120]}", dur)


async def check_capeff(cfg: SSHConfig, user: str) -> CheckResult:
    """Check that the container init (PID 1) has SYS_ADMIN capability."""
    t0 = time.monotonic()
    rc, out = await ssh_run(cfg, user, "cat /proc/1/status")
    dur = time.monotonic() - t0
    if rc != 0:
        return CheckResult(
            "capeff", False, f"Cannot read /proc/1/status: {out.strip()[:80]}", dur
        )
    for line in out.splitlines():
        if line.startswith("CapEff:") or line.startswith("CapBnd:"):
            hex_val = line.split(":")[1].strip()
            try:
                val = int(hex_val, 16)
                has_sysadmin = bool(val & (1 << 21))
                if has_sysadmin:
                    label = line.split(":")[0]
                    return CheckResult(
                        "capeff", True, f"{label}={hex_val} has SYS_ADMIN", dur
                    )
            except ValueError:
                pass
    return CheckResult(
        "capeff", False, "SYS_ADMIN (bit 21) not found in capabilities", dur
    )


async def check_hosts_resolution(cfg: SSHConfig, user: str) -> CheckResult:
    t0 = time.monotonic()
    rc, out = await ssh_run(cfg, user, "getent hosts challenge.localhost")
    dur = time.monotonic() - t0
    if rc == 0 and out.strip():
        return CheckResult(
            "hosts_resolution", True, f"resolves to {out.strip()[:40]}", dur
        )
    rc2, out2 = await ssh_run(cfg, user, "grep challenge.localhost /etc/hosts")
    dur = time.monotonic() - t0
    if rc2 == 0 and "challenge.localhost" in out2:
        return CheckResult(
            "hosts_resolution", True, "/etc/hosts has entry", dur
        )
    return CheckResult(
        "hosts_resolution", False, "challenge.localhost not resolvable", dur
    )


async def check_pids_limit(cfg: SSHConfig, user: str) -> CheckResult:
    t0 = time.monotonic()
    rc, out = await ssh_run(
        cfg,
        user,
        "cat /sys/fs/cgroup/pids.max 2>/dev/null || cat /sys/fs/cgroup/pids/pids.max 2>/dev/null",
    )
    dur = time.monotonic() - t0
    val = out.strip()
    if val == "max":
        return CheckResult("pids_limit", True, "unlimited", dur)
    try:
        limit = int(val)
        if limit >= 1024:
            return CheckResult("pids_limit", True, f"pids_limit={limit}", dur)
        return CheckResult(
            "pids_limit", False, f"pids_limit={limit} (need >= 1024)", dur
        )
    except ValueError:
        return CheckResult("pids_limit", False, f"Cannot parse: {val[:60]}", dur)


async def check_mem_limit(cfg: SSHConfig, user: str) -> CheckResult:
    t0 = time.monotonic()
    rc, out = await ssh_run(
        cfg,
        user,
        "cat /sys/fs/cgroup/memory.max 2>/dev/null || cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null",
    )
    dur = time.monotonic() - t0
    val = out.strip()
    if val == "max":
        return CheckResult("mem_limit", True, "unlimited", dur)
    try:
        limit = int(val)
        limit_gb = limit / (1024**3)
        if (
            limit_gb >= 1.8
        ):  # 2GB for privileged RL containers (not 4GB to manage memory pressure)
            return CheckResult("mem_limit", True, f"mem={limit_gb:.1f}GB", dur)
        return CheckResult(
            "mem_limit", False, f"mem={limit_gb:.1f}GB (need >= 2GB)", dur
        )
    except ValueError:
        return CheckResult("mem_limit", False, f"Cannot parse: {val[:60]}", dur)


async def check_challenge_run(cfg: SSHConfig, user: str) -> CheckResult:
    """Run /challenge/run and verify no PermissionError."""
    t0 = time.monotonic()
    rc, out = await ssh_run(cfg, user, "/challenge/run < /dev/null", timeout=15)
    dur = time.monotonic() - t0
    if "PermissionError" in out or "Operation not permitted" in out:
        snippet = [l for l in out.splitlines() if "Permission" in l or "Operation" in l]
        return CheckResult(
            "challenge_run",
            False,
            snippet[0][:120] if snippet else "PermissionError",
            dur,
        )
    return CheckResult("challenge_run", True, f"No permission errors (rc={rc})", dur)


# ── Test cases ───────────────────────────────────────────────────────────────

TEST_CASES = [
    TestCase("unprivileged_basic", "hello/hello", [check_ssh_echo]),
    TestCase(
        "privileged_caps",
        "intercepting-communication/udp-1",
        [check_ssh_echo, check_capeff],
    ),
    TestCase(
        "privileged_challenge_run",
        "intercepting-communication/udp-1",
        [check_challenge_run],
    ),
    TestCase(
        "web_challenge_hosts",
        "web-security/path-traversal-1",
        [check_ssh_echo, check_hosts_resolution],
    ),
    TestCase(
        "resource_limits",
        "intercepting-communication/udp-1",
        [check_pids_limit, check_mem_limit],
    ),
]


# ── Runner ───────────────────────────────────────────────────────────────────


async def run_tests(args) -> dict:
    cfg = SSHConfig(host=args.ssh_host, port=args.ssh_port, key=args.ssh_key)
    client = DojoRLClient(args.base_url)

    status = await client.status()
    print(
        f"Server: {args.base_url} (RL={'enabled' if status.enabled else 'DISABLED'}, "
        f"{status.max_instances} max, {status.running} running)"
    )
    if status.running > 0:
        n = await client.destroy_all()
        print(f"Cleaned up {n} instance(s)")
    print()

    results: list[TestResult] = []
    test_num = 0
    total = len(TEST_CASES) + (0 if args.skip_concurrent else 1)
    start_time = time.monotonic()

    for tc in TEST_CASES:
        test_num += 1
        t0 = time.monotonic()
        tr = TestResult(name=tc.name, challenge=tc.challenge)
        print(f"[{test_num}/{total}] {tc.name} ({tc.challenge})")

        try:
            inst = await client.create_instance(tc.challenge)
        except Exception as e:
            err = str(e)
            if "404" in err or "not found" in err.lower() or "Invalid" in err:
                tr.skipped = True
                tr.error = f"Challenge not available: {err[:80]}"
                print(f"  SKIP  {tr.error}")
            else:
                tr.error = f"create_instance failed: {err[:100]}"
                print(f"  ERR   {tr.error}")
            tr.duration = time.monotonic() - t0
            results.append(tr)
            print(f"  --- {'SKIP' if tr.skipped else 'FAIL'} ({tr.duration:.1f}s)\n")
            continue

        try:
            ready = await wait_ssh_ready(cfg, inst.ssh_user)
            if not ready:
                tr.error = "SSH not ready after 10 retries"
                tr.checks.append(
                    CheckResult("ssh_ready", False, tr.error, time.monotonic() - t0)
                )
                print(f"  FAIL  ssh_ready: {tr.error}")
            else:
                for check_fn in tc.checks:
                    cr = await check_fn(cfg, inst.ssh_user)
                    tr.checks.append(cr)
                    tag = "PASS" if cr.passed else "FAIL"
                    extra = f"  ({cr.message})" if args.verbose or not cr.passed else ""
                    print(f"  {tag}  {cr.name:30s} {cr.duration:.1f}s{extra}")
                    if not cr.passed:
                        break
        finally:
            try:
                await client.destroy_instance(inst.slot)
            except Exception as e:
                print(f"  WARN  destroy failed: {e}")

        tr.passed = all(c.passed for c in tr.checks) and not tr.error
        tr.duration = time.monotonic() - t0
        results.append(tr)
        print(f"  --- {'PASS' if tr.passed else 'FAIL'} ({tr.duration:.1f}s)\n")

    if not args.skip_concurrent:
        test_num += 1
        t0 = time.monotonic()
        tr = TestResult(name="concurrent_lifecycle", challenge="8x hello/hello")
        n_concurrent = min(8, status.max_instances)
        print(
            f"[{test_num}/{total}] concurrent_lifecycle ({n_concurrent}x hello/hello)"
        )

        try:
            ct0 = time.monotonic()
            tasks = [client.create_instance("hello/hello") for _ in range(n_concurrent)]
            instances = await asyncio.gather(*tasks, return_exceptions=True)
            create_dur = time.monotonic() - ct0

            created = [i for i in instances if not isinstance(i, Exception)]
            errors = [i for i in instances if isinstance(i, Exception)]
            if errors:
                tr.checks.append(
                    CheckResult(
                        "create_all",
                        False,
                        f"{len(errors)}/{n_concurrent} failed: {errors[0]}",
                        create_dur,
                    )
                )
            else:
                tr.checks.append(
                    CheckResult(
                        "create_all", True, f"{n_concurrent} created", create_dur
                    )
                )

            if created:
                await asyncio.sleep(3)
                et0 = time.monotonic()
                echo_tasks = [
                    ssh_run(cfg, i.ssh_user, "echo ok", timeout=15) for i in created
                ]
                echo_results = await asyncio.gather(*echo_tasks, return_exceptions=True)
                echo_ok = sum(
                    1
                    for r in echo_results
                    if not isinstance(r, Exception) and r[0] == 0
                )
                tr.checks.append(
                    CheckResult(
                        "ssh_echo_all",
                        echo_ok == len(created),
                        f"{echo_ok}/{len(created)} connected",
                        time.monotonic() - et0,
                    )
                )

            dt0 = time.monotonic()
            destroyed = await client.destroy_all()
            tr.checks.append(
                CheckResult(
                    "destroy_all",
                    True,
                    f"destroyed {destroyed}",
                    time.monotonic() - dt0,
                )
            )

            st = await client.status()
            live = sum(1 for i in st.instances if i.status == "running")
            tr.checks.append(
                CheckResult(
                    "slot_cleanup",
                    live == 0,
                    f"running={live} (total listed={st.running})",
                    0.0,
                )
            )
        except Exception as e:
            tr.error = str(e)[:200]
            tr.checks.append(CheckResult("concurrent", False, str(e)[:100], 0.0))

        tr.passed = all(c.passed for c in tr.checks) and not tr.error
        tr.duration = time.monotonic() - t0
        results.append(tr)
        for cr in tr.checks:
            tag = "PASS" if cr.passed else "FAIL"
            extra = f"  ({cr.message})" if args.verbose or not cr.passed else ""
            print(f"  {tag}  {cr.name:30s} {cr.duration:.1f}s{extra}")
        print(f"  --- {'PASS' if tr.passed else 'FAIL'} ({tr.duration:.1f}s)\n")

    total_dur = time.monotonic() - start_time
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed and not r.skipped)
    skipped = sum(1 for r in results if r.skipped)

    print("=" * 50)
    parts = [f"{passed}/{len(results)} passed"]
    if failed:
        parts.append(f"{failed} failed")
    if skipped:
        parts.append(f"{skipped} skipped")
    print(f"RESULTS: {', '.join(parts)} in {total_dur:.0f}s")
    print("=" * 50)

    return {
        "test": "capability_verification",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "server": args.base_url,
        "summary": {
            "total": len(results),
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "duration_seconds": round(total_dur, 1),
        },
        "tests": [
            {
                "name": r.name,
                "challenge": r.challenge,
                "passed": r.passed,
                "skipped": r.skipped,
                "error": r.error,
                "duration": round(r.duration, 1),
                "checks": [asdict(c) for c in r.checks],
            }
            for r in results
        ],
    }


def main():
    parser = argparse.ArgumentParser(
        description="Capability verification test for pwn-dojo RL infrastructure",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--base-url", default="http://100.120.55.25:8080")
    parser.add_argument("--ssh-host", default="100.120.55.25")
    parser.add_argument("--ssh-port", type=int, default=2222)
    parser.add_argument(
        "--ssh-key", default="environments/pwncollege_env/keys/rl_test_key"
    )
    parser.add_argument("--output", "-o", help="Write JSON report")
    parser.add_argument("--skip-concurrent", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    args = parser.parse_args()

    key = Path(args.ssh_key)
    if not key.exists():
        key = _repo_root / args.ssh_key
    if not key.exists():
        print(f"SSH key not found: {args.ssh_key}")
        sys.exit(1)
    args.ssh_key = str(key)

    if not args.yes:
        print(f"Will test against {args.base_url}")
        if input("Continue? [y/N] ").lower() != "y":
            sys.exit(0)

    report = asyncio.run(run_tests(args))

    if args.output:
        with open(args.output, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nJSON report: {args.output}")

    sys.exit(0 if report["summary"]["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
