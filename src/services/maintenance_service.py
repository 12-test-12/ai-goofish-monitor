"""Periodic, bounded maintenance independent of scheduler task reloads."""
import asyncio
from contextlib import asynccontextmanager, suppress

from src.services.image_cache_service import cleanup_expired_images


@asynccontextmanager
async def image_cache_maintenance(*, max_age_hours=24, interval_seconds=3600):
    async def sweep():
        try:
            await asyncio.to_thread(cleanup_expired_images, max_age_hours=max_age_hours)
        except Exception as exc:
            print(f"[图片缓存] 巡检失败: {exc}")

    async def repeat():
        while True:
            await asyncio.sleep(interval_seconds)
            await sweep()

    await sweep()
    task = asyncio.create_task(repeat())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
