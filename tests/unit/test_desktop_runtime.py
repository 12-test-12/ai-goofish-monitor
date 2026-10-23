import asyncio
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import desktop_launcher as launcher
from src.services.process_service import ProcessService
from src.services import maintenance_service


def test_windowed_headless_startup_logs_errors_without_a_console(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(launcher, "BASE_DIR", tmp_path / "bundle")
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("GOOFISH_DATA_DIR", str(tmp_path / "user"))
    monkeypatch.setitem(sys.modules, "src.app", SimpleNamespace(app=object()))
    monkeypatch.setattr(launcher.uvicorn, "Config", lambda *_args, **_kwargs: None)

    def fail():
        assert sys.stdout is not None and sys.stderr is not None
        raise RuntimeError("synthetic windowed startup failure")

    monkeypatch.setattr(launcher.uvicorn, "Server", lambda _config: SimpleNamespace(run=fail))
    output = None
    try:
        with monkeypatch.context() as streams:
            streams.setattr(sys, "frozen", True, raising=False)
            streams.setattr(sys, "stdout", None)
            streams.setattr(sys, "stderr", None)
            with pytest.raises(SystemExit) as caught:
                launcher.main(["--no-browser"])
            assert caught.value.code == 1
            output = sys.stdout
    finally:
        if output is not None:
            output.close()
    log = (tmp_path / "user/logs/desktop.log").read_text(encoding="utf-8")
    assert "RuntimeError: synthetic windowed startup failure" in log


def test_frozen_explicit_data_directory_and_existing_settings_are_preserved(tmp_path, monkeypatch):
    bundle, user_data = tmp_path / "bundle", tmp_path / "user"
    (bundle / "prompts").mkdir(parents=True)
    (bundle / "prompts/base_prompt.txt").write_text("base", encoding="utf-8")
    (bundle / "prompts/macbook_criteria.txt").write_text("default", encoding="utf-8")
    (bundle / ".env.example").write_text("OPENAI_API_KEY=", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(launcher, "BASE_DIR", bundle)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("GOOFISH_DATA_DIR", str(user_data))
    launcher._prepare_environment()
    assert Path.cwd() == user_data
    assert (user_data / "prompts/base_prompt.txt").read_text(encoding="utf-8") == "base"
    (user_data / ".env").write_text("USER_SETTING=keep", encoding="utf-8")
    launcher._prepare_environment()
    assert (user_data / ".env").read_text(encoding="utf-8") == "USER_SETTING=keep"
    assert (bundle / ".env.example").exists()


def test_frozen_default_data_lives_beside_executable_not_appdata_or_cwd(tmp_path, monkeypatch):
    bundle = tmp_path / "extracted-resources"
    portable = tmp_path / "portable app"
    working = tmp_path / "shortcut-working-directory"
    old_data = tmp_path / "appdata" / "GoofishMonitor"
    for directory in (bundle / "prompts", portable, working, old_data):
        directory.mkdir(parents=True)
    (bundle / "prompts/base_prompt.txt").write_text("default", encoding="utf-8")
    (bundle / ".env.example").write_text("DEFAULT=yes", encoding="utf-8")
    (old_data / ".env").write_text("OLD_SETTING=keep", encoding="utf-8")
    monkeypatch.chdir(working)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(portable / "GoofishMonitor.exe"))
    monkeypatch.setattr(launcher, "BASE_DIR", bundle)
    monkeypatch.setenv("LOCALAPPDATA", str(old_data.parent))
    monkeypatch.setenv("GOOFISH_DATA_DIR", "")

    launcher._prepare_environment()

    assert Path.cwd() == portable
    assert launcher.os.environ["GOOFISH_DATA_DIR"] == str(portable)
    assert (portable / "prompts/base_prompt.txt").read_text(encoding="utf-8") == "default"
    assert (portable / ".env").read_text(encoding="utf-8") == "DEFAULT=yes"
    assert not (working / "static").exists()
    assert not (bundle / "static").exists()
    assert (old_data / ".env").read_text(encoding="utf-8") == "OLD_SETTING=keep"
    (portable / ".env").write_text("USER_SETTING=keep", encoding="utf-8")
    launcher._prepare_environment()
    assert (portable / ".env").read_text(encoding="utf-8") == "USER_SETTING=keep"


def test_source_default_data_stays_in_repository(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    working = tmp_path / "elsewhere"
    working.mkdir()
    monkeypatch.chdir(working)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(launcher, "BASE_DIR", repository)
    monkeypatch.setenv("GOOFISH_DATA_DIR", "")
    launcher._prepare_environment()
    assert Path.cwd() == repository
    assert (repository / "static").is_dir()


def test_frozen_spawn_uses_worker_dispatch_and_explicit_log_file(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delenv("SPIDER_DEBUG_LIMIT", raising=False)
    spawn = AsyncMock(return_value=SimpleNamespace(pid=42))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with (tmp_path / "task.log").open("a", encoding="utf-8") as output:
        asyncio.run(ProcessService()._spawn_process(7, "same", output))
    command = spawn.await_args.args
    assert command[:4] == (sys.executable, "--spider-worker", "--task-id", "7")
    assert command[-2:] == ("--worker-log", str(tmp_path / "task.log"))


def test_worker_entry_dispatches_cli_without_opening_a_second_ui(monkeypatch):
    run = AsyncMock()
    monkeypatch.setitem(sys.modules, "spider_v2", SimpleNamespace(main=run))
    monkeypatch.setattr(launcher, "_prepare_environment", lambda: None)
    monkeypatch.setattr(launcher, "run_app", lambda: (_ for _ in ()).throw(AssertionError("UI must not start")))
    monkeypatch.setattr(sys, "argv", ["desktop.exe"])
    launcher.main(["--spider-worker", "--task-id", "7"])
    run.assert_awaited_once()
    assert sys.argv[1:] == ["--task-id", "7"]


def test_periodic_cache_sweeps_stop_when_application_exits(monkeypatch):
    calls = []
    monkeypatch.setattr(maintenance_service, "cleanup_expired_images", lambda **kwargs: calls.append(kwargs))

    async def run():
        async with maintenance_service.image_cache_maintenance(max_age_hours=24, interval_seconds=0.01):
            for _ in range(20):
                if len(calls) >= 2:
                    break
                await asyncio.sleep(0.01)
            assert len(calls) >= 2
        count = len(calls)
        await asyncio.sleep(0.03)
        assert len(calls) == count
    asyncio.run(run())


def test_browser_opens_only_after_server_is_ready_and_not_after_failure(monkeypatch):
    opened = threading.Event()
    server = SimpleNamespace(started=False, should_exit=False)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda _: opened.set())
    stop = threading.Event()
    thread = threading.Thread(target=launcher._open_when_ready, args=(server, "http://127.0.0.1", stop))
    thread.start()
    try:
        assert not opened.wait(timeout=0.1)
        server.started = True
        assert opened.wait(timeout=1)
    finally:
        stop.set()
        thread.join(timeout=1)
    opened.clear()
    launcher._open_when_ready(SimpleNamespace(started=False, should_exit=True), "http://127.0.0.1", threading.Event())
    assert not opened.is_set()
