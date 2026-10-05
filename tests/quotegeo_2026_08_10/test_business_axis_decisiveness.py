"""业务轴的否决力边界 —— 成对判据。

开发中 `tests/test_keyword_expansion_quantity_gate.py` 实跑抓到:
业务轴一旦无条件否决,资料薄的客户(只有一个 industry 标签、没填 business_scope)
会被判成"每个词都业务待确认" → **一个词都交付不出来**,报价页整页空。
那不是业务决策,是拿"没有依据"当负面结论,同时违反元指令 13(永远不中断)。

所以规则是:业务轴**只有在代理真的声明过业务范围时**才具备否决力。

这条规则最容易被人误读成"为了凑词降低标准"(工单 §8 禁止的假修复),
因此这里配成对判据 —— 两边都必须成立才算实现正确:

  ① 没填业务范围 → 商业 + 地域都对的词**必须**能进交付(不因判不了而扣住);
  ② 填了业务范围 → 同样那批词里对不上的**必须**被扣住(判据一分不松)。

只锁 ① 会被"业务轴恒不否决"的实现骗过;只锁 ② 会被"业务轴恒否决"骗过。
"""
from __future__ import annotations

from services.keyword_delivery_decision import (
    build_business_profile,
    classify_business_scope,
)
from tests.quotegeo_2026_08_10.test_quote_keyword_double_inversion_2026_08_10 import (
    FIXTURE_SCOPE_NO_FURNITURE,
    run_expansion,
)

# 这批词与"商场运营/品牌招商/商铺租赁/餐饮入驻"这套业务范围**对不上**
OFF_SCOPE_BUT_COMMERCIAL = [
    "深圳龙岗办公家具批发市场在哪",
    "深圳龙岗建材市场有哪些",
]


def test_without_declared_scope_the_axis_must_not_block():
    """① 没填业务范围 → 不能因为"判不了"就把词扣住。"""
    matrix = run_expansion(OFF_SCOPE_BUT_COMMERCIAL, business_scope="")
    for keyword in OFF_SCOPE_BUT_COMMERCIAL:
        row = matrix[keyword]
        assert row["business_scope"] == "uncertain", row
        assert row["default_selected"] is True, (
            f"「{keyword}」在客户没填业务范围时被扣住了 —— "
            f"拿'没有依据'当负面结论,资料薄的客户会一个词都交付不出来。{row}"
        )


def test_with_declared_scope_the_axis_must_block():
    """② 填了业务范围 → 对不上的必须扣住(判据不松)。"""
    matrix = run_expansion(
        OFF_SCOPE_BUT_COMMERCIAL, business_scope=FIXTURE_SCOPE_NO_FURNITURE,
    )
    for keyword in OFF_SCOPE_BUT_COMMERCIAL:
        row = matrix[keyword]
        assert row["business_scope"] in ("uncertain", "mismatched"), row
        assert row["default_selected"] is False, (
            f"「{keyword}」在客户已声明业务范围、且明显对不上时仍被默认勾选。{row}"
        )
        assert row["reason_group"] == "needs_confirm", row


def test_industry_label_alone_does_not_grant_veto_power():
    """`industry` 是粗分类,不足以宣判某个词"不属于客户业务"。"""
    only_industry = build_business_profile(industry="商业地产运营")
    assert only_industry.tokens, "industry 仍应进 token 池(提高召回)"
    assert only_industry.is_decisive is False

    declared = build_business_profile(
        industry="商业地产运营", business_scope="商场运营、品牌招商",
    )
    assert declared.is_decisive is True


def test_profile_data_business_fields_grant_veto_power_but_soft_fields_do_not():
    """成对:core_business 赋予否决力;target_customers / selling_points 不赋予。"""
    hard = build_business_profile(profile_data={"core_business": "商场运营、品牌招商"})
    assert hard.is_decisive is True

    soft = build_business_profile(profile_data={
        "target_customers": "本地商户", "selling_points": ["位置好", "客流大"],
    })
    assert soft.tokens, "软字段仍应进 token 池"
    assert soft.is_decisive is False


def test_undeclared_profile_never_returns_mismatched():
    """没声明业务范围时,连 `mismatched` 都不许出现 —— 只能是 `uncertain`。

    否则 `_SERVICE_ACTIONS` 那条规则会把几乎所有服务类问法判成"不匹配"。
    """
    profile = build_business_profile(industry="商业地产运营")
    verdict, _ = classify_business_scope("商业综合体设计公司", profile)
    assert verdict == "uncertain", verdict

    declared = build_business_profile(
        industry="商业地产运营", business_scope="商场运营、品牌招商",
    )
    verdict2, _ = classify_business_scope("商业综合体设计公司", declared)
    assert verdict2 == "mismatched", verdict2
