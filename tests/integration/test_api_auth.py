from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.api.auth import WebAuthMiddleware
from src.api.routes import auth, websocket
from src.services.web_auth_service import SESSION_COOKIE, WebAuthService


@pytest.fixture()
def auth_context():
    settings = SimpleNamespace(
        web_username="admin", web_password="test-password", web_session_ttl_seconds=60,
        web_cookie_secure=False, web_allowed_origins="",
    )
    now = [100.0]
    sessions = WebAuthService(settings_provider=lambda: settings, clock=lambda: now[0])
    app = FastAPI()
    app.state.web_auth_service = sessions
    app.add_middleware(WebAuthMiddleware, sessions=sessions)
    app.include_router(auth.router)
    app.include_router(websocket.router)

    @app.get("/api/accounts/demo")
    def account():
        return {"content": "private-cookie-data"}

    @app.post("/api/task")
    def update():
        return {"updated": True}

    return TestClient(app), sessions, now, settings


def login(client):
    response = client.post("/auth/status", json={"username": "admin", "password": "test-password"})
    assert response.status_code == 200
    return response


def test_production_app_protects_every_api_and_static_mount():
    from src.app import app
    client = TestClient(app)
    for path in ["/api/tasks", "/api/accounts", "/api/accounts/demo", "/api/settings/ai", "/api/results", "/static/screenshot.png", "/openapi.json"]:
        assert client.get(path).status_code == 401, path
    assert client.get("/health").status_code == 200
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect("/ws"):
            pass
    assert error.value.code == 4401


def test_login_sets_http_only_cookie_and_logout_revokes_replay(auth_context):
    client, sessions, _, _ = auth_context
    assert client.get("/api/accounts/demo").status_code == 401
    assert client.post("/auth/status", json={"username": "admin", "password": "wrong"}).status_code == 401
    response = login(client)
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "samesite=strict" in response.headers["set-cookie"].lower()
    token = client.cookies[SESSION_COOKIE]
    assert token not in sessions._sessions
    assert token not in response.text
    assert client.get("/api/accounts/demo").json()["content"] == "private-cookie-data"
    assert client.get("/api/accounts/demo").headers["cache-control"] == "no-store"
    assert client.get("/auth/status").json()["username"] == "admin"
    assert client.post("/auth/logout").status_code == 200
    client.cookies.set(SESSION_COOKIE, token)
    assert client.get("/api/accounts/demo").status_code == 401


def test_session_expiry_and_password_change_revoke_access(auth_context):
    client, _, now, settings = auth_context
    login(client)
    now[0] += 61
    assert client.get("/api/accounts/demo").status_code == 401
    login(client)
    settings.web_password = "changed-password"
    assert client.get("/api/accounts/demo").status_code == 401


def test_cross_origin_login_writes_and_websockets_are_rejected(auth_context):
    client, _, _, _ = auth_context
    hostile = {"origin": "https://untrusted.example"}
    assert client.post("/auth/status", headers=hostile, json={"username": "admin", "password": "test-password"}).status_code == 403
    login(client)
    assert client.post("/api/task", headers=hostile).status_code == 403
    assert client.post("/api/task", headers={"origin": "http://testserver"}).status_code == 200
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect("/ws", headers=hostile):
            pass
    assert error.value.code == 4403


def test_authenticated_websocket_stops_after_session_expiry(auth_context):
    client, _, now, _ = auth_context
    login(client)
    with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as socket:
        now[0] += 61
        socket.send_text("check-session")
        with pytest.raises(WebSocketDisconnect) as error:
            socket.receive_text()
        assert error.value.code == 4401


def test_https_login_sets_secure_cookie(auth_context):
    client, _, _, settings = auth_context
    settings.web_cookie_secure = True
    response = login(client)
    assert "; secure" in response.headers["set-cookie"].lower()


def test_mounting_under_a_url_prefix_cannot_bypass_authentication(auth_context):
    inner, _, _, _ = auth_context
    outer = FastAPI()
    outer.mount("/monitor", inner.app)
    client = TestClient(outer)
    assert client.get("/monitor/api/accounts/demo").status_code == 401
    response = client.post("/monitor/auth/status", json={"username": "admin", "password": "test-password"})
    assert response.status_code == 200
    assert client.get("/monitor/api/accounts/demo").status_code == 200
    with pytest.raises(WebSocketDisconnect) as error:
        with TestClient(outer).websocket_connect("/monitor/ws"):
            pass
    assert error.value.code == 4401
