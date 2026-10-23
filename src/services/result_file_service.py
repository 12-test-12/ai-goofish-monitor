"""
结果记录富化与文件名校验服务
"""

from src.infrastructure.persistence.storage_names import normalize_keyword_from_filename
from src.services.price_history_service import (
    build_item_price_context,
    load_price_snapshots,
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
    page_ids = {str((record.get("商品信息") or {}).get("商品ID") or "") for record in records}
    snapshots = load_price_snapshots(keyword, item_ids=page_ids)
    if not snapshots:
        return records

    market_summary = load_visible_market_summary(filename, normalize_keyword_slug(keyword))
    enriched = []
    for record in records:
        info = record.get("商品信息", {}) or {}
        clone = dict(record)
        clone["price_insight"] = build_item_price_context(
            snapshots,
            item_id=str(info.get("商品ID") or ""),
            current_price=parse_price_value(info.get("当前售价")),
            market_summary=market_summary,
        )
        enriched.append(clone)
    return enriched
