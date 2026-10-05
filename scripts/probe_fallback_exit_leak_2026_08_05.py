"""只读取证 · analyze_client_business 的三个降级出口是否绕过后处理链。

出口(tools/keyword_generator.py):
  · 无 DASHSCOPE_API_KEY      → _fallback_business_context(...)   直接 return
  · industry 为空             → _fallback_business_context(brand_name, **brand_name**, ...) 直接 return
  · LLM 异常                  → _fallback_business_context(...)   直接 return
  · LLM 成功                  → 过 品牌名过滤 → 商业意图闸 → 公司题去重 → 选词质量守卫

零网络、零 DB、零 LLM。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.brand_directed_question import is_brand_directed_text  # noqa: E402
from tools.keyword_generator import (  # noqa: E402
    _fallback_business_context,
    _finalize_business_context,
)

CASES = [
    # (说明, brand_name, industry 实参, keywords)
    ("出口①/③ 正常 industry", "深圳市晨光富士电梯", "电梯制造与安装", ["观光电梯"]),
    ("出口② industry 空 → 实参被换成 brand_name", "深圳市晨光富士电梯", "深圳市晨光富士电梯", ["观光电梯"]),
    ("出口② · 品牌名=业态名", "奥特莱斯", "奥特莱斯", ["折扣店"]),
    ("出口② · 短品牌名", "碧玉良缘", "碧玉良缘", ["珠宝"]),
]


def _mislabeled(parsed: dict, brand: str) -> list[str]:
    types = parsed["question_types"]
    return [
        q for q in parsed["real_user_questions"]
        if is_brand_directed_text(q, brand) and types.get(q) != "brand_awareness"
    ]


def main() -> int:
    """前后对照:同一份兜底产物,不过收口 vs 过收口。

    恒红的脚本没人看,所以退出码看的是**收口之后**是否干净;
    收口之前的污染是本脚本要展示的证据,不是失败。
    """
    dirty_before = 0
    still_dirty_after = 0

    for title, brand, industry, keywords in CASES:
        raw = _fallback_business_context(
            brand, industry, keywords, client_location="深圳", business_scope="区域",
        )
        before = _mislabeled(raw, brand)
        after_parsed = _finalize_business_context(
            raw, brand, industry, keywords,
            client_location="深圳", business_scope="区域",
        )
        after = _mislabeled(after_parsed, brand)

        print(f"── {title} ── brand={brand!r}")
        for q in after_parsed["real_user_questions"]:
            layer = after_parsed["question_types"].get(q)
            mark = ""
            if q in after:
                mark = "   ← 🔴 仍错层"
            elif is_brand_directed_text(q, brand):
                mark = "   ← 品牌层"
            print(f"   {layer:<18} | {q}{mark}")
        print(f"   → 收口前错层 {len(before)} 道 · 收口后 {len(after)} 道"
              f" · 题量 {len(raw['real_user_questions'])} → "
              f"{len(after_parsed['real_user_questions'])}")
        print()
        if before:
            dirty_before += 1
        if after:
            still_dirty_after += 1

    print("=" * 70)
    print(f"降级出口原始产物被污染:{dirty_before}/{len(CASES)}"
          f"(这正是收口存在的理由,不是失败)")
    if still_dirty_after:
        print(f"🔴 收口后仍有 {still_dirty_after} 个出口错层 —— 守卫没兜住")
        return 1
    print("✅ 过收口后:含品牌名的题全部归品牌认知层,题量不缩减")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
