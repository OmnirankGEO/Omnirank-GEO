"""只读取证 · 公司题漏进竞争层的真实产出口(WO_BRAND_QUESTION_LEAK_2026-08-05 §1 复核)。

工单 §1 判定根因是「LLM 生成时归错层」。本脚本用**仓库里的真函数**复现 551 的题面,
证明真凶是另一处:``_enforce_commercial_questions`` 的等槽兜底池第 0 条恒为
``f"{brand_name}是什么公司？"``(品牌题),被替换槽位的**原层标签**又被原样继承 →
品牌题带着 regional_industry / super_tier1 标签进入非品牌层。

零网络、零 DB、零 LLM:只调纯函数。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.keyword_generator import (  # noqa: E402
    _enforce_commercial_questions,
    _fallback_business_context,
)

BRAND = "深圳市晨光富士电梯"
INDUSTRY = "电梯制造与安装"
KEYWORDS = ["观光电梯", "别墅电梯", "旧楼加装电梯"]


def main() -> int:
    fallback = _fallback_business_context(
        BRAND, INDUSTRY, KEYWORDS, client_location="深圳", business_scope="区域",
    )
    pool = fallback.get("real_user_questions") or []
    print("── A. 等槽兜底池(_fallback_business_context 产物)──")
    for i, q in enumerate(pool):
        print(f"   [{i}] {q}   ← 层={fallback['question_types'].get(q)}")
    print()
    print(f"   🔴 池子第 0 条 = {pool[0]!r}")
    print(f"      它是品牌定向题吗? {'是' if BRAND in pool[0] else '否'}")
    print()

    # 构造 551 的 LLM 原始 8 题:第 8 题是一道会被 CommercialQueryPolicy 判废的知识题,
    # 层标签是 super_tier1(场景转化层)。其余 7 题照抄 551 落库题面。
    llm_questions = [
        "深圳市晨光富士电梯有限公司是做什么的？",
        "深圳观光电梯定制哪家靠谱？",
        "深圳别墅电梯安装哪家公司服务好？",
        "深圳旧楼加装电梯找谁比较放心？",
        "深圳电梯维保公司哪家响应速度快？",
        "深圳非标井道电梯定制哪家好？",
        "深圳观光电梯定制一般怎么收费？",
        "电梯的曳引比是什么原理？",  # ← 纯知识题,必被判废
    ]
    llm_types = {
        llm_questions[0]: "brand_awareness",
        llm_questions[1]: "regional_industry",
        llm_questions[2]: "regional_industry",
        llm_questions[3]: "regional_industry",
        llm_questions[4]: "regional_industry",
        llm_questions[5]: "regional_industry",
        llm_questions[6]: "super_tier1",
        llm_questions[7]: "super_tier1",
    }

    print("── B. 过 _enforce_commercial_questions(等槽替换器)──")
    out = _enforce_commercial_questions(
        {"real_user_questions": list(llm_questions), "question_types": dict(llm_types)},
        BRAND, INDUSTRY, KEYWORDS,
        client_location="深圳", business_scope="区域",
    )
    questions = out["real_user_questions"]
    types = out["question_types"]
    for q in questions:
        flag = "  ← 🔴 品牌题" if BRAND in q else ""
        print(f"   {types.get(q):<20} | {q}{flag}")
    print()

    brand_directed = [q for q in questions if BRAND in q]
    mislabeled = [q for q in brand_directed if types.get(q) != "brand_awareness"]
    print("── C. 判定 ──")
    print(f"   题面含品牌名的题数 = {len(brand_directed)}")
    print(f"   其中标签不是 brand_awareness 的 = {len(mislabeled)} → {mislabeled}")
    if len(brand_directed) > 1 and mislabeled:
        print()
        print("   ✅ 复现成功:等槽替换器把品牌题发给了非品牌槽位,并继承了原槽的层标签。")
        print("      → 工单 §1「LLM 生成时归错层」是**误判**;LLM 那道品牌题标签一直是对的。")
        return 0
    print()
    print("   ❌ 未复现 —— 结论作废,需要重新定位。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
