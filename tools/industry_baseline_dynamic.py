"""
industry_baseline_dynamic — 动态行业基线 P50/P90(替代填不满的 INDUSTRY_MEDIAN_MONTHLY)

设计(2026-06-11 · 老板 pricing v2 拍板):
  - 取代 tools/industry_median.py 的 30 行业硬编码字典(永远填不满)
  - 数据源优先级:
      1. prod paid quotes 反推 P50/P90(样本 ≥ 5 时优先 · 真实代理愿付价)
      2. LLM 估行业基线(deepseek-v4-flash · 数据不足兜底)
      3. 全局兜底(P50=4000 / P90=10000 · LLM 失败保底)
  - 6h 内存缓存 · 避免每次调 LLM 烧 token
  - 行业子串匹配(避免"装修服务" vs "装修"完全匹配漏)

红线:
  - 不碰 billing.py / connection.py / 红线 4 文件 baseline
  - 不调 prod 写权限(只读 SELECT)
  - LLM 失败必兜底 · 绝不 raise 让算价崩

用法:
  from tools.industry_baseline_dynamic import get_industry_baseline
  baseline = await get_industry_baseline("装修")
  # {"p50": 3500, "p90": 8000, "source": "prod_paid"|"llm_estimate"|"fallback", "sample_size": N}
"""
from __future__ import annotations
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH

import asyncio
import json
import logging
import os
import re
import time
from typing import Optional

import httpx

logger = logging.getLogger("GEO-IndustryBaseline")

# ============ 兜底常量(LLM 全失败时保底 · 不进算价主链路) ============
_FALLBACK_P50 = 4000
_FALLBACK_P90 = 10000

# ============ 缓存 ============
_BASELINE_CACHE: dict[str, tuple[dict, float]] = {}  # industry → (baseline, expires_at)
_CACHE_TTL_SEC = 6 * 3600  # 6 小时

# ============ prod paid 样本阈值 ============
_MIN_SAMPLE_FOR_PROD_REPLACE = 5  # < 5 个 paid quote → 走 LLM(数据不足)


def _normalize_industry(industry: str) -> str:
    """归一化行业名(去标点 · 去空格 · 小写 · 用作 cache key)"""
    if not industry:
        return ""
    return re.sub(r"[\s\W_]+", "", industry).lower()


def _cache_get(industry: str) -> Optional[dict]:
    key = _normalize_industry(industry)
    if not key:
        return None
    entry = _BASELINE_CACHE.get(key)
    if not entry:
        return None
    baseline, expires_at = entry
    if expires_at < time.time():
        _BASELINE_CACHE.pop(key, None)
        return None
    return baseline


def _cache_set(industry: str, baseline: dict) -> None:
    key = _normalize_industry(industry)
    if not key:
        return
    _BASELINE_CACHE[key] = (baseline, time.time() + _CACHE_TTL_SEC)
    if len(_BASELINE_CACHE) > 200:
        oldest = min(_BASELINE_CACHE.items(), key=lambda kv: kv[1][1])
        _BASELINE_CACHE.pop(oldest[0], None)


async def _query_prod_paid_baseline(industry: str) -> Optional[dict]:
    """从 prod quotes 表反推 P50/P90(WHERE status='paid' AND industry LIKE %industry%)

    注:industry 子串模糊匹配 · 兼容"家装" vs "装修"差异
    返:{p50, p90, source: 'prod_paid', sample_size} 或 None(查询失败/样本不足)
    """
    if not industry:
        return None
    try:
        from db.connection import get_db
    except Exception as exc:
        logger.debug("industry_baseline: get_db import 失败 · %s", exc)
        return None

    norm = industry.strip()
    if not norm:
        return None

    # 列名 prod 实证(2026-06-11 \d quotes):月费列 = monthly_price(real)· 旧版查错的总额列不存在
    # 样本清洗:排除测试品牌(M3 测试客户隔离铁律)+ 软删(脏样本会把基线拉飞 · prod 实证 1 笔
    #   is_test 品牌 ¥9386 + 1 笔历史爆价 bug 单 ¥77791 曾混入)
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
    pattern_a = f"%{norm}%"
    pattern_b = f"%{norm[:2]}%" if len(norm) >= 2 else pattern_a

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (pattern_a, pattern_b))
                row = cur.fetchone()
        if not row:
            return None
        if isinstance(row, dict):
            p50 = row.get("p50") or 0
            p90 = row.get("p90") or 0
            sample = row.get("sample_size") or 0
        else:
            p50, p90, sample = row[0] or 0, row[1] or 0, row[2] or 0
        if sample < _MIN_SAMPLE_FOR_PROD_REPLACE:
            return None
        return {
            "p50": int(p50),
            "p90": int(p90),
            "source": "prod_paid",
            "sample_size": int(sample),
        }
    except Exception as exc:
        logger.debug("industry_baseline: prod 查询失败 · %s", exc)
        return None


