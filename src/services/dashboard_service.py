"""
Dashboard 聚合服务
统一汇总任务、结果文件和最近活动，供首页概览使用。
"""
from __future__ import annotations

from typing import Any

from src.domain.models.task import Task
from src.services.dashboard_payloads import (
    build_empty_summary,
    build_task_state_activities,
    serialize_timestamp,
    sort_key_by_activity_time,
    sort_key_by_latest_time,
    summarize_result_file,
)
from src.services.result_storage_service import list_result_groups

MAX_RECENT_ACTIVITIES = 8


def _build_summary_metrics(tasks: list[Task], summary_list: list[dict[str, Any]], last_updated_at: Any, file_count: int) -> dict[str, Any]:
    return {
        "enabled_tasks": sum(1 for task in tasks if task.enabled),
        "running_tasks": sum(1 for task in tasks if task.is_running),
        "result_files": file_count,
        "scanned_items": sum(int(item["total_items"]) for item in summary_list),
        "recommended_items": sum(int(item["recommended_items"]) for item in summary_list),
        "ai_recommended_items": sum(int(item["ai_recommended_items"]) for item in summary_list),
        "keyword_recommended_items": sum(int(item["keyword_recommended_items"]) for item in summary_list),
        "last_updated_at": serialize_timestamp(last_updated_at),
    }


async def build_dashboard_snapshot(tasks: list[Task]) -> dict[str, Any]:
    task_lookup = {task.id: task for task in tasks}
    task_summaries: dict[tuple, dict[str, Any]] = {
        ("task", task.id): build_empty_summary(task) for task in tasks
    }
    recent_activities = build_task_state_activities(tasks)
    latest_updated_at = None

    groups = await list_result_groups()
    visible_files = set()
    for group in groups:
        filename = group["result_filename"]
        legacy_name = group["legacy_task_name"] if group["task_id"] == -1 else None
        summary, activities, file_latest_time = await summarize_result_file(
            filename, task_lookup, task_id=group["task_id"], legacy_task_name=legacy_name,
        )
        if summary:
            visible_files.add(filename)
            key = ("task", summary["task_id"]) if summary["task_id"] is not None else ("legacy", filename, group["task_id"], legacy_name)
            previous = task_summaries.get(key)
            if previous and previous.get("filename"):
                counts = {name: previous[name] + summary[name] for name in (
                    "total_items", "recommended_items", "ai_recommended_items", "keyword_recommended_items")}
                latest = summary if sort_key_by_latest_time(summary) >= sort_key_by_latest_time(previous) else previous
                recommendation = max((previous, summary), key=lambda item: item.get("latest_recommendation_time") or "")
                summary = {**latest, **counts, **{name: recommendation.get(name) for name in (
                    "latest_recommended_title", "latest_recommended_price", "latest_recommendation_time")}}
            task_summaries[key] = summary
        recent_activities.extend(activities)
        if file_latest_time and (latest_updated_at is None or file_latest_time > latest_updated_at):
            latest_updated_at = file_latest_time

    summary_list = sorted(task_summaries.values(), key=sort_key_by_latest_time, reverse=True)
    focus_file = next((item["filename"] for item in summary_list if item.get("filename")), None)
    return {
        "summary": _build_summary_metrics(tasks, summary_list, latest_updated_at, len(visible_files)),
        "task_summaries": summary_list,
        "recent_activities": sorted(
            recent_activities,
            key=sort_key_by_activity_time,
            reverse=True,
        )[:MAX_RECENT_ACTIVITIES],
        "focus_file": focus_file,
    }
