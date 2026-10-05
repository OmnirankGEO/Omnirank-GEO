"""本轮调研结果报表 · 工单 WO_MEDIA_BOARD_UX_CLOSURE_2026-08-05 §3。

生产实证(2026-08-05,Owner 亲测单):自助调研 queue id 3
(`round_20260805_102956_659694`,1 题「深圳GEO交付系统哪家靠谱」,completed 4.5 分钟)
—— **数据反哺其实成功了**:4 次引擎调用、`geo_research_article_citations` 落 29 条、
榜数据源已更新。**断的是交付面**:完成只有一句 toast,无结果视图、无「已反哺」明示;
且该单归并进已有行业(榜上本就有数据),用户肉眼看不出任何变化 ——
**花 3900 算力像什么都没发生**。

本模块 = 按 `round_id` 从**既有落库数据**直出报表,**零新增采集、零 LLM、纯只读**。

🔴 三条口径纪律:
  1. **不编造**。"分值变化"这一项本工单点名要了,但当前数据结构给不出来:
     `media_entity_score_snapshots` 的唯一键是 `(entity_id, industry_key, score_version)`
     且写侧是 **upsert 覆盖** —— 同一 score_version 的历史分值不留痕,逐轮回溯无源。
     故报表**不出分值变化**,而是返回 `score_delta_available=false` +
     `score_delta_reason`,由前端如实说"本轮不提供分值对比",绝不拿别的数糊上去。
  2. **新进榜按域名算,判据是"本轮之前从没被引用过"**(`cited_at < 本轮起点`),
     不是 `geo_research_articles.first_seen_round_id`(那是 URL 粒度,同一家媒体换篇
     文章就会被算成"新媒体")。
  3. **失败题照实列**。跑失败的题不藏 —— 藏了用户只会以为"钱花了什么都没发生"。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("GEO-ResearchRoundReport")

#: 报表里最多列多少家媒体(报表是给人看的,不是导出;超出部分给总数)。
MAX_MEDIA_ROWS = 30
#: 最多列多少条失败题。
MAX_FAILED_ROWS = 20

SCORE_DELTA_REASON = (
    "本轮不提供媒体分值对比:分值快照按版本覆盖写入,历史分值不留痕,逐轮回溯无数据源。"
)


def _rows(cur) -> list[dict[str, Any]]:
    return [dict(r) for r in (cur.fetchall() or [])]


def build_round_report(round_id: str) -> dict[str, Any]:
    """按 round_id 直出「本轮调研结果」报表。纯只读 · fail-soft。

    返回顶层 key:
      round_id / status / started_at / finished_at /
      prompts(本轮题目清单) / prompt_count / engine_count / call_stats /
      media(引用到的媒体清单,带 §1 人话名) / media_total / new_media_domains /
      citation_count / failed_calls /
      score_delta_available / score_delta_reason / has_data

    任何子查询失败 → 该段退化为空,**整体不抛**(报表取数失败不许影响榜与主流程)。
    """
    rid = str(round_id or "").strip()
    empty: dict[str, Any] = {
        "round_id": rid,
        "status": "",
        "started_at": None,
        "finished_at": None,
        "prompts": [],
        "prompt_count": 0,
        "engine_count": 0,
        "call_stats": {"total": 0, "success": 0, "failed": 0, "skipped": 0, "pending": 0},
        "media": [],
        "media_total": 0,
        "new_media_domains": [],
        "citation_count": 0,
        "failed_calls": [],
        "score_delta_available": False,
        "score_delta_reason": SCORE_DELTA_REASON,
        "has_data": False,
    }
    if not rid:
        return empty

    from db.connection import get_connection

    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()

        # ① 轮次主记录(起点时间是"新进榜"判据的基准,必须先拿到)。
        cur.execute(
            "SELECT round_id, status, started_at, finished_at, created_at "
            "  FROM geo_research_round WHERE round_id = %s",
            (rid,),
        )
        round_row = cur.fetchone()
        if not round_row:
            return empty
        started_at = round_row.get("started_at") or round_row.get("created_at")

        # ② 题 × 引擎调用日志。
        try:
            cur.execute(
                "SELECT platform, status, COUNT(*) AS cnt "
                "  FROM geo_research_round_call WHERE round_id = %s "
                " GROUP BY platform, status",
                (rid,),
            )
            call_rows = _rows(cur)
        except Exception as exc:
            logger.warning("[round-report] 调用日志聚合失败: %s", str(exc)[:200])
            call_rows = []

        stats = {"total": 0, "success": 0, "failed": 0, "skipped": 0, "pending": 0}
        engines: set[str] = set()
        for r in call_rows:
            cnt = int(r.get("cnt") or 0)
            status = str(r.get("status") or "")
            stats["total"] += cnt
            if status in stats:
                stats[status] += cnt
            platform = str(r.get("platform") or "").strip()
            if platform:
                engines.add(platform)

        try:
            cur.execute(
                "SELECT DISTINCT prompt_id, prompt_text FROM geo_research_round_call "
                " WHERE round_id = %s AND COALESCE(prompt_text, '') <> '' "
                " ORDER BY prompt_id",
                (rid,),
            )
            prompt_rows = _rows(cur)
        except Exception as exc:
            logger.warning("[round-report] 题目清单查询失败: %s", str(exc)[:200])
            prompt_rows = []
        prompts = [
            {"prompt_id": r.get("prompt_id"), "text": str(r.get("prompt_text") or "")}
            for r in prompt_rows
        ]

        # ③ 失败题照实列(不藏)。
        try:
            cur.execute(
                "SELECT prompt_text, platform, error_message FROM geo_research_round_call "
                " WHERE round_id = %s AND status = 'failed' ORDER BY id LIMIT %s",
                (rid, MAX_FAILED_ROWS),
            )
            failed_calls = [
                {
                    "text": str(r.get("prompt_text") or ""),
                    "platform": str(r.get("platform") or ""),
                    "error": str(r.get("error_message") or "")[:200],
                }
                for r in _rows(cur)
            ]
        except Exception as exc:
            logger.warning("[round-report] 失败题查询失败: %s", str(exc)[:200])
            failed_calls = []

        # ④ 本轮引用到的媒体(域名口径聚合)。
        try:
            cur.execute(
                "SELECT a.domain AS domain, COUNT(*) AS citations, "
                "       COUNT(DISTINCT a.id) AS articles, "
                "       ARRAY_AGG(DISTINCT c.platform) AS platforms "
                "  FROM geo_research_article_citations c "
                "  JOIN geo_research_articles a ON a.id = c.article_id "
                " WHERE c.round_id = %s AND COALESCE(a.domain, '') <> '' "
                " GROUP BY a.domain ORDER BY citations DESC, a.domain ASC",
                (rid,),
            )
            media_rows = _rows(cur)
        except Exception as exc:
            logger.warning("[round-report] 引用媒体聚合失败: %s", str(exc)[:200])
            media_rows = []

        citation_count = sum(int(r.get("citations") or 0) for r in media_rows)
        domains = [str(r.get("domain") or "") for r in media_rows if r.get("domain")]

        # ⑤ 新进榜 = 本轮之前从没被引用过的域名(URL 粒度会把"同一家媒体换篇文章"
        #    误算成新媒体,故走域名 + cited_at 基准)。
        new_domains: list[str] = []
        if domains and started_at is not None:
            try:
                cur.execute(
                    "SELECT DISTINCT a.domain AS domain "
                    "  FROM geo_research_article_citations c "
                    "  JOIN geo_research_articles a ON a.id = c.article_id "
                    " WHERE a.domain = ANY(%s) AND c.cited_at < %s",
                    (domains, started_at),
                )
                seen_before = {str(r.get("domain") or "") for r in _rows(cur)}
                new_domains = [d for d in domains if d not in seen_before]
            except Exception as exc:
                logger.warning("[round-report] 新进榜判定失败(本段留空): %s", str(exc)[:200])
                new_domains = []
    except Exception as exc:
        logger.warning("[round-report] 报表取数失败(返回空报表): %s", str(exc)[:200])
        return empty
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    # ⑥ [§1 复用] 域名 → 人话名 + 一句话简介。目录不可用 → 显示域名(fail-soft)。
    name_map: dict[str, dict[str, str]] = {}
    try:
        from services.media_domain_directory import (
            get_directory_entries,
            normalize_directory_domain,
            request_distillation,
        )

        name_map = get_directory_entries(domains)
        missing = [
            d for d in domains
            if not (name_map.get(normalize_directory_domain(d)) or {}).get("zh_name")
        ]
        if missing:
            request_distillation(missing)
    except Exception as exc:
        logger.warning("[round-report] 域名人话化失败(退化为域名): %s", str(exc)[:200])
        name_map = {}

    def _label(domain: str) -> tuple[str, str]:
        try:
            from services.media_domain_directory import normalize_directory_domain
            from services.media_effectiveness_board import _platform_display_name

            key = normalize_directory_domain(domain)
            hit = name_map.get(key) or {}
            # 平台公认名(代码内置 · 已过 UGC 归属纠偏)优先于蒸馏名,与出榜同序。
            return (
                _platform_display_name(domain) or str(hit.get("zh_name") or "") or domain,
                str(hit.get("one_liner") or ""),
            )
        except Exception:
            return (domain, "")

    new_set = set(new_domains)
    media: list[dict[str, Any]] = []
    for r in media_rows[:MAX_MEDIA_ROWS]:
        domain = str(r.get("domain") or "")
        display_name, one_liner = _label(domain)
        media.append({
            "domain": domain,
            "display_name": display_name,
            "one_liner": one_liner,
            "citations": int(r.get("citations") or 0),
            "articles": int(r.get("articles") or 0),
            "platforms": [str(p) for p in (r.get("platforms") or []) if p],
            "is_new": domain in new_set,
        })

    return {
        "round_id": rid,
        "status": str(round_row.get("status") or ""),
        "started_at": started_at.isoformat() if hasattr(started_at, "isoformat") else None,
        "finished_at": (
            round_row["finished_at"].isoformat()
            if round_row.get("finished_at") is not None
            and hasattr(round_row.get("finished_at"), "isoformat")
            else None
        ),
        "prompts": prompts,
        "prompt_count": len(prompts),
        "engine_count": len(engines),
        "call_stats": stats,
        "media": media,
        "media_total": len(media_rows),
        "new_media_domains": new_domains,
        "citation_count": citation_count,
        "failed_calls": failed_calls,
        "score_delta_available": False,
        "score_delta_reason": SCORE_DELTA_REASON,
        # has_data 只看"有没有可展示的实质内容":一条引用都没有且一题都没跑成,
        # 才算真空 —— 前端据此显示"结果整理中",而不是摊一张全 0 的表。
        "has_data": bool(media_rows or stats["total"]),
    }
