"""
结果导出服务
"""
import csv
from io import StringIO


EXPORT_HEADERS = [
    "任务名称",
    "搜索关键字",
    "商品ID",
    "商品标题",
    "当前售价",
    "发布时间",
    "卖家昵称",
    "AI是否推荐",
    "分析来源",
    "原因",
    "价格观察次数",
    "价格最低值",
    "价格最高值",
    "市场均价",
    "性价比分数",
    "性价比标签",
    "商品链接",
    "采集时间",
    "出现次数",
    "出现天数",
    "首次出现时间",
    "最近出现时间",
    "重点关注",
]


def _safe_csv_cell(value):
    if isinstance(value, str) and value.lstrip(" \t\r\n\ufeff").startswith(("=", "+", "-", "@")):
        return "'" + value
    if isinstance(value, str) and value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def build_results_csv(records: list[dict]) -> str:
    buffer = StringIO()
    write_results_csv(records, buffer)
    return buffer.getvalue()


def write_results_csv(records, output, *, include_header=True) -> None:
    writer = csv.DictWriter(output, fieldnames=EXPORT_HEADERS)
    if include_header:
        writer.writeheader()

    for record in records:
        item = record.get("商品信息", {}) or {}
        seller = record.get("卖家信息", {}) or {}
        ai_analysis = record.get("ai_analysis", {}) or {}
        price_insight = record.get("price_insight", {}) or {}
        occurrence = record.get("occurrence") or {}
        seen_count = int(occurrence.get("seen_count") or 1)
        seen_days = int(occurrence.get("seen_days") or 1)
        row = {
                "任务名称": record.get("任务名称", ""),
                "搜索关键字": record.get("搜索关键字", ""),
                "商品ID": item.get("商品ID", ""),
                "商品标题": item.get("商品标题", ""),
                "当前售价": item.get("当前售价", ""),
                "发布时间": item.get("发布时间", ""),
                "卖家昵称": seller.get("卖家昵称") or item.get("卖家昵称", ""),
                "AI是否推荐": "是" if ai_analysis.get("is_recommended") else "否",
                "分析来源": ai_analysis.get("analysis_source", ""),
                "原因": ai_analysis.get("reason", ""),
                "价格观察次数": price_insight.get("observation_count", ""),
                "价格最低值": price_insight.get("min_price", ""),
                "价格最高值": price_insight.get("max_price", ""),
                "市场均价": price_insight.get("market_avg_price", ""),
                "性价比分数": ai_analysis.get("value_score", price_insight.get("deal_score", "")),
                "性价比标签": ai_analysis.get("value_summary", price_insight.get("deal_label", "")),
                "商品链接": item.get("商品链接", ""),
                "采集时间": record.get("爬取时间", ""),
                "出现次数": seen_count,
                "出现天数": seen_days,
                "首次出现时间": occurrence.get("first_seen_at") or "",
                "最近出现时间": occurrence.get("last_seen_at") or "",
                "重点关注": "是" if seen_count > 1 else "否",
        }
        writer.writerow({key: _safe_csv_cell(value) for key, value in row.items()})
