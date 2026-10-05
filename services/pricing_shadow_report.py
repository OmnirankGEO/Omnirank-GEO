"""[B4-2] 飞轮行业引用格局 → 定价影响 shadow 报告(放行门)。

用途:开 PRICING_P0D_TRUST_ASSET_ENABLED 前,让老板/Deploy 看清"开启飞轮水源"会怎样改动报价篇数。
纪律:只读 · 不写价格缓存 · 不翻 flag · 不落库。landscape 因子只在 flag 开 + 自然重算触发点生效
(价格锁语义不破)。篇数 ±X% ≈ 价格 ±X%(价随篇数走);逐词精确 comp 变化见 _assemble_result guards.industry_landscape。
红线:不 import billing/charge/deduct。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("GEO-PricingShadow")


def _safe_flag(fn) -> bool:
    try:
        return bool(fn())
    except Exception:
        return False


def build_flywheel_landscape_shadow_report(industry: str | None = None, min_signals: int | None = None) -> dict:
    """按行业算飞轮引用格局难度因子对报价篇数的影响。industry 指定则只算该行业,否则全量有数据行业。"""
    from db.connection import get_connection
    from services.source_authority_analyzer import get_industry_citation_landscape
    from config.pricing_config import get_industry_landscape_config
    from tools.pricing_llm_assessor import _industry_landscape_factor
    from tools.llm_pricing_flag import is_trust_asset_enabled

    lcfg = get_industry_landscape_config()
    min_sources = int(min_signals if min_signals is not None else lcfg["min_sources"])

    conn = get_connection()
    industries_report: list[dict] = []
    summary = {"industries_with_data": 0, "would_uplift": 0, "would_discount": 0, "neutral": 0}
    try:
        cur = conn.cursor()
        if industry:
            from services.media_entity_flywheel import normalize_industry_key
            industry_keys = [normalize_industry_key(industry)]
        else:
            cur.execute("""
                SELECT industry_key, COUNT(*) AS n
                FROM geo_research_source_signals
                WHERE domain IS NOT NULL AND domain <> ''
                GROUP BY industry_key
                HAVING COUNT(*) >= %s
                ORDER BY n DESC
                LIMIT 100
            """, (min_sources,))
            industry_keys = [r["industry_key"] for r in cur.fetchall()]

        for ik in industry_keys:
            landscape = get_industry_citation_landscape(ik, conn=conn)
            if not landscape or int(landscape.get("total_signals") or 0) < min_sources:
                continue
            share = float(landscape.get("top_domain_share") or 0)
            factor = _industry_landscape_factor(
                share, lcfg["low_share"], lcfg["high_share"], lcfg["max_uplift"], lcfg["max_discount"])
            if factor > 1.0 + 1e-6:
                direction = "集中·上调篇数"
                summary["would_uplift"] += 1
            elif factor < 1.0 - 1e-6:
                direction = "分散·下调篇数"
                summary["would_discount"] += 1
            else:
                direction = "中性·不调"
                summary["neutral"] += 1
            affected = 0
            try:
                cur.execute("""
                    SELECT COUNT(*) AS c FROM keyword_price_cache
                    WHERE LOWER(COALESCE(industry, '')) = LOWER(%s)
                      AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
                """, (ik,))
                affected = int((cur.fetchone() or {}).get("c") or 0)
            except Exception:
                affected = 0
            industries_report.append({
                "industry_key": ik,
                "top_domain_share": round(share, 4),
                "total_signals": int(landscape.get("total_signals") or 0),
                "distinct_domains": int(landscape.get("distinct_domains") or 0),
                "landscape_factor": round(factor, 4),
                "direction": direction,
                "article_impact_pct": f"{(factor - 1.0) * 100:+.1f}%",
                "affected_cached_keywords": affected,
            })
            summary["industries_with_data"] += 1

        industries_report.sort(key=lambda x: abs(x["landscape_factor"] - 1.0), reverse=True)
        return {
            "flag": "PRICING_P0D_TRUST_ASSET_ENABLED",
            "flag_currently_on": _safe_flag(is_trust_asset_enabled),
            "config": lcfg,
            "note": "只算不落库·不写价格缓存·不翻 flag。landscape 因子只在 flag 开 + 自然重算触发点生效"
                    "(价格锁语义不破)。篇数 ±X% ≈ 价格 ±X%;逐词精确 comp 变化见 guards.industry_landscape。",
            "industries": industries_report,
            "summary": summary,
        }
    finally:
        conn.close()
