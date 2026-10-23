import asyncio
import csv
import io
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
from src.services import notification_outbox_service as outbox
from src.services import result_storage_service as storage
from src.services import result_download_service as downloads
from src.services import result_file_service as files
from src.services.price_history_service import record_market_snapshots
from src.services.dashboard_service import build_dashboard_snapshot
from src.domain.models.task import Task
from src.services.notification_service import NotificationService
from src.services.process_service import ProcessService, TaskDeletionError


@pytest.fixture()
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "app.sqlite3"))
    return tmp_path


def result(item_id="1", task_id=7, task_name="same", keyword="camera", recommended=True):
    return {"任务ID": task_id, "任务名称": task_name, "搜索关键字": keyword,
            "爬取时间": "2026-10-22T12:00:00", "商品信息": {"商品ID": item_id, "商品标题": "camera",
            "当前售价": "100", "商品链接": f"https://www.goofish.com/item?id={item_id}"},
            "ai_analysis": {"is_recommended": recommended, "analysis_source": "ai", "reason": "recommended"}}


def task(task_id, name="same", keyword="camera"):
    return Task(id=task_id, task_name=name, keyword=keyword, enabled=True, max_pages=1, personal_only=True,
                ai_prompt_base_file="", ai_prompt_criteria_file="")


@pytest.mark.parametrize("raw", ["{broken", "[]", "null", '"text"', '{"商品信息": []}', '{"ai_analysis": "text"}', '{"卖家信息": 1}'])
@pytest.mark.parametrize("blacklist", [[], ["broken"]])
def test_dirty_rows_are_excluded_from_pages_counts_ids_and_summaries(isolated_storage, raw, blacklist):
    storage._save_result_record_sync(result("good", recommended=False), "camera")
    storage._save_result_record_sync(result("bad"), "camera")
    filename = "camera_full_data.jsonl"
    with sqlite_connection() as conn:
        conn.execute("UPDATE result_items SET raw_json=?, crawl_time='2099-01-01' WHERE item_id='bad'", (raw,))
        conn.commit()
    storage._save_result_blacklist_keywords_sync(filename, blacklist)
    for hidden in (False, True):
        total, page = storage._query_result_records_sync(filename, False, False, "crawl_time", "desc", 1, 1, hidden)
        assert total == 1 and page[0]["商品信息"]["商品ID"] == "good"
    summary = storage._load_result_summary_sync(filename)
    assert summary["total_items"] == 1 and summary["recommended_items"] == 0
    assert summary["latest_record"]["商品信息"]["商品ID"] == "good"
    assert storage.load_visible_result_item_ids(filename) == {"good"}
    assert len(storage.load_processed_link_keys("camera", 7)) == 1
    assert len(storage._load_all_result_records_sync(filename, False, False, "price", "asc", True)) == 1
    assert len(storage._build_result_ndjson_sync(filename).splitlines()) == 1


def test_null_optional_fields_and_non_list_blacklist_are_safe(isolated_storage):
    payload = result(recommended=False)
    payload.update({"商品信息": None, "卖家信息": None, "ai_analysis": None, "price_insight": None})
    storage._save_result_record_sync(payload, "camera")
    filename = "camera_full_data.jsonl"
    storage._save_result_blacklist_keywords_sync(filename, ["broken"])
    total, page = storage._query_result_records_sync(filename, False, False, "price", "asc", 1, 20, False)
    assert total == 1 and page[0]["商品信息"] == {}
    with sqlite_connection() as conn:
        conn.execute("UPDATE result_blacklist_rules SET blacklist_keywords_json='123'")
        conn.commit()
    assert storage._load_result_blacklist_keywords_sync(filename) == []


def test_dashboard_keeps_namesakes_and_assigns_same_keyword_results_by_id(isolated_storage):
    tasks = [task(7), task(8)]
    assert len(asyncio.run(build_dashboard_snapshot(tasks))["task_summaries"]) == 2
    storage._save_result_record_sync(result("1", 7), "camera")
    storage._save_result_record_sync(result("2", 8, recommended=False), "camera")
    snapshot = asyncio.run(build_dashboard_snapshot(tasks))
    summaries = {row["task_id"]: row for row in snapshot["task_summaries"]}
    assert summaries[7]["recommended_items"] == 1
    assert summaries[8]["recommended_items"] == 0
    assert all(row["total_items"] == 1 for row in summaries.values())
    assert snapshot["summary"]["result_files"] == 1
    ids = [row["id"] for row in snapshot["recent_activities"]]
    assert len(ids) == len(set(ids))


