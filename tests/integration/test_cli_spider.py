import asyncio
import importlib
import json
import sys
import types
import pytest


@pytest.mark.parametrize("outcome", ["failed", "cancelled", "empty"])
def test_cli_distinguishes_failure_cancellation_and_success(tmp_path, monkeypatch, capsys, outcome):
    fake_scraper = types.ModuleType("src.scraper")

    async def scrape(task_config, debug_limit):
        if task_config["id"] == 1:
            if outcome == "failed":
                raise RuntimeError("未找到可用的抓取浏览器")
            if outcome == "cancelled":
                raise asyncio.CancelledError()
        return 0

    fake_scraper.scrape_xianyu = scrape
    monkeypatch.setitem(sys.modules, "src.scraper", fake_scraper)
    sys.modules.pop("spider_v2", None)
    spider = importlib.import_module("spider_v2")
    config = tmp_path / "tasks.json"
    config.write_text(json.dumps([
        {"id": task_id, "task_name": f"task-{task_id}", "keyword": "test", "enabled": True, "decision_mode": "keyword"}
        for task_id in (1, 2)
    ]), encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(spider, "STATE_FILE", str(state))
    monkeypatch.setattr(sys, "argv", ["spider_v2.py", "--config", str(config)])
    if outcome == "failed":
        with pytest.raises(SystemExit) as caught:
            asyncio.run(spider.main())
        assert caught.value.code == 1
    else:
        asyncio.run(spider.main())
    output = capsys.readouterr().out
    assert "任务 'task-2' 正常结束" in output
    expected = {"failed": "因异常而终止", "cancelled": "已取消", "empty": "正常结束"}[outcome]
    assert f"任务 'task-1' {expected}" in output
    if outcome != "empty":
        assert "任务 'task-1' 正常结束" not in output


def test_cli_runs_single_task_with_prompt(tmp_path, load_json_fixture, monkeypatch):
    fake_scraper = types.ModuleType("src.scraper")

    async def placeholder_scrape(task_config, debug_limit):
        return 0

    fake_scraper.scrape_xianyu = placeholder_scrape
    monkeypatch.setitem(sys.modules, "src.scraper", fake_scraper)
    sys.modules.pop("spider_v2", None)

    spider_v2 = importlib.import_module("spider_v2")
    config_data = load_json_fixture("config.sample.json")

    base_prompt = "Base prompt. " + ("x" * 120) + " {{CRITERIA_SECTION}}"
    criteria_prompt = "Criteria text for A7M4."

    base_path = tmp_path / "base_prompt.txt"
    criteria_path = tmp_path / "criteria_prompt.txt"
    base_path.write_text(base_prompt, encoding="utf-8")
    criteria_path.write_text(criteria_prompt, encoding="utf-8")

    config_data[0]["ai_prompt_base_file"] = str(base_path)
    config_data[0]["ai_prompt_criteria_file"] = str(criteria_path)

    config_data[1]["ai_prompt_base_file"] = str(base_path)
    config_data[1]["ai_prompt_criteria_file"] = str(criteria_path)

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_data, ensure_ascii=False), encoding="utf-8")

    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(spider_v2, "STATE_FILE", str(state_path))

    called = []

    async def fake_scrape_xianyu(task_config, debug_limit):
        called.append(task_config["task_name"])
        assert "{{CRITERIA_SECTION}}" not in task_config["ai_prompt_text"]
        assert "Criteria text for A7M4." in task_config["ai_prompt_text"]
        return 1

    monkeypatch.setattr(spider_v2, "scrape_xianyu", fake_scrape_xianyu)
    monkeypatch.setattr(sys, "argv", ["spider_v2.py", "--config", str(config_path), "--task-name", "Sony A7M4"])

    asyncio.run(spider_v2.main())

    assert called == ["Sony A7M4"]


