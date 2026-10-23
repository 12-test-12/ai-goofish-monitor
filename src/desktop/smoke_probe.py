"""Opt-in native GUI probe, invoked only with --smoke-report in isolated CI data."""
import json
import os
from pathlib import Path
import time
import threading


def _wait(predicate, description, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise RuntimeError(f"桌面冒烟超时: {description}")


def run_probe(window, tray, url, report: Path):
    report.parent.mkdir(parents=True, exist_ok=True)
    _wait(lambda: window.evaluate_js("document.querySelector('#username') !== null"), "登录界面")
    assert tray.ready.is_set(), "System tray did not initialize"
    assert window.evaluate_js("document.querySelector('img[src=\"/app-icon.png\"]').naturalWidth > 0")
    credentials = json.dumps({"username": os.environ["WEB_USERNAME"], "password": os.environ["WEB_PASSWORD"]})
    authenticated = threading.Event()
    response = []
    def on_login(value):
        response.append(value)
        authenticated.set()
    window.evaluate_js("fetch('/auth/status', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(" + credentials + ")}).then(r => r.status)", callback=on_login)
    assert authenticated.wait(10) and response == [200], f"Login failed: {response}"
    window.load_url(url + "/dashboard?desktop=1")
    _wait(lambda: window.evaluate_js("document.querySelector('#main-content') !== null"), "工作区")
    assert window.evaluate_js("document.documentElement.classList.contains('desktop-shell')")
    assert tray.on_closing() is False, "Closing must minimize to tray"
    _wait(lambda: not window.native.Visible, "托盘隐藏")
    tray.show()
    _wait(lambda: window.native.Visible, "托盘恢复")

    # Exercise the bundled AccountsView without requesting an external QR code.
    window.load_url(url + "/accounts?desktop=1")
    _wait(lambda: window.evaluate_js("Array.from(document.querySelectorAll('button')).some(b => /添加账号|Add Account/.test(b.textContent))"), "账号管理")
    window.evaluate_js("Array.from(document.querySelectorAll('button')).find(b => /添加账号|Add Account/.test(b.textContent)).click()")
    _wait(lambda: window.evaluate_js("document.querySelector('#new-account-name') !== null"), "添加账号")
    assert window.evaluate_js("Array.from(document.querySelectorAll('[role=dialog] button')).some(b => /生成登录二维码|Generate Login QR Code/.test(b.textContent))"), "QR sign-in is not the default account dialog"
    assert window.evaluate_js("document.querySelector('[role=dialog] textarea') === null"), "Account dialog still defaults to JSON import"

    # Capture only the app's WebView surface, without capturing the user's desktop.
    from System import Action
    from System.IO import File, FileMode
    from Microsoft.Web.WebView2.Core import CoreWebView2CapturePreviewImageFormat
    capture = {}
    screenshot = report.with_suffix(".png")

    def start_capture():
        capture["stream"] = File.Open(str(screenshot), FileMode.Create)
        capture["task"] = window.native.webview.CoreWebView2.CapturePreviewAsync(
            CoreWebView2CapturePreviewImageFormat.Png, capture["stream"])

    # Allow the account dialog's opening animation to settle.
    time.sleep(1)
    window.native.Invoke(Action(start_capture))
    try:
        _wait(lambda: capture["task"].IsCompleted, "窗口截图")
        capture["task"].GetAwaiter().GetResult()
    finally:
        capture["stream"].Dispose()
    report.write_text(json.dumps({"native_window": True, "login": True,
        "workspace": True, "tray_hide_restore": True, "icon_loaded": True,
        "account_qr_dialog": True,
        "url": url, "screenshot": str(screenshot)}, ensure_ascii=False, indent=2), encoding="utf-8")
