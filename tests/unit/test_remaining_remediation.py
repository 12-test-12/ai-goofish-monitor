import asyncio
import csv
import io
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import ai_handler
from src.domain.models.task import TaskGenerateRequest
from src.infrastructure.persistence.sqlite_connection import SCHEMA_STATEMENTS, sqlite_connection
from src.services import result_storage_service as storage
from src.services import task_generation_runner as generation
from src.services.image_cache_service import cleanup_expired_images, task_image_lease
from src.services.price_history_service import load_price_snapshots, record_market_snapshots, build_price_history_insights
from src.services.result_export_service import build_results_csv


def record(item_id="1", task_id=None, title="camera", price="100"):
    return {"任务ID": task_id, "任务名称": "same", "爬取时间": "2026-10-22T12:00:00",
            "商品信息": {"商品ID": item_id, "商品标题": title, "当前售价": price,
                         "商品链接": f"https://www.goofish.com/item?id={item_id}"},
            "ai_analysis": {"is_recommended": True, "analysis_source": "ai"}}


def test_task_results_are_independent_and_duplicate_inserts_report_false(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "app.sqlite3"))
    with ThreadPoolExecutor(max_workers=5) as pool:
        saved = list(pool.map(lambda _: storage._save_result_record_sync(record(task_id=7), "camera"), range(5)))
    assert sum(saved) == 1
    assert storage._save_result_record_sync(record(task_id=8), "camera") is True
    assert len(storage.load_processed_link_keys("camera", 7)) == 1
    assert len(storage.load_processed_link_keys("camera", 8)) == 1
    assert not storage.load_processed_link_keys("camera", 9)
    assert not storage.load_processed_link_keys("camera")


def test_old_result_schema_migrates_without_losing_raw_data_or_status(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "app.sqlite3"))
    schema = next(sql for sql in SCHEMA_STATEMENTS if "CREATE TABLE IF NOT EXISTS result_items (" in sql)
    old_schema = schema.replace("        task_id INTEGER NOT NULL DEFAULT -1,\n", "").replace(
        "UNIQUE(result_filename, task_id, link_unique_key)", "UNIQUE(result_filename, link_unique_key)")
    raw = json.dumps(record(), ensure_ascii=False)
    with sqlite_connection() as conn:
        conn.execute(old_schema)
        conn.execute("""INSERT INTO result_items (id, result_filename, keyword, task_name, crawl_time,
            link_unique_key, is_recommended, analysis_source, keyword_hit_count, status, raw_json)
            VALUES (77, 'camera_full_data.jsonl', 'camera', 'same', '', 'legacy', 1, 'ai', 0, 'hidden', ?)""", (raw,))
        conn.commit()
    assert storage._save_result_record_sync(record(task_id=7), "camera")
    with sqlite_connection() as conn:
        row = conn.execute("SELECT * FROM result_items WHERE id=77").fetchone()
        assert row["status"] == "hidden" and row["raw_json"] == raw and row["task_id"] == -1
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(result_items)")}
        assert "idx_results_filename_crawl" in indexes


def test_page_decodes_only_requested_rows_and_blacklist_counts_stay_correct(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "app.sqlite3"))
    for index in range(15):
        storage._save_result_record_sync(record(str(index), title="broken" if index < 5 else "camera", price=str(index)), "camera")
    decoded = []
    original = storage._parse_raw_record

    def parse(*args, **kwargs):
        decoded.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(storage, "_parse_raw_record", parse)
    total, page = storage._query_result_records_sync("camera_full_data.jsonl", False, False, "price", "asc", 2, 3, False)
    assert total == 15 and [row["商品信息"]["商品ID"] for row in page] == ["3", "4", "5"]
    assert len(decoded) == 3
    storage._save_result_blacklist_keywords_sync("camera_full_data.jsonl", ["broken"])
    storage._update_item_status_sync("camera_full_data.jsonl", "5", "hidden")
    total, page = storage._query_result_records_sync("camera_full_data.jsonl", False, False, "price", "asc", 1, 3, False)
    assert total == 9 and [row["商品信息"]["商品ID"] for row in page] == ["6", "7", "8"]


@pytest.mark.parametrize("payload", ["=HYPERLINK(1)", "+cmd", "-cmd", "@SUM(1)", " \t=1", "\n=1", "\ttext", "\ufeff=1"])
def test_csv_external_text_is_not_exported_as_a_formula(payload):
    output = build_results_csv([record(title=payload)])
    row = next(csv.DictReader(io.StringIO(output)))
    assert row["商品标题"] == "'" + payload
    assert row["当前售价"] == "100"


