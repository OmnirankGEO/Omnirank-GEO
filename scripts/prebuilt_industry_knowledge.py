"""
预建行业公共库 — 批量采集 L1 行业层 + L2 品类层

用法:
    python scripts/prebuilt_industry_knowledge.py              # 全量采集
    python scripts/prebuilt_industry_knowledge.py --test       # 只采集1个行业测试
    python scripts/prebuilt_industry_knowledge.py --industry 酒吧  # 指定行业
"""

import sys
import json
import asyncio
import argparse
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

# 预建行业列表
INDUSTRIES = {
    "餐饮": ["火锅", "奶茶", "烧烤", "快餐", "西餐", "烘焙", "咖啡"],
    "美业": ["美容院", "美甲", "美发", "医美", "皮肤管理"],
    "教育": ["K12培训", "成人教育", "职业培训", "艺术培训", "留学"],
    "房产家居": ["房产中介", "全屋定制", "装修公司", "家具", "建材"],
    "汽车": ["汽车销售", "汽修保养", "二手车", "驾校"],
    "健康": ["口腔诊所", "中医养生", "健身房", "瑜伽"],
    "零售": ["服装", "母婴", "珠宝", "眼镜", "花店"],
    "本地服务": ["酒吧", "KTV", "民宿", "旅行社", "婚庆", "摄影"],
    "B2B": ["SaaS软件", "企业服务", "代运营", "广告公司", "法律财税"],
    "其他": ["宠物", "农产品", "电商", "知识付费"],
}


async def collect_one_industry(industry, categories):
    """采集一个行业的 L1 + L2"""
    from tools.industry_knowledge_collector import (
        collect_industry_knowledge,
        collect_category_knowledge,
        get_industry_knowledge,
    )

    results = {"industry": industry, "l1": None, "l2": []}

    # L1
    existing = get_industry_knowledge(industry, level="industry")
    if existing:
        print(f"  [L1] {industry} 已存在，跳过")
        results["l1"] = "exists"
    else:
        try:
            l1 = await collect_industry_knowledge(industry)
            results["l1"] = "ok"
            print(f"  [L1] {industry} OK, 品类={l1.get('common_categories', [])}")
        except Exception as e:
            results["l1"] = f"error: {e}"
            print(f"  [L1] {industry} FAIL: {e}")

    # L2
    for cat in categories:
        existing = get_industry_knowledge(industry, cat)
        if existing:
            print(f"  [L2] {industry}/{cat} 已存在，跳过")
            results["l2"].append({"category": cat, "status": "exists"})
        else:
            try:
                l2 = await collect_category_knowledge(industry, cat)
                results["l2"].append({"category": cat, "status": "ok"})
                print(f"  [L2] {industry}/{cat} OK")
            except Exception as e:
                results["l2"].append({"category": cat, "status": f"error: {e}"})
                print(f"  [L2] {industry}/{cat} FAIL: {e}")

    return results


async def main():
    parser = argparse.ArgumentParser(description="预建行业知识公共库")
    parser.add_argument("--test", action="store_true", help="只采集1个行业测试")
    parser.add_argument("--industry", type=str, help="指定行业")
    args = parser.parse_args()

    if args.industry:
        targets = {args.industry: INDUSTRIES.get(args.industry, [])}
    elif args.test:
        first_key = list(INDUSTRIES.keys())[0]
        targets = {first_key: INDUSTRIES[first_key][:2]}
    else:
        targets = INDUSTRIES

    total_l1 = len(targets)
    total_l2 = sum(len(cats) for cats in targets.values())
    print(f"预建行业知识: {total_l1} 个行业, {total_l2} 个品类")
    print("=" * 60)

    t0 = time.time()
    all_results = []

    for industry, categories in targets.items():
        print(f"\n--- {industry} ({len(categories)} 品类) ---")
        result = await collect_one_industry(industry, categories)
        all_results.append(result)

    elapsed = time.time() - t0
    ok_l1 = sum(1 for r in all_results if r["l1"] in ("ok", "exists"))
    ok_l2 = sum(1 for r in all_results for l in r["l2"] if l["status"] in ("ok", "exists"))

    print(f"\n{'=' * 60}")
    print(f"完成: L1 {ok_l1}/{total_l1}, L2 {ok_l2}/{total_l2}")
    print(f"耗时: {elapsed:.1f}s")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    asyncio.run(main())
