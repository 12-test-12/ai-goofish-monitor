"""
结果数据的 SQLite 读写服务。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date, datetime

from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.infrastructure.persistence.storage_names import build_result_filename
from src.services.price_history_service import parse_price_value
from src.services.result_blacklist_service import (
    match_blacklist_keywords,
    normalize_blacklist_keywords,
)


SORT_COLUMN_MAP = {
    "crawl_time": "crawl_time",
    "publish_time": "COALESCE(publish_time, '')",
    "price": "COALESCE(price, 0)",
    "keyword_hit_count": "keyword_hit_count",
}

# Guard json_type with CASE: SQLite may reorder separate WHERE predicates.
# Optional object fields must be objects or null before downstream .get() calls.
VALID_RECORD_SQL = """CASE WHEN json_valid(raw_json) THEN
    json_type(raw_json) = 'object'
    AND COALESCE(json_type(raw_json, '$.\"商品信息\"'), 'null') IN ('object', 'null')
    AND COALESCE(json_type(raw_json, '$.\"卖家信息\"'), 'null') IN ('object', 'null')
    AND COALESCE(json_type(raw_json, '$.ai_analysis'), 'null') IN ('object', 'null')
    AND COALESCE(json_type(raw_json, '$.price_insight'), 'null') IN ('object', 'null')
    ELSE 0 END"""


def _get_link_unique_key(link: str) -> str:
    return link.split("&", 1)[0]


def _fallback_unique_key(record: dict, item: dict) -> str:
    item_id = str(item.get("商品ID") or "").strip()
    if item_id:
        return f"item:{item_id}"
    digest = hashlib.sha1(
        json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"hash:{digest}"


def _parse_raw_record(raw_json: str, *, status: str | None = None) -> dict:
    record = json.loads(raw_json)
    if not isinstance(record, dict):
        raise ValueError("结果记录必须是 JSON 对象")
    for field in ("商品信息", "卖家信息", "ai_analysis", "price_insight"):
        if field in record:
            if record[field] is None:
                record[field] = {}
            elif not isinstance(record[field], dict):
                raise ValueError(f"结果字段 {field} 必须是 JSON 对象")
    if status is not None:
        record["_status"] = status
    return record


def _build_query_conditions(
    *,
    filename: str,
    ai_recommended_only: bool,
    keyword_recommended_only: bool,
) -> tuple[str, list]:
    conditions = ["result_filename = ?", f"({VALID_RECORD_SQL})"]
    params: list = [filename]
    if ai_recommended_only:
        conditions.append("is_recommended = 1")
        conditions.append("analysis_source = ?")
        params.append("ai")
    if keyword_recommended_only:
        conditions.append("is_recommended = 1")
        conditions.append("analysis_source = ?")
        params.append("keyword")
    return " AND ".join(conditions), params


def _sort_expression(sort_by: str, sort_order: str, group_by_day: bool = False) -> str:
    column = SORT_COLUMN_MAP.get(sort_by, SORT_COLUMN_MAP["crawl_time"])
    direction = "ASC" if sort_order == "asc" else "DESC"
    if group_by_day:
        day_direction = direction if sort_by == "crawl_time" else "DESC"
        return f"substr(crawl_time, 1, 10) {day_direction}, {column} {direction}, id {direction}"
    return f"(CASE WHEN status = 'active' THEN 0 ELSE 1 END), {column} {direction}, id {direction}"


def _filter_crawl_date(where: str, params: list, crawl_date: str | date | None) -> tuple[str, list]:
    if not crawl_date:
        return where, params
    if isinstance(crawl_date, date):
        date_str = crawl_date.isoformat()
    elif isinstance(crawl_date, str):
        date_str = crawl_date.strip()
    else:
        raise ValueError("采集日期必须是有效的 YYYY-MM-DD 日期")
    try:
        if date.fromisoformat(date_str).isoformat() != date_str:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("采集日期必须是有效的 YYYY-MM-DD 日期") from None
    # Crawl timestamps are stored in the scraper's local calendar, matching the UI groups.
    return where + " AND substr(crawl_time, 1, 10) = ?", [*params, date_str]


def _load_blacklist_keywords_from_conn(conn, filename: str) -> list[str]:
    row = conn.execute(
        """
        SELECT blacklist_keywords_json
        FROM result_blacklist_rules
        WHERE result_filename = ?
        """,
        (filename,),
    ).fetchone()
    if row is None:
        return []
    try:
        payload = json.loads(row["blacklist_keywords_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(payload, (list, str)):
        return []
    return normalize_blacklist_keywords(payload)


def _decorate_record_visibility(record: dict, status: str | None, blacklist_keywords: list[str]) -> dict:
    matched_keywords = match_blacklist_keywords(record, blacklist_keywords)
    hidden_reason = None
    if status == "expired":
        hidden_reason = "expired"
    elif status and status != "active":
        hidden_reason = "manual"
    elif matched_keywords:
        hidden_reason = "rule"

    record["_status"] = status or "active"
    record["_matched_blacklist_keywords"] = matched_keywords
    record["_hidden_reason"] = hidden_reason
    record["_effective_hidden"] = hidden_reason is not None
    return record


def _is_record_visible(record: dict) -> bool:
    return record.get("_effective_hidden") is not True


def _query_scope(conn, filename, ai_only=False, keyword_only=False, include_hidden=False):
    where, params = _build_query_conditions(
        filename=filename, ai_recommended_only=ai_only, keyword_recommended_only=keyword_only,
    )
    keywords = _load_blacklist_keywords_from_conn(conn, filename)
    if not include_hidden:
        where += " AND status = 'active'"
        if keywords:
            def _check_blacklisted(raw):
                try:
                    return bool(match_blacklist_keywords(_parse_raw_record(raw), keywords))
                except (ValueError, TypeError, AttributeError, RecursionError):
                    return True
            conn.create_function("is_blacklisted", 1, _check_blacklisted)
            where += " AND is_blacklisted(raw_json) = 0"
    return where, params, keywords


def _load_filtered_records_from_conn(
    conn,
    *,
    filename: str,
    ai_recommended_only: bool,
    keyword_recommended_only: bool,
    sort_by: str,
    sort_order: str,
    include_hidden: bool,
) -> list[dict]:
    where_clause, params, blacklist_keywords = _query_scope(
        conn, filename, ai_recommended_only, keyword_recommended_only, include_hidden,
    )
    order_clause = _sort_expression(sort_by, sort_order)
    rows = conn.execute(
        f"""
        SELECT raw_json, status
        FROM result_items
        WHERE {where_clause}
        ORDER BY {order_clause}
        """,
        tuple(params),
    )

    records: list[dict] = []
    for row in rows:
        record = _parse_raw_record(str(row["raw_json"]), status=row["status"])
        decorated = _decorate_record_visibility(record, row["status"], blacklist_keywords)
        if include_hidden or _is_record_visible(decorated):
            records.append(decorated)
    return records


async def save_result_record(record: dict, keyword: str) -> bool:
    return await asyncio.to_thread(_save_result_record_sync, record, keyword)


def _save_result_record_sync(record: dict, keyword: str) -> bool:
    bootstrap_sqlite_storage()
    item = record.get("商品信息", {}) or {}
    analysis = record.get("ai_analysis", {}) or {}
    link = str(item.get("商品链接") or "")
    link_unique_key = _get_link_unique_key(link) if link else _fallback_unique_key(record, item)
    keyword_hit_count = analysis.get("keyword_hit_count", 0)
    try:
        keyword_hit_count = int(keyword_hit_count)
    except (TypeError, ValueError):
        keyword_hit_count = 0

    with sqlite_connection() as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO result_items (
                result_filename, keyword, task_name, crawl_time, publish_time, price,
                price_display, item_id, title, link, link_unique_key, seller_nickname,
                is_recommended, analysis_source, keyword_hit_count, raw_json, task_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                build_result_filename(keyword),
                record.get("搜索关键字", keyword),
                record.get("任务名称", ""),
                record.get("爬取时间", ""),
                item.get("发布时间"),
                parse_price_value(item.get("当前售价")),
                item.get("当前售价"),
                item.get("商品ID"),
                item.get("商品标题"),
                link,
                link_unique_key,
                (record.get("卖家信息", {}) or {}).get("卖家昵称") or item.get("卖家昵称"),
                1 if analysis.get("is_recommended") else 0,
                analysis.get("analysis_source"),
                keyword_hit_count,
                json.dumps(record, ensure_ascii=False),
                record.get("任务ID") if record.get("任务ID") is not None else -1,
            ),
        )
        if cursor.rowcount:
            from src.services.notification_outbox_service import enqueue_recommendation
            enqueue_recommendation(conn, cursor.lastrowid, record)
        conn.commit()
    return cursor.rowcount > 0


def load_processed_link_keys(keyword: str, task_id: int | None = None) -> set[str]:
    bootstrap_sqlite_storage()
    filename = build_result_filename(keyword)
    with sqlite_connection() as conn:
        rows = conn.execute(
            f"SELECT link_unique_key FROM result_items WHERE result_filename = ? AND task_id = ? AND ({VALID_RECORD_SQL})",
            (filename, task_id if task_id is not None else -1),
        ).fetchall()
    return {str(row["link_unique_key"]) for row in rows if row["link_unique_key"]}


async def list_result_filenames() -> list[str]:
    return await asyncio.to_thread(_list_result_filenames_sync)


def _list_result_filenames_sync() -> list[str]:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT result_filename, MAX(crawl_time) AS latest_crawl_time
            FROM result_items
            WHERE ({VALID_RECORD_SQL})
            GROUP BY result_filename
            ORDER BY latest_crawl_time DESC, result_filename DESC
            """
        ).fetchall()
    return [str(row["result_filename"]) for row in rows]