_LLM_BASELINE_PROMPT = """请估算下面中文行业的 GEO 关键词月报价基线(代理给客户的月费 · ¥)。

行业:{industry}

参考锚点(可类比 · 行业越接近金融/医疗/法律越高 · 越偏个人服务/资讯越低):
- 装修 / 家装 / 家居:P50 ¥3500 / P90 ¥8000
- 美容 / 医美 / 摄影:P50 ¥3500 / P90 ¥9000
- 法律 / 律所 / 咨询:P50 ¥4500-5000 / P90 ¥10000-11000
- 教育 / 培训 / 留学:P50 ¥4000-5500 / P90 ¥9500-13000
- 医疗 / 医院 / 口腔:P50 ¥5000-5500 / P90 ¥11500-12000
- 机械 / 化工 / 工业 B2B:P50 ¥5500-6500 / P90 ¥13000-14500
- SaaS / CRM / ERP / 软件:P50 ¥6000-7000 / P90 ¥14000-16000
- 餐饮 / 婚庆 / 生活服务:P50 ¥2800-3200 / P90 ¥7000-7800

考虑因素:
1. 行业客单价(高决策金额行业 → 高基线)
2. 竞争烈度(红海 → 高基线)
3. 监管/合规成本(医美/律所 → 高基线)
4. 受众规模(C 端大众 → 中等 / B 端窄 → 高)

严格按 JSON 返回(无任何额外文字):
{{"p50": <int>, "p90": <int>, "reasoning": "<≤30 字理由>"}}
"""


async def _call_llm_baseline(industry: str, timeout: float = 30.0) -> Optional[dict]:
    """调 deepseek-v4-flash 估行业基线(走 main 916211db 多 key failover · 单 key 直调零开销)· 失败返 None"""
    prompt = _LLM_BASELINE_PROMPT.format(industry=industry or "(未提供)")
    try:
        from services.llm.deepseek_key_pool import adeepseek_call_with_failover

        async def _do(key: str):
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    "https://api.deepseek.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={
                        "model": DEEPSEEK_OFFICIAL_FLASH,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.1,
                        "max_tokens": 200,
                        "thinking": {"type": "disabled"},
                    },
                )
            if resp.status_code != 200:
                raise RuntimeError(f"industry_baseline LLM HTTP {resp.status_code}")
            return resp

        response = await adeepseek_call_with_failover(_do, role="normal")
        content = response.json()["choices"][0]["message"]["content"]
        m = re.search(r"\{[^{}]*\}", content, re.DOTALL)
        if not m:
            return None
        data = json.loads(m.group())
        p50 = int(data.get("p50") or 0)
        p90 = int(data.get("p90") or 0)
        if p50 <= 0 or p90 <= 0 or p90 < p50:
            return None
        return {
            "p50": p50,
            "p90": p90,
            "source": "llm_estimate",
            "sample_size": 0,
            "reasoning": (data.get("reasoning") or "")[:100],
        }
    except Exception as exc:
        logger.debug("industry_baseline LLM 调用失败 · %s", exc)
        return None


async def get_industry_baseline(industry: str, force_refresh: bool = False) -> dict:
    """行业月费基线 P50/P90(优先 prod paid 反推 · 回落 LLM · 兜底硬常量)

    Returns:
        {p50: int, p90: int, source: 'prod_paid'|'llm_estimate'|'fallback', sample_size: int}
    """
    if not industry:
        return {"p50": _FALLBACK_P50, "p90": _FALLBACK_P90, "source": "fallback", "sample_size": 0}

    if not force_refresh:
        cached = _cache_get(industry)
        if cached:
            return cached

    prod = await _query_prod_paid_baseline(industry)
    if prod:
        _cache_set(industry, prod)
        return prod

    llm = await _call_llm_baseline(industry)
    if llm:
        _cache_set(industry, llm)
        return llm

    fallback = {
        "p50": _FALLBACK_P50,
        "p90": _FALLBACK_P90,
        "source": "fallback",
        "sample_size": 0,
    }
    _cache_set(industry, fallback)
    return fallback


def get_industry_baseline_sync(industry: str) -> dict:
    """同步包装(给非 async 调用方 · report_writer 的 build_module_5_opportunity 等)"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            return {"p50": _FALLBACK_P50, "p90": _FALLBACK_P90, "source": "fallback", "sample_size": 0}
        return loop.run_until_complete(get_industry_baseline(industry))
    except RuntimeError:
        return asyncio.run(get_industry_baseline(industry))
    except Exception as exc:
        logger.debug("industry_baseline_sync: 异常 · %s", exc)
        return {"p50": _FALLBACK_P50, "p90": _FALLBACK_P90, "source": "fallback", "sample_size": 0}


def _reset_cache_for_test() -> None:
    """测试用:清空缓存"""
    _BASELINE_CACHE.clear()
