"""Browser capacity shared by API workers and independently launched CLI processes."""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from src.infrastructure.file_lock import FileLock
from src.infrastructure.persistence.sqlite_connection import get_database_path


@asynccontextmanager
async def browser_slot(*, limit=None, directory=None):
    capacity = int(os.getenv("MAX_BROWSER_CONCURRENCY", "2")) if limit is None else int(limit)
    if not 1 <= capacity <= 32:
        raise ValueError("MAX_BROWSER_CONCURRENCY 必须在 1 到 32 之间")
    root = Path(directory) if directory is not None else Path(get_database_path()).resolve().parent / "browser-slots"
    held = None
    try:
        while held is None:
            for index in range(capacity):
                candidate = FileLock(root / f"browser-{index}.lock")
                if candidate.acquire(blocking=False):
                    held = candidate
                    break
            if held is None:
                await asyncio.sleep(0.2)
        yield
    finally:
        if held is not None:
            held.release()
