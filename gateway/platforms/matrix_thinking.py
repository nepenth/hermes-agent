"""Matrix thinking / tool-use collapsible field manager.

Provides live-updating <details> blocks for agentic workflows in Matrix rooms.
Uses only stable, spec-compliant primitives:
  - m.room.message with org.matrix.custom.html + <details><summary>
  - Live message edits via m.replace relation + m.new_content

No server changes, no matrix-nio forks, no new dependencies.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from gateway.platforms.matrix import MatrixAdapter

logger = logging.getLogger(__name__)

# Rate limit: minimum seconds between edit updates
_MIN_EDIT_INTERVAL = 3.0

# Maximum HTML body size (bytes) to prevent oversized events
_MAX_BODY_SIZE = 60_000


@dataclass
class ThinkingSession:
    """Tracks one active thinking field per task."""

    room_id: str
    event_id: str
    task_id: str
    started_at: float
    last_update: float
    step_count: int = 0
    content_lines: list = field(default_factory=list)
    finalized: bool = False


class ThinkingManager:
    """Manages collapsible thinking fields for the Matrix adapter.

    Thread-safe via asyncio.Lock.  One thinking session per task_id.
    """

    def __init__(self, adapter: "MatrixAdapter"):
        self._adapter = adapter
        self._sessions: Dict[str, ThinkingSession] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(
        self,
        room_id: str,
        task_id: str,
        initial_summary: str = "Processing request...",
    ) -> Optional[str]:
        """Send initial thinking block and return event_id, or None on failure."""
        import nio

        now = time.time()
        html_body = self._build_html(
            summary=initial_summary,
            step=0,
            ts=now,
            content_html="<em>Thinking…</em>",
            open_tag=True,
        )
        plaintext = f"🤔 Hermes Agent is thinking — {initial_summary} (expand for details)"
        content = self._msg_content(html_body, plaintext)

        try:
            resp = await asyncio.wait_for(
                self._adapter._client.room_send(
                    room_id,
                    "m.room.message",
                    content,
                    ignore_unverified_devices=True,
                ),
                timeout=30,
            )
        except Exception as exc:
            logger.error("Matrix thinking: failed to start in %s: %s", room_id, exc)
            return None

        if not isinstance(resp, nio.RoomSendResponse):
            logger.error(
                "Matrix thinking: unexpected response %s",
                getattr(resp, "message", resp),
            )
            return None

        event_id = resp.event_id
        async with self._lock:
            self._sessions[task_id] = ThinkingSession(
                room_id=room_id,
                event_id=event_id,
                task_id=task_id,
                started_at=now,
                last_update=now,
            )

        logger.info("Matrix thinking started in %s (task %s, event %s)", room_id, task_id, event_id)
        return event_id

    async def update(
        self,
        task_id: str,
        step_info: str,
        content_md: str = "",
    ) -> None:
        """Live-update the thinking block.  Rate-limited to avoid flooding."""
        async with self._lock:
            session = self._sessions.get(task_id)
            if not session or session.finalized:
                return

            now = time.time()
            if now - session.last_update < _MIN_EDIT_INTERVAL:
                return  # throttle

            session.step_count += 1
            session.last_update = now

            # Accumulate content
            if content_md:
                session.content_lines.append(content_md)

            # Build snapshot under lock
            step = session.step_count
            event_id = session.event_id
            room_id = session.room_id
            lines = list(session.content_lines)

        # Build HTML outside lock
        content_html = self._lines_to_html(lines)
        elapsed = self._elapsed_str(session.started_at)

        html_body = self._build_html(
            summary=step_info,
            step=step,
            ts=time.time(),
            content_html=content_html,
            open_tag=True,
            elapsed=elapsed,
        )
        plaintext = f"🤔 Step {step} — {step_info} ({elapsed})"
        edit_content = self._edit_content(event_id, html_body, plaintext)

        try:
            await asyncio.wait_for(
                self._adapter._client.room_send(
                    room_id,
                    "m.room.message",
                    edit_content,
                    ignore_unverified_devices=True,
                ),
                timeout=15,
            )
        except Exception as exc:
            logger.debug("Matrix thinking: update failed for task %s: %s", task_id, exc)

    async def finalize(
        self,
        task_id: str,
        final_summary: str = "Task complete",
        collapse: bool = True,
    ) -> None:
        """Close the thinking block and optionally collapse it."""
        async with self._lock:
            session = self._sessions.get(task_id)
            if not session:
                return
            session.finalized = True
            event_id = session.event_id
            room_id = session.room_id
            step = session.step_count
            lines = list(session.content_lines)

        elapsed = self._elapsed_str(session.started_at)
        content_html = self._lines_to_html(lines)

        html_body = self._build_html(
            summary=f"✅ {final_summary}",
            step=step,
            ts=time.time(),
            content_html=content_html,
            open_tag=not collapse,
            elapsed=elapsed,
            final=True,
        )
        plaintext = f"✅ {final_summary} ({step} steps, {elapsed})"
        edit_content = self._edit_content(event_id, html_body, plaintext)

        try:
            await asyncio.wait_for(
                self._adapter._client.room_send(
                    room_id,
                    "m.room.message",
                    edit_content,
                    ignore_unverified_devices=True,
                ),
                timeout=15,
            )
        except Exception as exc:
            logger.debug("Matrix thinking: finalize failed for task %s: %s", task_id, exc)

        # Clean up
        async with self._lock:
            self._sessions.pop(task_id, None)

        logger.info("Matrix thinking finalized for task %s (%s)", task_id, elapsed)

    async def abort(self, task_id: str, reason: str = "Aborted") -> None:
        """Abort a thinking session (error / timeout)."""
        async with self._lock:
            session = self._sessions.get(task_id)
            if not session:
                return
            session.finalized = True
            event_id = session.event_id
            room_id = session.room_id
            step = session.step_count
            lines = list(session.content_lines)

        elapsed = self._elapsed_str(session.started_at)
        content_html = self._lines_to_html(lines)

        html_body = self._build_html(
            summary=f"⚠️ {reason}",
            step=step,
            ts=time.time(),
            content_html=content_html,
            open_tag=False,
            elapsed=elapsed,
            final=True,
        )
        plaintext = f"⚠️ {reason} ({step} steps, {elapsed})"
        edit_content = self._edit_content(event_id, html_body, plaintext)

        try:
            await asyncio.wait_for(
                self._adapter._client.room_send(
                    room_id,
                    "m.room.message",
                    edit_content,
                    ignore_unverified_devices=True,
                ),
                timeout=15,
            )
        except Exception:
            pass

        async with self._lock:
            self._sessions.pop(task_id, None)

    def has_session(self, task_id: str) -> bool:
        """Check if a thinking session exists (sync-safe, approximate)."""
        return task_id in self._sessions

    async def cleanup_stale(self, max_age: float = 1800) -> None:
        """Remove sessions older than max_age seconds."""
        now = time.time()
        async with self._lock:
            stale = [
                tid
                for tid, s in self._sessions.items()
                if now - s.started_at > max_age
            ]
            for tid in stale:
                self._sessions.pop(tid, None)
                logger.warning("Matrix thinking: cleaned up stale session %s", tid)

    # ------------------------------------------------------------------
    # HTML generation
    # ------------------------------------------------------------------

    def _build_html(
        self,
        summary: str,
        step: int,
        ts: float,
        content_html: str,
        open_tag: bool = True,
        elapsed: str = "",
        final: bool = False,
    ) -> str:
        """Build sanitized HTML for <details> block."""
        open_attr = " open" if open_tag else ""
        timestamp = time.strftime("%H:%M:%S", time.localtime(ts))

        step_info = f"Step {step}" if step > 0 else "Starting"
        elapsed_info = f" • {elapsed}" if elapsed else ""

        # Truncate content if too large
        if len(content_html.encode("utf-8")) > _MAX_BODY_SIZE:
            content_html = content_html[: _MAX_BODY_SIZE] + "\n… (truncated)"

        result = (
            f"<details{open_attr}>"
            f"<summary>🤔 <strong>Hermes Agent</strong> "
            f"({step_info}{elapsed_info} • {timestamp}) — "
            f"{html.escape(summary)}</summary>"
            f"<pre><code>{content_html}</code></pre>"
            f"</details>"
        )
        return result

    def _lines_to_html(self, lines: list) -> str:
        """Convert accumulated content lines to escaped HTML."""
        if not lines:
            return ""
        # Escape each line individually for safety
        return "\n".join(html.escape(line) for line in lines)

    @staticmethod
    def _elapsed_str(started_at: float) -> str:
        """Human-readable elapsed time."""
        elapsed = time.time() - started_at
        if elapsed < 60:
            return f"{elapsed:.0f}s"
        minutes = int(elapsed // 60)
        seconds = int(elapsed % 60)
        return f"{minutes}m{seconds}s"

    # ------------------------------------------------------------------
    # Message content builders
    # ------------------------------------------------------------------

    @staticmethod
    def _msg_content(html_body: str, plaintext: str) -> Dict[str, Any]:
        """Build m.room.message content dict with HTML formatting."""
        return {
            "msgtype": "m.text",
            "body": plaintext,
            "format": "org.matrix.custom.html",
            "formatted_body": html_body,
        }

    @staticmethod
    def _edit_content(
        original_event_id: str, html_body: str, plaintext: str
    ) -> Dict[str, Any]:
        """Build m.replace edit content dict."""
        new_content = {
            "msgtype": "m.text",
            "body": plaintext,
            "format": "org.matrix.custom.html",
            "formatted_body": html_body,
        }
        return {
            **new_content,
            "body": f"* {plaintext}",
            "formatted_body": f"* {html_body}",
            "m.new_content": new_content,
            "m.relates_to": {
                "rel_type": "m.replace",
                "event_id": original_event_id,
            },
        }