def test_streaming_body_and_file_writes_run_off_the_event_loop(tmp_path, monkeypatch):
    main_thread = threading.get_ident()
    observed = {}

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            observed["closed"] = True
        def raise_for_status(self):
            pass
        def iter_content(self, **_):
            observed["thread"] = threading.get_ident()
            time.sleep(0.03)
            yield b"image"

    monkeypatch.setattr(ai_handler.requests, "get", lambda *a, **kw: Response())
    output = tmp_path / "image.jpg"
    asyncio.run(ai_handler._download_single_image("https://example.com/image", str(output)))
    assert observed["thread"] != main_thread and observed["closed"] is True
    assert output.read_bytes() == b"image"
    assert not list(tmp_path.glob("*.part"))


def test_generated_criteria_are_unique_and_failures_preserve_existing_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("prompts").mkdir()
    old = Path("prompts/camera_criteria.txt")
    old.write_text("old", encoding="utf-8")
    names = {generation.build_criteria_filename("camera") for _ in range(20)}
    assert len(names) == 20
    monkeypatch.setattr(generation, "generate_criteria", AsyncMock(side_effect=RuntimeError("generation failed")))
    job = SimpleNamespace(advance=AsyncMock(), fail=AsyncMock(), complete=AsyncMock())
    asyncio.run(generation.run_ai_generation_job(
        job_id="job", req=TaskGenerateRequest(task_name="camera", keyword="camera", description="camera"),
        task_service=SimpleNamespace(create_task=AsyncMock()), scheduler_service=SimpleNamespace(), generation_service=job,
    ))
    job.fail.assert_awaited_once()
    assert old.read_text(encoding="utf-8") == "old"


def test_scheduler_failure_preserves_the_prompt_for_the_created_task(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(generation, "generate_criteria", AsyncMock(return_value="new criteria"))
    service = SimpleNamespace(create_task=AsyncMock(return_value=SimpleNamespace(id=7)), get_all_tasks=AsyncMock(return_value=[]))
    job = SimpleNamespace(advance=AsyncMock(), fail=AsyncMock(), complete=AsyncMock())
    asyncio.run(generation.run_ai_generation_job(
        job_id="job", req=TaskGenerateRequest(task_name="camera", keyword="camera", description="camera"),
        task_service=service, scheduler_service=SimpleNamespace(reload_jobs=AsyncMock(side_effect=RuntimeError("scheduler failed"))), generation_service=job,
    ))
    path = Path(service.create_task.await_args.args[0].ai_prompt_criteria_file)
    assert path.read_text(encoding="utf-8") == "new criteria"
    job.complete.assert_awaited_once()
    job.fail.assert_not_awaited()


def test_cache_expiry_skips_active_recent_and_unrelated_content(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "app.sqlite3"))
    root = tmp_path / "images"
    root.mkdir()
    import hashlib
    def cache(key):
        path = root / ("task_images_" + hashlib.sha256(key.encode()).hexdigest())
        path.mkdir()
        image = path / "image.jpg"
        image.write_bytes(b"image")
        os.utime(image, (1, 1))
        os.utime(path, (1, 1))
        return path
    stale, active = cache("stale"), cache("active")
    recent = cache("recent")
    os.utime(recent / "image.jpg", None)
    unrelated = root / "task_images_old_name"
    unrelated.mkdir()

    async def run():
        async with task_image_lease("active", root=root):
            assert cleanup_expired_images(root=root) == 1
            assert active.exists()
        assert cleanup_expired_images(root=root) == 1

    asyncio.run(run())
    assert not stale.exists() and not active.exists()
    assert recent.exists() and unrelated.exists()


def test_history_window_is_applied_before_loading_and_dedupes_daily(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "app.sqlite3"))
    for run_id, timestamp, price in [("old", "2025-01-01T12:00:00", "1"), ("today1", "2026-10-22T10:00:00", "100"), ("today2", "2026-10-22T12:00:00", "90")]:
        record_market_snapshots(keyword="camera", task_name="same", items=[record(price=price)["商品信息"]], run_id=run_id, snapshot_time=timestamp)
    assert len(load_price_snapshots("camera")) == 2
    assert len(load_price_snapshots("camera", window_days=None)) == 3
    assert len(load_price_snapshots("camera", dedupe_daily=True)) == 1
    insights = build_price_history_insights("camera")
    assert insights["history_summary"]["unique_items"] == 1
    assert insights["daily_trend"][0]["avg_price"] == 90
