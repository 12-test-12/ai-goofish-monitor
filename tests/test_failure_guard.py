from __future__ import annotations

from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import sys
from pathlib import Path
from threading import Event

import pytest

import src.failure_guard as guard_module
from src.failure_guard import FailureGuard


def test_failure_guard_opens_circuit_after_threshold_and_rate_limits(tmp_path):
    guard_path = tmp_path / "guard.json"
    cookie_path = tmp_path / "xianyu_state.json"
    cookie_path.write_text("{}", encoding="utf-8")

    guard = FailureGuard(
        path=str(guard_path),
        threshold=3,
        pause_seconds=3 * 24 * 60 * 60,
        tz_name="Asia/Shanghai",
    )

    base = datetime(2026, 3, 4, 12, 0, 0)

    r1 = guard.record_failure("task-a", "err-1", cookie_path=str(cookie_path), now=base)
    assert r1["should_notify"] is False
    assert r1["opened_circuit"] is False

    r2 = guard.record_failure("task-a", "err-2", cookie_path=str(cookie_path), now=base)
    assert r2["should_notify"] is False
    assert r2["opened_circuit"] is False

    r3 = guard.record_failure("task-a", "err-3", cookie_path=str(cookie_path), now=base)
    assert r3["should_notify"] is True
    assert r3["opened_circuit"] is True
    assert r3["paused_until"] is not None

    d0 = guard.should_skip_start("task-a", cookie_path=str(cookie_path), now=base)
    assert d0.skip is True
    assert d0.should_notify is False

    next_day = base + timedelta(days=1, minutes=1)
    d1 = guard.should_skip_start("task-a", cookie_path=str(cookie_path), now=next_day)
    assert d1.skip is True
    assert d1.should_notify is True

    d1b = guard.should_skip_start("task-a", cookie_path=str(cookie_path), now=next_day)
    assert d1b.skip is True
    assert d1b.should_notify is False


def test_failure_guard_auto_recovers_on_cookie_change(tmp_path):
    guard_path = tmp_path / "guard.json"
    cookie_path = tmp_path / "xianyu_state.json"
    cookie_path.write_text("{}", encoding="utf-8")

    guard = FailureGuard(
        path=str(guard_path),
        threshold=2,
        pause_seconds=3 * 24 * 60 * 60,
        tz_name="Asia/Shanghai",
    )

    base = datetime(2026, 3, 4, 12, 0, 0)

    guard.record_failure("task-a", "err-1", cookie_path=str(cookie_path), now=base)
    guard.record_failure("task-a", "err-2", cookie_path=str(cookie_path), now=base)

    paused = guard.should_skip_start("task-a", cookie_path=str(cookie_path), now=base)
    assert paused.skip is True

    cookie_path.write_text('{"updated": true}', encoding="utf-8")

    recovered = guard.should_skip_start(
        "task-a",
        cookie_path=str(cookie_path),
        now=base + timedelta(minutes=1),
    )
    assert recovered.skip is False


@pytest.mark.parametrize("update", ["automatic", "same_timestamp", "older_timestamp", "different_account", "missing_created"])
def test_login_pause_recovers_when_actual_account_is_updated(tmp_path, update):
    state = tmp_path / "1.json"
    if update != "missing_created":
        state.write_text('{"cookies": ["old"]}', encoding="utf-8")
    guard = FailureGuard(path=str(tmp_path / "guard.json"), threshold=1)
    guard.record_failure("task", "LoginRequiredError: login required", cookie_path=str(state))
    assert guard.should_skip_start("task").skip
    stamp = state.stat().st_mtime if state.exists() else 1000
    target = tmp_path / "2.json" if update == "different_account" else state
    target.write_text('{"cookies": ["new"]}', encoding="utf-8")
    if update == "same_timestamp":
        os.utime(target, (stamp, stamp))
    elif update in {"older_timestamp", "different_account"}:
        os.utime(target, (1000, 1000))
    decision = guard.should_skip_start("task", cookie_path=None if update == "automatic" else str(target))
    assert not decision.skip
    assert not guard.should_skip_start("task").skip


