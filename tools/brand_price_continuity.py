"""
brand_price_continuity — 同 brand 历史 paid 价连续性 ±25% 警示(pricing v2 第 4 道护栏)

设计(2026-06-11 · 老板 pricing v2 拍板):
  - 同一 brand 在 v1.x 已 paid quote 平均月价 vs v2 LLM 评估师新报价
  - 偏差超 ± 25% → 警示 needs_review(不挡 · 仅复盘)
  - 目的:同 brand 价格突变 → 代理用户感知"乱算"(老板原话:"客户宁愿看今天和昨天一样")
  - 90 天 lookback 窗口(代理服务周期长 · 月报频繁动会动摇代理信任)
  - 样本 < 2 paid quote → 跳过判定(数据不足无信号)

红线:
  - 只查 quotes 表 status='paid' · 不动 confirmed(玩法 B paid_at NULL · 噪声大)
  - 失败 fail-soft 返 {ok: True} · 不阻塞算价
  - 不进 cache(逻辑轻 · 每次查 prod 即可 · 1 个 brand ~10 ms)

用法:
  from tools.brand_price_continuity import check_brand_continuity
  r = await check_brand_continuity(brand_id=88, new_monthly_price=12500)
  # {"ok": True, "last_avg": 9800, "deviation_pct": 27.6, "needs_review": True, "sample_size": 3}
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("GEO-BrandContinuity")

_LOOKBACK_DAYS = 90
_MIN_SAMPLE = 2
_DEVIATION_THRESHOLD = 0.25  # ±25%


async def check_brand_continuity(
    brand_id: Optional[int],
    new_monthly_price: float,
    lookback_days: int = _LOOKBACK_DAYS,
) -> dict:
    """同 brand 历史 paid 月价 vs 新报价偏差判定。

    Returns:
        {
          ok: bool,                  # 判定是否成功(prod 查询/数据足)
          last_avg: int,             # 最近 N 天 paid 平均月价(0=无样本)
          sample_size: int,          # 样本数
          deviation_pct: float,      # 偏差百分比(正=新价偏高 · 负=偏低)
          needs_review: bool,        # 是否触发警示(|偏差| > 25% 且样本 ≥ 2)
          lookback_days: int,
        }
    """
    default = {
        "ok": False,
        "last_avg": 0,
        "sample_size": 0,
        "deviation_pct": 0.0,
        "needs_review": False,
        "lookback_days": lookback_days,
    }
    if not brand_id or new_monthly_price <= 0:
        return default
    try:
        from db.connection import get_db
    except Exception as exc:
        logger.debug("brand_continuity: get_db import 失败 · %s", exc)
        return default

    # 列名 prod 实证(2026-06-11 \d quotes):月费列 = monthly_price(real)· 旧版查错的总额列不存在
    # 口径:quote 级月费 vs quote 级月费(同量纲)· 单词价不可比 → 本函数只在 batch 聚合层调用
    sql = """
        SELECT
            AVG(q.monthly_price)::int AS avg_price,
            COUNT(*)::int AS sample_size
        FROM quotes q
        WHERE q.brand_id = %s
          AND q.status = 'paid'
          AND COALESCE(q.monthly_price, 0) > 0
          AND q.deleted_at IS NULL
          AND q.paid_at >= NOW() - (%s || ' days')::interval
    """
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (brand_id, str(lookback_days)))
                row = cur.fetchone()
        if not row:
            return default
        if isinstance(row, dict):
            avg = row.get("avg_price") or 0
            sample = row.get("sample_size") or 0
        else:
            avg = row[0] or 0
            sample = row[1] or 0
        if sample < _MIN_SAMPLE or avg <= 0:
            return {
                "ok": True,
                "last_avg": int(avg),
                "sample_size": int(sample),
                "deviation_pct": 0.0,
                "needs_review": False,
                "lookback_days": lookback_days,
            }
        deviation = (float(new_monthly_price) - float(avg)) / float(avg)
        needs_review = abs(deviation) > _DEVIATION_THRESHOLD
        return {
            "ok": True,
            "last_avg": int(avg),
            "sample_size": int(sample),
            "deviation_pct": round(deviation * 100, 2),
            "needs_review": needs_review,
            "lookback_days": lookback_days,
        }
    except Exception as exc:
        logger.debug("brand_continuity: prod 查询失败 · %s", exc)
        return default
