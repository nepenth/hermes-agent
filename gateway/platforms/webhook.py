"""Generic webhook inbound platform adapter.

Runs a lightweight HTTP server that accepts POST /message requests and
routes them through the gateway as regular conversations.  Each unique
``chat_id`` in the request gets its own session — supporting multiple
concurrent agents/conversations.

The response is returned synchronously in the HTTP response body (the
connection is held open until the agent finishes).  This makes it
trivially easy for external bridges, automation tools, or other agent
frameworks to integrate with Hermes.

Enable via env var:
    WEBHOOK_PORT=4568  (any port number enables the adapter)

API:
    POST /message
    {
        "chat_id": "hermes-1",        // required — maps to a session
        "message": "Hello!",          // required — the message text
        "from":    "other-agent",     // optional — sender display name
        "user_id": "agent-123"        // optional — sender ID
    }

    Response (200):
    {
        "ok": true,
        "response": "Hi there!",
        "session_id": "20260312_..."
    }

    GET /health
    {"ok": true, "adapter": "webhook", "port": 4568}
"""

import asyncio
import json
import logging
import os
import time

from aiohttp import web

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    SendResult,
    SessionSource,
)

logger = logging.getLogger(__name__)


def check_webhook_requirements() -> bool:
    """Webhook adapter is available when WEBHOOK_PORT is set."""
    return bool(os.getenv("WEBHOOK_PORT"))


class WebhookAdapter(BasePlatformAdapter):
    """HTTP webhook adapter — accepts POST requests as inbound messages.

    External services (bridges, automation tools, other agents) POST
    messages and receive the agent's response in the HTTP response body.
    Each chat_id maps to a separate gateway session.
    """

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.WEBHOOK)
        self.port = int(os.getenv("WEBHOOK_PORT", "4568"))
        self._app: web.Application = None
        self._runner: web.AppRunner = None
        self._site: web.TCPSite = None
        # Accumulated responses keyed by session_key — we always want the LAST
        # send() call (the final agent response), not intermediate notifications.
        self._response_accumulators: dict[str, list[str]] = {}
        self._response_events: dict[str, asyncio.Event] = {}

    async def connect(self) -> bool:
        """Start the HTTP server."""
        self._app = web.Application()
        self._app.router.add_post("/message", self._handle_post)
        self._app.router.add_get("/health", self._handle_health)

        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()

        try:
            self._site = web.TCPSite(self._runner, "0.0.0.0", self.port)
            await self._site.start()
        except OSError as e:
            logger.error("Webhook adapter failed to bind port %d: %s", self.port, e)
            return False

        print(f"[webhook] Listening on port {self.port}")
        print(f"[webhook]   POST http://localhost:{self.port}/message")
        return True

    async def disconnect(self) -> None:
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()

    async def send(self, chat_id: str, content: str,
                   reply_to: str = None, metadata: dict = None) -> SendResult:
        """Accumulate responses — the last one is the final agent response."""
        from gateway.session import build_session_key

        # Find the accumulator for this chat_id or session_key
        acc = self._response_accumulators.get(chat_id)
        evt = self._response_events.get(chat_id)
        if acc is None:
            source = self.build_source(chat_id=chat_id, chat_type="dm")
            sk = build_session_key(source)
            acc = self._response_accumulators.get(sk)
            evt = self._response_events.get(sk)

        if acc is not None:
            acc.append(content)
            if evt:
                evt.set()  # signal that we have at least one response

        return SendResult(success=True, message_id=str(int(time.time())))

    async def send_typing(self, chat_id: str, metadata: dict = None) -> None:
        pass  # No typing indicator for webhooks

    async def get_chat_info(self, chat_id: str) -> dict:
        return {"id": chat_id, "name": f"webhook:{chat_id}", "type": "dm"}

    # ── HTTP Handlers ────────────────────────────────────────────────────

    async def _handle_post(self, request: web.Request) -> web.Response:
        """Accept an inbound message and return the agent's response."""
        try:
            data = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response(
                {"ok": False, "error": "Invalid JSON"}, status=400
            )

        chat_id = data.get("chat_id", "").strip()
        message = data.get("message", "").strip()

        if not chat_id or not message:
            return web.json_response(
                {"ok": False, "error": "Missing required fields: chat_id, message"},
                status=400,
            )

        from_name = data.get("from", "webhook")
        user_id = data.get("user_id", from_name)

        # Prepend sender info if provided
        display_message = message
        if from_name and from_name != "webhook":
            display_message = f"[Message from {from_name}]: {message}"

        # Build source and event
        source = self.build_source(
            chat_id=chat_id,
            chat_type="dm",
            user_id=user_id,
            user_name=from_name,
        )

        from gateway.session import build_session_key
        session_key = build_session_key(source)

        event = MessageEvent(
            text=display_message,
            source=source,
            message_id=str(int(time.time() * 1000)),
        )

        # Set up response accumulator — send() appends here, we take the last
        response_list: list[str] = []
        done_event = asyncio.Event()
        self._response_accumulators[session_key] = response_list
        self._response_accumulators[chat_id] = response_list
        self._response_events[session_key] = done_event
        self._response_events[chat_id] = done_event

        # Submit the message for processing (spawns background task)
        await self.handle_message(event)

        # Wait for the agent to finish.  The background task in
        # _process_message_background removes session_key from
        # _active_sessions when done.  We poll for that + accumulator.
        try:
            deadline = asyncio.get_event_loop().time() + 300
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                # Wait for at least one send() call
                try:
                    await asyncio.wait_for(done_event.wait(), timeout=min(remaining, 2.0))
                except asyncio.TimeoutError:
                    pass
                # Check if the session is done processing
                if session_key not in self._active_sessions and response_list:
                    # Processing finished — give a tiny grace period for
                    # any final send() calls to arrive
                    await asyncio.sleep(0.2)
                    break

            if response_list:
                # Return the LAST response (the final agent response)
                return web.json_response({
                    "ok": True,
                    "response": response_list[-1],
                    "chat_id": chat_id,
                })
            else:
                return web.json_response(
                    {"ok": False, "error": "Agent timed out (300s)", "chat_id": chat_id},
                    status=504,
                )
        finally:
            self._response_accumulators.pop(session_key, None)
            self._response_accumulators.pop(chat_id, None)
            self._response_events.pop(session_key, None)
            self._response_events.pop(chat_id, None)

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({
            "ok": True,
            "adapter": "webhook",
            "port": self.port,
        })
