"""
industry_median(v2.0 thin wrapper · 2026-06-11)— 算价 sanity 老 API 兼容层

【v2.0 老板拍 一次性重构】
  - 删 INDUSTRY_MEDIAN_MONTHLY 字典(30 行业硬编码 · 永远填不满)
  - 删 UNKNOWN_INDUSTRY_P50/P90 硬兜底常量
  - get_industry_median 内部改:同步查 prod paid quotes 反推 P50/P90(真实代理愿付价)
  - 老 API 接口完全兼容(get_industry_median / describe_price_health / describe_price_deviation /
    is_keyword_price_high / industry_median_check)

调用方(继续工作):
  - tools/keyword_value_scorer 历史 · v2 已切 pricing_llm_assessor(本 wrapper 算价路径不再用)
  - services/report_writer_v2.build_module_5_opportunity(报告 Module 5 机会估算)
  - api/admin_api · audit C2.3 prompt 调优 suggestion 文案
  - tests/test_keyword_quality_regression(回归测试)

主算价路径:已切 pricing_llm_assessor.assess_keyword_pricing(双 LLM + 6 护栏)· 不依赖本模块。

红线:不碰 billing.py / 红线 4 文件 baseline · 失败 fail-soft 返保守兜底(P50=4000/P90=10000)。
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

logger = logging.getLogger("GEO-IndustryMedian")

# ===== 业务定义类(老板拍 · v2 保留)=====
WARN_SINGLE_KW = 1500          # 单关键词高价警示线(元指令 10 · 前端红标)
DANGER_TOTAL_MONTHLY = 10000   # 总价全局预警线(超出建议先和客户沟通预算)

# ===== 兜底常量(prod 查询失败 / 样本不足时返此 · 不再走字典命中)=====
# ⚠️ 跟 tools/industry_baseline_dynamic.py 是同一"行业 P50/P90"真相的两份实现(本模块同步 · 那边 async+6h 缓存)
#   SQL 过滤/阈值两边必须同步改(P2 跟踪:下批合并为单实现 · 本批先对齐口径)
_FALLBACK_P50 = 4000
_FALLBACK_P90 = 10000
_MIN_SAMPLE = 5                 # < 5 个 paid quote → 走兜底(对齐 industry_baseline_dynamic)


def get_industry_median(industry: str) -> dict[str, int]:
    """行业月费 P50/P90 · 同步查 prod paid quotes 反推 · 失败回兜底。

    [v2.0 thin wrapper] 删原 INDUSTRY_MEDIAN_MONTHLY 字典 · 改实时查真实代理付款数据
    样本不足(< 3 个 paid quote)→ 返保守兜底 {p50: 4000, p90: 10000}
    """
    if not industry:
        return {"p50": _FALLBACK_P50, "p90": _FALLBACK_P90}
    try:
        from db.connection import get_db
    except Exception:
        return {"p50": _FALLBACK_P50, "p90": _FALLBACK_P90}

    norm = industry.strip()
    if not norm:
        return {"p50": _FALLBACK_P50, "p90": _FALLBACK_P90}

    # 列名 prod 实证(2026-06-11 \d quotes):月费列 = monthly_price(real)· 旧版查错的总额列不存在
    # 样本清洗:排除测试品牌 + 软删(对齐 industry_baseline_dynamic 同口径)
    sql = """
        SELECT
            percentile_cont(0.5) WITHIN GROUP (ORDER BY q.monthly_price)::int AS p50,
            percentile_cont(0.9) WITHIN GROUP (ORDER BY q.monthly_price)::int AS p90,
            COUNT(*)::int AS sample_size
        FROM quotes q
        JOIN brands b ON q.brand_id = b.id
        WHERE q.status = 'paid'
          AND COALESCE(q.monthly_price, 0) > 0
          AND COALESCE(b.is_test, FALSE) = FALSE
          AND q.deleted_at IS NULL
          AND (b.industry ILIKE %s OR b.industry ILIKE %s)
    """
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (f"%{norm}%", f"%{norm[:2]}%" if len(norm) >= 2 else f"%{norm}%"))
                row = cur.fetchone()
        if not row:
            return {"p50": _FALLBACK_P50, "p90": _FALLBACK_P90}
        if isinstance(row, dict):
            p50 = row.get("p50") or 0
            p90 = row.get("p90") or 0
            sample = row.get("sample_size") or 0
        else:
            p50, p90, sample = row[0] or 0, row[1] or 0, row[2] or 0
        if sample < _MIN_SAMPLE or p50 <= 0 or p90 <= 0:
            return {"p50": _FALLBACK_P50, "p90": _FALLBACK_P90}
        return {"p50": int(p50), "p90": int(p90)}
    except Exception as exc:
        logger.debug("industry_median: prod 查询失败 · 回兜底 · %s", exc)
        return {"p50": _FALLBACK_P50, "p90": _FALLBACK_P90}


def describe_price_health(total_monthly: float, industry: str,
                          median: Optional[dict] = None) -> str:
    """返 normal / warn / danger(可传已查好的 median 避免重复 DB 查询)"""
    if total_monthly >= DANGER_TOTAL_MONTHLY:
        return "danger"
    median = median or get_industry_median(industry)
    if total_monthly > median["p90"]:
        return "danger"
    if total_monthly > median["p50"] * 1.3:
        return "warn"
    return "normal"


def describe_price_deviation(total_monthly: float, industry: str,
                             median: Optional[dict] = None) -> int:
    """相对 p50 偏差百分比(正=偏高 · 负=偏低)(可传已查好的 median 避免重复 DB 查询)"""
    median = median or get_industry_median(industry)
    p50 = median["p50"]
    if p50 == 0:
        return 0
    return round(((total_monthly - p50) / p50) * 100)


def is_keyword_price_high(price: float) -> bool:
    return price >= WARN_SINGLE_KW


def industry_median_check(
    total_monthly: float,
    industry: str = "",
    keyword_prices: Optional[Iterable[float]] = None,
    keyword_entries: Optional[list[dict]] = None,
) -> dict:
    """M1b M3 综合审计 · 建议模式(代理可采纳/拒绝/忽略)。

    返结构跟 v1.1 完全兼容(level/industry_median_p50/p90/deviation_pct/suggestion/
    single_kw_outliers/auditor_suggested_cap)。
    """
    # 单次查询复用(原实现 3 处各查一次 · 同入参 3 倍重复 DB 查询)
    median = get_industry_median(industry)
    level = describe_price_health(total_monthly, industry, median=median)
    deviation_pct = describe_price_deviation(total_monthly, industry, median=median)

    outliers: list[dict] = []
    if keyword_entries:
        for entry in keyword_entries:
            if not isinstance(entry, dict):
                continue
            price = entry.get("price") or entry.get("selling_price") or 0
            try:
                price_f = float(price)
            except (TypeError, ValueError):
                continue
            if is_keyword_price_high(price_f):
                outliers.append({
                    "keyword": entry.get("keyword", ""),
                    "price": round(price_f),
                    "threshold": WARN_SINGLE_KW,
                })
    elif keyword_prices:
        for p in keyword_prices:
            try:
                p_f = float(p)
            except (TypeError, ValueError):
                continue
            if is_keyword_price_high(p_f):
                outliers.append({"keyword": "", "price": round(p_f), "threshold": WARN_SINGLE_KW})

    suggestion_parts: list[str] = []
    if level == "normal":
        suggestion_parts.append(
            f"总价 ¥{round(total_monthly)} 在 {industry or '该行业'} 合理区间(中位 ¥{median['p50']} · 偏差 {deviation_pct:+d}%)"
        )
    elif level == "warn":
        suggestion_parts.append(
            f"总价 ¥{round(total_monthly)} 高于 {industry or '该行业'} 中位 ¥{median['p50']} 约 {deviation_pct}%,"
            f"仍在合理区间(上限 ¥{median['p90']})· 代理可酌情说明"
        )
    else:
        if total_monthly >= DANGER_TOTAL_MONTHLY:
            suggestion_parts.append(
                f"总价 ¥{round(total_monthly)} 超过全局预警线 ¥{DANGER_TOTAL_MONTHLY},"
                "建议先和客户沟通预算再发送"
            )
        else:
            p90_dev_basis = round(((median["p90"] - median["p50"]) / max(median["p50"], 1)) * 100)
            suggestion_parts.append(
                f"总价 ¥{round(total_monthly)} 高于 {industry or '该行业'} p90(¥{median['p90']}) "
                f"约 {deviation_pct - p90_dev_basis}%,"
                "建议复查关键词集合 / 降档套餐"
            )

    if outliers:
        suggestion_parts.append(
            f"其中 {len(outliers)} 个关键词单价 ≥ ¥{WARN_SINGLE_KW}(元指令 10 · 前端已红标)· 代理审阅"
        )

    auditor_suggested_cap: Optional[int] = None
    if level == "danger":
        auditor_suggested_cap = median["p90"]

    return {
        "level": level,
        "industry_median_p50": median["p50"],
        "industry_median_p90": median["p90"],
        "deviation_pct": deviation_pct,
        "suggestion": " · ".join(suggestion_parts),
        "single_kw_outliers": outliers,
        "auditor_suggested_cap": auditor_suggested_cap,
    }
