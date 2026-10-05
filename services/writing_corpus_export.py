"""W5 · 一键获取被采纳范文集(老板点名 · 含安全边界)。

按 行业/文体/intent/信号层(采纳/对照)筛选,批量返回正文,供运营/写手一键拿到「这类问题的
被采纳范文集」。安全边界(Codex 审核补强):
  ① _require_admin(端点层)+ 单日导出上限(env,默认 500 篇)
  ② 每次导出写审计事件(operator / 筛选条件 / 篇数)
  ③ 导出内容只含 正文 + 来源 URL + 标题,**剥离内部字段**(oss_key / 内部 id / signal 权重 / 特征)
  ④ OSS 超时/失败该篇跳过并标注(fail-soft 不整批失败)

正文经 W1-3 后台路径 OSS 批读(放开 40 封顶);OSS 未回读到正文的行按 fail-soft 跳过并计数。
"""
from __future__ import annotations

import logging
from typing import Any

from db.writing_corpus_export_db import (
    DAILY_EXPORT_CAP,
    get_today_export_count,
    record_export,
)
from services.article_structure_analysis import load_labeled_article_rows
from writing.intent_style_map import resolve_style_code

logger = logging.getLogger("GEO-WritingFlywheel.CorpusExport")

MAX_PAGE_SIZE = 50
_SIGNAL_GROUP = {
    "adopted": "adopted_group",
    "control": "search_only_control_group",
    "cited": "cited_group",
}


def export_corpus(
    *,
    industry_key: str,
    style_code: str | None = None,
    intent_type: str | None = None,
    signal_layer: str = "adopted",
    limit: int = MAX_PAGE_SIZE,
    min_chars: int = 500,
    actor_id: int = 0,
) -> dict[str, Any]:
    """导出被采纳/对照范文集(剥内部字段 · 日限 · 审计 · fail-soft)。"""
    page_size = min(max(1, int(limit or MAX_PAGE_SIZE)), MAX_PAGE_SIZE)
    target_group = _SIGNAL_GROUP.get(signal_layer, "adopted_group")

    # 日限:今日已导出 + 本次 ≤ cap
    today = get_today_export_count()
    remaining = max(0, DAILY_EXPORT_CAP - today)
    if remaining <= 0:
        return {
            "items": [], "count": 0, "daily_cap": DAILY_EXPORT_CAP, "daily_used": today,
            "daily_remaining": 0, "capped": True,
            "message": f"今日导出已达上限 {DAILY_EXPORT_CAP} 篇,请明日再试。",
        }
    effective = min(page_size, remaining)

    _industry_key, rows = load_labeled_article_rows(industry_key, limit=1000, min_chars=min_chars)
    pool = [r for r in rows if r.get("group_key") == target_group]
    if style_code:
        pool = [
            r for r in pool
            if resolve_style_code(r.get("intent_type") or "", r.get("style_family") or "") == style_code
        ]
    if intent_type:
        pool = [r for r in pool if (r.get("intent_type") or "") == intent_type]

    items: list[dict[str, Any]] = []
    skipped_no_body = 0
    for r in pool:
        if len(items) >= effective:
            break
        body = str(r.get("body") or "").strip()
        if not body:
            skipped_no_body += 1  # fail-soft:OSS 未回读到正文的行跳过并计数,不整批失败
            continue
        # 只含 正文 + 来源 URL + 标题 + 信号层;剥离 oss_key / 内部 id / source_weight / features
        items.append({
            "title": str(r.get("title") or ""),
            "source_url": str(r.get("url") or ""),
            "domain": str(r.get("domain") or ""),
            "signal_layer": signal_layer,
            "content": body,
        })

    truncated = len([r for r in pool if str(r.get("body") or "").strip()]) > len(items)
    filters = {
        "industry_key": industry_key, "style_code": style_code,
        "intent_type": intent_type, "signal_layer": signal_layer, "limit": page_size,
    }
    try:
        record_export(actor_id, filters, len(items))
    except Exception as exc:
        logger.warning("[corpus-export] 审计写入失败(不阻断导出): %s", str(exc)[:200])

    return {
        "items": items,
        "count": len(items),
        "skipped_no_body": skipped_no_body,
        "truncated": truncated,
        "daily_cap": DAILY_EXPORT_CAP,
        "daily_used": today + len(items),
        "daily_remaining": max(0, remaining - len(items)),
        "capped": False,
    }
