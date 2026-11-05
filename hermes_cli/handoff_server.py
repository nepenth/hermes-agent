"""Small profile-local viewer for the Bot Desktop's single-use human handoff links.

The reverse proxy may mount this app below an agent-specific path, so the page and its WebSocket use
relative URLs. The RFB pump itself is the same lease-filtered bridge used by the Hermes dashboard.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

COOKIE_NAME = "hermes_handoff"
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'none'"
)
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": _CSP,
}

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Desktop handoff</title><style>
:root{color-scheme:dark;font:15px system-ui,sans-serif;background:#111827;color:#f3f4f6}
*{box-sizing:border-box}body{margin:0;height:100dvh;display:flex;flex-direction:column}
header{display:flex;align-items:center;gap:1rem;padding:.7rem 1rem;background:#1f2937;border-bottom:1px solid #374151}
#identity{flex:1;min-width:0}#agent{font-weight:650}#reason{display:block;color:#cbd5e1;font-size:.9rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#timer{font-variant-numeric:tabular-nums;color:#cbd5e1;white-space:nowrap}button{font:inherit;border:1px solid #64748b;border-radius:.45rem;padding:.5rem .8rem;background:#334155;color:#f8fafc;cursor:pointer}button.primary{background:#b45309;border-color:#d97706}button:disabled{opacity:.5;cursor:default}
#screen{flex:1;min-height:0;background:#030712;display:grid;place-items:center;overflow:hidden}#screen canvas{max-width:100%;max-height:100%;object-fit:contain}
#message{position:fixed;inset:50% auto auto 50%;transform:translate(-50%,-50%);padding:1rem 1.3rem;background:#1f2937;border:1px solid #475569;border-radius:.6rem;max-width:min(90vw,36rem);text-align:center;white-space:pre-wrap}
@media(max-width:650px){header{gap:.5rem;padding:.55rem}button{padding:.48rem .55rem;font-size:.82rem}#reason{max-width:34vw}}
</style></head><body><header>
<div id="identity"><span id="agent">Connecting…</span><span id="reason"></span></div><span id="timer"></span>
<button id="mode" type="button">Watch only</button><button id="handback" class="primary" type="button">Hand back to agent</button>
</header><main id="screen"></main><div id="message" role="status">Opening your secure desktop…</div>
<script type="module" src="./static/handoff.js"></script></body></html>"""


def _load_config() -> dict:
    from hermes_cli.config import load_config

    return load_config() or {}


def _client_allowed(host: Optional[str], allowed: list) -> bool:
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    if not allowed:
        return address.is_loopback
    for value in allowed:
        try:
            network = ipaddress.ip_network(str(value), strict=False)
        except ValueError:
            continue
        if address in network:
            return True
    return False


def _origin_allowed(origin: Optional[str], host: Optional[str], scheme: str, allowed: list) -> bool:
    if not origin:
        return False
    if allowed:
        return origin in {str(item) for item in allowed}
    expected_scheme = "https" if scheme in {"https", "wss"} else "http"
    return origin == f"{expected_scheme}://{host or ''}"


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _handoff_config(config: dict) -> dict:
    return (config.get("bot_desktop") or {}).get("handoff") or {}


def _agent_name(config: dict) -> str:
    display = config.get("display") or {}
    skin = display.get("skin") if isinstance(display, dict) else None
    branding = skin.get("branding") if isinstance(skin, dict) else None
    if isinstance(branding, dict) and branding.get("agent_name"):
        return str(branding["agent_name"])
    return str((config.get("agent") or {}).get("name") or "Hermes Agent")


def _session_payload(config: dict, record: dict) -> dict:
    return {
        "agent": _agent_name(config),
        "reason": record["reason"],
        "session_deadline": record["session_deadline"],
    }


def _expected_origins(handoff_cfg: dict) -> list:
    """Explicit ``allowed_origins`` win; otherwise the browser-facing origin of ``public_url``.

    Behind a TLS-terminating proxy the app sees ``http``/``ws``, so the request's own scheme
    cannot be used to rebuild the browser's Origin.
    """
    from urllib.parse import urlsplit

    allowed = list(handoff_cfg.get("allowed_origins") or [])
    if allowed:
        return allowed
    parts = urlsplit(str(handoff_cfg.get("public_url") or ""))
    return [f"{parts.scheme}://{parts.netloc}"] if parts.scheme and parts.netloc else []


def _origin_for_request(request: Request, config: dict) -> bool:
    return _origin_allowed(
        request.headers.get("origin"), request.headers.get("host"), request.url.scheme,
        _expected_origins(_handoff_config(config)),
    )


