"""W7 · 飞轮全景 + 趋势 只读聚合(喂飞轮环五节点 + C3/C4 图表)。

W7-6 允许新增 ≤2 个只读聚合端点。本模块提供:
  - get_flywheel_panorama():环五节点计数(采集→学习→人工→生效→效果)· 数字同源(同屏同指标出自这里)
  - get_flywheel_trends():C3 引用率周走势 + C4 累计增长周计数

🔴 纯 SELECT 不写表、不调 LLM。每个指标各自 try/except fail-soft(缺表/缺列 → 该项 0/[],
不 500 整个面板);真实数据优先,无数据显示 0(前端渲染「数据积累中」,绝不造假)。

[行业口径 2026-08-18] 页面顶部有行业选择器,而本端点原先**不接行业参数** —— 选了「汽车」
五个数仍是全站总数(汽车 17,948 vs 全站 239,298),= 口径误导。现在按工单
`WO_FLYWHEEL_PANORAMA_INDUSTRY_SCOPE_2026-08-16` §1 走 A 路(真过滤)+ 逐环诚实标注:

  环          源表                                    行业列              可过滤
  collect     geo_research_raw                        industry(中文名)   ✅
  learn       geo_research_source_signals             industry_key(英文) ✅
  review      绑定候选扫描 + style_control drafts      —                  ❌ → industry_scope="site"
  apply       style_control active_by_style            —                  ❌ → industry_scope="site"
  outcome     writing_article_version_fingerprints     industry_key       ✅

🔴 两张主表的行业**词表不同**(raw 存中文「汽车」,shadow 表存英文 key「auto」)——
   所以必须过 `industry_filter_values()`(既有 SSOT,article_structure_analysis 同款用法)
   取别名全集再 `= ANY(...)`;直接拿一个字符串两边比会静默查出 0 行。
🔴 没有行业维度的环**不硬造**(硬造 = 新的误导),只标 industry_scope="site",前端标「全站口径」。
🔴 全站口径(industry 为空/general/全部行业)时 SQL 与本改动前**逐字相同** —— 不许把全量口径改坏。
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from db.connection import get_connection
from services.media_entity_flywheel import industry_filter_values

logger = logging.getLogger("GEO-WritingFlywheel.Panorama")

# [P0 2026-08-16] fail-soft 降级痕迹收集(线程局部:panorama 在 to_thread 里跑,
# 多个请求各自独立,不能用模块级全局互相污染)。
_DEGRADED = threading.local()


def _mark_degraded(what: str, exc: Exception) -> None:
    bucket = getattr(_DEGRADED, "items", None)
    if bucket is None:
        return  # 不在 _degrade_scope() 内(如被别处直接调用)→ 不收集,行为与旧版一致
    bucket.append(f"{what}: {str(exc)[:120]}")


@contextmanager
def _degrade_scope() -> Iterator[list[str]]:
    """一次 panorama 计算期间收集所有被 fail-soft 吞掉的失败。"""
    items: list[str] = []
    prev = getattr(_DEGRADED, "items", None)
    _DEGRADED.items = items
    try:
        yield items
    finally:
        _DEGRADED.items = prev


def _scalar(sql: str, params: tuple = ()) -> int:
    """跑一个 COUNT 类查询,独立连接,fail-soft 返回 0。

    🔴 [P0 2026-08-16] 只返回 0 会把「查不出来」伪装成「真的是 0」。
    2026-08-16 全站被锁排死时,本函数每一项都静静吞成 0,面板照常渲染
    「采集 0 / 学习 0 · 运行正常」—— 事故被伪装成正常态。
    现在失败会记进 `_DEGRADED`,由 get_flywheel_panorama() 汇总成 degraded 标记
    交给前端;数值本身**保持 0 不变**(下游按 int 消费,改成 None 会连锁炸)。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        if not row:
            return 0
        val = list(row.values())[0] if isinstance(row, dict) else row[0]
        return int(val or 0)
    except Exception as exc:
        logger.warning("[panorama] scalar 查询失败(fail-soft 0 · 已标 degraded): %s", str(exc)[:150])
        _mark_degraded("scalar", exc)
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _scalar_opt(sql: str, params: tuple = ()) -> "int | None":
    """同 _scalar,但查询**失败返回 None**(区分「真实 0」与「查询失败」)。

    用于派生比率:cited 查询若瞬时失败,不能把 None 当 0 → 否则 citation_rate=0 会把
    「取数 bug」伪装成「真实 0% 引用率」喂给 LLM(fail-soft 吞取数 bug 的经典坑)。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        if not row:
            return 0
        val = list(row.values())[0] if isinstance(row, dict) else row[0]
        return int(val or 0)
    except Exception as exc:
        logger.warning("[panorama] scalar_opt 查询失败(返回 None 标未知 · 已标 degraded): %s", str(exc)[:150])
        _mark_degraded("scalar_opt", exc)
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _active_writing_versions() -> int:
    try:
        from writing.style_control import load_control_state
        state = load_control_state()
        # 非 baseline 的 active(真正被替换过的文体)
        versions = {v.get("version_id"): v for v in state.get("versions") or []}
        cnt = 0
        for vid in (state.get("active_by_style") or {}).values():
            v = versions.get(vid)
            if v and not v.get("stable_baseline"):
                cnt += 1
        return cnt
    except Exception as exc:
        _mark_degraded("active_writing_versions", exc)
        return 0


def _pending_reviews() -> dict[str, int]:
    """人工待办三类:媒体绑定建议 / 写作新候选 / 引擎重要性建议。各自 fail-soft。"""
    out = {"binding_candidates": 0, "writing_drafts": 0, "engine_weight_candidates": 0}
    try:
        from db.media_entity_flywheel_db import count_recommended_binding_candidates
        from services.media_binding_candidates import RECOMMENDED_BINDING_MIN_CONFIDENCE
        # [review fix] min_confidence 是必传参(缺参 TypeError 被 fail-soft 吞成恒 0);SSOT 与媒体飞轮 tab 同源
        out["binding_candidates"] = int(count_recommended_binding_candidates(RECOMMENDED_BINDING_MIN_CONFIDENCE) or 0)
    except Exception as exc:
        _mark_degraded("pending.binding_candidates", exc)
    try:
        from writing.style_control import load_control_state
        state = load_control_state()
        out["writing_drafts"] = sum(1 for v in state.get("versions") or [] if v.get("status") == "draft")
    except Exception as exc:
        _mark_degraded("pending.writing_drafts", exc)
    return out


def get_flywheel_panorama(industry_key: str | None = None) -> dict[str, Any]:
    """飞轮环五节点计数(数字同源单一来源)。

    industry_key 为空 / general / 全部行业 → 全站口径(SQL 与加行业过滤前逐字相同)。
    指定行业 → 有行业列的三环真过滤,没有行业列的两环保持全站并标 industry_scope="site"。
    """
    # [] = 全站口径。非空 = 该行业的**别名全集**(含中文名与英文 key),两张主表词表不同靠它对齐。
    scope_values = industry_filter_values(industry_key or "")
    scoped = bool(scope_values)
    node_scope = "industry" if scoped else "site"

    with _degrade_scope() as degraded_items:
        if scoped:
            collected = _scalar("SELECT COUNT(*) FROM geo_research_raw WHERE industry = ANY(%s)", (scope_values,))
            learned = _scalar(
                "SELECT COUNT(*) FROM geo_research_source_signals WHERE industry_key = ANY(%s)", (scope_values,)
            )
            fingerprinted = _scalar(
                "SELECT COUNT(*) FROM writing_article_version_fingerprints WHERE industry_key = ANY(%s)",
                (scope_values,),
            )
            cited = _scalar_opt(
                "SELECT COUNT(*) FROM geo_research_raw WHERE is_answer_cited = TRUE AND industry = ANY(%s)",
                (scope_values,),
            )
        else:
            collected = _scalar("SELECT COUNT(*) FROM geo_research_raw")
            # [review fix] 表名是 geo_research_source_signals(geo_source_signals 不存在,fail-soft 会吞成恒 0 假空态)
            learned = _scalar("SELECT COUNT(*) FROM geo_research_source_signals")
            fingerprinted = _scalar("SELECT COUNT(*) FROM writing_article_version_fingerprints")
            cited = _scalar_opt("SELECT COUNT(*) FROM geo_research_raw WHERE is_answer_cited = TRUE")
        # 🔴 这两环在库里**没有行业维度**(绑定候选扫描无行业列;style_control 版本按 style_code 存,
        #    行业只烘在 strategy_summary 文本里,不可靠)→ 不硬造过滤,保持全站并如实标注。
        pending = _pending_reviews()
        pending_total = sum(pending.values())
        applied = _active_writing_versions()
    # [review fix · P1] cited 查询失败(None)≠ 真实 0:此时 citation_rate 标未知,不让下游把假 0 当真相
    citation_known = collected > 0 and cited is not None
    citation_rate = round(cited / collected, 4) if citation_known else 0.0

    return {
        # 🔴 [P0 2026-08-16] degraded=True 时上面这些 0 是"没查出来",不是"真的是 0"。
        # 前端必须渲染「数据加载失败」而不是「0 · 运行正常」—— 2026-08-16 全站被锁
        # 排死时,本面板正是靠一片 0 把事故伪装成了正常态。
        "degraded": bool(degraded_items),
        "degraded_reasons": degraded_items,
        # [行业口径 2026-08-18] 每环自带 industry_scope:
        #   "industry" = 这个数已按所选行业过滤;"site" = 该环库里没有行业维度,仍是全站口径。
        #   前端必须对 "site" 的环标「全站口径」—— 否则就是本工单要修的那种口径误导。
        "nodes": [
            {"key": "collect", "label": "采集 AI 回答", "value": collected, "industry_scope": node_scope},
            {"key": "learn", "label": "自动学习规律", "value": learned, "industry_scope": node_scope},
            {"key": "review", "label": "人工把关", "value": pending_total, "detail": pending,
             "industry_scope": "site"},
            {"key": "apply", "label": "应用到写作", "value": applied, "industry_scope": "site"},
            {"key": "outcome", "label": "效果回流", "value": fingerprinted,
             "citation_rate": citation_rate, "industry_scope": node_scope},
        ],
        "citation_rate": citation_rate,
        "citation_rate_known": citation_known,  # [P1] False = 引用率数据暂缺(空/查询失败),下游勿当真实 0
        "pending_total": pending_total,
        # 顶层行业口径:industry_key = 请求的行业(空 = 全站);industry_scoped = 是否真过滤了;
        # industry_values = 实际用于比对的别名全集(可判别、可复现)。
        "industry_key": (industry_key or "").strip() or "all",
        "industry_scoped": scoped,
        "industry_values": scope_values,
        "site_scope_nodes": ["review", "apply"] if scoped else [],
        "source": "flywheel_panorama · 纯 SELECT · 每项 fail-soft",
    }


def get_flywheel_trends(weeks: int = 12) -> dict[str, Any]:
    """C3 引用率周走势 + C4 累计增长。fail-soft:查询失败返回空序列。"""
    weeks = max(1, min(int(weeks or 12), 52))
    trend_rows: list[dict[str, Any]] = []
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT date_trunc('week', created_at) AS wk,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE is_answer_cited = TRUE) AS cited
            FROM geo_research_raw
            WHERE created_at >= NOW() - (%s || ' weeks')::interval
            GROUP BY wk ORDER BY wk
            """,
            (weeks,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        cumulative = 0
        for r in rows:
            total = int(r.get("total") or 0)
            cited = int(r.get("cited") or 0)
            cumulative += total
            wk = r.get("wk")
            trend_rows.append({
                "week": wk.strftime("%Y-%m-%d") if hasattr(wk, "strftime") else str(wk),
                "total": total,
                "cited": cited,
                "citation_rate": round(cited / total, 4) if total else 0.0,
                "cumulative": cumulative,
            })
    except Exception as exc:
        logger.debug("[panorama] trends 查询失败(fail-soft []): %s", str(exc)[:150])
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return {"weeks": weeks, "series": trend_rows, "source": "geo_research_raw 周聚合 · 纯 SELECT"}
