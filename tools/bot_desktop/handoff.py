"""Profile-scoped, single-use authority for handing Bot Desktop to a human.

The browser receives only a short-lived link token and then an HttpOnly session cookie. Persisting
hashes under the profile lock lets the tool process and the viewer server agree on one handoff without
putting either bearer secret on disk.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home, secure_parent_dir

try:
    import fcntl
except ImportError:  # pragma: no cover - same single-process fallback as the desktop lease
    fcntl = None

logger = logging.getLogger(__name__)

_LIVE = frozenset({"pending", "opened"})
_RETAIN_SECONDS = 86400
# The waiting tool heartbeats its live record; a record whose tool died (gateway restart/crash, so
# its `finally` never ran) goes stale and is expired, so a late-opened link cannot hand a human the
# lease with nobody waiting for the hand-back.
HEARTBEAT_STALE_SECONDS = 20.0


class HandoffInvalid(RuntimeError):
    """The supplied link or session no longer names a live handoff."""


def _home(profile_key: Optional[str]) -> Path:
    return Path(profile_key) if profile_key else get_hermes_home()


def _path(profile_key: Optional[str]) -> Path:
    return _home(profile_key) / "bot-desktop" / "handoff.json"


def _private_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    secure_parent_dir(path)


def _open_private(path: str | bytes | os.PathLike, flags: int) -> int:
    return os.open(path, flags, 0o600)


class _locked:
    """Cross-process critical section over the profile handoff file."""

    def __init__(self, path: Path):
        self._lockfile = path.with_suffix(".lock")
        self._fh = None

    def __enter__(self):
        if fcntl is None:
            return self
        _private_dir(self._lockfile)
        self._fh = open(self._lockfile, "a+", encoding="utf-8", opener=_open_private)
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self._fh is None:
            return
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        self._fh.close()


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {"records": []}
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Bot Desktop handoff state is unreadable: {path}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise RuntimeError(f"Bot Desktop handoff state is corrupt: {path}")
    return data


def _write(path: Path, data: dict) -> None:
    _private_dir(path)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8", opener=_open_private) as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def _public(record: dict) -> dict:
    """Return the persisted public fields; neither raw bearer secret is ever stored here."""
    return dict(record)


def _release(record: dict, profile_key: Optional[str]) -> None:
    from tools.bot_desktop import lease

    lease.release(record["viewer_id"], profile_key=profile_key)


def mint(reason: str, *, link_ttl: float, session_max: float,
         profile_key: Optional[str] = None) -> tuple[dict, str]:
    """Create the profile's sole live link, cancelling any earlier pending or open handoff."""
    path = _path(profile_key)
    now = time.time()
    token = secrets.token_urlsafe(32)
    record_id = secrets.token_urlsafe(7)
    record = {
        "id": record_id,
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "session_sha256": None,
        "reason": str(reason),
        "created": now,
        "open_deadline": now + max(0.0, float(link_ttl)),
        "session_deadline": None,
        "state": "pending",
        "viewer_id": f"handoff-{record_id}",
        "heartbeat": now,
    }
    release = []
    with _locked(path):
        data = _read(path)
        for old in data["records"]:
            if isinstance(old, dict) and old.get("state") in _LIVE:
                old["state"] = "cancelled"
                if old.get("viewer_id"):
                    release.append(old)
        # Finished handoffs are kept for a day (status replies after hand-back), then dropped so
        # the file does not grow without bound.
        cutoff = now - _RETAIN_SECONDS
        data["records"] = [r for r in data["records"] if isinstance(r, dict) and (
            r.get("state") in _LIVE or float(r.get("created", 0)) >= cutoff)]
        kept = {r.get("id") for r in data["records"]}
        data["session_max"] = {k: v for k, v in data.get("session_max", {}).items() if k in kept}
        data["records"].append(record)
        # The record contract stays minimal; the requested session lifetime is private metadata
        # needed by the separate viewer process when it spends the link.
        data.setdefault("session_max", {})[record_id] = max(0.0, float(session_max))
        _write(path, data)
    for old in release:
        _release(old, profile_key)
    return _public(record), token


def _find_hash(records: list, secret_hash: str, field: str) -> Optional[dict]:
    match = None
    for record in records:
        stored = record.get(field) if isinstance(record, dict) else None
        # Compare every stored digest so the matching row's position is not observable in timing.
        equal = hmac.compare_digest(str(stored or ""), secret_hash)
        if equal:
            match = record
    return match


def _expire_locked(data: dict, now: float) -> list[dict]:
    expired = []
    for record in data["records"]:
        if not isinstance(record, dict):
            continue
        state = record.get("state")
        stale = state in _LIVE and now - float(record.get("heartbeat", record.get("created", 0))) \
            >= HEARTBEAT_STALE_SECONDS
        due = stale or (state == "pending" and now >= float(record.get("open_deadline", 0))) or (
            state == "opened" and record.get("session_deadline") is not None
            and now >= float(record["session_deadline"]))
        if due:
            record["state"] = "expired"
            expired.append(_public(record))
    return expired


