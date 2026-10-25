"""Short-lived, isolated Goofish QR logins; browser work stays off the API loop."""

import base64
import json
import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None



LOGIN_URL = (
    "https://passport.goofish.com/mini_login.htm?lang=zh_cn&appName=xianyu"
    "&appEntrance=web&styleType=vertical&isMobile=false&qrCodeFirst=true"
    "&notLoadSsoView=true&notKeepLogin=false&stie=77"
)
TERMINAL_STATUSES = {"success", "expired", "failed", "cancelled"}


def has_login_cookies(cookies: list[dict]) -> bool:
    """Anonymous mtop tokens alone do not prove a completed login."""
    names = {
        cookie["name"] for cookie in cookies
        if (cookie.get("domain", "").lstrip(".") == "goofish.com"
            or cookie.get("domain", "").endswith(".goofish.com"))
        and cookie.get("value")
        and (cookie.get("expires", -1) == -1 or cookie["expires"] > time.time())
    }
    return {"unb", "cookie2"}.issubset(names)


def save_new_state(path: Path, state: dict) -> None:
    """Publish a complete state file atomically without overwriting an account."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".login-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False)
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


@dataclass
class LoginSession:
    name: str
    path: Path
    session_id: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    created_at: float = field(default_factory=time.monotonic)
    status: str = "starting"
    qr_image: str | None = None
    error: str | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)
    lock: threading.RLock = field(default_factory=threading.RLock)
    thread: threading.Thread | None = None

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "session_id": self.session_id, "name": self.name,
                "status": self.status, "qr_image": self.qr_image, "error": self.error,
            }

    def update(self, status: str, *, qr_image=None, error=None) -> None:
        with self.lock:
            if self.cancelled.is_set() or self.status in TERMINAL_STATUSES:
                return
            self.status = status
            self.qr_image = qr_image if status not in TERMINAL_STATUSES else None
            self.error = error


class AccountLoginService:
    def __init__(self, *, timeout=180, max_sessions=2):
        self.timeout = timeout
        self.max_sessions = max_sessions
        self._sessions: dict[str, LoginSession] = {}
        self._lock = threading.Lock()

    def start(self, name: str, path: Path) -> dict:
        with self._lock:
            # Retain terminal results briefly for clients that missed the last poll.
            self._sessions = {
                key: session for key, session in self._sessions.items()
                if session.thread.is_alive() or time.monotonic() - session.created_at < self.timeout + 300
            }
            active = [s for s in self._sessions.values() if s.thread.is_alive()]
            if any(s.path == path for s in active):
                raise ValueError("该账号正在扫码登录，请先关闭原扫码窗口。")
            if len(active) >= self.max_sessions:
                raise ValueError("扫码登录会话已满，请稍后重试。")
            session = LoginSession(name=name, path=path)
            session.thread = threading.Thread(
                target=self._run, args=(session,), name="account-qr-login", daemon=True,
            )
            self._sessions[session.session_id] = session
            session.thread.start()
            return session.snapshot()

    def get(self, session_id: str) -> dict:
        with self._lock:
            return self._sessions[session_id].snapshot()

    def cancel(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions[session_id]
            with session.lock:
                session.cancelled.set()
                if session.status not in TERMINAL_STATUSES:
                    session.status = "cancelled"
                session.qr_image = None

    def shutdown(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            self.cancel(session.session_id)
        deadline = time.monotonic() + 20
        for session in sessions:
            session.thread.join(timeout=max(0, deadline - time.monotonic()))

    def _launch_browser(self, playwright):
        from src.services.browser_launcher import launch_browser_sync

        preferred = "msedge" if os.getenv("LOGIN_IS_EDGE", "false").lower() == "true" else "chrome"
        return launch_browser_sync(
            playwright, {"headless": True, "channel": preferred, "timeout": 15000},
            running_in_docker=os.getenv("RUNNING_IN_DOCKER", "false").lower() == "true",
        )

    def _run(self, session: LoginSession) -> None:
        try:
            if sync_playwright is None:
                session.update("failed", error="未安装 playwright 依赖，无法启动扫码浏览器。请安装 playwright 后重试。")
                return
            # sync_playwright owns its loop in this thread, including on Windows
            # where uvicorn's reload loop cannot launch asyncio subprocesses.
            with sync_playwright() as playwright:
                browser = self._launch_browser(playwright)
                try:
                    context = browser.new_context(viewport={"width": 1000, "height": 760}, locale="zh-CN")
                    page = context.new_page()
                    page.set_default_timeout(2000)
                    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
                    qr = page.locator("#qrcode-img")
                    qr.locator("canvas, img:visible").first.wait_for(state="visible", timeout=20000)
                    qr_image = "data:image/png;base64," + base64.b64encode(qr.screenshot()).decode("ascii")
                    session.update("waiting", qr_image=qr_image)
                    while not session.cancelled.is_set():
                        if time.monotonic() - session.created_at >= self.timeout:
                            session.update("expired")
                            return
                        keep_login = page.locator(".keep-login-confirm-btn.primary:visible")
                        if keep_login.count():
                            keep_login.click()
                        if has_login_cookies(context.cookies()):
                            page.goto("https://www.goofish.com/", wait_until="domcontentloaded", timeout=30000)
                            state = context.storage_state()
                            if not has_login_cookies(state["cookies"]):
                                raise RuntimeError("未获取到有效的闲鱼登录状态，请重新扫码。")
                            with session.lock:
                                if session.cancelled.is_set():
                                    return
                                save_new_state(session.path, state)
                                session.update("success")
                            return
                        panel = page.locator(".qrcode-login:visible")
                        if panel.count():
                            message = panel.inner_text()
                            if any(word in message for word in ("二维码已失效", "二维码已过期", "二维码失效", "二维码过期")):
                                session.update("expired")
                                return
                            status = "scanned" if any(word in message for word in ("扫码成功", "扫描成功", "已扫码", "请在手机")) else "waiting"
                            session.update(status, qr_image=qr_image)
                        session.cancelled.wait(1)
                finally:
                    browser.close()
        except FileExistsError:
            session.update("failed", error="账号已存在，请使用其他账号名称。")
        except Exception as exc:
            # Never expose browser URLs, QR tokens or credentials in API errors.
            message = str(exc) if isinstance(exc, RuntimeError) else "无法获取闲鱼扫码登录状态，请检查网络或稍后重新生成二维码。"
            session.update("failed", error=message)


account_login_service = AccountLoginService()
