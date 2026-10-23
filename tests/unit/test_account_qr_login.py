import json
import threading
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import accounts
from src.api.auth import WebAuthMiddleware
from src.services import account_login_service as login


def login_cookies(domain=".goofish.com"):
    return [
        {"name": name, "value": "test-only", "domain": domain, "expires": -1}
        for name in ("unb", "cookie2")
    ]


@pytest.mark.parametrize("cookies, expected", [
    (login_cookies(), True),
    (login_cookies("passport.goofish.com"), True),
    (login_cookies("evilgoofish.com"), False),
    (login_cookies(".goofish.com.evil.example"), False),
    ([{"name": "_m_h5_tk", "value": "anonymous", "domain": ".goofish.com"}], False),
    (login_cookies()[:1], False),
    ([dict(cookie, expires=1) for cookie in login_cookies()], False),
    ([dict(cookie, value="") for cookie in login_cookies()], False),
])
def test_login_requires_real_unexpired_authentication_cookies(cookies, expected):
    assert login.has_login_cookies(cookies) is expected


def test_state_publication_never_overwrites_an_account(tmp_path):
    path = tmp_path / "account.json"
    state = {"cookies": login_cookies(), "origins": []}
    login.save_new_state(path, state)
    with pytest.raises(FileExistsError):
        login.save_new_state(path, {"cookies": []})
    assert json.loads(path.read_text(encoding="utf-8")) == state
    assert list(tmp_path.iterdir()) == [path]


@pytest.fixture
def browser(monkeypatch):
    manager = MagicMock()
    playwright = manager.__enter__.return_value
    instance = playwright.chromium.launch.return_value
    context = instance.new_context.return_value
    page = context.new_page.return_value
    qr = page.locator.return_value
    qr.screenshot.return_value = b"test-qr-png"
    qr.count.return_value = 0
    context.cookies.return_value = login_cookies()
    context.storage_state.return_value = {"cookies": login_cookies(), "origins": []}
    monkeypatch.setattr(login, "sync_playwright", lambda: manager)
    return instance, context, page


def test_worker_saves_full_state_only_after_login_and_closes_browser(tmp_path, browser):
    instance, context, page = browser
    session = login.LoginSession("acc", tmp_path / "acc.json")
    login.AccountLoginService()._run(session)
    assert session.status == "success"
    assert session.qr_image is None
    assert json.loads(session.path.read_text()) == context.storage_state.return_value
    assert page.goto.call_args.args == ("https://www.goofish.com/",)
    instance.close.assert_called_once()


def test_worker_keeps_waiting_for_phone_confirmation(tmp_path, browser):
    _, context, page = browser
    context.cookies.side_effect = [[], login_cookies()]
    page.locator.return_value.count.side_effect = [0, 1, 0]
    page.locator.return_value.inner_text.return_value = "扫码成功，请在手机确认"
    session = login.LoginSession("acc", tmp_path / "acc.json")
    observations = []
    session.cancelled.wait = lambda _: observations.append((session.snapshot(), session.path.exists()))
    login.AccountLoginService()._run(session)
    assert observations[0][0]["status"] == "scanned"
    assert observations[0][0]["qr_image"].startswith("data:image/png;base64,")
    assert observations[0][1] is False
    assert session.status == "success"


@pytest.mark.parametrize("reason", ["cancel", "timeout", "qr_expired", "invalid_state", "network"])
def test_worker_does_not_save_failed_or_cancelled_logins(tmp_path, browser, reason):
    instance, context, page = browser
    session = login.LoginSession("acc", tmp_path / "acc.json")
    service = login.AccountLoginService(timeout=0 if reason == "timeout" else 180)
    if reason == "cancel":
        session.cancelled.set()
    elif reason == "qr_expired":
        context.cookies.return_value = []
        page.locator.return_value.count.side_effect = [0, 1]
        page.locator.return_value.inner_text.return_value = "二维码已失效，请刷新"
    elif reason == "invalid_state":
        context.storage_state.return_value = {"cookies": [], "origins": []}
    elif reason == "network":
        page.goto.side_effect = Exception("secret-token-must-not-leak")
    service._run(session)
    assert not session.path.exists()
    assert session.qr_image is None
    assert "secret-token" not in (session.error or "")
    if reason in {"timeout", "qr_expired"}:
        assert session.status == "expired"
    elif reason != "cancel":
        assert session.status == "failed"
    instance.close.assert_called_once()