def test_touching_unchanged_invalid_cookies_does_not_clear_pause(tmp_path):
    state = tmp_path / "1.json"
    state.write_text("{}", encoding="utf-8")
    guard = FailureGuard(path=str(tmp_path / "guard.json"), threshold=1)
    guard.record_failure("task", "LoginRequiredError", cookie_path=str(state))
    stamp = state.stat().st_mtime + 10
    os.utime(state, (stamp, stamp))
    assert guard.should_skip_start("task").skip


def test_old_pause_without_hash_recovers_on_recorded_account_update(tmp_path):
    state = tmp_path / "1.json"
    state.write_text("{}", encoding="utf-8")
    guard = FailureGuard(path=str(tmp_path / "guard.json"), threshold=1)
    guard.record_failure("task", "LoginRequiredError", cookie_path=str(state))
    data = json.loads(Path(guard.path).read_text(encoding="utf-8"))
    data["tasks"]["task"].pop("cookie_sha256")
    Path(guard.path).write_text(json.dumps(data), encoding="utf-8")
    os.utime(state, (1000, 1000))
    assert not guard.should_skip_start("task").skip


def test_failure_guard_serializes_writers_and_keeps_valid_json(tmp_path):
    path = tmp_path / "guard.json"

    def record(_):
        guard = FailureGuard(path=str(path), threshold=99)
        guard.record_failure("shared-task", "test failure")

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(record, range(12)))
    state = json.loads(path.read_text(encoding="utf-8"))
    assert state["tasks"]["shared-task"]["consecutive_failures"] == 12
    assert not list(tmp_path.glob("*.tmp"))


def test_legacy_pause_survives_id_upgrade_without_being_reapplied_after_recovery(tmp_path):
    guard = FailureGuard(path=str(tmp_path / "guard.json"), threshold=1)
    guard.record_failure("legacy-name", "old failure")
    key = guard.task_key(7, "legacy-name")
    assert guard.should_skip_start(key).skip is True
    guard.record_success(key)
    assert guard.should_skip_start(guard.task_key(7, "legacy-name")).skip is False


def test_failed_atomic_replace_preserves_state_and_cleans_temporary_file(tmp_path, monkeypatch):
    path = tmp_path / "guard.json"
    guard = FailureGuard(path=str(path))
    guard.record_failure("task", "initial")
    original = path.read_bytes()

    def denied(*_args):
        raise PermissionError("simulated Windows sharing violation")

    monkeypatch.setattr(guard_module.os, "replace", denied)
    with pytest.raises(PermissionError):
        guard.record_failure("task", "second")
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


def test_read_permission_error_does_not_quarantine_or_overwrite_valid_state(tmp_path, monkeypatch):
    path = tmp_path / "guard.json"
    guard = FailureGuard(path=str(path))
    guard.record_failure("task", "initial")
    original = path.read_bytes()

    def denied(*_args):
        raise PermissionError("temporary read failure")

    monkeypatch.setattr(guard_module.json, "load", denied)
    with pytest.raises(PermissionError):
        guard.record_failure("task", "second")
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.corrupt.*"))


def test_concurrent_readers_claim_daily_notification_only_once(tmp_path):
    path = tmp_path / "guard.json"
    base = datetime(2026, 3, 4, 12)
    guard = FailureGuard(path=str(path), threshold=1, pause_seconds=3 * 24 * 3600)
    guard.record_failure("task", "risk", now=base)
    start = Event()

    def check(_):
        start.wait(timeout=5)
        return FailureGuard(path=str(path)).should_skip_start("task", now=base + timedelta(days=1))

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(check, index) for index in range(8)]
        start.set()
        decisions = [future.result(timeout=15) for future in futures]
    assert all(decision.skip for decision in decisions)
    assert sum(decision.should_notify for decision in decisions) == 1


def test_separate_processes_preserve_all_failure_updates(tmp_path):
    path = tmp_path / "guard.json"
    script = "from src.failure_guard import FailureGuard; import sys; g=FailureGuard(path=sys.argv[1], threshold=99); [g.record_failure('task', 'failure') for _ in range(5)]"
    processes = []
    try:
        for _ in range(3):
            processes.append(subprocess.Popen(
                [sys.executable, "-c", script, str(path)], cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ))
        for process in processes:
            _, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stderr
        state = json.loads(path.read_text(encoding="utf-8"))
        assert state["tasks"]["task"]["consecutive_failures"] == 15
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
