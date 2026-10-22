import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import scraper
from src.failure_guard import FailureGuard


@pytest.mark.parametrize("failure_stage", ["detail_risk", "search_risk", "login_redirect", "login_timeout", "session_expired"])
def test_detail_risk_control_stops_before_next_item_and_cancels_analysis(tmp_path, monkeypatch, failure_stage):
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "app.sqlite3"))
    state = tmp_path / "state.json"
    state.write_text('{"cookies": []}', encoding="utf-8")
    search_response = SimpleNamespace(ok=True, json=AsyncMock(return_value={}))
    risk_response = SimpleNamespace(ok=True, json=AsyncMock(return_value={"ret": ["FAIL_SYS_USER_VALIDATE::risk"]}))
    if failure_stage == "search_risk":
        search_response = risk_response
    elif failure_stage == "session_expired":
        risk_response.json.return_value = {"ret": ["FAIL_SYS_SESSION_EXPIRED::login"]}

    @asynccontextmanager
    async def expected_response(response):
        future = asyncio.get_running_loop().create_future()
        future.set_result(response)
        yield SimpleNamespace(value=future)

    page = SimpleNamespace(
        url="https://www.goofish.com/search", goto=AsyncMock(), evaluate=AsyncMock(),
        wait_for_selector=AsyncMock(), click=AsyncMock(side_effect=scraper.PlaywrightTimeoutError("no ad")),
        locator=lambda *_: SimpleNamespace(wait_for=AsyncMock(side_effect=scraper.PlaywrightTimeoutError("no dialog"))),
        expect_response=lambda *a, **kw: expected_response(search_response),
    )
    detail = SimpleNamespace(goto=AsyncMock(), close=AsyncMock(), expect_response=lambda *a, **kw: expected_response(risk_response))
    if failure_stage in {"login_redirect", "login_timeout"}:
        detail.url = "https://passport.goofish.com/mini_login.htm"
    if failure_stage == "login_timeout":
        detail.goto.side_effect = scraper.PlaywrightTimeoutError("login navigation")
    context = SimpleNamespace(add_init_script=AsyncMock(), new_page=AsyncMock(side_effect=[page, detail]))
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    playwright_instance = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
    dispatcher = SimpleNamespace(join=AsyncMock(), cancel=AsyncMock())

    @asynccontextmanager
    async def playwright():
        yield playwright_instance

    monkeypatch.setattr(scraper, "async_playwright", playwright)
    monkeypatch.setattr(scraper, "load_state_files", lambda _: [])
    monkeypatch.setattr(scraper, "load_price_snapshots", lambda _: [])
    monkeypatch.setattr(scraper, "load_processed_link_keys", lambda _: set())
    monkeypatch.setattr(scraper, "record_market_snapshots", lambda **kw: [])
    monkeypatch.setattr(scraper, "random_sleep", AsyncMock())
    monkeypatch.setattr(scraper, "prepare_chromium_launch_args", lambda: [])
    monkeypatch.setattr(scraper, "ItemAnalysisDispatcher", lambda **kw: dispatcher)
    monkeypatch.setattr(scraper, "FAILURE_GUARD", FailureGuard(path=str(tmp_path / "guard.json")))
    monkeypatch.setattr(scraper, "cleanup_task_images", lambda _: None)
    failure = AsyncMock()
    monkeypatch.setattr(scraper, "_notify_task_failure", failure)
    monkeypatch.setattr(scraper, "_parse_search_results_json", AsyncMock(return_value=[
        {"商品链接": f"https://www.goofish.com/item?id={index}", "商品标题": "test", "商品ID": str(index)}
        for index in range(2)
    ]))

    result = asyncio.run(scraper.scrape_xianyu({
        "id": 42, "task_name": "risk-test", "keyword": "test", "max_pages": 2,
        "account_state_file": str(state), "account_rotation": {"retry_limit": 3},
        "proxy_rotation": {"enabled": False, "retry_limit": 3},
    }))
    assert result == 0
    assert context.new_page.await_count == (1 if failure_stage == "search_risk" else 2)
    playwright_instance.chromium.launch.assert_awaited_once()
    dispatcher.cancel.assert_awaited_once()
    assert detail.close.await_count == (0 if failure_stage == "search_risk" else 1)
    browser.close.assert_awaited_once()
    failure.assert_awaited_once()
    assert failure.await_args.args[1].startswith(
        "RiskControlError:" if failure_stage.endswith("risk") else "LoginRequiredError:"
    )


def test_first_risk_control_failure_opens_circuit_for_only_its_task_id(tmp_path, monkeypatch):
    guard = FailureGuard(path=str(tmp_path / "guard.json"), threshold=3)
    monkeypatch.setattr(scraper, "FAILURE_GUARD", guard)
    monkeypatch.setattr(scraper, "send_ntfy_notification", AsyncMock())
    asyncio.run(scraper._notify_task_failure({"id": 7, "task_name": "same"}, "FAIL_SYS_USER_VALIDATE", cookie_path=None))
    assert guard.should_skip_start(guard.task_key(7, "same")).skip is True
    assert guard.should_skip_start(guard.task_key(8, "same")).skip is False


@pytest.mark.parametrize("reason", ["RiskControlError: verification required", "LoginRequiredError: login required"])
def test_typed_failures_pause_immediately_without_matching_specific_text(tmp_path, monkeypatch, reason):
    guard = FailureGuard(path=str(tmp_path / "guard.json"), threshold=3)
    monkeypatch.setattr(scraper, "FAILURE_GUARD", guard)
    monkeypatch.setattr(scraper, "send_ntfy_notification", AsyncMock())
    asyncio.run(scraper._notify_task_failure({"id": 7, "task_name": "same"}, reason, cookie_path=None))
    decision = guard.should_skip_start(guard.task_key(7, "same"))
    assert decision.skip is True
    assert decision.consecutive_failures == 1