def test_dashboard_merges_one_task_across_keywords_without_losing_totals(isolated_storage):
    storage._save_result_record_sync(result("1", 7, keyword="old"), "old")
    storage._save_result_record_sync(result("2", 7, keyword="new"), "new")
    snapshot = asyncio.run(build_dashboard_snapshot([task(7, keyword="new")]))
    assert len(snapshot["task_summaries"]) == 1
    assert snapshot["task_summaries"][0]["total_items"] == 2
    assert snapshot["summary"]["result_files"] == 2
    assert snapshot["summary"]["scanned_items"] == 2


def queue_rows():
    with sqlite_connection() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM notification_outbox ORDER BY id")]


def test_notification_failure_is_durable_and_retries_only_failed_channels(isolated_storage):
    assert storage._save_result_record_sync(result(), "camera")
    assert not storage._save_result_record_sync(result(), "camera")
    assert len(queue_rows()) == 1
    sender = AsyncMock(side_effect=[{"bark": {"success": True}, "telegram": {"success": False}},
                                   {"telegram": {"success": True}}])
    assert asyncio.run(outbox.dispatch_pending_notifications(sender=sender, now=0)) == 0
    assert json.loads(queue_rows()[0]["pending_channels_json"]) == ["telegram"]
    assert asyncio.run(outbox.dispatch_pending_notifications(sender=sender, now=29)) == 0
    assert sender.await_count == 1
    assert asyncio.run(outbox.dispatch_pending_notifications(sender=sender, now=31)) == 1
    assert sender.await_args.args[2] == ["telegram"]
    assert not queue_rows()


def test_notification_crash_or_cancel_releases_lock_and_preserves_retry(isolated_storage):
    storage._save_result_record_sync(result(), "camera")
    # Simulate a worker dying after claiming, before acknowledging any delivery.
    assert outbox._claim(0) is not None
    sender = AsyncMock(return_value={"bark": {"success": True}})
    assert asyncio.run(outbox.dispatch_pending_notifications(sender=sender, now=10)) == 0
    assert asyncio.run(outbox.dispatch_pending_notifications(sender=sender, now=31)) == 1
    assert not queue_rows()


def test_only_one_notification_dispatcher_sends_a_claimed_batch(isolated_storage):
    storage._save_result_record_sync(result(), "camera")

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def send(*_):
            entered.set()
            await release.wait()
            return {"bark": {"success": True}}
        first = asyncio.create_task(outbox.dispatch_pending_notifications(sender=send, now=0))
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert await outbox.dispatch_pending_notifications(sender=send, now=0) == 0
        release.set()
        assert await first == 1
    asyncio.run(run())


def test_notification_outbox_insert_and_result_save_are_atomic(isolated_storage, monkeypatch):
    monkeypatch.setattr(outbox, "enqueue_recommendation", lambda *args: (_ for _ in ()).throw(RuntimeError("queue failed")))
    with pytest.raises(RuntimeError, match="queue failed"):
        storage._save_result_record_sync(result(), "camera")
    with sqlite_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM result_items").fetchone()[0] == 0


def test_deleted_results_cancel_their_pending_notifications(isolated_storage):
    storage._save_result_record_sync(result(), "camera")
    assert len(queue_rows()) == 1
    storage._delete_result_file_records_sync("camera_full_data.jsonl")
    assert not queue_rows()


def test_deleting_a_task_cancels_only_its_notifications_and_keeps_history(isolated_storage):
    repository = SqliteTaskRepository(legacy_config_file=None)
    repository._save_sync(task(7))
    repository._save_sync(task(8))
    storage._save_result_record_sync(result("1", 7), "camera")
    storage._save_result_record_sync(result("2", 8), "camera")
    assert len(queue_rows()) == 2
    assert repository._delete_sync(7)
    assert len(queue_rows()) == 1
    with sqlite_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM result_items").fetchone()[0] == 2
        row = conn.execute("SELECT task_id FROM result_items WHERE id=?", (queue_rows()[0]["result_id"],)).fetchone()
        assert row["task_id"] == 8


def test_explicit_false_from_notification_client_is_a_failure():
    client = SimpleNamespace(channel_key="test", display_name="test", is_enabled=lambda: True, send=AsyncMock(return_value=False))
    result_map = asyncio.run(NotificationService([client]).send_notification({}, "test"))
    assert result_map["test"]["success"] is False


