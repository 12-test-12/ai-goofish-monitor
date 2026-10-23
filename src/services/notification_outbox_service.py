"""Durable recommendation delivery; result inserts enqueue in the same transaction."""
import asyncio
import json
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from src.infrastructure.file_lock import FileLock
from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import get_database_path, sqlite_connection

MAX_NOTIFICATION_ATTEMPTS = 15


def enqueue_recommendation(conn, result_id: int, record: dict) -> None:
    analysis = record.get("ai_analysis") or {}
    if analysis.get("is_recommended"):
        conn.execute("""INSERT OR IGNORE INTO notification_outbox (result_id, product_json, reason)
            VALUES (?, ?, ?)""", (result_id, json.dumps(record.get("商品信息") or {}, ensure_ascii=False),
                                   str(analysis.get("reason") or "无")))


def _claim(now, *, max_attempts=MAX_NOTIFICATION_ATTEMPTS):
    with sqlite_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        expired = conn.execute(
            "DELETE FROM notification_outbox WHERE available_at <= ? AND attempts >= ?",
            (now, max_attempts),
        ).rowcount
        if expired:
            print(f"[通知队列] 已清理 {expired} 条达到最大重试次数 ({max_attempts}) 的消息")
        row = conn.execute("SELECT * FROM notification_outbox WHERE available_at <= ? ORDER BY id LIMIT 1", (now,)).fetchone()
        if row is None:
            conn.commit()
            return None
        attempts = row["attempts"] + 1
        delay = min(3600, 30 * 2 ** min(attempts - 1, 7))
        # Crash before acknowledgement leaves the job pending, with bounded backoff.
        conn.execute("UPDATE notification_outbox SET attempts=?, available_at=? WHERE id=?", (attempts, now + delay, row["id"]))
        conn.commit()
        return dict(row)


async def _send(product, reason, pending_channels):
    from src.services.notification_service import build_notification_service
    service = build_notification_service()
    if pending_channels is not None:
        service.clients = [client for client in service.clients if client.channel_key in pending_channels]
    return await service.send_notification(product, reason)


async def dispatch_pending_notifications(*, sender=None, batch_size=20, now=None) -> int:
    await asyncio.to_thread(bootstrap_sqlite_storage)
    lock = FileLock(Path(get_database_path()).resolve().parent / "notification-outbox.lock")
    if not lock.acquire(blocking=False):
        return 0
    delivered = 0
    try:
        for _ in range(batch_size):
            job = await asyncio.to_thread(_claim, time.time() if now is None else now)
            if job is None:
                break
            try:
                pending = json.loads(job["pending_channels_json"]) if job["pending_channels_json"] else None
                if pending is not None and (
                    not isinstance(pending, list) or not all(isinstance(channel, str) and channel for channel in pending)
                ):
                    raise ValueError("Invalid pending notification channels")
                results = await (sender or _send)(json.loads(job["product_json"]), job["reason"], pending)
                if not isinstance(results, dict):
                    raise TypeError("Invalid notification receipts")
                channels = dict.fromkeys([*(pending or []), *results])
                failed = [channel for channel in channels if not (
                    isinstance(results.get(channel), dict) and results[channel].get("success") is True
                )]
                error = "部分通知渠道发送失败" if failed else ""
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Keep the previous channel set when no trustworthy acknowledgement exists.
                error = type(exc).__name__
                await asyncio.to_thread(_record_error, job["id"], error)
                print(f"[通知队列] 第 {job['attempts'] + 1} 次发送失败，稍后重试: {error}")
                continue
            if failed:
                await asyncio.to_thread(_retain, job["id"], failed, error)
                print(f"[通知队列] 待重试渠道: {', '.join(failed)}")
            else:
                await asyncio.to_thread(_acknowledge, job["id"])
                delivered += 1
    finally:
        lock.release()
    return delivered


def _retain(job_id, channels, error):
    with sqlite_connection() as conn:
        conn.execute("UPDATE notification_outbox SET pending_channels_json=?, last_error=? WHERE id=?",
                     (json.dumps(channels) if channels is not None else None, error, job_id))
        conn.commit()


def _record_error(job_id, error):
    with sqlite_connection() as conn:
        conn.execute("UPDATE notification_outbox SET last_error=? WHERE id=?", (error, job_id))
        conn.commit()


def _acknowledge(job_id):
    with sqlite_connection() as conn:
        conn.execute("DELETE FROM notification_outbox WHERE id=?", (job_id,))
        conn.commit()


async def send_queued_recommendation(_product, _reason):
    # Keep recommendation analysis from waiting on a backlog of unrelated jobs.
    await dispatch_pending_notifications(batch_size=1)


@asynccontextmanager
async def notification_delivery_worker(*, interval_seconds=15):
    async def run():
        while True:
            try:
                await dispatch_pending_notifications()
            except Exception as exc:
                print(f"[通知队列] 巡检失败: {type(exc).__name__}")
            await asyncio.sleep(interval_seconds)
    worker = asyncio.create_task(run())
    try:
        yield
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
