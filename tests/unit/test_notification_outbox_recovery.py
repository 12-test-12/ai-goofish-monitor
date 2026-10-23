import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.services import notification_outbox_service as outbox
from src.services import result_storage_service as storage


@pytest.fixture()
def queued_results(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "app.sqlite3"))
    for item_id in ("first", "second"):
        assert storage._save_result_record_sync({
            "任务ID": 7,
            "商品信息": {"商品ID": item_id, "商品链接": f"https://www.goofish.com/item?id={item_id}"},
            "ai_analysis": {"is_recommended": True, "reason": "test"},
        }, "camera")


def rows():
    with sqlite_connection() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM notification_outbox ORDER BY id")]


def test_exhausted_message_does_not_block_the_next_delivery(queued_results):
    with sqlite_connection() as conn:
        conn.execute("UPDATE notification_outbox SET attempts=? WHERE id=?",
                     (outbox.MAX_NOTIFICATION_ATTEMPTS, rows()[0]["id"]))
        conn.commit()
    sender = AsyncMock(return_value={"bark": {"success": True}})
    assert asyncio.run(outbox.dispatch_pending_notifications(sender=sender, now=0)) == 1
    assert not rows()
    assert sender.await_args.args[0]["商品ID"] == "second"


@pytest.mark.parametrize("receipt", [{}, {"bark": {"success": True}}])
def test_missing_retry_channel_receipts_remain_pending(queued_results, receipt):
    first_id = rows()[0]["id"]
    with sqlite_connection() as conn:
        conn.execute("UPDATE notification_outbox SET pending_channels_json=? WHERE id=?",
                     (json.dumps(["bark", "telegram"]), first_id))
        conn.commit()
    sender = AsyncMock(side_effect=[receipt, {"bark": {"success": True}}])
    assert asyncio.run(outbox.dispatch_pending_notifications(sender=sender, now=0)) == 1
    remaining = rows()
    assert len(remaining) == 1 and remaining[0]["id"] == first_id
    expected = ["telegram"] if receipt else ["bark", "telegram"]
    assert json.loads(remaining[0]["pending_channels_json"]) == expected
    retry = AsyncMock(return_value={channel: {"success": True} for channel in expected})
    assert asyncio.run(outbox.dispatch_pending_notifications(sender=retry, now=31)) == 1
    assert retry.await_args.args[2] == expected
    assert not rows()


@pytest.mark.parametrize("pending_json, error", [
    ("broken", "JSONDecodeError"),
    ("{}", "ValueError"),
    ('["bark", null]', "ValueError"),
])
def test_malformed_message_does_not_interrupt_other_deliveries(queued_results, pending_json, error):
    with sqlite_connection() as conn:
        conn.execute("UPDATE notification_outbox SET pending_channels_json=? WHERE id=?", (pending_json, rows()[0]["id"]))
        conn.commit()
    sender = AsyncMock(return_value={"bark": {"success": True}})
    assert asyncio.run(outbox.dispatch_pending_notifications(sender=sender, now=0)) == 1
    assert len(rows()) == 1
    assert rows()[0]["last_error"] == error
    assert rows()[0]["pending_channels_json"] == pending_json
    assert sender.await_count == 1


@pytest.mark.parametrize("receipt", [None, {"bark": None}, {"bark": {"success": "false"}}])
def test_invalid_receipts_cannot_acknowledge_pending_channels(queued_results, receipt):
    first_id = rows()[0]["id"]
    with sqlite_connection() as conn:
        conn.execute("UPDATE notification_outbox SET pending_channels_json=? WHERE id=?", ('["bark"]', first_id))
        conn.commit()
    sender = AsyncMock(side_effect=[receipt, {"bark": {"success": True}}])
    assert asyncio.run(outbox.dispatch_pending_notifications(sender=sender, now=0)) == 1
    assert len(rows()) == 1
    assert rows()[0]["id"] == first_id
    assert json.loads(rows()[0]["pending_channels_json"]) == ["bark"]
