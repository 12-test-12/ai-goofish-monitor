"""
任务生成作业执行器
"""
from __future__ import annotations
import os
from uuid import uuid4

import aiofiles

from src.domain.models.task import Task, TaskCreate, TaskGenerateRequest, TaskUpdate
from src.prompt_utils import generate_criteria
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.services.scheduler_service import SchedulerService
from src.services.task_generation_service import TaskGenerationService
from src.services.task_service import TaskService
from src.infrastructure.paths import safe_child_path

def build_criteria_filename(keyword: str) -> str:
    safe_keyword = "".join(
        char for char in keyword.lower().replace(" ", "_")
        if char.isalnum() or char in "_-"
    ).rstrip()
    filename = f"{safe_keyword[:80] or 'task'}_{uuid4().hex}_criteria.txt"
    return f"prompts/{filename}"


def build_task_create(req: TaskGenerateRequest, criteria_file: str) -> TaskCreate:
    return TaskCreate(
        task_name=req.task_name,
        enabled=True,
        keyword=req.keyword,
        description=req.description or "",
        analyze_images=req.analyze_images,
        max_pages=req.max_pages,
        personal_only=req.personal_only,
        min_price=req.min_price,
        max_price=req.max_price,
        cron=req.cron,
        ai_prompt_base_file="prompts/base_prompt.txt",
        ai_prompt_criteria_file=criteria_file,
        account_state_file=req.account_state_file,
        account_strategy=req.account_strategy,
        free_shipping=req.free_shipping,
        new_publish_option=req.new_publish_option,
        region=req.region,
        decision_mode=req.decision_mode or "ai",
        keyword_rules=req.keyword_rules,
    )


async def save_generated_criteria(output_filename: str, generated_criteria: str) -> None:
    if not generated_criteria or not generated_criteria.strip():
        raise RuntimeError("AI 未能生成分析标准，返回内容为空。")

    os.makedirs("prompts", exist_ok=True)
    safe_path = safe_child_path("prompts", os.path.basename(output_filename))
    created = False
    try:
        async with aiofiles.open(safe_path, "x", encoding="utf-8") as file:
            created = True
            await file.write(generated_criteria)
    except BaseException:
        if created:
            try:
                os.remove(safe_path)
            except OSError:
                pass
        raise


async def reload_scheduler(
    task_service: TaskService,
    scheduler_service: SchedulerService,
) -> None:
    tasks = await task_service.get_all_tasks()
    await scheduler_service.reload_jobs(tasks)


async def advance_job(
    generation_service: TaskGenerationService,
    job_id: str,
    step_key: str,
    message: str,
) -> None:
    await generation_service.advance(job_id, step_key, message)


async def run_ai_generation_job(
    *,
    job_id: str,
    req: TaskGenerateRequest,
    task_service: TaskService,
    scheduler_service: SchedulerService,
    generation_service: TaskGenerationService,
) -> None:
    output_filename = build_criteria_filename(req.keyword)
    criteria_saved = False
    task = None
    try:
        await advance_job(
            generation_service,
            job_id,
            "prepare",
            "已接收请求，开始准备分析标准。",
        )

        async def report_progress(step_key: str, message: str) -> None:
            await advance_job(generation_service, job_id, step_key, message)

        generated_criteria = await generate_criteria(
            user_description=req.description or "",
            reference_file_path="prompts/macbook_criteria.txt",
            progress_callback=report_progress,
        )

        await advance_job(
            generation_service,
            job_id,
            "persist",
            f"正在保存分析标准到 {output_filename}。",
        )
        await save_generated_criteria(output_filename, generated_criteria)
        criteria_saved = True

        await advance_job(
            generation_service,
            job_id,
            "task",
            "分析标准已生成，正在创建任务记录。",
        )
        task = await task_service.create_task(build_task_create(req, output_filename))
        try:
            await reload_scheduler(task_service, scheduler_service)
        except Exception as exc:
            await generation_service.complete(job_id, task, f"任务“{req.task_name}”已创建，但定时调度刷新失败，请重启服务: {exc}")
            return
        await generation_service.complete(job_id, task, f"任务“{req.task_name}”创建完成。")
    except Exception as exc:
        try:
            safe_path = safe_child_path("prompts", os.path.basename(output_filename))
            if criteria_saved and task is None and os.path.exists(safe_path):
                os.remove(safe_path)
        except (OSError, ValueError):
            # A rejected path must never prevent the job from reaching failed.
            pass
        await generation_service.fail(job_id, f"AI 任务生成失败: {exc}")


async def run_ai_update_job(
    *, job_id: str, existing_task: Task, changes: TaskUpdate,
    task_service: TaskService, scheduler_service: SchedulerService,
    generation_service: TaskGenerationService,
) -> None:
    output_filename = build_criteria_filename(changes.keyword or existing_task.keyword)
    criteria_saved = False
    task = None
    try:
        await generation_service.advance(job_id, "prepare", "已接收修改，生成完成前继续保留原任务。")

        async def progress(step_key: str, message: str) -> None:
            await generation_service.advance(job_id, step_key, message)

        criteria = await generate_criteria(
            user_description=changes.description if changes.description is not None else existing_task.description,
            reference_file_path="prompts/macbook_criteria.txt", progress_callback=progress,
        )
        await generation_service.advance(job_id, "persist", "正在保存新的分析标准。")
        await save_generated_criteria(output_filename, criteria)
        criteria_saved = True
        changes = changes.model_copy(update={"ai_prompt_criteria_file": output_filename})
        await generation_service.advance(job_id, "task", "正在保存任务修改。")
        task = await task_service.update_task_if_unchanged(existing_task.id, changes, existing_task)
        if any(getattr(task, field) != getattr(existing_task, field) for field in ("cron", "enabled", "task_name")):
            try:
                await reload_scheduler(task_service, scheduler_service)
            except Exception as exc:
                await generation_service.complete(job_id, task, f"修改已保存，但定时调度刷新失败，请重启软件：{exc}")
                return
        await generation_service.complete(job_id, task, f"任务“{task.task_name}”修改已保存。")
    except Exception as exc:
        if task is None and criteria_saved:
            try:
                os.remove(safe_child_path("prompts", os.path.basename(output_filename)))
            except (OSError, ValueError):
                pass
        await generation_service.fail(job_id, f"任务修改未保存，原任务保留：{exc}")
