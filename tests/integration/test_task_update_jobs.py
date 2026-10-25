import asyncio
from pathlib import Path
import threading
import time
from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def isolated_prompt_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)


def wait_for_job(client, job_id):
    for _ in range(200):
        job = client.get(f"/api/tasks/generate-jobs/{job_id}").json()["job"]
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(0.01)
    raise AssertionError("background edit did not finish")


def test_non_ai_edits_do_not_generate_or_reload_unchanged_schedules(api_client, api_context, sample_task_payload, monkeypatch):
    api_client.post("/api/tasks/", json=sample_task_payload)
    generate = AsyncMock(side_effect=AssertionError("ordinary edits must not call AI"))
    monkeypatch.setattr("src.services.task_generation_runner.generate_criteria", generate)
    reloads = api_context["scheduler_service"].reload_calls
    response = api_client.patch("/api/tasks/0", json={
        "min_price": "10", "account_state_file": "state/new.json", "max_pages": 2,
        "description": "  " + sample_task_payload["description"] + "\n",
    })
    assert response.status_code == 200
    assert response.json()["task"]["min_price"] == "10"
    assert api_context["scheduler_service"].reload_calls == reloads
    generate.assert_not_awaited()
    assert api_client.patch("/api/tasks/0", json={"cron": "0 8 * * *"}).status_code == 200
    assert api_context["scheduler_service"].reload_calls == reloads + 1


def test_editing_only_price_preserves_account_rotation_strategy(api_client, sample_task_payload):
    api_client.post("/api/tasks/", json={**sample_task_payload, "account_strategy": "rotate"})
    response = api_client.patch("/api/tasks/0", json={"min_price": "10"})
    assert response.status_code == 200
    assert response.json()["task"]["account_strategy"] == "rotate"


@pytest.mark.parametrize("concurrent_edit", [None, "price", "runtime", "delete"])
def test_slow_ai_edit_returns_job_and_never_overwrites_later_changes(
    api_client, sample_task_payload, monkeypatch, concurrent_edit,
):
    api_client.post("/api/tasks/", json=sample_task_payload)
    release = threading.Event()
    entered = threading.Event()

    async def slow_ai(**kwargs):
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return "generated criteria"

    monkeypatch.setattr("src.services.task_generation_runner.generate_criteria", slow_ai)
    try:
        started = time.monotonic()
        response = api_client.patch("/api/tasks/0", json={"description": "new requirements", "min_price": "50"})
        assert response.status_code == 202
        assert time.monotonic() - started < 2
        job = response.json()["job"]
        assert job["operation"] == "update" and job["task_id"] == 0
        assert entered.wait(1)
        assert api_client.get("/api/tasks/0").json()["description"] == sample_task_payload["description"]
        duplicate = api_client.patch("/api/tasks/0", json={"description": "another request"})
        assert duplicate.status_code == 409
        if concurrent_edit == "price":
            assert api_client.patch("/api/tasks/0", json={"min_price": "99"}).status_code == 200
        elif concurrent_edit == "runtime":
            assert api_client.patch("/api/tasks/0", json={"is_running": True}).status_code == 200
        elif concurrent_edit == "delete":
            assert api_client.delete("/api/tasks/0").status_code == 200
    finally:
        release.set()
    result = wait_for_job(api_client, job["job_id"])
    if concurrent_edit in {"price", "delete"}:
        assert result["status"] == "failed"
        assert not list(Path("prompts").glob("*_criteria.txt"))
        if concurrent_edit == "delete":
            assert api_client.get("/api/tasks/0").status_code == 404
        else:
            current = api_client.get("/api/tasks/0").json()
            assert current["min_price"] == "99"
            assert current["description"] == sample_task_payload["description"]
    else:
        assert result["status"] == "completed", result
        current = api_client.get("/api/tasks/0").json()
        assert current["description"] == "new requirements"
        assert current["is_running"] is (concurrent_edit == "runtime")
        assert Path(current["ai_prompt_criteria_file"]).read_text(encoding="utf-8") == "generated criteria"


@pytest.mark.parametrize("failure", [RuntimeError("model offline"), "", "file_collision"])
def test_failed_ai_edit_keeps_original_task_and_prompt(api_client, sample_task_payload, monkeypatch, failure):
    Path("prompts").mkdir(exist_ok=True)
    original = Path(sample_task_payload["ai_prompt_criteria_file"])
    original.write_text("original criteria", encoding="utf-8")
    before = api_client.post("/api/tasks/", json=sample_task_payload).json()["task"]
    generate = AsyncMock(side_effect=failure) if isinstance(failure, Exception) else AsyncMock(return_value=failure)
    if failure == "file_collision":
        generate.return_value = "new criteria"
        monkeypatch.setattr("src.services.task_generation_runner.build_criteria_filename", lambda _: str(original))
    monkeypatch.setattr("src.services.task_generation_runner.generate_criteria", generate)
    response = api_client.patch("/api/tasks/0", json={"description": "new requirements", "max_pages": 9})
    assert response.status_code == 202
    result = wait_for_job(api_client, response.json()["job"]["job_id"])
    assert result["status"] == "failed"
    assert api_client.get("/api/tasks/0").json() == before
    assert original.read_text(encoding="utf-8") == "original criteria"


def test_explicit_regenerate_works_with_unchanged_description(api_client, sample_task_payload, monkeypatch):
    api_client.post("/api/tasks/", json=sample_task_payload)
    generate = AsyncMock(return_value="regenerated")
    monkeypatch.setattr("src.services.task_generation_runner.generate_criteria", generate)
    response = api_client.patch("/api/tasks/0", json={"regenerate_criteria": True})
    assert response.status_code == 202
    assert wait_for_job(api_client, response.json()["job"]["job_id"])["status"] == "completed"
    generate.assert_awaited_once()
