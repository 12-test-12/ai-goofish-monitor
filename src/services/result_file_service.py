"""
结果记录富化与文件名校验服务
"""

from src.infrastructure.persistence.storage_names import normalize_keyword_from_filename
from src.services.price_history_service import (
    build_item_price_context,
    load_price_snapshots,
    load_item_occurrences,
    parse_price_value,
    normalize_keyword_slug,
)
from src.services.result_storage_service import load_visible_market_summary


def validate_result_filename(filename: str) -> None:
    if not filename or not filename.endswith(".jsonl") or "/" in filename or "\\" in filename or ".." in filename:
        raise ValueError("无效的文件名")


def enrich_records_with_price_insight(records: list[dict], filename: str) -> list[dict]:
    if not records:
        return records
    keyword = normalize_keyword_from_filename(filename)

    def item_key(record: dict) -> str:
        info = record.get("商品信息") or {}
        return str(info.get("商品ID") or info.get("商品链接") or "").strip()

    page_ids = {item_key(record) for record in records} - {""}
    occurrences = load_item_occurrences(keyword, page_ids)
    snapshots = load_price_snapshots(keyword, item_ids=page_ids)

    market_summary = load_visible_market_summary(filename, normalize_keyword_slug(keyword)) if snapshots else {}
    enriched = []
    for record in records:
        info = record.get("商品信息", {}) or {}
        clone = dict(record)
        clone["occurrence"] = occurrences.get(item_key(record), {
            "seen_count": 1, "seen_days": 1,
            "first_seen_at": record.get("爬取时间") or None,
            "last_seen_at": record.get("爬取时间") or None,
        })
        if snapshots:
            clone["price_insight"] = build_item_price_context(
                snapshots,
                item_id=item_key(record),
                current_price=parse_price_value(info.get("当前售价")),
                market_summary=market_summary,
            )
        enriched.append(clone)
    return enriched