def expire_due(now: Optional[float] = None, *, profile_key: Optional[str] = None) -> list[dict]:
    """Expire due links and sessions, releasing any lease a human still holds; safe to repeat."""
    path = _path(profile_key)
    with _locked(path):
        data = _read(path)
        expired = _expire_locked(data, time.time() if now is None else float(now))
        if expired:
            _write(path, data)
    for record in expired:
        if record.get("session_deadline") is not None:
            _release(record, profile_key)
        logger.info("bot-desktop handoff expired id=%s", record["id"])
    return expired


def redeem(token: str, *, session_max: float = 1800, profile_key: Optional[str] = None) -> tuple[dict, str]:
    """Spend a pending link once and mint a new session secret for the HttpOnly cookie."""
    path = _path(profile_key)
    token_hash = hashlib.sha256(str(token).encode()).hexdigest()
    now = time.time()
    session_secret = secrets.token_urlsafe(32)
    with _locked(path):
        data = _read(path)
        expired = _expire_locked(data, now)
        record = _find_hash(data["records"], token_hash, "token_sha256")
        if expired:
            _write(path, data)
        if not record or record.get("state") != "pending" or now >= float(record["open_deadline"]):
            invalid = True
        else:
            lifetime = data.get("session_max", {}).get(record["id"], session_max)
            record.update(state="opened", session_sha256=hashlib.sha256(session_secret.encode()).hexdigest(),
                          session_deadline=now + max(0.0, float(lifetime)))
            _write(path, data)
            invalid = False
            redeemed = _public(record)
    for item in expired:
        if item.get("session_deadline") is not None:
            _release(item, profile_key)
        logger.info("bot-desktop handoff expired id=%s", item["id"])
    if invalid:
        raise HandoffInvalid("handoff link is invalid, expired or already used")
    return redeemed, session_secret


def _opened_record_locked(data: dict, secret: str, now: float) -> dict:
    session_hash = hashlib.sha256(str(secret).encode()).hexdigest()
    record = _find_hash(data["records"], session_hash, "session_sha256")
    if (not record or record.get("state") != "opened" or record.get("session_deadline") is None
            or now >= float(record["session_deadline"])):
        raise HandoffInvalid("handoff session is invalid or expired")
    return record


def session(secret: str, *, profile_key: Optional[str] = None) -> dict:
    """Resolve a live opened session; a reader also persists due expiry before returning."""
    expire_due(profile_key=profile_key)
    path = _path(profile_key)
    with _locked(path):
        data = _read(path)
        return _public(_opened_record_locked(data, secret, time.time()))


def session_state(secret: str, *, profile_key: Optional[str] = None) -> Optional[dict]:
    """Find the cookie's record even after hand-back, for a precise 410 status response."""
    expire_due(profile_key=profile_key)
    secret_hash = hashlib.sha256(str(secret).encode()).hexdigest()
    with _locked(_path(profile_key)):
        data = _read(_path(profile_key))
        record = _find_hash(data["records"], secret_hash, "session_sha256")
        return _public(record) if record else None


def take_control(secret: str, *, profile_key: Optional[str] = None):
    """Take the lease for this opened handoff, serialized against hand-back and expiry."""
    from tools.bot_desktop import lease

    expire_due(profile_key=profile_key)
    path = _path(profile_key)
    with _locked(path):
        data = _read(path)
        record = _opened_record_locked(data, secret, time.time())
        return lease.acquire(record["viewer_id"], profile_key=profile_key,
                             reason=f"handoff: {record['reason']}")


def hand_back(secret: str, *, profile_key: Optional[str] = None) -> dict:
    """End an opened session and return its lease to the agent."""
    path = _path(profile_key)
    with _locked(path):
        data = _read(path)
        record = _opened_record_locked(data, secret, time.time())
        record["state"] = "handed_back"
        _release(record, profile_key)
        _write(path, data)
        result = _public(record)
    logger.info("bot-desktop handoff handed_back id=%s", result["id"])
    return result


def heartbeat(record_id: str, *, profile_key: Optional[str] = None) -> None:
    """Mark the waiting tool alive for a live record (see ``HEARTBEAT_STALE_SECONDS``)."""
    path = _path(profile_key)
    with _locked(path):
        data = _read(path)
        record = next((r for r in data["records"] if isinstance(r, dict) and r.get("id") == record_id), None)
        if record is not None and record.get("state") in _LIVE:
            record["heartbeat"] = time.time()
            _write(path, data)


def cancel(record_id: str, *, profile_key: Optional[str] = None) -> Optional[dict]:
    """Cancel a tool-owned handoff and release its viewer lease, if any."""
    path = _path(profile_key)
    with _locked(path):
        data = _read(path)
        record = next((r for r in data["records"] if isinstance(r, dict) and r.get("id") == record_id), None)
        if record is None:
            return None
        if record.get("state") in _LIVE:
            record["state"] = "cancelled"
            _release(record, profile_key)
            _write(path, data)
        return _public(record)


def get_state(record_id: str, *, profile_key: Optional[str] = None) -> Optional[dict]:
    """Read one record after expiring anything due in this profile."""
    expire_due(profile_key=profile_key)
    path = _path(profile_key)
    with _locked(path):
        data = _read(path)
        record = next((r for r in data["records"] if isinstance(r, dict) and r.get("id") == record_id), None)
        return _public(record) if record is not None else None
