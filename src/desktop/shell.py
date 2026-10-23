"""Native window, tray lifecycle and a loopback-only application server."""
import os
from pathlib import Path
import socket
import sys
import threading
import time

from src.infrastructure.file_lock import FileLock

APP_TITLE = "闲鱼智能监控"


class DesktopTray:
    def __init__(self, window, icon_path):
        self.window = window
        self.icon_path = icon_path
        self.ready = threading.Event()
        self.exiting = threading.Event()
        self.icon = None
        self.thread = None

    def show(self, *_):
        self.window.show()
        self.window.restore()
        try:
            if hasattr(self.window, "focus"):
                self.window.focus()
            elif getattr(self.window, "native", None):
                self.window.native.Activate()
                self.window.native.BringToFront()
        except Exception:
            pass

    def quit(self, *_):
        self.exiting.set()
        self.window.destroy()

    def on_closing(self):
        if self.ready.is_set() and not self.exiting.is_set():
            self.window.hide()
            return False
        return True

    def start(self):
        try:
            import pystray
            from PIL import Image
            icon_file = Path(self.icon_path)
            if icon_file.is_file():
                with Image.open(icon_file) as source:
                    image = source.copy()
            else:
                image = Image.new("RGBA", (64, 64), color=(37, 99, 235, 255))
            self.icon = pystray.Icon("GoofishMonitor", image,
                f"{APP_TITLE} · 关闭窗口后继续监控", menu=pystray.Menu(
                    pystray.MenuItem("打开主窗口", self.show, default=True),
                    pystray.MenuItem("退出并停止监控", self.quit),
                ))

            def setup(icon):
                icon.visible = True
                self.ready.set()

            def run():
                try:
                    self.icon.run(setup=setup)
                except Exception:
                    import traceback
                    traceback.print_exc()
                finally:
                    self.ready.clear()

            self.thread = threading.Thread(target=run, name="desktop-tray", daemon=True)
            self.thread.start()
            # If the tray cannot start, closing the window must still allow an exit.
            self.ready.wait(5)
        except Exception:
            import traceback
            traceback.print_exc()
            self.ready.clear()

    def stop(self):
        self.exiting.set()
        if self.icon:
            self.icon.stop()
        if self.thread:
            self.thread.join(timeout=5)


def wait_for_server(server, thread, errors, *, timeout=30):
    deadline = time.monotonic() + timeout
    while not server.started:
        if errors or not thread.is_alive() or time.monotonic() >= deadline:
            raise RuntimeError("本地服务未能就绪，请查看桌面日志") from (errors[0] if errors else None)
        time.sleep(0.05)


def run_desktop(base_dir: Path, *, smoke_report=None):
    if sys.platform != "win32":
        raise RuntimeError("桌面窗口目前支持 Windows；其他平台请使用 --browser")
    import ctypes
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Goofish.Monitor.Desktop")
    lock = FileLock(Path(".desktop.lock").resolve())
    if not lock.acquire(blocking=False):
        raise RuntimeError("程序已经运行，请从系统托盘打开主窗口")
    try:
        _run_window(base_dir, smoke_report=smoke_report)
    finally:
        lock.release()


def _run_window(base_dir, *, smoke_report=None):
    import uvicorn
    import webview
    # Reserve an ephemeral loopback port before loading settings; never connect
    # the window to a pre-existing process on a fixed port.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        os.environ["SERVER_PORT"] = str(port)
        from src.app import app
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
            log_level="info", use_colors=False, timeout_graceful_shutdown=15))
        errors = []

        def serve():
            try:
                server.run(sockets=[listener])
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=serve, name="desktop-server", daemon=True)
        tray = None
        thread.start()
        try:
            wait_for_server(server, thread, errors)
            url = f"http://127.0.0.1:{port}"
            webview.settings["ALLOW_DOWNLOADS"] = True
            window = webview.create_window(APP_TITLE, url + "/?desktop=1",
                width=1280, height=840, min_size=(960, 640),
                background_color="#f8fafc", text_select=True)
            tray = DesktopTray(window, base_dir / "assets/desktop/goofish.png")
            window.events.closing += tray.on_closing
            probe_errors = []

            def on_ready():
                try:
                    tray.start()
                except Exception:
                    import traceback
                    traceback.print_exc()
                if smoke_report:
                    from src.desktop.smoke_probe import run_probe
                    try:
                        run_probe(window, tray, url, Path(smoke_report))
                    except BaseException as exc:
                        probe_errors.append(exc)
                    finally:
                        tray.quit()

            ico_file = base_dir / "assets/desktop/goofish.ico"
            webview_icon = str(ico_file) if ico_file.is_file() else None
            webview.start(on_ready, gui="edgechromium", private_mode=False,
                storage_path=str(Path("desktop-profile").resolve()),
                icon=webview_icon)
            if probe_errors:
                raise RuntimeError("桌面界面冒烟失败") from probe_errors[0]
        finally:
            if tray:
                tray.stop()
            server.should_exit = True
            thread.join(timeout=30)
            if thread.is_alive():
                raise RuntimeError("后台服务未正常退出，请查看桌面日志")
