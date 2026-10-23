import asyncio
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import ai_handler, scraper
from src.failure_guard import FailureGuard
from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.services import notification_outbox_service as outbox
from src.services import result_storage_service as storage
from src.services.image_cache_service import cleanup_expired_images


@pytest.mark.parametrize("failure", [RuntimeError("history failed"), asyncio.CancelledError()])
def test_early_failure_or_cancellation_still_cleans_task_images(tmp_path, monkeypatch, failure):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "app.sqlite3"))
    cleanup = []
    monkeypatch.setattr(scraper, "cleanup_task_images", cleanup.append)

    def fail(_keyword):
        raise failure

    monkeypatch.setattr(scraper, "load_price_snapshots", fail)
    with pytest.raises(type(failure)):
        asyncio.run(scraper.scrape_xianyu({"id": 7, "keyword": "camera"}))
    assert cleanup == ["task-id:7"]


def test_active_task_images_survive_expiry_sweep_and_are_cleaned_on_exit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "app.sqlite3"))
    cache = ai_handler._task_image_directory("task-id:7")
    cache.mkdir(parents=True)
    image = cache / "item.jpg"
    image.write_bytes(b"image")
    os.utime(image, (1, 1))
    os.utime(cache, (1, 1))
    sweeps = []

    def history(_keyword):
        sweeps.append(cleanup_expired_images())
        assert image.exists()
        raise RuntimeError("stop before browsing")

    monkeypatch.setattr(scraper, "load_price_snapshots", history)
    with pytest.raises(RuntimeError, match="stop before browsing"):
        asyncio.run(scraper.scrape_xianyu({"id": 7, "keyword": "camera"}))
    assert sweeps == [0]
    assert not cache.exists()


def test_scraping_isolates_tasks_and_sends_each_recommendation_once(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "app.sqlite3"))
    state = tmp_path / "state.json"
    state.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    response = SimpleNamespace(ok=True, json=AsyncMock(return_value={}))

    @asynccontextmanager
    async def expect_response(*_args, **_kwargs):
        future = asyncio.get_running_loop().create_future()
        future.set_result(response)
        yield SimpleNamespace(value=future)

    def make_context(**_kwargs):
        page = SimpleNamespace(
            url="https://www.goofish.com/search", goto=AsyncMock(), evaluate=AsyncMock(),
            wait_for_selector=AsyncMock(), expect_response=expect_response,
            click=AsyncMock(side_effect=scraper.PlaywrightTimeoutError("no ad")),
            locator=lambda *_: SimpleNamespace(wait_for=AsyncMock(side_effect=scraper.PlaywrightTimeoutError("no dialog"))),
        )
        detail = SimpleNamespace(goto=AsyncMock(), close=AsyncMock(), expect_response=expect_response)
        return SimpleNamespace(add_init_script=AsyncMock(), new_page=AsyncMock(side_effect=[page, detail]))

    @asynccontextmanager
    async def playwright():
        browser = SimpleNamespace(new_context=AsyncMock(side_effect=make_context), close=AsyncMock())
        yield SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))

    monkeypatch.setattr(scraper, "async_playwright", playwright)
    monkeypatch.setattr(scraper, "load_state_files", lambda _: [])
    monkeypatch.setattr(scraper, "load_price_snapshots", lambda _: [])
    monkeypatch.setattr(scraper, "record_market_snapshots", lambda **_: [])
    monkeypatch.setattr(scraper, "build_market_reference", lambda **_: {})
    monkeypatch.setattr(scraper, "prepare_chromium_launch_args", lambda: [])
    monkeypatch.setattr(scraper, "random_sleep", AsyncMock())
    monkeypatch.setattr(scraper, "scrape_user_profile", AsyncMock(return_value={}))
    monkeypatch.setattr(scraper, "FAILURE_GUARD", FailureGuard(path=str(tmp_path / "guard.json")))
    monkeypatch.setattr(scraper, "_parse_search_results_json", AsyncMock(side_effect=lambda *_: [
        {"商品ID": "1", "商品标题": "camera", "商品链接": "https://www.goofish.com/item?id=1"},
    ]))
    sender = AsyncMock(return_value={"bark": {"success": True}})
    monkeypatch.setattr(outbox, "_send", sender)
    direct_sender = AsyncMock()
    monkeypatch.setattr(scraper, "send_ntfy_notification", direct_sender)
    results = []
    for task_id in (7, 8, 7):
        results.append(asyncio.run(scraper.scrape_xianyu({
            "id": task_id, "task_name": "same", "keyword": "camera", "max_pages": 1,
            "decision_mode": "keyword", "keyword_rules": ["camera"],
            "account_state_file": str(state), "proxy_rotation": {"enabled": False},
        })))
    assert results == [1, 1, 0]
    assert len(storage.load_processed_link_keys("camera", 7)) == 1
    assert len(storage.load_processed_link_keys("camera", 8)) == 1
    assert sender.await_count == 2
    direct_sender.assert_not_awaited()
    with sqlite_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 0
