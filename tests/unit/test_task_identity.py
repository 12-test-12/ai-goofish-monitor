import asyncio
import pytest
from concurrent.futures import ThreadPoolExecutor

from src.domain.models.task import Task, TaskUpdate
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
from src.services.task_service import TaskService


def test_status_and_unrelated_updates_preserve_bound_account(sample_task_payload, tmp_path):
    task = Task(**{**sample_task_payload, "account_state_file": "state/camera.json", "account_strategy": "fixed"})
    assert TaskUpdate(is_running=True).model_dump(exclude_unset=True) == {"is_running": True}
    repository = SqliteTaskRepository(str(tmp_path / "tasks.sqlite3"), legacy_config_file=None)

    async def scenario():
        saved = await repository.save(task)
        service = TaskService(repository)
        for update in [TaskUpdate(is_running=True), TaskUpdate(description="updated"), TaskUpdate(is_running=False)]:
            result = await service.update_task(saved.id, update)
            assert result.account_state_file == "state/camera.json"
            assert result.account_strategy == "fixed"
        # Explicit changes still work; omission must not act like clearing.
        result = await service.update_task(saved.id, TaskUpdate(account_state_file=None, account_strategy="auto"))
        assert result.account_state_file is None
        assert result.account_strategy == "auto"

    asyncio.run(scenario())


def test_concurrent_creates_never_overwrite_and_deleted_ids_are_not_reused(sample_task_payload, tmp_path):
    repository = SqliteTaskRepository(str(tmp_path / "tasks.sqlite3"), legacy_config_file=None)

    def create(index):
        return repository._save_sync(Task(**{**sample_task_payload, "task_name": f"task-{index}"}))

    with ThreadPoolExecutor(max_workers=8) as pool:
        saved = list(pool.map(create, range(12)))
    assert len({task.id for task in saved}) == 12
    assert len(repository._find_all_sync()) == 12
    highest = max(task.id for task in saved)
    assert repository._delete_sync(highest)
    assert create(99).id > highest


def test_status_races_do_not_overwrite_account_edits_or_resurrect_deleted_tasks(sample_task_payload, tmp_path):
    repository = SqliteTaskRepository(str(tmp_path / "tasks.sqlite3"), legacy_config_file=None)

    async def scenario():
        task = await repository.save(Task(**{**sample_task_payload, "account_state_file": "state/old.json", "account_strategy": "fixed"}))
        service = TaskService(repository)
        await asyncio.gather(
            service.update_task_status(task.id, True),
            service.update_task(task.id, TaskUpdate(account_state_file="state/new.json")),
        )
        saved = await repository.find_by_id(task.id)
        assert saved.account_state_file == "state/new.json"
        assert saved.is_running is True
        await service.delete_task(task.id)
        with pytest.raises(ValueError):
            await service.update_task_status(task.id, False)
        assert await repository.find_by_id(task.id) is None

    asyncio.run(scenario())
