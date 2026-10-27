"""History remains accessible past the first page; repeat markers use retained runs."""
import csv
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import results
from src.services import result_storage_service as storage
from src.services.price_history_service import record_market_snapshots


FILENAME = "camera_full_data.jsonl"
URL = f"/api/results/{FILENAME}"


@pytest.fixture()
def history_client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "history.sqlite3"))
    app = FastAPI()
    app.include_router(results.router)
    return TestClient(app)


def save(item_id, when="2026-10-26T12:00:00", price="100", recommended=True):
    record = {
        "任务ID": 7, "任务名称": "Camera", "搜索关键字": "camera", "爬取时间": when,
        "商品信息": {"商品ID": str(item_id), "商品标题": f"Camera {item_id}",
                     "当前售价": price, "商品链接": f"https://www.goofish.com/item?id={item_id}"},
        "ai_analysis": {"is_recommended": recommended, "analysis_source": "ai"},
    }
    storage._save_result_record_sync(record, "camera")
    return record


def ids(payload):
    return [row["商品信息"]["商品ID"] for row in payload["items"]]


def test_older_results_remain_on_later_pages_and_after_repeated_save(history_client):
    old = save("yesterday", "2026-10-25T23:59:00")
    for index in range(101):
        save(index)
    assert not storage._save_result_record_sync(old, "camera")
    first = history_client.get(URL, params={"limit": 100, "group_by_day": True}).json()
    second = history_client.get(URL, params={"limit": 100, "page": 2, "group_by_day": True}).json()
    assert first["total_items"] == second["total_items"] == 102
    assert len(first["items"]) == 100
    assert ids(second) == ["0", "yesterday"]
    filtered = history_client.get(URL, params={"crawl_date": "2026-10-25"}).json()
    assert filtered["total_items"] == 1 and ids(filtered) == ["yesterday"]


def test_daily_groups_sort_within_days_before_pagination(history_client):
    save("old-low", "2026-10-25T23:59:00", "1")
    save("new-high", "2026-10-26T02:00:00", "300")
    save("new-low", "2026-10-26T01:00:00", "200")
    params = {"sort_by": "price", "sort_order": "asc", "group_by_day": True, "limit": 2}
    assert ids(history_client.get(URL, params=params).json()) == ["new-low", "new-high"]
    assert ids(history_client.get(URL, params={**params, "page": 2}).json()) == ["old-low"]
    # Existing API clients can still request a global price sort.
    assert ids(history_client.get(URL, params={**params, "group_by_day": False}).json()) == ["old-low", "new-low"]
    assert ids(history_client.get(URL, params={"group_by_day": True, "sort_order": "asc"}).json()) == ["old-low", "new-low", "new-high"]


def test_date_filter_combines_visibility_recommendations_and_export(history_client):
    save("first", "2026-10-25T12:00:00", "100")
    save("second", "2026-10-25T13:00:00", "200", recommended=False)
    save("hidden", "2026-10-25T14:00:00", "300")
    save("other-day", "2026-10-26T12:00:00", "400")
    storage._update_item_status_sync(FILENAME, "hidden", "hidden")
    params = {"crawl_date": "2026-10-25", "group_by_day": True, "sort_by": "price", "sort_order": "desc"}
    response = history_client.get(URL, params=params)
    assert response.status_code == 200
    assert ids(response.json()) == ["second", "first"]
    rows = list(csv.DictReader(io.StringIO(history_client.get(URL + "/export", params=params).text)))
    assert [row["商品ID"] for row in rows] == ["second", "first"]
    assert rows[0]["采集时间"] == "2026-10-25T13:00:00"
    assert rows[0]["出现次数"] == "1" and rows[0]["重点关注"] == "否"
    assert ids(history_client.get(URL, params={**params, "ai_recommended_only": True}).json()) == ["first"]
    assert ids(history_client.get(URL, params={**params, "include_hidden": True}).json()) == ["hidden", "second", "first"]
    empty = history_client.get(URL, params={"crawl_date": "2020-01-01"})
    assert empty.status_code == 200 and empty.json()["total_items"] == 0


@pytest.mark.parametrize("value", ["2026-02-30", "yesterday", "2026-10-25' OR 1=1 --"])
def test_invalid_dates_are_rejected_for_page_and_export(history_client, value):
    assert history_client.get(URL, params={"crawl_date": value}).status_code == 422
    assert history_client.get(URL + "/export", params={"crawl_date": value}).status_code == 422


def test_repeat_counts_span_all_history_and_count_each_run_once(history_client):
    record = save("repeated", "2026-01-01T12:00:00")
    for run, when in [("one", "2026-01-01T12:00:00"), ("two", "2026-10-26T11:00:00"), ("three", "2026-10-26T12:00:00")]:
        for _ in range(2):
            record_market_snapshots(keyword="camera", task_name="Camera", items=[record["商品信息"]] * 2,
                                    run_id=run, snapshot_time=when)
    record_market_snapshots(keyword="another", task_name="Other", items=[record["商品信息"]],
                            run_id="unrelated", snapshot_time="2026-10-27T12:00:00")
    save("single")
    payload = history_client.get(URL).json()
    repeated = next(row for row in payload["items"] if row["商品信息"]["商品ID"] == "repeated")
    assert repeated["occurrence"] == {
        "seen_count": 3, "seen_days": 2, "first_seen_at": "2026-01-01T12:00:00", "last_seen_at": "2026-10-26T12:00:00",
    }
    assert repeated["price_insight"]["observation_count"] == 2
    single = next(row for row in payload["items"] if row["商品信息"]["商品ID"] == "single")
    assert single["occurrence"]["seen_count"] == 1
    rows = list(csv.DictReader(io.StringIO(history_client.get(URL + "/export").text)))
    exported = next(row for row in rows if row["商品ID"] == "repeated")
    assert exported["出现次数"] == "3" and exported["重点关注"] == "是"

def test_occurrences_helper_reentrancy_and_export_robustness(history_client):
    from datetime import date
    from src.services.price_history_service import load_item_occurrences
    from src.services.result_export_service import build_results_csv
    from src.services.result_storage_service import _filter_crawl_date

    # Consecutively call load_item_occurrences (re-entrancy / no table already exists error)
    first_call = load_item_occurrences('camera', {'repeated', 12345})
    second_call = load_item_occurrences('camera', {'repeated'})
    assert isinstance(first_call, dict)
    assert isinstance(second_call, dict)

    # Date object support in _filter_crawl_date
    w, p = _filter_crawl_date('1=1', [], date(2026, 10, 25))
    assert 'substr(crawl_time, 1, 10) = ?' in w
    assert p == ['2026-10-25']

    # Export robustness when occurrence fields are None
    raw_record = {
        '商品信息': {'商品ID': 'none-test'},
        'occurrence': {'seen_count': None, 'seen_days': None, 'first_seen_at': None, 'last_seen_at': None},
    }
    csv_text = build_results_csv([raw_record])
    assert 'none-test' in csv_text