def test_start_cannot_race_with_deletion_and_busy_start_rejects_delete():
    service = ProcessService()
    async def run():
        async with service.protect_task_deletion(7):
            assert await service.start_task(7, "same") is False
        assert await service.start_task(7, "stale scheduled job") is False
        service._starting_task_ids.add(8)
        with pytest.raises(TaskDeletionError):
            async with service.protect_task_deletion(8):
                pytest.fail("cannot delete a starting task")
        with pytest.raises(ValueError):
            async with service.protect_task_deletion(9):
                raise ValueError("stop failed")
        assert 9 not in service._deleted_task_ids and 9 not in service._deleting_task_ids
    asyncio.run(run())


def test_cancelled_notification_sender_leaves_a_recoverable_job(isolated_storage):
    storage._save_result_record_sync(result(), "camera")
    async def run():
        entered = asyncio.Event()
        async def send(*_):
            entered.set()
            await asyncio.Event().wait()
        worker = asyncio.create_task(outbox.dispatch_pending_notifications(sender=send, now=0))
        await asyncio.wait_for(entered.wait(), timeout=2)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        sender = AsyncMock(return_value={"bark": {"success": True}})
        assert await outbox.dispatch_pending_notifications(sender=sender, now=31) == 1
    asyncio.run(run())
    assert not queue_rows()


def test_exception_before_channel_results_retries_without_losing_the_job(isolated_storage):
    storage._save_result_record_sync(result(), "camera")
    sender = AsyncMock(side_effect=[RuntimeError("network down"), {"bark": {"success": True}}])
    assert asyncio.run(outbox.dispatch_pending_notifications(sender=sender, now=0)) == 0
    assert queue_rows()[0]["last_error"] == "RuntimeError"
    assert asyncio.run(outbox.dispatch_pending_notifications(sender=sender, now=31)) == 1


def test_visible_market_summary_is_aggregated_without_loading_all_ids(isolated_storage):
    for index, price in enumerate((100, 200, 300, 400)):
        payload = result(str(index))
        payload["商品信息"]["当前售价"] = str(price)
        storage._save_result_record_sync(payload, "camera")
        record_market_snapshots(keyword="camera", task_name="same", items=[payload["商品信息"]], run_id="run", snapshot_time="2026-10-22T12:00:00")
    market = storage.load_visible_market_summary("camera_full_data.jsonl", "camera")
    assert market["avg_price"] == 250 and market["median_price"] == 250
    storage._update_item_status_sync("camera_full_data.jsonl", "3", "hidden")
    market = storage.load_visible_market_summary("camera_full_data.jsonl", "camera")
    assert market["sample_count"] == 3 and market["avg_price"] == 200 and market["median_price"] == 200
    enriched = files.enrich_records_with_price_insight([result("0")], "camera_full_data.jsonl")
    assert enriched[0]["price_insight"]["market_avg_price"] == 200


def test_download_batches_stay_bounded_and_temp_files_are_removed(isolated_storage, monkeypatch):
    for index in range(7):
        storage._save_result_record_sync(result(str(index)), "camera")
    batches = list(storage.iter_result_batches("camera_full_data.jsonl", batch_size=3))
    assert [len(batch) for batch in batches] == [3, 3, 1]
    original = downloads.tempfile.NamedTemporaryFile
    monkeypatch.setattr(downloads.tempfile, "NamedTemporaryFile", lambda *args, **kwargs: original(*args, dir=isolated_storage, **kwargs))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.api.routes import results
    app = FastAPI()
    app.include_router(results.router)
    client = TestClient(app)
    response = client.get("/api/results/camera_full_data.jsonl/export")
    assert response.status_code == 200
    assert len(list(csv.DictReader(io.StringIO(response.text)))) == 7
    assert not list(isolated_storage.glob("*.csv"))
    response = client.get("/api/results/files/camera_full_data.jsonl")
    assert response.status_code == 200 and len(response.text.splitlines()) == 7
    assert not list(isolated_storage.glob("*.jsonl"))


def test_cancelled_export_cleans_the_file_after_its_worker_finishes(isolated_storage, monkeypatch):
    started, release = threading.Event(), threading.Event()
    output = isolated_storage / "cancelled.csv"
    def write(*_):
        started.set()
        assert release.wait(timeout=3)
        output.write_text("done", encoding="utf-8")
        return str(output)
    monkeypatch.setattr(downloads, "_create_download", write)
    async def run():
        worker = asyncio.create_task(downloads.create_result_download("camera_full_data.jsonl", csv_export=True))
        assert await asyncio.to_thread(started.wait, 2)
        worker.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await worker
    asyncio.run(run())
    assert not output.exists()


