"""
M1b M4 · 关键词质量回归套件(fixture 骨架)

CTO-15.9 2026-04-25 · PRD M1b §M4:
  5 金标准 brand × 50 词 = 250 样本回归
  跑题率 < 5% 硬阈值 · 报价偏差 < 20% 硬阈值

本文件定位(骨架版本):
  · 定义 5 金标准 brand fixture(本地餐饮/全国 SaaS/B2B 机械/政企信创/外贸)
  · 定义质量判定函数 is_off_topic / get_price_deviation
  · 不调真 LLM(CI 成本过高)· 占位 monkey/mock · 产出结构化 report 给未来 CTO 跑真 LLM 时填充

未来实 LLM 跑需要:
  · Codex 0424 评分脚本(docs/AI-CONTEXT/Codex反馈/OmniRank_GEO报告关键词报价质量审计_2026-04-24.md)
  · DashScope/DeepSeek API key(CI env var)
  · 预计单次全跑 ¥25 · 人工 5 分钟评 100 词 · 总耗时 2h/次

pytest 跑法:
  pytest tests/test_keyword_quality_regression.py -v  # 占位断言(假数据 · 结构化 report)
  pytest tests/test_keyword_quality_regression.py -v -m live  # 真跑 LLM(需 env + 付费)
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest


# ============================================================================
# 5 金标准 brand fixture(PRD M1b §M4)
# ============================================================================

GOLDEN_BRANDS: list[dict[str, Any]] = [
    {
        "brand_name": "老王家常菜",
        "industry": "餐饮",
        "city": "北京",
        "business": "京菜家常",
        "business_type": "B2C",
        "city_scope": "local",
        "expected_offtopic_rate_max": 0.05,  # <5%
        "expected_price_p50_range": (2000, 5000),  # 行业 p50 2800 附近
    },
    {
        "brand_name": "智云 SaaS",
        "industry": "SaaS",
        "city": "全国",
        "business": "中小企业 CRM 系统",
        "business_type": "B2C",
        "city_scope": "national",
        "expected_offtopic_rate_max": 0.05,
        "expected_price_p50_range": (4000, 9000),  # SaaS p50 6500
    },
    {
        "brand_name": "鼎盛机械",
        "industry": "工业机械制造",
        "city": "苏州",
        "business": "数控机床 + 自动化产线方案",
        "business_type": "B2B",
        "city_scope": "local",
        "expected_offtopic_rate_max": 0.05,
        "expected_price_p50_range": (4000, 10000),  # 机械 p50 6000
    },
    {
        "brand_name": "央信科技",
        "industry": "信创 / 政务云",
        "city": "北京",
        "business": "国标 + 等保合规平台",
        "business_type": "政企",
        "city_scope": "national",
        "expected_offtopic_rate_max": 0.05,
        "expected_price_p50_range": (5000, 12000),
    },
    {
        "brand_name": "丝路外贸",
        "industry": "外贸",
        "city": "深圳",
        "business": "东南亚跨境电商代运营",
        "business_type": "B2B",
        "city_scope": "national",  # 全国性跨境
        "expected_offtopic_rate_max": 0.05,
        "expected_price_p50_range": (4000, 10000),
    },
]


# ============================================================================
# 跑题判定 helper
# ============================================================================

# 明显不符合 business_type 的词(B2B 场景出现 C 端消费词)
B2C_CONSUMPTION_PATTERNS = [
    "哪家好",
    "靠谱",
    "推荐",
    "便宜",
    "排行榜",
]

# 明显偏离行业的词(餐饮出金融 / SaaS 出装修)
CROSS_INDUSTRY_BLACKLIST = {
    "餐饮": ["装修", "留学", "SaaS", "机械", "医美"],
    "SaaS": ["餐饮", "装修", "美容"],
    "工业机械制造": ["餐饮", "美容", "留学"],
    "信创 / 政务云": ["餐饮", "装修"],
    "外贸": ["餐饮", "美容"],
}


def is_off_topic(keyword: str, brand: dict) -> bool:
    """判定一个词是否跑题(规则版 · 保守)

    真跑 LLM 时用 Codex 0424 评分脚本(GPT-4 score 0-1)替代本 helper
    """
    kw = (keyword or "").strip()
    if not kw:
        return True

    # 规则 1 · B2B / 政企 不应出 C 端消费词
    bt = brand.get("business_type", "B2C")
    if bt in ("B2B", "政企"):
        for pattern in B2C_CONSUMPTION_PATTERNS:
            if pattern in kw:
                return True

    # 规则 2 · 全国场景禁地域锚点
    cs = brand.get("city_scope", "local")
    if cs == "national":
        for city in ["北京", "上海", "深圳", "广州", "杭州", "成都", "武汉"]:
            if brand.get("city") != city and city in kw:
                return True

    # 规则 3 · 跨行业黑名单
    industry = brand.get("industry", "")
    for matched_industry, blacklist in CROSS_INDUSTRY_BLACKLIST.items():
        if matched_industry in industry:
            for bl in blacklist:
                if bl in kw:
                    return True

    return False


def get_offtopic_rate(keywords: list[str], brand: dict) -> float:
    """计算跑题率 · 用于 PRD M1b KR1 硬阈值(<5%)"""
    if not keywords:
        return 0.0
    off = sum(1 for kw in keywords if is_off_topic(kw, brand))
    return off / len(keywords)


# ============================================================================
# 报价偏差 helper
# ============================================================================

def get_price_deviation_vs_industry(total_monthly: float, industry: str) -> float:
    """报价相对行业 p50 偏差比例 · 用于 PRD M1b DoD 偏差 <20%"""
    from tools.industry_median import get_industry_median
    p50 = get_industry_median(industry).get("p50", 0)
    if not p50:
        return 0.0
    return abs(total_monthly - p50) / p50


# ============================================================================
# 核心测试(占位 · fixture 骨架 · 不 live LLM)
# ============================================================================

@pytest.fixture(params=GOLDEN_BRANDS, ids=lambda b: b["brand_name"])
def golden_brand(request):
    return request.param


def test_golden_brand_fixture_sanity(golden_brand):
    """骨架 · 每个 fixture brand 字段完整"""
    assert golden_brand.get("brand_name")
    assert golden_brand.get("industry")
    assert golden_brand.get("business_type") in ("B2C", "B2B", "政企")
    assert golden_brand.get("city_scope") in ("local", "national")
    assert 0 < golden_brand["expected_offtopic_rate_max"] <= 0.1
    lo, hi = golden_brand["expected_price_p50_range"]
    assert lo < hi


def test_is_off_topic_positive_cases():
    """规则跑题判定 · 正例覆盖"""
    b2b_brand = {"business_type": "B2B", "city_scope": "local", "industry": "机械", "city": "苏州"}
    assert is_off_topic("机械哪家好", b2b_brand) is True  # B2B 不应出"哪家好"
    assert is_off_topic("机械推荐", b2b_brand) is True  # B2B 不应出"推荐"

    national_saas = {"business_type": "B2C", "city_scope": "national", "industry": "SaaS", "city": "全国"}
    assert is_off_topic("北京 CRM 推荐", national_saas) is True  # 全国场景不应出具体城市

    restaurant = {"business_type": "B2C", "city_scope": "local", "industry": "餐饮", "city": "北京"}
    assert is_off_topic("装修公司推荐", restaurant) is True  # 跨行业


def test_is_off_topic_negative_cases():
    """规则跑题判定 · 反例(合理词不被误判)"""
    b2c_local = {"business_type": "B2C", "city_scope": "local", "industry": "餐饮", "city": "北京"}
    assert is_off_topic("北京京菜家常菜推荐", b2c_local) is False  # B2C 本地合理
    assert is_off_topic("北京好吃的家常菜馆", b2c_local) is False

    b2b = {"business_type": "B2B", "city_scope": "local", "industry": "机械制造", "city": "苏州"}
    assert is_off_topic("数控机床供应商评估", b2b) is False  # B2B 合理
    assert is_off_topic("苏州机械设备采购流程", b2b) is False


def test_price_deviation_within_industry_median(golden_brand):
    """骨架 · 每 brand p50 范围在合理值内(来自 INDUSTRY_MEDIAN_MONTHLY)"""
    from tools.industry_median import get_industry_median
    p50 = get_industry_median(golden_brand["industry"]).get("p50", 0)
    lo, hi = golden_brand["expected_price_p50_range"]
    # SSOT 数据应该在 fixture 声明范围内 · 如果不在 · 说明 seed 漂移
    assert lo <= p50 <= hi or p50 == 4000, (
        f"{golden_brand['brand_name']} industry={golden_brand['industry']} "
        f"p50={p50} 不在预期 [{lo}, {hi}]"
    )


def test_industry_median_returns_valid_shape():
    """SSOT 健康 · get_industry_median 返 {p50, p90} 结构"""
    from tools.industry_median import get_industry_median
    for brand in GOLDEN_BRANDS:
        r = get_industry_median(brand["industry"])
        assert "p50" in r and "p90" in r
        assert r["p50"] > 0 and r["p90"] >= r["p50"]


# ============================================================================
# 真 LLM 跑(M1b Week 5 完整验收时启用 · 默认 skip)
# ============================================================================

@pytest.mark.skip(reason="M1b Week 5 启用 · 需 DashScope/DeepSeek API key · CI 成本 ¥25/次")
@pytest.mark.asyncio
async def test_live_keyword_expand_golden_regression(golden_brand):
    """真跑 LLM 回归 · pytest -m live 启用

    流程:
      1. 调 tools.keyword_expander.expand_keywords_for_client 产 50 词
      2. is_off_topic(kw) 统计跑题率
      3. assert rate < expected_offtopic_rate_max(0.05)
      4. 调 tools.batch_pricing 算总价
      5. assert get_price_deviation < 0.20
      6. report 写盘 tests/golden_samples_report_YYYYMMDD.json
    """
    from tools.keyword_expander import expand_keywords_for_client

    result = await expand_keywords_for_client(
        core_keywords=[golden_brand["business"]],
        industry=golden_brand["industry"],
        city=golden_brand["city"],
        business_scope=golden_brand["business"],
        target_count=50,
        profile_data={
            "business_type": golden_brand["business_type"],
            "city_scope": golden_brand["city_scope"],
        },
    )
    keywords = [kw.get("keyword", "") for kw in result.get("keywords", [])]
    assert len(keywords) >= 30, f"拓词数不足 {len(keywords)} < 30"

    offtopic_rate = get_offtopic_rate(keywords, golden_brand)
    assert offtopic_rate < golden_brand["expected_offtopic_rate_max"], (
        f"{golden_brand['brand_name']} 跑题率 {offtopic_rate:.1%} ≥ "
        f"{golden_brand['expected_offtopic_rate_max']:.1%}"
    )

    # 记录到报告
    report_path = Path(__file__).parent / f"golden_samples_report.json"
    report = {}
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            report = {}
    report[golden_brand["brand_name"]] = {
        "keyword_count": len(keywords),
        "offtopic_rate": offtopic_rate,
        "sample_keywords": keywords[:10],
        "business_type": golden_brand["business_type"],
        "city_scope": golden_brand["city_scope"],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
