"""Expire abandoned image directories while protecting active CLI/API workers."""
import asyncio
import hashlib
import re
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

from src.infrastructure.file_lock import FileLock
from src.infrastructure.persistence.sqlite_connection import get_database_path


def _lease_path(root: Path, name: str) -> Path:
    namespace = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()
    return Path(get_database_path()).resolve().parent / "image-leases" / namespace / f"{name}.lock"


@asynccontextmanager
async def task_image_lease(task_key, root="images", prefix="task_images_"):
    name = prefix + hashlib.sha256(str(task_key).encode("utf-8")).hexdigest()
    lock = FileLock(_lease_path(Path(root), name))
    try:
        while not lock.acquire(blocking=False):
            await asyncio.sleep(0.2)
        yield
    finally:
        lock.release()


def cleanup_expired_images(root="images", prefix="task_images_", *, max_age_hours=24, now=None) -> int:
    if max_age_hours <= 0:
        return 0
    root = Path(root).resolve()
    if not root.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - max_age_hours * 3600
    removed = 0
    for directory in root.iterdir():
        if not re.fullmatch(re.escape(prefix) + r"[0-9a-f]{64}", directory.name):
            continue
        lock = FileLock(_lease_path(root, directory.name))
        try:
            if not lock.acquire(blocking=False):
                continue
            if directory.is_symlink() or directory.resolve() != directory or not directory.is_dir():
                continue
            children = list(directory.iterdir())
            # Generated caches are flat. Skip legacy/nested/linked content entirely.
            if any(child.is_symlink() or child.resolve() != child or not child.is_file() for child in children):
                continue
            newest = max([directory.stat().st_mtime, *(child.stat().st_mtime for child in children)])
            if newest < cutoff:
                shutil.rmtree(directory)
                removed += 1
        except OSError as exc:
            print(f"[图片缓存] 清理失败 {directory.name}: {exc}")
        finally:
            lock.release()
    return removed
