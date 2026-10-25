import asyncio
import io
import sys
from types import SimpleNamespace

import pytest

from src.services.process_service import ProcessService, TaskStartError


class FakeProcess:
    def __init__(self, pid: int):
        self.pid = pid
        self.returncode = None
        self._done = asyncio.Event()

    async def wait(self):
        await self._done.wait()
        return self.returncode

    def finish(self, returncode: int = 0):
        self.returncode = returncode
        self._done.set()

    def terminate(self):
        self.finish(-15)

    def kill(self):
        self.finish(-9)


def test_process_service_marks_task_stopped_when_process_exits(monkeypatch, tmp_path):
    fake_process = FakeProcess(pid=4321)
    events = []

    async def run_scenario():
        service = ProcessService()
        service.failure_guard.path = str(tmp_path / "guard.json")
        monkeypatch.setattr(service, "_resolve_cookie_path", lambda _: None)
        service.failure_guard.should_skip_start = lambda *args, **kwargs: SimpleNamespace(
            skip=False,
            should_notify=False,
            reason="",
            consecutive_failures=0,
            paused_until=None,
        )

        stopped = asyncio.Event()

        async def on_started(task_id: int):
            events.append(("started", task_id))

        async def on_stopped(task_id: int):
            events.append(("stopped", task_id))
            stopped.set()

        service.set_lifecycle_hooks(on_started=on_started, on_stopped=on_stopped)

        async def fake_create_subprocess_exec(*_args, **_kwargs):
            return fake_process

        monkeypatch.setattr(
            "src.services.process_service.build_task_log_path",
            lambda task_id, _task_name: str(tmp_path / f"task-{task_id}.log"),
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        started = await service.start_task(0, "task-a")
        assert started is True
        assert events == [("started", 0)]
        assert service.is_running(0) is True

        fake_process.finish(0)
        await asyncio.wait_for(stopped.wait(), timeout=1)

        assert ("stopped", 0) in events
        assert service.is_running(0) is False

    asyncio.run(run_scenario())


@pytest.mark.parametrize("reason,expected", [
    ("LoginRequiredError: redirected", "更新任务所用账号的登录状态"),
    ("RiskControlError: verification", "完成账号验证"),
    ("network error", "network error"),
])
def test_paused_start_explains_reason_without_launching(monkeypatch, tmp_path, reason, expected):
    service = ProcessService()
    service.failure_guard.path = str(tmp_path / "guard.json")
    monkeypatch.setattr(service, "_resolve_cookie_path", lambda _: None)
    key = service.failure_guard.task_key(7, "task")
    service.failure_guard.record_failure(key, reason, min_failures_to_pause=1)
    async def spawn(*args):
        pytest.fail("paused task must not launch a browser")
    monkeypatch.setattr(service, "_spawn_process", spawn)
    with pytest.raises(TaskStartError, match=expected) as caught:
        asyncio.run(service.start_task(7, "task"))
    assert caught.value.status_code == 409


@pytest.mark.parametrize("recovery", ["automatic_account_updated", "legacy_browser_missing"])
def test_paused_tasks_can_start_after_account_update_or_browser_upgrade(monkeypatch, tmp_path, recovery):
    service = ProcessService()
    service.failure_guard.path = str(tmp_path / "guard.json")
    monkeypatch.setattr(service, "_resolve_cookie_path", lambda _: None)
    monkeypatch.setattr(service, "_open_log_file", lambda *a: ("", io.StringIO()))
    state = tmp_path / "1.json"
    state.write_text("{}", encoding="utf-8")
    reason = "LoginRequiredError: redirected" if recovery == "automatic_account_updated" else "Chromium distribution 'chrome' is not found at C:/missing"
    service.failure_guard.record_failure(service.failure_guard.task_key(7, "task"), reason,
                                         cookie_path=str(state), min_failures_to_pause=1)
    if recovery == "automatic_account_updated":
        state.write_text('{"updated":true}', encoding="utf-8")
    process = FakeProcess(42)

    async def scenario():
        async def spawn(*args):
            return process
        monkeypatch.setattr(service, "_spawn_process", spawn)
        assert await service.start_task(7, "task")
        process.finish()
        await service._await_exit_watcher(7)
    asyncio.run(scenario())


def test_spawn_failure_explains_error_and_releases_start_lock(monkeypatch, startable_service):
    async def spawn(*args):
        raise PermissionError("permission denied")
    monkeypatch.setattr(startable_service, "_spawn_process", spawn)
    with pytest.raises(TaskStartError, match="permission denied"):
        asyncio.run(startable_service.start_task(7, "task"))
    assert not startable_service._starting_task_ids
    assert not startable_service.processes


def test_immediate_worker_exit_cannot_leave_status_running(monkeypatch, startable_service):
    async def scenario():
        process = FakeProcess(42)
        state = {"running": False}
        hook_entered = asyncio.Event()
        release_hook = asyncio.Event()
        stopped = asyncio.Event()

        async def spawn(*args):
            return process

        async def on_started(task_id):
            hook_entered.set()
            await release_hook.wait()
            state["running"] = True

        async def on_stopped(task_id):
            state["running"] = False
            stopped.set()

        monkeypatch.setattr(startable_service, "_spawn_process", spawn)
        startable_service.set_lifecycle_hooks(on_started=on_started, on_stopped=on_stopped)
        start = asyncio.create_task(startable_service.start_task(7, "task"))
        await hook_entered.wait()
        process.finish(1)
        await stopped.wait()
        release_hook.set()
        assert await start
        assert not state["running"]
        assert not startable_service.processes
    asyncio.run(scenario())


def test_process_service_reindexes_runtime_maps_after_delete():
    service = ProcessService()
    proc_a = object()
    proc_c = object()
    watcher_a = object()
    watcher_c = object()

    service.processes = {0: proc_a, 2: proc_c}
    service.log_paths = {0: "a.log", 2: "c.log"}
    service.task_names = {0: "A", 2: "C"}
    service.exit_watchers = {0: watcher_a, 2: watcher_c}

    service.reindex_after_delete(1)

    assert service.processes == {0: proc_a, 1: proc_c}
    assert service.log_paths == {0: "a.log", 1: "c.log"}
    assert service.task_names == {0: "A", 1: "C"}
    assert service.exit_watchers == {0: watcher_a, 1: watcher_c}


def test_process_service_adds_debug_limit_arg_when_env_enabled(monkeypatch):
    monkeypatch.setenv("SPIDER_DEBUG_LIMIT", "1")
    service = ProcessService()

    command = service._build_spawn_command(7)

    assert command == [
        sys.executable,
        "-u",
        "spider_v2.py",
        "--task-id",
        "7",
        "--debug-limit",
        "1",
    ]


@pytest.mark.parametrize("strategy,expected", [("rotate", None), ("auto", "root.json"), ("fixed", "state/1.json")])
def test_pause_recovery_checks_only_the_account_selected_by_task_strategy(monkeypatch, tmp_path, strategy, expected):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "root.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("src.services.process_service.STATE_FILE", "root.json")
    monkeypatch.setattr("src.services.process_service.find_task_by_id_sync", lambda _: SimpleNamespace(
        account_strategy=strategy, account_state_file="state/1.json"
    ))
    assert ProcessService()._resolve_cookie_path(7) == expected


def test_process_service_windows_terminate_invokes_taskkill(monkeypatch):
    service = ProcessService()
    taskkill_calls = []

    async def fake_taskkill(pid: int):
        taskkill_calls.append(pid)
        fake_proc.finish(0)
        return True

    monkeypatch.setattr(service, "_kill_process_tree_windows", fake_taskkill)
    monkeypatch.setattr(sys, "platform", "win32")

    fake_proc = FakeProcess(pid=9999)

    async def run():
        await service._terminate_process(fake_proc, 1)

    asyncio.run(run())
    assert taskkill_calls == [9999]


def test_process_service_kill_process_tree_executes_taskkill_command(monkeypatch):
    service = ProcessService()
    exec_args = []

    class DummyProc:
        returncode = 0

        async def wait(self):
            return 0

    async def fake_exec(*args, **kwargs):
        exec_args.append(args)
        return DummyProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    assert asyncio.run(service._kill_process_tree_windows(8888)) is True
    assert len(exec_args) == 1
    assert exec_args[0] == ("taskkill", "/F", "/T", "/PID", "8888")


@pytest.fixture()
def startable_service(monkeypatch, tmp_path):
    service = ProcessService()
    service.failure_guard.path = str(tmp_path / "guard.json")
    monkeypatch.setattr(service.failure_guard, "should_skip_start", lambda *a, **kw: SimpleNamespace(skip=False))
    monkeypatch.setattr(service, "_resolve_cookie_path", lambda _: None)
    monkeypatch.setattr(service, "_open_log_file", lambda *a: ("", io.StringIO()))
    return service


def test_concurrent_starts_spawn_only_once(monkeypatch, startable_service):
    async def scenario():
        service = startable_service
        spawn_entered = asyncio.Event()
        release_spawn = asyncio.Event()
        process = FakeProcess(1234)
        calls = []

        async def spawn(*args):
            calls.append(args)
            spawn_entered.set()
            await release_spawn.wait()
            return process

        monkeypatch.setattr(service, "_spawn_process", spawn)
        first = asyncio.create_task(service.start_task(1, "task-a"))
        await spawn_entered.wait()
        assert await service.start_task(1, "task-a") is False
        release_spawn.set()
        assert await first is True
        assert len(calls) == 1
        process.finish()
        await service._await_exit_watcher(1)

    asyncio.run(scenario())


@pytest.mark.parametrize("spawn_fails", [False, True])
def test_cancelled_start_releases_resources_and_allows_retry(monkeypatch, startable_service, spawn_fails):
    async def scenario():
        service = startable_service
        spawn_entered = asyncio.Event()
        release_spawn = asyncio.Event()
        process = FakeProcess(1234)
        log_handle = io.StringIO()
        monkeypatch.setattr(service, "_open_log_file", lambda *a: ("", log_handle))

        async def spawn(*args):
            spawn_entered.set()
            await release_spawn.wait()
            if spawn_fails:
                raise OSError("spawn failed")
            return process

        async def terminate(proc, task_id):
            proc.finish(-9)

        monkeypatch.setattr(service, "_spawn_process", spawn)
        monkeypatch.setattr(service, "_terminate_process", terminate)
        starting = asyncio.create_task(service.start_task(1, "task-a"))
        await spawn_entered.wait()
        starting.cancel()
        release_spawn.set()
        with pytest.raises(asyncio.CancelledError):
            await starting

        assert not service._starting_task_ids
        assert not service.processes
        assert not service.exit_watchers
        assert log_handle.closed
        if not spawn_fails:
            assert process.returncode == -9

        async def retry_spawn(*args):
            return FakeProcess(5678)

        monkeypatch.setattr(service, "_open_log_file", lambda *a: ("", io.StringIO()))
        monkeypatch.setattr(service, "_spawn_process", retry_spawn)
        assert await service.start_task(1, "task-a") is True
        service.processes[1].finish()
        await service._await_exit_watcher(1)

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["exit_code", "missing_command", "timeout", "helper_unresponsive"])
def test_windows_taskkill_failure_falls_back_and_reaps_helper(monkeypatch, failure):
    async def scenario():
        service = ProcessService()
        process = FakeProcess(1234)
        taskkill = FakeProcess(4321)
        if failure == "exit_code":
            taskkill.finish(1)
        if failure == "helper_unresponsive":
            taskkill.kill = lambda: None

        async def spawn(*args, **kwargs):
            if failure == "missing_command":
                raise FileNotFoundError("taskkill unavailable")
            return taskkill

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr("src.services.process_service.STOP_TIMEOUT_SECONDS", 0.01)
        await service._terminate_process(process, 1)
        assert process.returncode == -9
        if failure == "timeout":
            assert taskkill.returncode == -9

    asyncio.run(scenario())


def test_windows_final_wait_is_bounded(monkeypatch):
    class UnresponsiveProcess(FakeProcess):
        def kill(self):
            pass

    async def scenario():
        service = ProcessService()

        async def tree_kill(pid):
            return True

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(service, "_kill_process_tree_windows", tree_kill)
        monkeypatch.setattr("src.services.process_service.STOP_TIMEOUT_SECONDS", 0.01)
        with pytest.raises(asyncio.TimeoutError):
            await service._terminate_process(UnresponsiveProcess(1234), 1)

    asyncio.run(scenario())
