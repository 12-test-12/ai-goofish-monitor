"""
桌面启动入口
使用 PyInstaller 打包后作为单一可执行文件的入口，默认打开独立桌面窗口。
"""
import os
import sys
import threading
import webbrowser
import argparse
import asyncio
import multiprocessing
import shutil
from pathlib import Path

import uvicorn

# PyInstaller 运行时资源目录：_MEIPASS；未打包时则为当前文件所在目录
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def _prepare_environment() -> None:
    """确保工作目录和模块路径正确"""
    frozen = getattr(sys, "frozen", False)
    configured = os.getenv("GOOFISH_DATA_DIR")
    if configured:
        data_dir = Path(configured).expanduser().resolve()
    elif frozen:
        data_dir = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "GoofishMonitor"
    else:
        data_dir = BASE_DIR
    data_dir.mkdir(parents=True, exist_ok=True)
    if data_dir != BASE_DIR:
        for relative in ("prompts/base_prompt.txt", "prompts/macbook_criteria.txt", ".env.example"):
            source = BASE_DIR / relative
            target = data_dir / (".env" if relative == ".env.example" else relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_file():
                try:
                    with source.open("rb") as reader, target.open("xb") as writer:
                        shutil.copyfileobj(reader, writer)
                except FileExistsError:
                    pass
    (data_dir / "static").mkdir(exist_ok=True)
    os.environ["GOOFISH_DATA_DIR"] = str(data_dir)
    os.chdir(data_dir)
    if str(BASE_DIR) not in sys.path:
        sys.path.insert(0, str(BASE_DIR))


def _open_when_ready(server, url: str, stopped: threading.Event) -> None:
    while not stopped.wait(0.05):
        if server.should_exit:
            return
        if server.started:
            webbrowser.open(url)
            return


def run_app(*, open_browser: bool = True) -> None:
    """启动 FastAPI 应用并自动打开浏览器"""
    _prepare_environment()
    _configure_desktop_logging()

    from src.app import app
    from src.infrastructure.config.settings import settings

    url = f"http://127.0.0.1:{settings.server_port}"
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=settings.server_port,
        log_level="info", reload=False, use_colors=False,
    ))
    stopped = threading.Event()
    opener = None
    if open_browser:
        opener = threading.Thread(target=_open_when_ready, args=(server, url, stopped), daemon=True)
        opener.start()
    try:
        server.run()
    finally:
        stopped.set()
        if opener is not None:
            opener.join(timeout=1)


def main(argv=None) -> None:
    multiprocessing.freeze_support()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "--spider-worker":
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--worker-log")
        options, spider_args = parser.parse_known_args(arguments[1:])
        _prepare_environment()
        if options.worker_log:
            sys.stdout = open(options.worker_log, "a", encoding="utf-8", buffering=1)
            sys.stderr = sys.stdout
        from spider_v2 import main as run_spider
        sys.argv = [sys.executable, *spider_args]
        asyncio.run(run_spider())
    else:
        parser = argparse.ArgumentParser(description="闲鱼监控桌面服务")
        parser.add_argument("--no-browser", action="store_true", help="只启动本地服务，不打开浏览器")
        parser.add_argument("--browser", action="store_true", help="使用外部浏览器打开界面")
        parser.add_argument("--smoke-report", type=Path, help=argparse.SUPPRESS)
        options = parser.parse_args(arguments)
        if options.no_browser or options.browser:
            try:
                run_app(open_browser=options.browser and not options.no_browser)
            except Exception:
                import traceback
                traceback.print_exc()
                raise SystemExit(1)
        else:
            _prepare_environment()
            _configure_desktop_logging()
            try:
                from src.desktop.shell import run_desktop
                run_desktop(BASE_DIR, smoke_report=options.smoke_report)
            except Exception:
                import traceback
                traceback.print_exc()
                if options.smoke_report:
                    raise
                _show_startup_error()
                raise SystemExit(1)


def _configure_desktop_logging():
    """Windowed builds have no stdio; keep startup diagnostics in persistent data."""
    if getattr(sys, "frozen", False):
        Path("logs").mkdir(exist_ok=True)
        output = open("logs/desktop.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = output


def _show_startup_error():
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None,
            "桌面程序启动失败。请检查 logs/desktop.log。\n"
            "若缺少界面组件，请安装 Microsoft Edge WebView2 Runtime。\n"
            "若程序已经运行，请从系统托盘打开。", "闲鱼智能监控", 0x10)


if __name__ == "__main__":
    main()