def test_cli_runs_keyword_mode_without_prompt_files(tmp_path, load_json_fixture, monkeypatch):
    fake_scraper = types.ModuleType("src.scraper")

    async def placeholder_scrape(task_config, debug_limit):
        return 0

    fake_scraper.scrape_xianyu = placeholder_scrape
    monkeypatch.setitem(sys.modules, "src.scraper", fake_scraper)
    sys.modules.pop("spider_v2", None)

    spider_v2 = importlib.import_module("spider_v2")
    config_data = load_json_fixture("config.sample.json")
    config_data[0]["enabled"] = True
    config_data[0]["decision_mode"] = "keyword"
    config_data[0]["keyword_rules"] = ["a7m4", "验货宝"]
    config_data[0]["ai_prompt_base_file"] = "missing_base.txt"
    config_data[0]["ai_prompt_criteria_file"] = "missing_criteria.txt"

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_data, ensure_ascii=False), encoding="utf-8")

    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(spider_v2, "STATE_FILE", str(state_path))

    captured = []

    async def fake_scrape_xianyu(task_config, debug_limit):
        captured.append(task_config)
        return 1

    monkeypatch.setattr(spider_v2, "scrape_xianyu", fake_scrape_xianyu)
    monkeypatch.setattr(sys, "argv", ["spider_v2.py", "--config", str(config_path), "--task-name", "Sony A7M4"])

    asyncio.run(spider_v2.main())

    assert len(captured) == 1
    assert captured[0]["decision_mode"] == "keyword"
    assert captured[0]["ai_prompt_text"] == ""


@pytest.mark.parametrize("selector", ["id", "ambiguous_name"])
def test_cli_selects_id_and_refuses_ambiguous_names(tmp_path, monkeypatch, selector):
    fake_scraper = types.ModuleType("src.scraper")
    called = []

    async def scrape(task_config, debug_limit):
        called.append(task_config)
        return 0

    fake_scraper.scrape_xianyu = scrape
    monkeypatch.setitem(sys.modules, "src.scraper", fake_scraper)
    sys.modules.pop("spider_v2", None)
    spider = importlib.import_module("spider_v2")
    configs = [
        {"id": 3, "task_name": "same", "keyword": "camera", "enabled": True, "decision_mode": "keyword"},
        {"id": 9, "task_name": "same", "keyword": "phone", "enabled": True, "decision_mode": "keyword"},
    ]
    config = tmp_path / "tasks.json"
    config.write_text(json.dumps(configs), encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(spider, "STATE_FILE", str(state))
    args = ["--task-id", "9"] if selector == "id" else ["--task-name", "same"]
    monkeypatch.setattr(sys, "argv", ["spider_v2.py", "--config", str(config), *args])
    if selector == "ambiguous_name":
        with pytest.raises(SystemExit):
            asyncio.run(spider.main())
        assert called == []
    else:
        asyncio.run(spider.main())
        assert len(called) == 1
        assert called[0]["id"] == 9
        assert called[0]["keyword"] == "phone"


@pytest.mark.parametrize("ids", [[1, 1], [1, None], [True, 2], ["1", 2], [-1, 2]])
def test_cli_rejects_conflicting_or_invalid_ids_before_running(tmp_path, monkeypatch, ids):
    fake_scraper = types.ModuleType("src.scraper")
    called = []

    async def fake_scrape(task_config, debug_limit=0):
        called.append(task_config)
        return 0

    fake_scraper.scrape_xianyu = fake_scrape
    monkeypatch.setitem(sys.modules, "src.scraper", fake_scraper)
    sys.modules.pop("spider_v2", None)
    spider = importlib.import_module("spider_v2")
    config = tmp_path / "tasks.json"
    config.write_text(json.dumps([
        {"id": task_id, "task_name": f"task-{index}", "enabled": True, "decision_mode": "keyword"}
        for index, task_id in enumerate(ids)
    ]), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["spider_v2.py", "--config", str(config)])
    with pytest.raises(SystemExit) as exc:
        asyncio.run(spider.main())
    assert exc.value.code == 2
    assert not called
