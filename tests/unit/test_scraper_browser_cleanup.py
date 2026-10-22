import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import scraper


@pytest.mark.parametrize("stage", ["new_context", "add_init_script", "new_page", "join"])
def test_scraper_closes_browser_on_initialization_or_analysis_failure(monkeypatch, tmp_path, stage):
    state_file = tmp_path / "state.json"
    state_file.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    context = SimpleNamespace(add_init_script=AsyncMock(), new_page=AsyncMock())
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    dispatcher = SimpleNamespace(join=AsyncMock())
    if stage == "new_context":
        browser.new_context.side_effect = RuntimeError(stage)
    elif stage == "join":
        # 不抓取页面，直接进入后台分析收尾路径。
        page = SimpleNamespace(goto=AsyncMock(side_effect=scraper.PlaywrightTimeoutError("navigation")))
        context.new_page.return_value = page
        dispatcher.join.side_effect = RuntimeError(stage)
    else:
        getattr(context, stage).side_effect = RuntimeError(stage)

    @asynccontextmanager
    async def playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))

    monkeypatch.setattr(scraper, "async_playwright", playwright)
    monkeypatch.setattr(scraper, "STATE_FILE", str(state_file))
    monkeypatch.setattr(scraper, "load_state_files", lambda _: [])
    monkeypatch.setattr(scraper, "load_price_snapshots", lambda _: [])
    monkeypatch.setattr(scraper, "load_processed_link_keys", lambda _: set())
    monkeypatch.setattr(scraper, "prepare_chromium_launch_args", lambda: [])
    monkeypatch.setattr(scraper, "ItemAnalysisDispatcher", lambda **kwargs: dispatcher)
    monkeypatch.setattr(scraper.FAILURE_GUARD, "should_skip_start", lambda *a, **kw: SimpleNamespace(skip=False))
    notify_failure = AsyncMock()
    monkeypatch.setattr(scraper, "_notify_task_failure", notify_failure)
    monkeypatch.setattr(scraper, "cleanup_task_images", lambda _: None)

    assert asyncio.run(scraper.scrape_xianyu({
        "keyword": "cleanup-test",
        "account_state_file": str(state_file),
        "account_rotation": {"enabled": False, "retry_limit": 1},
        "proxy_rotation": {"enabled": False, "retry_limit": 1},
    })) == 0
    notify_failure.assert_awaited_once()
    assert notify_failure.await_args.args[1] == f"RuntimeError: {stage}"
    browser.close.assert_awaited_once()


def test_managed_browser_closes_on_cancellation():
    async def scenario():
        browser = SimpleNamespace(close=AsyncMock())
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
        entered = asyncio.Event()

        async def run():
            async with scraper._managed_scrape_browser(playwright, {}):
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(run())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        browser.close.assert_awaited_once()

    asyncio.run(scenario())


def test_browser_close_error_preserves_original_failure():
    browser = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("close failed")))
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))

    async def scenario():
        async with scraper._managed_scrape_browser(playwright, {}):
            raise ValueError("original failure")

    with pytest.raises(ValueError, match="original failure"):
        asyncio.run(scenario())
