"""
cluster_delivery_mapping · C4.1 (CTO-15.9 session 3 · 2026-04-25)

Codex 0424 P1.3b 主题包 + 交付映射 helper

主题包(keyword_clusters)增 3 字段:
  - platforms_jsonb · 推荐发布平台 ['知乎','百家号',...]
  - weeks_to_complete · 完成周期(周)
  - delivery_notes · 文字备注

本 service 提供:
  - apply_delivery_defaults(cluster) · 启发式推荐 platforms + weeks
  - format_delivery_table(clusters) · markdown 表格(报价页/Module 4 复用)
  - calculate_total_weeks(clusters) · 多包并行 · 取最长周期(瓶颈)
"""
from __future__ import annotations
import logging
from typing import Iterable, List, Dict, Any, Optional

logger = logging.getLogger("GEO-ClusterDelivery")


# ============ 默认平台推荐(按 business_tag) ============

_BUSINESS_TAG_TO_PLATFORMS: dict[str, list[str]] = {
    "品牌词": ["百度百科", "知乎", "百家号"],          # 品牌防守 · 权威源优先
    "类目词": ["知乎", "百家号", "搜狐", "今日头条"],   # 类目抢占 · 大流量平台
    "场景词": ["小红书", "知乎", "百家号"],            # 场景决策 · 种草型
    "地域词": ["大众点评", "美团", "搜狐地方频道"],    # 地域转化 · 本地服务
    "竞品词": ["知乎", "搜狐", "百家号"],              # 竞品对比 · 偏专业评测
    "证据词": ["百度学术", "知网", "行业协会官网"],    # 证据信任 · 权威背书
    "通用": ["知乎", "百家号", "搜狐"],
}


def _default_platforms_for(business_tag: str) -> list[str]:
    """按主题包业务标签推荐平台(默认 3 个 · 启发式)"""
    if not business_tag:
        return _BUSINESS_TAG_TO_PLATFORMS["通用"]
    return _BUSINESS_TAG_TO_PLATFORMS.get(business_tag, _BUSINESS_TAG_TO_PLATFORMS["通用"])


def _default_weeks_for(article_count: int) -> int:
    """按文章数推算完成周期(每周 ~3-5 篇 · 上限 8 周)

    1-3 篇 → 1 周
    4-7 篇 → 2 周
    8-15 篇 → 4 周
    16-30 篇 → 6 周
    > 30 篇 → 8 周(到顶)
    """
    if article_count <= 3:
        return 1
    if article_count <= 7:
        return 2
    if article_count <= 15:
        return 4
    if article_count <= 30:
        return 6
    return 8


def apply_delivery_defaults(cluster: Dict[str, Any]) -> Dict[str, Any]:
    """给单个 cluster dict 填默认 platforms + weeks(若空)

    不覆盖已有值 · 仅 fill missing
    入参:cluster dict(含 business_tag / articles_standard 等)
    返:同 dict(原地修改)
    """
    if not cluster.get("platforms_jsonb"):
        cluster["platforms_jsonb"] = _default_platforms_for(cluster.get("business_tag", ""))
    if not cluster.get("weeks_to_complete"):
        # 用 standard 档篇数推 · 没有则用 entry 兜底
        articles = (
            cluster.get("articles_standard")
            or cluster.get("articles_entry")
            or cluster.get("articles_flagship")
            or 0
        )
        cluster["weeks_to_complete"] = _default_weeks_for(int(articles))
    return cluster


def format_delivery_table(clusters: Iterable[Dict[str, Any]]) -> str:
    """生成主题包交付映射 markdown 表格

    用于:
      - 报价页(代理审核 · 给客户看 "做哪些 · 在哪发 · 多久")
      - Module 4 报告(竞品分析旁附主题包交付计划)
      - PDF 导出

    入参:cluster dict 列表(已 apply_delivery_defaults · 否则空字段)
    返:markdown 字符串(空列表返空字符串)
    """
    cluster_list = list(clusters or [])
    if not cluster_list:
        return ""

    md_lines = [
        "| 主题包 | 业务 | 核心词 | 篇数(标准档) | 推荐平台 | 完成周期 | 包价(标准档) |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for c in cluster_list:
        name = c.get("cluster_name", "—")
        biz = c.get("business_tag", "—")
        core = c.get("core_keyword_count", 0)
        articles = c.get("articles_standard", 0)
        platforms = c.get("platforms_jsonb") or []
        if isinstance(platforms, str):
            try:
                import json as _json
                platforms = _json.loads(platforms)
            except (ValueError, TypeError):
                platforms = []
        platforms_str = " / ".join(platforms[:3]) if platforms else "—"
        weeks = c.get("weeks_to_complete", 4)
        price = c.get("price_standard", 0)
        md_lines.append(
            f"| {name} | {biz} | {core} 个 | {articles} 篇 | {platforms_str} | {weeks} 周 | ¥{int(price):,} |"
        )
    return "\n".join(md_lines)


def calculate_total_weeks(clusters: Iterable[Dict[str, Any]]) -> int:
    """多包并行 · 总周期 = max(各包周期)(瓶颈包决定)

    用于:报价页"预计 X 周完成"提示
    """
    weeks = [int(c.get("weeks_to_complete") or 0) for c in (clusters or [])]
    weeks = [w for w in weeks if w > 0]
    return max(weeks) if weeks else 0


def get_cluster_delivery_summary(clusters: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """汇总 stats · 报价页顶部展示

    返:
      {
        cluster_count, total_articles, total_price,
        total_weeks(瓶颈), all_platforms(去重 union)
      }
    """
    cluster_list = list(clusters or [])
    if not cluster_list:
        return {
            "cluster_count": 0, "total_articles": 0, "total_price": 0,
            "total_weeks": 0, "all_platforms": [],
        }

    total_articles = sum(int(c.get("articles_standard") or 0) for c in cluster_list)
    total_price = sum(int(c.get("price_standard") or 0) for c in cluster_list)

    platforms_union: set[str] = set()
    for c in cluster_list:
        p = c.get("platforms_jsonb") or []
        if isinstance(p, str):
            try:
                import json as _json
                p = _json.loads(p)
            except (ValueError, TypeError):
                p = []
        for plat in p:
            if plat:
                platforms_union.add(plat)

    return {
        "cluster_count": len(cluster_list),
        "total_articles": total_articles,
        "total_price": total_price,
        "total_weeks": calculate_total_weeks(cluster_list),
        "all_platforms": sorted(platforms_union),
    }
