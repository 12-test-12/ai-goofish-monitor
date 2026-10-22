import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.api.routes import accounts, prompts
from src.infrastructure.paths import safe_child_path
from src.domain.models.task import TaskGenerateRequest
from src.services import task_generation_runner
from src.services.task_generation_runner import build_criteria_filename, save_generated_criteria
from src.utils import build_task_log_path, resolve_task_log_path


@pytest.mark.parametrize("filename", ["../secret", "..\\secret", "/secret", "C:\\secret", "C:secret", "\\\\host\\share", "sub/file", "a\0b", "", ".", ".."])
def test_child_path_rejects_traversal_on_all_platforms(tmp_path, filename):
    with pytest.raises(ValueError):
        safe_child_path(tmp_path, filename)


def test_child_path_rejects_symlink_to_sibling_directory(tmp_path):
    root = tmp_path / "logs"
    sibling = tmp_path / "logs-backup"
    root.mkdir()
    sibling.mkdir()
    outside = sibling / "secret.log"
    outside.write_text("secret")
    try:
        (root / "task.log").symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks requires Windows developer mode or privileges")
    with pytest.raises(ValueError):
        safe_child_path(root, "task.log")
    assert outside.read_text() == "secret"


def test_log_paths_confine_task_names_and_legacy_fallback(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("logs").mkdir()
    path = Path(build_task_log_path(7, "../camera\\evil"))
    assert path.parent == tmp_path / "logs"
    assert path.name.endswith("_7.log")
    legacy = Path("logs/old_7.log")
    legacy.write_text("legacy")
    assert Path(resolve_task_log_path(7, "new name")) == legacy.resolve()


def test_account_paths_validate_names_at_file_access(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "_state_dir", lambda: str(tmp_path))
    assert Path(accounts._account_path("camera-1")) == tmp_path / "camera-1.json"
    for name in ["../secret", "..\\secret", "camera\nother", "C:secret"]:
        with pytest.raises(HTTPException) as error:
            accounts._account_path(name)
        assert error.value.status_code == 400


def test_prompt_paths_and_generated_criteria_stay_inside_prompts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(prompts, "_PROMPTS_DIR", tmp_path / "prompts")
    with pytest.raises(HTTPException):
        prompts._safe_prompt_path("../secret.txt")
    filename = build_criteria_filename("../相机\\C:secret")
    asyncio.run(save_generated_criteria(filename, "criteria"))
    generated = prompts._safe_prompt_path(Path(filename).name)
    assert generated.parent == tmp_path / "prompts"
    assert generated.read_text(encoding="utf-8") == "criteria"


def test_rejected_generated_prompt_path_marks_job_failed_without_deleting_target(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("prompts").mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    try:
        Path("prompts/camera_criteria.txt").symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks requires Windows developer mode or privileges")
    monkeypatch.setattr(task_generation_runner, "generate_criteria", AsyncMock(return_value="criteria"))
    monkeypatch.setattr(task_generation_runner, "build_criteria_filename", lambda _: "prompts/camera_criteria.txt")
    generation_service = SimpleNamespace(advance=AsyncMock(), complete=AsyncMock(), fail=AsyncMock())
    task_service = SimpleNamespace(create_task=AsyncMock())
    asyncio.run(task_generation_runner.run_ai_generation_job(
        job_id="test-job",
        req=TaskGenerateRequest(task_name="camera", keyword="camera", description="camera"),
        task_service=task_service,
        scheduler_service=SimpleNamespace(),
        generation_service=generation_service,
    ))
    generation_service.fail.assert_awaited_once()
    task_service.create_task.assert_not_awaited()
    assert outside.read_text() == "secret"
