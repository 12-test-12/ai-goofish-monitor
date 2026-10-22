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

    # Allow the first rendered dashboard frame and charts to settle.
    time.sleep(1)
    window.native.Invoke(Action(start_capture))
    try:
        _wait(lambda: capture["task"].IsCompleted, "窗口截图")
        capture["task"].GetAwaiter().GetResult()
    finally:
        capture["stream"].Dispose()
    report.write_text(json.dumps({"native_window": True, "login": True,
        "workspace": True, "tray_hide_restore": True, "icon_loaded": True,
        "url": url, "screenshot": str(screenshot)}, ensure_ascii=False, indent=2), encoding="utf-8")