async def result_file_exists(filename: str) -> bool:
    return await asyncio.to_thread(_result_file_exists_sync, filename)


def _result_file_exists_sync(filename: str) -> bool:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM result_items WHERE result_filename = ? LIMIT 1",
            (filename,),
        ).fetchone()
    return row is not None


async def delete_result_file_records(filename: str) -> int:
    return await asyncio.to_thread(_delete_result_file_records_sync, filename)


def _delete_result_file_records_sync(filename: str) -> int:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM result_items WHERE result_filename = ?",
            (filename,),
        )
        conn.commit()
    return int(cursor.rowcount or 0)


async def query_result_records(
    filename: str,
    *,
    ai_recommended_only: bool,
    keyword_recommended_only: bool,
    sort_by: str,
    sort_order: str,
    page: int,
    limit: int,
    include_hidden: bool = False,
    crawl_date: str | None = None,
    group_by_day: bool = False,
) -> tuple[int, list[dict]]:
    return await asyncio.to_thread(
        _query_result_records_sync,
        filename,
        ai_recommended_only,
        keyword_recommended_only,
        sort_by,
        sort_order,
        page,
        limit,
        include_hidden,
        crawl_date,
        group_by_day,
    )


def _query_result_records_sync(
    filename: str,
    ai_recommended_only: bool,
    keyword_recommended_only: bool,
    sort_by: str,
    sort_order: str,
    page: int,
    limit: int,
    include_hidden: bool,
    crawl_date: str | None = None,
    group_by_day: bool = False,
) -> tuple[int, list[dict]]:
    bootstrap_sqlite_storage()
    offset = max(page - 1, 0) * limit
    with sqlite_connection() as conn:
        conn.execute("BEGIN")
        where, params, keywords = _query_scope(conn, filename, ai_recommended_only, keyword_recommended_only, include_hidden)
        where, params = _filter_crawl_date(where, params, crawl_date)
        total = conn.execute(f"SELECT COUNT(*) FROM result_items WHERE {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT raw_json, status FROM result_items WHERE {where} ORDER BY {_sort_expression(sort_by, sort_order, group_by_day)} LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        records = [_decorate_record_visibility(_parse_raw_record(row["raw_json"]), row["status"], keywords) for row in rows]
    return total, records


async def load_all_result_records(
    filename: str,
    *,
    ai_recommended_only: bool,
    keyword_recommended_only: bool,
    sort_by: str,
    sort_order: str,
    include_hidden: bool = False,
) -> list[dict]:
    return await asyncio.to_thread(
        _load_all_result_records_sync,
        filename,
        ai_recommended_only,
        keyword_recommended_only,
        sort_by,
        sort_order,
        include_hidden,
    )


def _load_all_result_records_sync(
    filename: str,
    ai_recommended_only: bool,
    keyword_recommended_only: bool,
    sort_by: str,
    sort_order: str,
    include_hidden: bool,
) -> list[dict]:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        return _load_filtered_records_from_conn(
            conn,
            filename=filename,
            ai_recommended_only=ai_recommended_only,
            keyword_recommended_only=keyword_recommended_only,
            sort_by=sort_by,
            sort_order=sort_order,
            include_hidden=include_hidden,
        )


async def build_result_ndjson(filename: str) -> str:
    return await asyncio.to_thread(_build_result_ndjson_sync, filename)


def _build_result_ndjson_sync(filename: str) -> str:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        rows = conn.execute(
            f"SELECT raw_json FROM result_items WHERE result_filename = ? AND ({VALID_RECORD_SQL}) ORDER BY id ASC",
            (filename,),
        ).fetchall()
    return "\n".join(str(row["raw_json"]) for row in rows)


def iter_result_batches(filename: str, *, ai_recommended_only=False, keyword_recommended_only=False,
                        sort_by="crawl_time", sort_order="desc", include_hidden=False, batch_size=200,
                        crawl_date: str | None = None, group_by_day: bool = False):
    """Consume in a single worker thread, keeping one stable read snapshot."""
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        try:
            conn.execute("BEGIN")
            where, params, keywords = _query_scope(conn, filename, ai_recommended_only, keyword_recommended_only, include_hidden)
            where, params = _filter_crawl_date(where, params, crawl_date)
            cursor = conn.execute(f"SELECT raw_json, status FROM result_items WHERE {where} ORDER BY {_sort_expression(sort_by, sort_order, group_by_day)}", params)
            while rows := cursor.fetchmany(batch_size):
                yield [_decorate_record_visibility(_parse_raw_record(row["raw_json"]), row["status"], keywords) for row in rows]
        finally:
            conn.rollback()


def write_result_ndjson(filename: str, output) -> None:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        cursor = conn.execute(f"SELECT raw_json FROM result_items WHERE result_filename = ? AND ({VALID_RECORD_SQL}) ORDER BY id", (filename,))
        while rows := cursor.fetchmany(200):
            for row in rows:
                output.write(row["raw_json"])
                output.write("\n")


async def load_result_summary(filename: str, *, task_id: int | None = None, task_name: str | None = None) -> dict | None:
    return await asyncio.to_thread(_load_result_summary_sync, filename, task_id, task_name)


def _load_result_summary_sync(filename: str, task_id: int | None = None, task_name: str | None = None) -> dict | None:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        try:
            conn.execute("BEGIN")
            where, params, keywords = _query_scope(conn, filename)
            if task_id is not None:
                where += " AND task_id = ?"
                params.append(task_id)
            if task_name is not None:
                where += " AND task_name = ?"
                params.append(task_name)
            counts = conn.execute(f"""SELECT COUNT(*) AS total_items,
                SUM(is_recommended) AS recommended_items,
                SUM(is_recommended = 1 AND analysis_source = 'ai') AS ai_recommended_items,
                SUM(is_recommended = 1 AND analysis_source = 'keyword') AS keyword_recommended_items
                FROM result_items WHERE {where}""", params).fetchone()
            if not counts["total_items"]:
                return None
            def latest(extra=""):
                row = conn.execute(f"SELECT raw_json, status FROM result_items WHERE {where} {extra} ORDER BY crawl_time DESC, id DESC LIMIT 1", params).fetchone()
                return _decorate_record_visibility(_parse_raw_record(row["raw_json"]), row["status"], keywords) if row else None
            latest_record = latest()
            counts_dict = {
                "total_items": int(counts["total_items"] or 0),
                "recommended_items": int(counts["recommended_items"] or 0),
                "ai_recommended_items": int(counts["ai_recommended_items"] or 0),
                "keyword_recommended_items": int(counts["keyword_recommended_items"] or 0),
            }
            return {**counts_dict, "latest_crawl_time": latest_record.get("爬取时间") if latest_record else None,
                    "latest_record": latest_record, "latest_recommendation": latest("AND is_recommended = 1")}
        finally:
            conn.rollback()


async def list_result_groups() -> list[dict]:
    def read():
        bootstrap_sqlite_storage()
        with sqlite_connection() as conn:
            return [dict(row) for row in conn.execute(f"""
                SELECT result_filename, task_id,
                    CASE WHEN task_id = -1 THEN task_name ELSE '' END AS legacy_task_name
                FROM result_items WHERE ({VALID_RECORD_SQL})
                GROUP BY result_filename, task_id, legacy_task_name
                ORDER BY result_filename, task_id, legacy_task_name
            """)]
    return await asyncio.to_thread(read)


async def update_item_status(filename: str, item_id: str, status: str) -> bool:
    valid = {"active", "hidden", "expired"}
    if status not in valid:
        raise ValueError(f"status must be one of {valid}")
    return await asyncio.to_thread(_update_item_status_sync, filename, item_id, status)


def _update_item_status_sync(filename: str, item_id: str, status: str) -> bool:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        cursor = conn.execute(
            "UPDATE result_items SET status = ? WHERE result_filename = ? AND item_id = ?",
            (status, filename, item_id),
        )
        conn.commit()
        return cursor.rowcount > 0


async def load_result_blacklist_keywords(filename: str) -> list[str]:
    return await asyncio.to_thread(_load_result_blacklist_keywords_sync, filename)


def _load_result_blacklist_keywords_sync(filename: str) -> list[str]:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        return _load_blacklist_keywords_from_conn(conn, filename)


async def save_result_blacklist_keywords(filename: str, keywords: list[str]) -> list[str]:
    return await asyncio.to_thread(_save_result_blacklist_keywords_sync, filename, keywords)


def _save_result_blacklist_keywords_sync(filename: str, keywords: list[str]) -> list[str]:
    bootstrap_sqlite_storage()
    normalized_keywords = normalize_blacklist_keywords(keywords)
    now = datetime.now().isoformat()
    with sqlite_connection() as conn:
        conn.execute(
            """
            INSERT INTO result_blacklist_rules (
                result_filename, blacklist_keywords_json, updated_at
            ) VALUES (?, ?, ?)
            ON CONFLICT(result_filename) DO UPDATE SET
                blacklist_keywords_json = excluded.blacklist_keywords_json,
                updated_at = excluded.updated_at
            """,
            (filename, json.dumps(normalized_keywords, ensure_ascii=False), now),
        )
        conn.commit()
    return normalized_keywords


def load_visible_result_item_ids(filename: str) -> set[str]:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        where, params, _ = _query_scope(conn, filename)
        return {str(row[0]).strip() for row in conn.execute(f"SELECT DISTINCT item_id FROM result_items WHERE {where} AND item_id IS NOT NULL AND item_id != ''", params)}


def load_visible_market_summary(filename: str, keyword_slug: str) -> dict:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        where, params, _ = _query_scope(conn, filename)
        row = conn.execute(f"""WITH visible AS (
                SELECT DISTINCT item_id FROM result_items WHERE {where}
            ), observations AS (
                SELECT * FROM price_snapshots WHERE keyword_slug = ?
                    AND item_id IN (SELECT item_id FROM visible)
                    AND typeof(price) IN ('real', 'integer')
            ), ranked AS (
                SELECT price, ROW_NUMBER() OVER (PARTITION BY item_id ORDER BY snapshot_time DESC, id DESC) AS seq
                FROM observations WHERE run_id = (SELECT run_id FROM observations ORDER BY snapshot_time DESC, id DESC LIMIT 1)
            ), prices AS (
                SELECT price, ROW_NUMBER() OVER (ORDER BY price) AS position, COUNT(*) OVER () AS n
                FROM ranked WHERE seq = 1
            ) SELECT COUNT(*) AS sample_count, ROUND(AVG(price), 2) AS avg_price,
                (SELECT ROUND(AVG(price), 2) FROM prices WHERE position IN ((n+1)/2, (n+2)/2)) AS median_price,
                MIN(price) AS min_price, MAX(price) AS max_price FROM prices
            """, [*params, keyword_slug]).fetchone()
        return dict(row)