def test_session_capacity_cancellation_and_duplicate_name(tmp_path, monkeypatch):
    service = login.AccountLoginService(max_sessions=1)
    entered = threading.Event()

    def wait_for_cancel(session):
        session.update("waiting", qr_image="test-only")
        entered.set()
        session.cancelled.wait(5)

    monkeypatch.setattr(service, "_run", wait_for_cancel)
    first = service.start("acc", tmp_path / "acc.json")
    try:
        assert entered.wait(2)
        with pytest.raises(ValueError, match="正在扫码"):
            service.start("acc", tmp_path / "acc.json")
        with pytest.raises(ValueError, match="会话已满"):
            service.start("other", tmp_path / "other.json")
        service.cancel(first["session_id"])
        snapshot = service.get(first["session_id"])
        assert snapshot["status"] == "cancelled"
        assert snapshot["qr_image"] is None
    finally:
        service.shutdown()
    assert not service._sessions[first["session_id"]].thread.is_alive()


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "_state_dir", lambda: str(tmp_path))
    service = MagicMock()
    service.start.return_value = {
        "session_id": "test-session", "name": "acc", "status": "starting", "qr_image": None, "error": None,
    }
    service.get.return_value = service.start.return_value
    monkeypatch.setattr(accounts, "account_login_service", service)
    app = FastAPI()
    app.include_router(accounts.router)
    with TestClient(app) as client:
        yield client, service


def test_qr_api_routes_validation_and_no_cache(api, tmp_path):
    client, service = api
    for invalid in ("../outside", "a/b", "", "a" * 51):
        assert client.post("/api/accounts/qr-login", json={"name": invalid}).status_code == 400
    assert not service.start.called
    response = client.post("/api/accounts/qr-login", json={"name": " acc "})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    service.start.assert_called_once_with("acc", (tmp_path / "acc.json").resolve())
    assert client.get("/api/accounts/qr-login/test-session").json()["status"] == "starting"
    assert client.get("/api/accounts/qr-login/test-session").headers["cache-control"] == "no-store"
    assert client.delete("/api/accounts/qr-login/test-session").status_code == 200
    service.cancel.assert_called_with("test-session")
    service.get.side_effect = KeyError
    assert client.get("/api/accounts/qr-login/missing").status_code == 404


def test_existing_accounts_are_not_replaced_by_qr_login(api, tmp_path):
    client, service = api
    (tmp_path / "acc.json").write_text("existing", encoding="utf-8")
    assert client.post("/api/accounts/qr-login", json={"name": "acc"}).status_code == 409
    service.start.assert_not_called()


def test_json_import_remains_available(api):
    client, _ = api
    assert client.post("/api/accounts", json={"name": "manual", "content": '{"cookies": []}'}).status_code == 200
    assert client.get("/api/accounts/manual").json()["name"] == "manual"


def test_qr_endpoints_require_web_authentication():
    app = FastAPI()
    sessions = MagicMock()
    sessions.validate.return_value = None
    app.add_middleware(WebAuthMiddleware, sessions=sessions)
    app.include_router(accounts.router)
    with TestClient(app) as client:
        assert client.post("/api/accounts/qr-login", json={"name": "acc"}).status_code == 401
        assert client.get("/api/accounts/qr-login/example").status_code == 401
        assert client.delete("/api/accounts/qr-login/example").status_code == 401