def test_api_price_work_runs_outside_its_event_loop(monkeypatch):
    from src.api.routes import results
    loop_thread = None
    observed = []
    def enrich(records, _filename):
        observed.append(threading.get_ident())
        assert threading.get_ident() != loop_thread
        return records
    monkeypatch.setattr(results, "enrich_records_with_price_insight", enrich)
    monkeypatch.setattr(results, "query_result_records", AsyncMock(return_value=(1, [{}])))
    async def run():
        nonlocal loop_thread
        loop_thread = threading.get_ident()
        await results.get_result_file_content("camera_full_data.jsonl", page=1, limit=20,
            recommended_only=False, ai_recommended_only=False, keyword_recommended_only=False,
            include_hidden=False, sort_by="crawl_time", sort_order="desc")
    asyncio.run(run())
    assert observed


def test_tray_missing_icon_uses_fallback_without_raising(monkeypatch):
    import sys
    from unittest.mock import Mock
    from src.desktop.shell import DesktopTray
    window = Mock()
    fake_pystray = Mock()
    fake_pil = Mock()
    fake_image_mod = Mock()
    fake_pil.Image = fake_image_mod
    monkeypatch.setattr("sys.modules", {**sys.modules, "pystray": fake_pystray, "PIL": fake_pil, "PIL.Image": fake_image_mod})
    tray = DesktopTray(window, Path("non_existent_icon.png"))
    tray.start()
    assert fake_pystray.Icon.called
    assert tray.icon is not None


def test_notification_outbox_drops_poison_pill_after_max_attempts(tmp_path, monkeypatch):
    import time
    from src.services import notification_outbox_service as outbox
    db_file = tmp_path / "test.sqlite3"
    monkeypatch.setenv("APP_DATABASE_FILE", str(db_file))
    storage.bootstrap_sqlite_storage()

    with storage.sqlite_connection() as conn:
        conn.execute("INSERT INTO result_items (id, result_filename, keyword, task_name, crawl_time, link_unique_key, is_recommended, keyword_hit_count, raw_json) VALUES (1, 't.jsonl', 'k', 'n', 'c', 'l', 1, 0, '{}')")
        conn.execute("INSERT INTO notification_outbox (id, result_id, product_json, reason, attempts, available_at, last_error) VALUES (1, 1, '{}', 'r', 15, 0, 'Permanent error')")
        conn.commit()

    claimed = outbox._claim(time.time(), max_attempts=15)
    assert claimed is None

    with storage.sqlite_connection() as conn:
        row = conn.execute("SELECT * FROM notification_outbox WHERE id = 1").fetchone()
        assert row is None


def test_validate_result_filename_rejects_windows_slashes_and_empty():
    from src.services.result_file_service import validate_result_filename
    with pytest.raises(ValueError):
        validate_result_filename("sub\\file.jsonl")
    with pytest.raises(ValueError):
        validate_result_filename("sub/file.jsonl")
    with pytest.raises(ValueError):
        validate_result_filename("..\\file.jsonl")
    with pytest.raises(ValueError):
        validate_result_filename("")
    validate_result_filename("safe_full_data.jsonl")


def test_write_result_ndjson_writes_complete_trailing_newlines(tmp_path, monkeypatch):
    import io
    db_file = tmp_path / "test.sqlite3"
    monkeypatch.setenv("APP_DATABASE_FILE", str(db_file))
    storage.bootstrap_sqlite_storage()

    with storage.sqlite_connection() as conn:
        conn.execute("INSERT INTO result_items (id, result_filename, keyword, task_name, crawl_time, link_unique_key, is_recommended, keyword_hit_count, raw_json) VALUES (1, 't.jsonl', 'k', 'n', 'c', 'l', 1, 0, '{\"item\": 1}')")
        conn.execute("INSERT INTO result_items (id, result_filename, keyword, task_name, crawl_time, link_unique_key, is_recommended, keyword_hit_count, raw_json) VALUES (2, 't.jsonl', 'k', 'n', 'c', 'l2', 1, 0, '{\"item\": 2}')")
        conn.commit()

    buf = io.StringIO()
    storage.write_result_ndjson("t.jsonl", buf)
    lines = buf.getvalue().splitlines()
    assert len(lines) == 2
    assert buf.getvalue().endswith("\n")