def _origin_for_websocket(ws: WebSocket, config: dict) -> bool:
    return _origin_allowed(
        ws.headers.get("origin"), ws.headers.get("host"), ws.url.scheme,
        _expected_origins(_handoff_config(config)),
    )


async def _close_live_sockets(app: FastAPI, record_id: str, reason: str) -> None:
    sockets = app.state.live_sockets.get(record_id, set())
    for ws in tuple(sockets):
        with suppress(Exception):
            await ws.close(code=4401, reason=reason)


async def _ticker(app: FastAPI) -> None:
    from tools.bot_desktop import handoff

    while True:
        # One bad read (e.g. a transiently unreadable state file) must not end expiry enforcement
        # for the life of the server: log and keep ticking.
        try:
            await asyncio.to_thread(handoff.expire_due, profile_key=app.state.profile_home)
            for record_id in tuple(app.state.live_sockets):
                record = await asyncio.to_thread(handoff.get_state, record_id, profile_key=app.state.profile_home)
                if record is None or record.get("state") != "opened":
                    await _close_live_sockets(app, record_id, "handoff session ended")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("bot-desktop handoff expiry tick failed")
        await asyncio.sleep(2)


def _attach_screen_host(home: str) -> None:
    """Open (or reuse) this process's connection to the host running the profile's screen."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop import runtime

    token = set_hermes_home_override(Path(home))
    try:
        if runtime.sandbox_screen_running():
            runtime._screen_env(create=True)
    finally:
        reset_hermes_home_override(token)


def _cookie_path(handoff_cfg: dict) -> str:
    """Scope the session cookie to this agent's mount (``/forge/``) on a shared front-end host.

    Several agents share one hostname behind a path-stripping proxy; a ``Path=/`` cookie with one
    fixed name would let a second agent's handoff overwrite the first one's session in the browser.
    """
    from urllib.parse import urlsplit

    path = urlsplit(str(handoff_cfg.get("public_url") or "")).path or "/"
    return path if path.endswith("/") else path + "/"


def create_app(config: Optional[dict] = None, *, profile_home: Optional[str | Path] = None) -> FastAPI:
    """Build an isolated app for the profile active when ``hermes computer-use handoff-serve`` runs."""
    active_config = config if config is not None else _load_config()
    handoff_cfg = _handoff_config(active_config)
    home = str(profile_home or get_hermes_home())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(_ticker(app), name="bot-desktop-handoff-expiry")
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            for record_id in tuple(app.state.live_sockets):
                await _close_live_sockets(app, record_id, "handoff server stopped")

    app = FastAPI(title="Hermes Bot Desktop Handoff", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.config = active_config
    app.state.handoff_config = handoff_cfg
    app.state.profile_home = home
    app.state.live_sockets = {}

    @app.middleware("http")
    async def gate_and_headers(request: Request, call_next):
        if not _client_allowed(request.client.host if request.client else None,
                                app.state.handoff_config.get("allowed_clients", [])):
            response = _error(403, "client is not allowed")
        else:
            response = await call_next(request)
        for key, value in _SECURITY_HEADERS.items():
            response.headers[key] = value
        return response

    @app.get("/", response_class=HTMLResponse)
    async def viewer_page():
        return HTMLResponse(_PAGE)

    @app.get("/static/handoff.js")
    async def handoff_script():
        from fastapi.responses import FileResponse

        return FileResponse(Path(__file__).with_name("handoff_static") / "handoff.js",
                            media_type="text/javascript")

    novnc_dir = str(handoff_cfg.get("novnc_dir") or "/usr/share/novnc")
    if Path(novnc_dir).is_dir():
        app.mount("/novnc", StaticFiles(directory=novnc_dir), name="novnc")
    else:
        @app.get("/novnc/{_asset_path:path}")
        async def missing_novnc_asset(_asset_path: str):
            return Response("noVNC is not installed", status_code=404)

    # Routes that touch the fcntl-locked state are plain `def` so FastAPI runs them in its threadpool:
    # a tool process holding the lock must not stall the event loop (and the RFB pump on it).
    @app.post("/api/redeem")
    def redeem(request: Request, body: dict):
        if not _origin_for_request(request, app.state.config):
            return _error(403, "origin is not allowed")
        token = body.get("token")
        if not isinstance(token, str) or not token:
            return _error(410, "handoff link is invalid or expired")
        from tools.bot_desktop import handoff

        try:
            record, session_secret = handoff.redeem(
                token, session_max=int(app.state.handoff_config.get("session_max_seconds", 1800)),
                profile_key=home,
            )
        except handoff.HandoffInvalid as exc:
            return _error(410, str(exc))
        logger.info("bot-desktop handoff redeemed id=%s", record["id"])
        response = JSONResponse(_session_payload(app.state.config, record))
        max_age = max(0, int(record["session_deadline"] - time.time()))
        response.set_cookie(COOKIE_NAME, session_secret, httponly=True, secure=True,
                            samesite="strict", path=_cookie_path(app.state.handoff_config),
                            max_age=max_age)
        return response

    def _authorized(request: Request, *, check_origin: bool = True):
        if check_origin and not _origin_for_request(request, app.state.config):
            return None, _error(403, "origin is not allowed")
        secret = request.cookies.get(COOKIE_NAME)
        if not secret:
            return None, _error(401, "handoff session cookie required")
        from tools.bot_desktop import handoff

        try:
            return handoff.session(secret, profile_key=home), None
        except handoff.HandoffInvalid:
            previous = handoff.session_state(secret, profile_key=home)
            if previous:
                return None, _error(410, "handoff session has ended")
            return None, _error(401, "handoff session is invalid")

    @app.get("/api/status")
    def status(request: Request):
        record, error = _authorized(request, check_origin=False)
        if error:
            return error
        return JSONResponse(_session_payload(app.state.config, record))

    @app.post("/api/takeover")
    def takeover(request: Request):
        record, error = _authorized(request)
        if error:
            return error
        from tools.bot_desktop import handoff

        secret = request.cookies[COOKIE_NAME]
        try:
            handoff.take_control(secret, profile_key=home)
        except handoff.HandoffInvalid:
            return _error(410, "handoff session has ended")
        logger.info("bot-desktop handoff takeover id=%s", record["id"])
        return JSONResponse({**_session_payload(app.state.config, record), "holding_control": True})

    @app.post("/api/release")
    def release(request: Request):
        record, error = _authorized(request)
        if error:
            return error
        from tools.bot_desktop import lease

        lease.release(record["viewer_id"], profile_key=home)
        return JSONResponse({**_session_payload(app.state.config, record), "holding_control": False})

    @app.post("/api/handback")
    async def handback(request: Request):
        _record, error = await asyncio.to_thread(_authorized, request)
        if error:
            return error
        from tools.bot_desktop import handoff

        try:
            final = await asyncio.to_thread(handoff.hand_back, request.cookies[COOKIE_NAME], profile_key=home)
        except handoff.HandoffInvalid:
            return _error(410, "handoff session has ended")
        await _close_live_sockets(app, final["id"], "handed back to agent")
        return JSONResponse({"state": "handed_back", "agent": _agent_name(app.state.config)})

    @app.websocket("/ws")
    async def viewer_ws(ws: WebSocket):
        if not _client_allowed(ws.client.host if ws.client else None,
                               app.state.handoff_config.get("allowed_clients", [])):
            await ws.close(code=4403, reason="client is not allowed")
            return
        if not _origin_for_websocket(ws, app.state.config):
            await ws.close(code=4403, reason="origin is not allowed")
            return
        await ws.accept()
        from tools.bot_desktop import handoff

        secret = ws.cookies.get(COOKIE_NAME)
        try:
            record = await asyncio.to_thread(handoff.session, secret or "", profile_key=home)
        except handoff.HandoffInvalid:
            await ws.close(code=4401, reason="handoff session is invalid or expired")
            return
        sockets = app.state.live_sockets.setdefault(record["id"], set())
        sockets.add(ws)
        try:
            # The screen's relay host (ssh desktop / sandbox) is attached per process: the gateway's
            # connection is not visible here, and display._open_rfb only reuses an already-open one
            # (create=False). Attach it off the event loop first; the ControlPath is keyed on
            # home+target, so this rides the gateway's ssh master when one exists.
            try:
                await asyncio.to_thread(_attach_screen_host, home)
            except (RuntimeError, OSError) as exc:  # EnvironmentConnectionError is a RuntimeError
                logger.warning("bot-desktop handoff could not attach the screen host: %s", exc)
                await ws.close(code=4001, reason="Bot Desktop is not running")
                return
            from hermes_cli.web_routers.display import _bridge

            await _bridge(ws, {"hermes_home": home, "viewer_id": record["viewer_id"]})
        finally:
            sockets.discard(ws)
            if not sockets:
                app.state.live_sockets.pop(record["id"], None)

    return app


def serve(*, host: Optional[str] = None, port: Optional[int] = None) -> None:
    """Run the handoff app for the active CLI profile."""
    import uvicorn

    config = _load_config()
    handoff_cfg = _handoff_config(config)
    app = create_app(config)
    uvicorn.run(app, host=host or str(handoff_cfg.get("bind_host") or "127.0.0.1"),
                port=int(port if port is not None else handoff_cfg.get("port", 8650)), log_level="info")
