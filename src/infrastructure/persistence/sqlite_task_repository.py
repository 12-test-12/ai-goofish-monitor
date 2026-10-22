"""
基于 SQLite 的任务仓储实现。
"""
from __future__ import annotations

import asyncio
import json
from typing import List, Optional

from src.domain.models.task import Task, TaskUpdate
from src.domain.repositories.task_repository import TaskRepository
from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import sqlite_connection


def _row_to_task(row) -> Task:
    payload = dict(row)
    payload["enabled"] = bool(payload["enabled"])
    payload["analyze_images"] = bool(payload["analyze_images"])
    payload["personal_only"] = bool(payload["personal_only"])
    payload["free_shipping"] = bool(payload["free_shipping"])
    payload["is_running"] = bool(payload["is_running"])
    payload["keyword_rules"] = json.loads(payload.pop("keyword_rules_json") or "[]")
    return Task(**payload)


def find_task_by_name_sync(task_name: str) -> Task | None:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE task_name = ? ORDER BY id ASC LIMIT 1",
            (task_name,),
        ).fetchone()
    return _row_to_task(row) if row else None


def find_task_by_id_sync(task_id: int) -> Task | None:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return _row_to_task(row) if row else None


class SqliteTaskRepository(TaskRepository):
    """基于 SQLite 的任务仓储"""

    def __init__(
        self,
        db_path: str | None = None,
        legacy_config_file: str | None = "config.json",
    ):
        self.db_path = db_path
        self.legacy_config_file = legacy_config_file

    async def find_all(self) -> List[Task]:
        return await asyncio.to_thread(self._find_all_sync)

    async def find_by_id(self, task_id: int) -> Optional[Task]:
        return await asyncio.to_thread(self._find_by_id_sync, task_id)

    async def save(self, task: Task) -> Task:
        return await asyncio.to_thread(self._save_sync, task)

    async def update(self, task_id: int, changes: TaskUpdate) -> Optional[Task]:
        return await asyncio.to_thread(self._update_sync, task_id, changes)

    async def delete(self, task_id: int) -> bool:
        return await asyncio.to_thread(self._delete_sync, task_id)

    def _find_all_sync(self) -> List[Task]:
        bootstrap_sqlite_storage(
            self.db_path,
            legacy_config_file=self.legacy_config_file,
        )
        with sqlite_connection(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM tasks ORDER BY id ASC").fetchall()
        return [_row_to_task(row) for row in rows]

    def _find_by_id_sync(self, task_id: int) -> Optional[Task]:
        bootstrap_sqlite_storage(
            self.db_path,
            legacy_config_file=self.legacy_config_file,
        )
        with sqlite_connection(self.db_path) as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None

    def _save_sync(self, task: Task) -> Task:
        bootstrap_sqlite_storage(
            self.db_path,
            legacy_config_file=self.legacy_config_file,
        )
        with sqlite_connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            task_id = task.id
            if task_id is None:
                task_id = self._next_task_id(conn)
            payload = self._task_values(task.model_copy(update={"id": task_id}))
            conn.execute(
                """
                INSERT INTO tasks (
                    id, task_name, enabled, keyword, description, analyze_images,
                    max_pages, personal_only, min_price, max_price, cron,
                    ai_prompt_base_file, ai_prompt_criteria_file, account_state_file,
                    account_strategy, free_shipping, new_publish_option, region,
                    decision_mode, keyword_rules_json, is_running
                ) VALUES (
                    :id, :task_name, :enabled, :keyword, :description, :analyze_images,
                    :max_pages, :personal_only, :min_price, :max_price, :cron,
                    :ai_prompt_base_file, :ai_prompt_criteria_file, :account_state_file,
                    :account_strategy, :free_shipping, :new_publish_option, :region,
                    :decision_mode, :keyword_rules_json, :is_running
                )
                ON CONFLICT(id) DO UPDATE SET
                    task_name=excluded.task_name, enabled=excluded.enabled,
                    keyword=excluded.keyword, description=excluded.description,
                    analyze_images=excluded.analyze_images, max_pages=excluded.max_pages,
                    personal_only=excluded.personal_only, min_price=excluded.min_price,
                    max_price=excluded.max_price, cron=excluded.cron,
                    ai_prompt_base_file=excluded.ai_prompt_base_file,
                    ai_prompt_criteria_file=excluded.ai_prompt_criteria_file,
                    account_state_file=excluded.account_state_file,
                    account_strategy=excluded.account_strategy, free_shipping=excluded.free_shipping,
                    new_publish_option=excluded.new_publish_option, region=excluded.region,
                    decision_mode=excluded.decision_mode, keyword_rules_json=excluded.keyword_rules_json,
                    is_running=excluded.is_running
                """,
                payload,
            )
            self._remember_task_id(conn, task_id)
            conn.commit()
        return task.model_copy(update={"id": task_id})

    def _update_sync(self, task_id: int, changes: TaskUpdate) -> Optional[Task]:
        bootstrap_sqlite_storage(self.db_path, legacy_config_file=self.legacy_config_file)
        with sqlite_connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                return None
            task = _row_to_task(row).apply_update(changes)
            values = self._task_values(task)
            # Read/merge/write under the same write transaction; never resurrect a deleted row.
            conn.execute("""
                UPDATE tasks SET task_name=:task_name, enabled=:enabled, keyword=:keyword,
                    description=:description, analyze_images=:analyze_images, max_pages=:max_pages,
                    personal_only=:personal_only, min_price=:min_price, max_price=:max_price, cron=:cron,
                    ai_prompt_base_file=:ai_prompt_base_file, ai_prompt_criteria_file=:ai_prompt_criteria_file,
                    account_state_file=:account_state_file, account_strategy=:account_strategy,
                    free_shipping=:free_shipping, new_publish_option=:new_publish_option, region=:region,
                    decision_mode=:decision_mode, keyword_rules_json=:keyword_rules_json, is_running=:is_running
                WHERE id=:id
            """, values)
            conn.commit()
        return task

    def _delete_sync(self, task_id: int) -> bool:
        bootstrap_sqlite_storage(
            self.db_path,
            legacy_config_file=self.legacy_config_file,
        )
        with sqlite_connection(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            if cursor.rowcount:
                # Older databases may not have allocated an ID since upgrading.
                # Preserve deleted IDs before they disappear from MAX(id).
                self._remember_task_id(conn, task_id)
            conn.commit()
        return cursor.rowcount > 0

    def _remember_task_id(self, conn, task_id: int) -> None:
        conn.execute("""
            INSERT INTO app_metadata(key, value) VALUES ('tasks:last_id', CAST(? AS TEXT))
            ON CONFLICT(key) DO UPDATE SET value = CAST(MAX(
                CAST(app_metadata.value AS INTEGER), CAST(excluded.value AS INTEGER)
            ) AS TEXT)
        """, (task_id,))

    def _next_task_id(self, conn) -> int:
        # A transactional high-water mark preserves existing zero-based IDs and
        # never reuses a deleted ID. SQLite serializes allocation across processes.
        row = conn.execute("""
            INSERT INTO app_metadata(key, value)
            VALUES ('tasks:last_id', (SELECT CAST(COALESCE(MAX(id), -1) + 1 AS TEXT) FROM tasks))
            ON CONFLICT(key) DO UPDATE SET value = CAST(MAX(
                CAST(app_metadata.value AS INTEGER) + 1, CAST(excluded.value AS INTEGER)
            ) AS TEXT)
            RETURNING CAST(value AS INTEGER) AS task_id
        """).fetchone()
        return int(row["task_id"])

    def _task_values(self, task: Task) -> dict:
        values = task.model_dump()
        values["enabled"] = int(task.enabled)
        values["analyze_images"] = int(task.analyze_images)
        values["personal_only"] = int(task.personal_only)
        values["free_shipping"] = int(task.free_shipping)
        values["is_running"] = int(task.is_running)
        values["keyword_rules_json"] = json.dumps(task.keyword_rules or [], ensure_ascii=False)
        values.pop("keyword_rules", None)
        return values
