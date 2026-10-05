"""
dry_run_pricing_v2_typical_cases.py — pricing v2 老板 5 case dry-run

【用途】
  上线前老板 5 case 验证 · LLM 评估师真实输出 vs 旧公式预期
  跑此脚本前需配 env:DEEPSEEK_API_KEY + DASHSCOPE_API_KEY

【老板 5 case】
  1. 深圳哪家装修公司靠谱     · 旧 std ¥2850 / v2.2 实测 ~¥12,400(× markup 3.0 · 含价值系数 1.6-1.7)
  2. 南山区办公室装修公司哪家好 · 旧 std ¥2850 / v2.2 实测 ~¥2,000(轻竞争按真实成本 · 老板知情口径)
  3. 深圳装修公司哪家性价比更高 · 旧异常 needs_review · 新预期合理化
  4. 罗平靠谱的装修公司推荐     · 县级地名 · 新预期低于深圳
  5. 深圳大宅全案设计公司       · 细分高端 · 新预期高客单

【运行】
  cd /app && python scripts/dry_run_pricing_v2_typical_cases.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# 加入项目根目录到 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# .env 加载(本机/prod 容器都从项目根读)
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except Exception:
    pass

# Windows GBK 控制台兜底(prod Linux 无影响)
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


CASES = [
    {
        "label": "Case 1 · 深圳哪家装修公司靠谱",
        "keyword": "深圳哪家装修公司靠谱",
        "brand_industry": "装修",
        "brand_name": "(代理工厂)",
        "default_city": "深圳",
        "five118": {"search_volume": 1200, "sem_price": 20.0, "bidword_company_count": 100},
        "metaso": {
            "content_count": 100, "competition_count": 95, "effective_competition": 5,
            "category_counts": {"A": 5, "B": 30, "C": 50, "D": 15},
            "source_authority": {"S": 30, "A": 25, "B": 35, "C": 10},
        },
        "markup": 3.0,
        "cost_per_article": 60.0,
        "expected_std_old": 2850,
        "expected_std_new_min": 4000,  # 老板预估 ≥ ¥4000(× markup 3.0)
    },
    {
        "label": "Case 2 · 南山区办公室装修公司哪家好",
        "keyword": "南山区办公室装修公司哪家好",
        "brand_industry": "装修",
        "brand_name": "(代理工厂)",
        "default_city": "深圳",
        "five118": {"search_volume": 600, "sem_price": 15.0, "bidword_company_count": 30},
        "metaso": {
            "content_count": 30, "competition_count": 1, "effective_competition": 1,
            "category_counts": {"A": 1, "B": 10, "C": 15, "D": 4},
            "source_authority": {"S": 5, "A": 8, "B": 12, "C": 5},
        },
        "markup": 3.0,
        "cost_per_article": 60.0,
        "expected_std_old": 2850,
        # [v2.1 知情口径] 轻竞争词按真实成本变便宜(老板已知情)· 无最低预期 · 只验差异化方向
        "expected_lower_than_case_1": True,
    },
    {
        "label": "Case 3 · 深圳装修公司哪家性价比更高",
        "keyword": "深圳装修公司哪家性价比更高",
        "brand_industry": "装修",
        "brand_name": "(代理工厂)",
        "default_city": "深圳",
        "five118": {"search_volume": 200, "sem_price": 8.0, "bidword_company_count": 20},
        "metaso": {
            "content_count": 50, "competition_count": 8, "effective_competition": 8,
            "category_counts": {"A": 8, "B": 20, "C": 18, "D": 4},
            "source_authority": {"S": 10, "A": 18, "B": 15, "C": 7},
        },
        "markup": 3.0,
        "cost_per_article": 60.0,
        "expected_std_old": 2850,
    },
    {
        "label": "Case 4 · 罗平靠谱的装修公司推荐",
        "keyword": "罗平靠谱的装修公司推荐",
        "brand_industry": "装修",
        "brand_name": "(代理工厂)",
        "default_city": "罗平",
        "five118": {"search_volume": 50, "sem_price": 3.0, "bidword_company_count": 3},
        "metaso": {
            "content_count": 8, "competition_count": 2, "effective_competition": 2,
            "category_counts": {"A": 2, "B": 3, "C": 2, "D": 1},
            "source_authority": {"S": 0, "A": 2, "B": 3, "C": 3},
        },
        "markup": 3.0,
        "cost_per_article": 60.0,
        "expected_keyword_type": "local_county",
    },
    {
        "label": "Case 5 · 深圳大宅全案设计公司",
        "keyword": "深圳大宅全案设计公司",
        "brand_industry": "装修",
        "brand_name": "(代理工厂)",
        "default_city": "深圳",
        "five118": {"search_volume": 300, "sem_price": 12.0, "bidword_company_count": 15},
        "metaso": {
            "content_count": 40, "competition_count": 5, "effective_competition": 5,
            "category_counts": {"A": 5, "B": 15, "C": 15, "D": 5},
            "source_authority": {"S": 5, "A": 12, "B": 18, "C": 5},
        },
        "markup": 3.0,
        "cost_per_article": 60.0,
        # [v2.1 知情口径] 轻竞争细分词按真实成本算 · 无最低预期
    },
    {
        "label": "Case 6 · 重庆璧山装修公司哪家好(历史爆价 ¥77,791 回归)",
        "keyword": "重庆璧山装修公司哪家好",
        "brand_industry": "装修",
        "brand_name": "(代理工厂)",
        "default_city": "重庆",
        "five118": {"search_volume": 80, "sem_price": 4.0, "bidword_company_count": 5},
        "metaso": {
            "content_count": 15, "competition_count": 3, "effective_competition": 3,
            "category_counts": {"A": 3, "B": 5, "C": 5, "D": 2},
            "source_authority": {"S": 0, "A": 1, "B": 6, "C": 8},
        },
        "markup": 3.0,
        "cost_per_article": 60.0,
        "expected_std_new_max": 4000,  # 历史灾难 ¥77,791 · 新架构必须 < ¥4,000(区县轻竞争)
    },
    {
        "label": "Case 7 · 龙岗装修公司哪家好(深圳 vs 龙岗倒挂对照)",
        "keyword": "龙岗装修公司哪家好",
        "brand_industry": "装修",
        "brand_name": "(代理工厂)",
        "default_city": "深圳",
        "five118": {"search_volume": 300, "sem_price": 8.0, "bidword_company_count": 20},
        "metaso": {
            "content_count": 45, "competition_count": 6, "effective_competition": 6,
            "category_counts": {"A": 6, "B": 18, "C": 16, "D": 5},
            "source_authority": {"S": 3, "A": 8, "B": 20, "C": 14},
        },
        "markup": 3.0,
        "cost_per_article": 60.0,
        "expected_not_above_case_1": True,  # 历史倒挂(龙岗 > 深圳)· 新架构区级 ≤ 市级红海
    },
]

# 同词重复跑波动门禁(真打 LLM · temperature 0.1 + C 量化 5 步进后应 < 10%)
_STABILITY_RUNS = 3
_STABILITY_MAX_DEVIATION_PCT = 10.0


def _fmt_money(v) -> str:
    try:
        return f"¥{int(v):,}"
    except Exception:
        return f"¥{v}"


async def run_one(case: dict) -> dict:
    from tools.pricing_llm_assessor import assess_keyword_pricing
    result = await assess_keyword_pricing(
        keyword=case["keyword"],
        brand_id=None,
        brand_industry=case["brand_industry"],
        brand_name=case["brand_name"],
        five118_data=case["five118"],
        metaso_data=case["metaso"],
        markup=case["markup"],
        cost_per_article=case["cost_per_article"],
        default_city=case["default_city"],
    )
    return result


async def main() -> int:
    if not os.getenv("DEEPSEEK_API_KEY"):
        print("⚠️ 未设 DEEPSEEK_API_KEY · 主 LLM 不可达 · 全部走兜底")
    if not os.getenv("DASHSCOPE_API_KEY"):
        print("⚠️ 未设 DASHSCOPE_API_KEY · 备 LLM 不可达 · 双验失败")

    print("=" * 78)
    print("pricing v2 LLM 评估师 · 老板 5 case dry-run")
    print("=" * 78)
    print()

    results = []
    for case in CASES:
        print(f"▶ {case['label']}")
        try:
            r = await run_one(case)
        except Exception as exc:
            print(f"  ❌ 异常 · {exc}")
            results.append({"case": case["label"], "error": str(exc)})
            continue

        results.append({"case": case["label"], "result": r})
        print(f"  keyword_type:      {r.get('keyword_type')}")
        print(f"  city / city_tier:  {r.get('city')} / {r.get('city_tier')}")
        print(f"  真实竞争量级:      {r.get('true_competition')}(实测 {r.get('measured_competition')} · "
              f"满召回={r.get('saturated_recall')})")
        print(f"  媒体档次 / 单篇成本: {r.get('media_tier_required')} / ¥{r.get('cost_per_article')}")
        print(f"  industry_baseline: P50={_fmt_money(r['industry_baseline']['p50'])} / "
              f"P90={_fmt_money(r['industry_baseline']['p90'])} (src={r['industry_baseline']['source']})")
        print(f"  入门 / 标准 / 旗舰(出厂):  "
              f"{_fmt_money(r.get('entry_price'))} / {_fmt_money(r.get('standard_price'))} / "
              f"{_fmt_money(r.get('flagship_price'))}")
        print(f"  入门 / 标准 / 旗舰(客户):  "
              f"{_fmt_money(r.get('selling_entry'))} / {_fmt_money(r.get('selling_standard'))} / "
              f"{_fmt_money(r.get('selling_flagship'))}")
        print(f"  篇数(入/标/旗):    "
              f"{r.get('entry_articles')} / {r.get('standard_articles')} / {r.get('flagship_articles')}")
        print(f"  LLM 双验:  primary={r.get('llm_primary_ok')} secondary={r.get('llm_secondary_ok')} "
              f"deviation={r.get('llm_deviation_pct')}% used={r.get('llm_used')}")
        print(f"  risk_flags:        {r.get('risk_flags')}")
        print(f"  needs_review:      {r.get('needs_review')}")
        print(f"  reasoning:         {r.get('reasoning', '')[:100]}")

        # 老板预期校验
        if "expected_std_new_min" in case:
            new_std = r.get("selling_standard", 0)
            ok = new_std >= case["expected_std_new_min"]
            mark = "✅" if ok else "⚠️"
            print(f"  {mark} 老板预期 ≥ {_fmt_money(case['expected_std_new_min'])} · 实际 {_fmt_money(new_std)}")
        if "expected_std_new_max" in case:
            new_std = r.get("selling_standard", 0)
            ok = 0 < new_std <= case["expected_std_new_max"]
            mark = "✅" if ok else "🔴 爆价回归!"
            print(f"  {mark} 历史爆价词回归门禁 ≤ {_fmt_money(case['expected_std_new_max'])} · 实际 {_fmt_money(new_std)}")
        if "expected_keyword_type" in case:
            ok = r.get("keyword_type") == case["expected_keyword_type"]
            mark = "✅" if ok else "⚠️"
            print(f"  {mark} 老板预期 keyword_type={case['expected_keyword_type']} · 实际 {r.get('keyword_type')}")
        print()

    # 差异化校验:Case 2 应低于 Case 1
    try:
        c1 = next(x["result"] for x in results if "Case 1" in x["case"])
        c2 = next(x["result"] for x in results if "Case 2" in x["case"])
        ratio = c1.get("selling_standard", 0) / max(c2.get("selling_standard", 1), 1)
        print(f"差异化校验:Case 1 / Case 2 = {ratio:.2f}x(老板预期 ~2.4x)· {'✅' if ratio > 1.5 else '⚠️ 差异化不足'}")
    except Exception:
        pass

    # 倒挂对照:龙岗(区级)不得高于深圳(市级红海)
    try:
        c1 = next(x["result"] for x in results if "Case 1" in x["case"])
        c7 = next(x["result"] for x in results if "Case 7" in x["case"])
        ok = c7.get("selling_standard", 0) <= c1.get("selling_standard", 0)
        print(f"倒挂对照:深圳 {_fmt_money(c1.get('selling_standard'))} vs 龙岗 {_fmt_money(c7.get('selling_standard'))} · "
              f"{'✅ 无倒挂' if ok else '🔴 倒挂回归!'}")
    except Exception:
        pass

    # 同词重复跑波动门禁(真打 LLM · C 量化 5 步进后应 < 10%)
    try:
        case1 = CASES[0]
        prices = []
        for i in range(_STABILITY_RUNS):
            rr = await run_one(case1)
            prices.append(rr.get("selling_standard", 0))
        lo, hi = min(prices), max(prices)
        dev_pct = (hi - lo) / max(lo, 1) * 100
        ok = dev_pct < _STABILITY_MAX_DEVIATION_PCT
        print(f"稳定性门禁:Case 1 同词 {_STABILITY_RUNS} 跑 = {prices} · 波动 {dev_pct:.1f}% "
              f"(门 {_STABILITY_MAX_DEVIATION_PCT}%) · {'✅' if ok else '🔴 抖动超门!'}")
    except Exception as exc:
        print(f"稳定性门禁:跑失败 {exc}")

    print("\n" + "=" * 78)
    print("dry-run 完成 · 输出 JSON 已存 dry_run_pricing_v2_output.json")
    out_path = PROJECT_ROOT / "scripts" / "dry_run_pricing_v2_output.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
