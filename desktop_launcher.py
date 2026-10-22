"""
桌面启动入口
使用 PyInstaller 打包后作为单一可执行文件的入口，自动启动 FastAPI 服务并打开浏览器。
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
        options = parser.parse_args(arguments)
        run_app(open_browser=not options.no_browser)


if __name__ == "__main__":
    main()
