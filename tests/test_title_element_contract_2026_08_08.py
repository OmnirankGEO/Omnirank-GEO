"""P2 标题要素合同的自身判别锁(2026-08-08)。

`test_c5_title_year_rules_2026_07_28.py` 锁的是**四层接线**(prompt / 兜底 / evidence-first /
深档规格 都读同一份合同);本文件锁的是**合同自身**:

① 附件数字逐字保真(9 张卡 × 5 个要素,与 `article_structure_feature_rates.csv` 同值);
② 分级门槛与附件 `threshold_contract` 同源(70/40),不是另拍的一套;
③ R-A(主卡按 N 降序)与 R-B(三态推导)真的按规则算,不是手填结论;
④ 代理映射不得抬高默认(诚实边界的机械化);
⑤ 三级只作提示词默认,永不作硬拦(附件 `allocation_boundary`)。

🔴 为什么要锁"附件保真":这些数字是本包**唯一**的事实依据。谁把 59.06 改成 65,
榜单族的 on 就变成了一句没有出处的话 —— 而那正是工单明令禁止的"凭记忆写规格数字"。
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: 附件原始 CSV(定稿研究的产物,不在本仓 —— 它是外部证据不是代码)。
#: 存在就逐行对账;不存在就跳过对账那一条,但**其余锁照跑**。
_ATTACHMENT = Path("C:/AI-Test/geo-article-upgrade-20260807/article_structure_feature_rates.csv")


def test_contract_self_audit_is_clean():
    from writing.title_element_contract import validate_title_element_contract

    assert validate_title_element_contract() == []


# ===========================================================================
# ① 附件数字逐字保真
# ===========================================================================
@pytest.mark.skipif(not _ATTACHMENT.exists(), reason="定稿附件 CSV 不在本机")
def test_corpus_cards_match_the_attachment_verbatim():
    """9 文体 × 5 个 title_has_* 的 present_pct 与 n,必须与附件逐字相同。

    变异(改任何一个数字)→ 本锁转红。
    """
    from writing.title_element_contract import CORPUS_TITLE_CARDS, TITLE_ELEMENTS

    from_csv: dict[str, dict[str, float]] = {}
    n_from_csv: dict[str, int] = {}
    with _ATTACHMENT.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            feature = row["feature"]
            if not feature.startswith("title_has_"):
                continue
            article_type = row["article_type"]
            element = feature[len("title_has_"):]
            from_csv.setdefault(article_type, {})[element] = float(row["present_pct"])
            n_from_csv[article_type] = int(row["n"])

    assert set(from_csv) == set(CORPUS_TITLE_CARDS), "文体集合与附件不一致"
    for article_type, card in CORPUS_TITLE_CARDS.items():
        assert card.n == n_from_csv[article_type], f"{article_type} 的 N 与附件不一致"
        for element in TITLE_ELEMENTS:
            assert card.rates[element] == pytest.approx(from_csv[article_type][element]), (
                f"{article_type}.{element} 与附件不一致")


def test_key_numbers_quoted_by_the_work_order():
    """工单正文点名的四个数字,原样在场(防"改了数字忘了改工单"式漂移)。"""
    from writing.title_element_contract import CORPUS_TITLE_CARDS

    ranking = CORPUS_TITLE_CARDS["ranking"].rates
    assert ranking["year"] == 59.06            # P2-1
    assert ranking["industry_marker"] == 71.11  # P2-2「必备级默认」
    assert CORPUS_TITLE_CARDS["data_report"].rates["year"] == 40.92  # 补章 R1「数据报告类」
    assert CORPUS_TITLE_CARDS["long_form"].rates["year"] == 19.04    # 与飞轮实证同侧


# ===========================================================================
# ② 门槛与附件同源
# ===========================================================================
def test_thresholds_match_the_attachment_contract():
    from writing.title_element_contract import (
        LEVEL_OPTIONAL,
        LEVEL_RECOMMENDED,
        LEVEL_REQUIRED,
        SPEC_LEVEL_RECOMMENDED_MIN,
        SPEC_LEVEL_REQUIRED_MIN,
        level_for_rate,
    )

    assert SPEC_LEVEL_REQUIRED_MIN == 70.0
    assert SPEC_LEVEL_RECOMMENDED_MIN == 40.0
    assert level_for_rate(70.0) == LEVEL_REQUIRED
    assert level_for_rate(69.99) == LEVEL_RECOMMENDED
    assert level_for_rate(40.0) == LEVEL_RECOMMENDED
    assert level_for_rate(39.99) == LEVEL_OPTIONAL
    assert level_for_rate(0.0) == LEVEL_OPTIONAL


@pytest.mark.skipif(not _ATTACHMENT.exists(), reason="定稿附件 CSV 不在本机")
def test_levels_agree_with_the_attachment_spec_level_column():
    """我们算出来的分级,必须与附件 `spec_level` 列给的分级一致。

    这是**跨口径互校**:附件那一列是研究方算的,我们这边是代码算的,
    两边独立算出同一结果才说明门槛没抄错。
    """
    from writing.title_element_contract import (
        LEVEL_OPTIONAL,
        LEVEL_RECOMMENDED,
        LEVEL_REQUIRED,
        level_for_rate,
    )

    zh_to_level = {"默认必备": LEVEL_REQUIRED, "推荐": LEVEL_RECOMMENDED, "可选": LEVEL_OPTIONAL}
    checked = 0
    with _ATTACHMENT.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if not row["feature"].startswith("title_has_"):
                continue
            expected = next(
                level for zh, level in zh_to_level.items() if row["spec_level"].startswith(zh)
            )
            assert level_for_rate(float(row["present_pct"])) == expected, row
            checked += 1
    assert checked == 45, f"应当校 9 文体 × 5 要素 = 45 行,实际 {checked}"


# ===========================================================================
# ③ R-A / R-B 真的按规则算
# ===========================================================================
def test_rule_a_primary_card_is_the_largest_n():
    from writing.title_element_contract import (
        CORPUS_TITLE_CARDS,
        FAMILY_TITLE_PROFILES,
        primary_card,
    )

    for family_code, profile in FAMILY_TITLE_PROFILES.items():
        cards = [CORPUS_TITLE_CARDS[t] for t in profile.corpus_types]
        assert primary_card(family_code) is max(cards, key=lambda c: c.n), family_code


def test_rule_b_is_derived_not_hand_filled():
    """把门槛/占比参数换掉,三态必须**跟着变**。恒定不变 = 结论是手填的。"""
    import writing.title_element_contract as contract

    original = contract.SPEC_LEVEL_RECOMMENDED_MIN
    try:
        contract.SPEC_LEVEL_RECOMMENDED_MIN = 70.0
        # 门槛提到 70 之后,榜单 59.06 掉出 on,数据报告 40.92 也掉出 → 全族 off
        assert contract.year_default_for_family("multi_brand_comparison") == \
            contract.YEAR_DEFAULT_OFF
        assert contract.year_default_for_family("case_data_roi") == contract.YEAR_DEFAULT_OFF
    finally:
        contract.SPEC_LEVEL_RECOMMENDED_MIN = original
    assert contract.year_default_for_family("multi_brand_comparison") == contract.YEAR_DEFAULT_ON

    original_share = contract.MINORITY_CARD_SHARE_MIN
    try:
        # 少数侧门槛抬到 0.9:data_report(303) 不到 case_study(558) 的 90%,
        # 案例族的"族内分歧"不再成立 → conditional 退回 off
        contract.MINORITY_CARD_SHARE_MIN = 0.9
        assert contract.year_default_for_family("case_data_roi") == contract.YEAR_DEFAULT_OFF
    finally:
        contract.MINORITY_CARD_SHARE_MIN = original_share
    assert contract.year_default_for_family("case_data_roi") == contract.YEAR_DEFAULT_CONDITIONAL


def test_conditional_takes_the_conservative_side_without_llm():
    """确定性路径上 conditional == off。变异(让它取 on)→ 本锁转红。"""
    from writing.title_element_contract import (
        YEAR_DEFAULT_CONDITIONAL,
        conditional_year_families,
        deterministic_year_default,
        year_default_for_family,
    )

    assert conditional_year_families() == ("case_data_roi",)
    for family in conditional_year_families():
        assert year_default_for_family(family) == YEAR_DEFAULT_CONDITIONAL
        assert deterministic_year_default(family) is False
    assert deterministic_year_default("multi_brand_comparison") is True


# ===========================================================================
# ④ 代理映射不得抬高默认
# ===========================================================================
def test_proxy_mapping_cannot_raise_the_default():
    """诚实边界的机械化:`other` 残余桶只作代理,不许靠它把某族抬成 on。"""
    import writing.title_element_contract as contract

    original = contract.CORPUS_TITLE_CARDS["other"]
    bumped = contract.CorpusTitleCard(
        original.corpus_type, original.label, original.n,
        {**original.rates, "year": 45.0},
    )
    contract.CORPUS_TITLE_CARDS["other"] = bumped
    try:
        errors = contract.validate_title_element_contract()
        assert any("proxy_mapping_must_not_raise_year_default" in e for e in errors), errors
    finally:
        contract.CORPUS_TITLE_CARDS["other"] = original
    assert contract.validate_title_element_contract() == []


# ===========================================================================
# ⑤ 只作默认,永不作硬拦
# ===========================================================================
def test_contract_exposes_no_blocking_api():
    """合同只回默认值与提示词,不提供任何"拒绝保存/拒绝发布"的判定。

    这条锁住的是**边界**:附件 `allocation_boundary` 列写着"仅作写作默认,
    永不作配额或验收线"。谁给本模块加一个 `reject_*`/`block_*`/`is_valid_title`
    之类的出口,本锁转红。
    """
    import writing.title_element_contract as contract

    forbidden_prefixes = ("reject", "block", "enforce", "must_", "assert_", "validate_title_form")
    exported = [name for name in dir(contract) if not name.startswith("_")]
    offenders = [
        name for name in exported
        if any(name.lower().startswith(p) for p in forbidden_prefixes)
    ]
    assert offenders == [], f"合同不得提供硬拦出口: {offenders}"
    src = (ROOT / "writing" / "title_element_contract.py").read_text(encoding="utf-8")
    assert "永不作保存或发布硬拦" in src


def test_prompt_blocks_never_claim_a_hard_gate():
    from writing.title_element_contract import (
        build_title_element_defaults_prompt,
        build_title_year_rule_prompt,
    )

    text = build_title_year_rule_prompt(year=2026) + build_title_element_defaults_prompt()
    for banned in ("必须删除", "否则不予保存", "不合格", "拒绝发布"):
        assert banned not in text
