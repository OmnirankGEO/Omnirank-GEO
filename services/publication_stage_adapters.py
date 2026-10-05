"""WP7 · 调用方 cutover 适配层(门户 / 周月报 / 运营 / 监测 / 发布中心共用这一处)。

## 为什么要一层适配,而不是让每个调用方直接写 SQL

规格 03 §10 的判别测试里有一条是**冲着可维护性**来的:

  「meijiehezi/history/writing-next/home-stats 若展示统一发布数必须走 projector;
    保留 raw attempts 时标签明确,**删 adapter 后一致性测试红**」

也就是说 cutover 的成败不是"这次改对了",而是"下次有人改回去会不会被抓到"。
一个共用适配层能做到:删掉它 → 全部调用方 import 失败 → 测试红。
25 处各写各的 SQL 做不到 —— 改坏其中一处不会惊动任何测试。

## 本模块**没有** brand 级合并入口,这是故意的

`_publication_outcome_summary(brand_id)` 那种形态正是 WP7 要修的缺陷:
同品牌 Q1 的成果会显示在 Q2 的门户上。所以这里只提供
`brand_quote_projections()` —— 返回 **per-quote 的列表**,不给 max / sum / merge。
需要"这个品牌总共发了几篇"的调用方必须自己说清楚它在加总哪些 quote,
那一步是业务决定,不该由本模块替谁拍板。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from services.publication_stage_projection import (
    STAGES,
    ProjectionUnavailable,
    freeze_projection,
    has_published_occurrence,
    source_versions,
)
from services.publication_stage_sources import load_quote_projection, load_service_completion

logger = logging.getLogger("GEO-PublicationStages")

#: 允许"保留 raw attempts"的展示位。这类位置**必须**把标签写成原始尝试数,
#: 不许叫"已发布" —— 规格:「保留 raw attempts 时标签明确」。
RAW_ATTEMPT_LABEL: str = "发布尝试(未按合同去重)"


def _cutoff(value: Optional[datetime]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def quote_stage_tuple(quote_id: int, *, cutoff: Optional[datetime] = None,
                      cursor: Any = None) -> dict[str, Any]:
    """一张报价在某个 cutoff 的六阶段元组。**这是门户/报告/运营唯一入口**。

    `cursor` 可传:调用方已在事务里时透传,避免自开连接(以及自开连接
    在别人事务中途插一脚导致的锁排队 —— 本仓 08-10 为此把生产打成过 503)。
    """
    if cursor is not None:
        return load_quote_projection(cursor, quote_id=int(quote_id), cutoff=_cutoff(cutoff))
    from db.connection import get_connection

    conn = get_connection()
    try:
        return load_quote_projection(conn.cursor(), quote_id=int(quote_id),
                                     cutoff=_cutoff(cutoff))
    finally:
        conn.close()


def quote_published_active(quote_id: int, *, cutoff: Optional[datetime] = None,
                           cursor: Any = None) -> Optional[int]:
    """真发布有效篇数。**不可用时返回 None**,调用方不许把它当 0 显示。"""
    try:
        projection = quote_stage_tuple(quote_id, cutoff=cutoff, cursor=cursor)
    except ProjectionUnavailable:
        return None
    stage = (projection.get("stages") or {}).get("published_active") or {}
    return stage.get("count") if stage.get("available") else None


def quote_has_published_occurrence(quote_id: int, *, cutoff: Optional[datetime] = None,
                                   cursor: Any = None) -> bool:
    """该报价**发生过**发布(含已撤稿)。

    监测订阅/claim 用的是 occurrence,不是 active —— 撤稿不应该让已经在跑的
    监测凭空停掉(规格:「撤稿仍保留 occurrence,是否停止监测不由本功能擅改」)。
    """
    try:
        return has_published_occurrence(
            quote_stage_tuple(quote_id, cutoff=cutoff, cursor=cursor))
    except ProjectionUnavailable:
        return False


def brand_quote_projections(brand_id: int, *, cutoff: Optional[datetime] = None,
                            cursor: Any = None) -> list[dict[str, Any]]:
    """品牌下每张报价**各自**的六阶段元组。返回列表,**不合并**。

    🔴 这里绝不能加一个"返回合并后的品牌总数"的便捷函数。那正是 WP7 要修掉的
       `brand max merge` 形态 —— 一旦存在,调用方一定会用,Q1 的成果就又会
       出现在 Q2 的门户上。
    """
    own_conn = None
    cur = cursor
    if cur is None:
        from db.connection import get_connection

        own_conn = get_connection()
        cur = own_conn.cursor()
    try:
        cur.execute("SELECT id FROM quotes WHERE brand_id = %s ORDER BY id", (int(brand_id),))
        quote_ids = [int(dict(row)["id"]) for row in (cur.fetchall() or [])]
        out = []
        for qid in quote_ids:
            try:
                out.append(quote_stage_tuple(qid, cutoff=cutoff, cursor=cur))
            except Exception as exc:
                logger.warning("[stage-projection] quote=%s 投影失败: %s", qid, exc)
        return out
    finally:
        if own_conn is not None:
            own_conn.close()


def frozen_report_body(quote_id: int, *, cutoff: Optional[datetime] = None,
                       cursor: Any = None) -> dict[str, Any]:
    """保存报告用的冻结体:cutoff + 六元组 + 全套 source_versions + watermark。

    D10 冻结 fallback(门户 live handler 5xx)读的就是这个体 —— 所以它必须
    **先 canonical projection、后 privacy scrub**,否则 scrub 过的数字再投影
    会得到一个既不是真值也不可复现的数。
    """
    projection = quote_stage_tuple(quote_id, cutoff=cutoff, cursor=cursor)
    return freeze_projection(projection, watermark=projection.get("watermark") or {})


def quote_service_completion(quote_id: int) -> dict[str, Any]:
    """W6 per-keyword 达标天数透传。**永不产出 quote 级 complete**。"""
    return load_service_completion(int(quote_id))


def stage_labels() -> dict[str, str]:
    """六阶段的用户面文案。工程术语不出前端(元指令 B.6)。"""
    return {
        "allocated_capacity": "合同分配",
        "produced_ready": "已做好",
        "submitted": "已提交发布",
        "published_active": "已发布在线",
        "monitored_covered": "已被监测覆盖",
        "strictly_attributed": "已被 AI 引用",
    }


def assert_no_brand_merge_helper() -> None:
    """形态锁:本模块不许出现"品牌级合并"入口。

    反向变异(加一个 `brand_published_total`)必须让这条红。
    """
    banned = {"brand_published_total", "brand_stage_tuple", "merge_brand_stages"}
    present = banned & set(globals())
    if present:
        raise AssertionError(
            f"出现了品牌级合并入口 {sorted(present)} —— 那正是 WP7 修掉的 brand max merge 形态")


__all__ = [
    "RAW_ATTEMPT_LABEL",
    "STAGES",
    "assert_no_brand_merge_helper",
    "brand_quote_projections",
    "frozen_report_body",
    "quote_has_published_occurrence",
    "quote_published_active",
    "quote_service_completion",
    "quote_stage_tuple",
    "source_versions",
    "stage_labels",
]
