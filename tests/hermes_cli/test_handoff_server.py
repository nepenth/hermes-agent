"""HTTP and WebSocket contracts for the standalone Bot Desktop handoff viewer."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tools.bot_desktop import handoff, lease

_ORIGIN = "https://desktop.example.test"


def _config(**overrides):
    handoff_cfg = {
        "public_url": "https://desktop.example.test/agent-a/",
        "allowed_clients": [],
        "allowed_origins": [_ORIGIN],
        "novnc_dir": "/no/such/novnc",
        "link_ttl_seconds": 60,
        "session_max_seconds": 120,
    }
    handoff_cfg.update(overrides)
    return {"bot_desktop": {"handoff": handoff_cfg}}


@pytest.fixture(autouse=True)
def _clean_lease():
    lease._reset_for_tests()
    yield
    lease._reset_for_tests()


def _client(config=None):
    from hermes_cli.handoff_server import create_app

    return TestClient(create_app(config or _config()), base_url="https://127.0.0.1",
                      client=("127.0.0.1", 56000))


def _redeem(client, token, *, origin=_ORIGIN):
    return client.post("/api/redeem", json={"token": token}, headers={"Origin": origin})


def test_client_gate_and_origin_rejection():
    denied = _client(_config(allowed_clients=["192.0.2.20/32"]))
    assert denied.get("/").status_code == 403

    client = _client()
    _, token = handoff.mint("Sign in", link_ttl=60, session_max=120)
    response = _redeem(client, token, origin="https://attacker.example")
    assert response.status_code == 403
    assert response.json() == {"error": "origin is not allowed"}


def test_redeem_cookie_flags_single_use_takeover_handback_and_status():
    client = _client()
    record, token = handoff.mint("Enter the one-time code", link_ttl=60, session_max=120)

    redeemed = _redeem(client, token)

    assert redeemed.status_code == 200
    cookie = redeemed.headers["set-cookie"].lower()
    assert "hermes_handoff=" in cookie
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
    # Scoped to this agent's mount on the shared front-end host (public_url path), not Path=/.
    assert "path=/agent-a/" in cookie
    assert redeemed.json()["reason"] == "Enter the one-time code"
    assert _redeem(client, token).status_code == 410
    # The browser sends it on /agent-a/api/*; the proxy strips /agent-a, so replay it at the app root.
    secret = redeemed.cookies.get("hermes_handoff")
    assert secret
    client.cookies.clear()
    client.cookies.set("hermes_handoff", secret)

    takeover = client.post("/api/takeover", json={}, headers={"Origin": _ORIGIN})
    assert takeover.status_code == 200
    assert lease.get().holder == lease.HUMAN
    assert lease.get().viewer_id == record["viewer_id"]

    returned = client.post("/api/handback", json={}, headers={"Origin": _ORIGIN})
    assert returned.status_code == 200
    assert returned.json()["state"] == "handed_back"
    assert lease.get().holder == lease.AGENT
    assert client.get("/api/status").status_code == 410


def test_security_headers_and_novnc_path_traversal_refusal(tmp_path):
    from pathlib import Path

    novnc_dir = tmp_path / "novnc"
    novnc_dir.mkdir()
    (Path(tmp_path) / "secret.txt").write_text("private", encoding="utf-8")
    client = _client(_config(novnc_dir=str(novnc_dir)))

    page = client.get("/")

    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert page.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    traversal = client.get("/novnc/%2e%2e/secret.txt")
    assert traversal.status_code != 200
    assert "private" not in traversal.text


def test_websocket_rejects_missing_cookie_and_bridge_receives_profile_identity(monkeypatch):
    client = _client()
    # The app accepts, then closes with an application code (mirrors display.py), so the
    # rejection surfaces on the first receive.
    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect("/ws", headers={"Origin": _ORIGIN}) as ws:
            ws.receive_bytes()
    assert rejected.value.code == 4401

    record, token = handoff.mint("Complete login", link_ttl=60, session_max=120)
    redeemed = _redeem(client, token)
    secret = redeemed.cookies["hermes_handoff"]
    client.post("/api/takeover", json={}, headers={"Origin": _ORIGIN})
    received = {}

    async def bridge(ws, info):
        received.update(info)
        await ws.close(code=1000)

    monkeypatch.setattr("hermes_cli.handoff_server._attach_screen_host", lambda home: None)
    monkeypatch.setattr("hermes_cli.web_routers.display._bridge", bridge)
    with client.websocket_connect("/ws", headers={
        "Origin": _ORIGIN, "Cookie": f"hermes_handoff={secret}",
    }) as ws:
        # Session lookup and host attach run off the loop; wait for the (stub) bridge's close.
        assert ws.receive()["type"] == "websocket.close"
    assert received == {"hermes_home": str(handoff._home(None)), "viewer_id": record["viewer_id"]}


def test_csp_restricts_connect_to_self_and_origin_falls_back_to_public_url():
    client = _client(_config(allowed_origins=[]))
    page = client.get("/")
    assert "connect-src 'self';" in page.headers["content-security-policy"]
    _, token = handoff.mint("Sign in", link_ttl=60, session_max=120)
    # Behind TLS-terminating Caddy the app sees http; the expected Origin comes from public_url.
    assert _redeem(client, token, origin="https://desktop.example.test").status_code == 200
    _, token2 = handoff.mint("Sign in", link_ttl=60, session_max=120)
    assert _redeem(client, token2, origin="http://desktop.example.test").status_code == 403


def test_websocket_attaches_screen_host_before_bridging(monkeypatch):
    import hermes_cli.handoff_server as server

    client = _client()
    _, token = handoff.mint("Complete login", link_ttl=60, session_max=120)
    secret = _redeem(client, token).cookies["hermes_handoff"]
    order = []
    monkeypatch.setattr(server, "_attach_screen_host", lambda home: order.append(("attach", home)))

    async def bridge(ws, info):
        order.append(("bridge", info["hermes_home"]))
        await ws.close(code=1000)

    monkeypatch.setattr("hermes_cli.web_routers.display._bridge", bridge)
    with client.websocket_connect("/ws", headers={"Origin": _ORIGIN, "Cookie": f"hermes_handoff={secret}"}) as ws:
        assert ws.receive()["type"] == "websocket.close"
    assert [step for step, _ in order] == ["attach", "bridge"]
    assert order[0][1] == order[1][1]


def test_websocket_closes_4001_when_screen_host_unavailable(monkeypatch):
    import hermes_cli.handoff_server as server

    client = _client()
    _, token = handoff.mint("Complete login", link_ttl=60, session_max=120)
    secret = _redeem(client, token).cookies["hermes_handoff"]

    def boom(home):
        raise RuntimeError("desktop host unreachable")

    monkeypatch.setattr(server, "_attach_screen_host", boom)
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect("/ws", headers={"Origin": _ORIGIN, "Cookie": f"hermes_handoff={secret}"}) as ws:
            ws.receive_bytes()
    assert closed.value.code == 4001


def test_wake_endpoint_starts_screen_and_reports_failure(monkeypatch):
    import hermes_cli.handoff_server as server

    client = _client()
    _, token = handoff.mint("Complete login", link_ttl=60, session_max=120)
    secret = _redeem(client, token).cookies["hermes_handoff"]
    client.cookies.clear()
    client.cookies.set("hermes_handoff", secret)
    calls = []
    monkeypatch.setattr(server, "_wake_screen", lambda home: calls.append(home))
    ok = client.post("/api/wake", json={}, headers={"Origin": _ORIGIN})
    assert ok.status_code == 200 and ok.json() == {"desktop": "running"}
    assert len(calls) == 1

    def boom(home):
        raise RuntimeError("desktop host unreachable")

    monkeypatch.setattr(server, "_wake_screen", boom)
    failed = client.post("/api/wake", json={}, headers={"Origin": _ORIGIN})
    assert failed.status_code == 503
    assert "could not be started" in failed.json()["error"]


def test_wake_endpoint_requires_a_session():
    client = _client()
    assert client.post("/api/wake", json={}, headers={"Origin": _ORIGIN}).status_code == 401


def test_wake_screen_starts_an_idle_stopped_off_gateway_screen(monkeypatch, tmp_path):
    import hermes_cli.handoff_server as server
    from tools.bot_desktop import runtime

    events = []
    monkeypatch.setattr(runtime, "in_sandbox", lambda: True)
    monkeypatch.setattr(runtime, "sandbox_screen_running", lambda: False)
    monkeypatch.setattr(runtime, "start", lambda: events.append("start"))
    monkeypatch.setattr(runtime, "touch_activity", lambda: events.append("touch"))
    server._wake_screen(str(tmp_path))
    assert events == ["start", "touch"]


def test_wake_screen_leaves_a_running_screen_alone(monkeypatch, tmp_path):
    import hermes_cli.handoff_server as server
    from tools.bot_desktop import runtime

    events = []
    monkeypatch.setattr(runtime, "in_sandbox", lambda: True)
    monkeypatch.setattr(runtime, "sandbox_screen_running", lambda: True)
    monkeypatch.setattr(runtime, "start", lambda: events.append("start"))
    monkeypatch.setattr(runtime, "touch_activity", lambda: events.append("touch"))
    server._wake_screen(str(tmp_path))
    assert events == ["touch"]


def test_attach_wakes_before_attaching(monkeypatch, tmp_path):
    import hermes_cli.handoff_server as server
    from tools.bot_desktop import runtime

    order = []
    monkeypatch.setattr(server, "_wake_screen", lambda home: order.append("wake"))
    monkeypatch.setattr(runtime, "sandbox_screen_running", lambda: True)
    monkeypatch.setattr(runtime, "_screen_env", lambda create: order.append(("env", create)))
    server._attach_screen_host(str(tmp_path))
    assert order == ["wake", ("env", True)]
