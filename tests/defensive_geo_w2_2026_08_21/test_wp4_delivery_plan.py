"""WP4 判据 · 交付计划 schema 与 activation 前置校验(DEL-01/02/11/12)。

每条「必须命中」都配一条「必须不命中」—— 只有正样本的判据证明不了锁在工作,
它可能是被别的规则顺手判红的(本仓 2026-08-21 记过这个形态)。
"""

from __future__ import annotations

import pytest

from services.defensive_geo.delivery_plan import (
    CONTRACT_MINIMUM_KEYS,
    OBJECTIVE_MODES,
    SCHEMA_VERSION,
    V1_ITEM_CAPACITY,
    capacity_of,
    contract_minimums_of,
    validate_plan,
)


def item(
    key: str,
    question: str,
    ordinal: int,
    *,
    minimum_articles: int = 1,
    minimum_publications: int = 1,
    capacity: int = 1,
    commitment: str = "contract",
    mode: str = "defensive",
    revision: int = 1,
) -> dict:
    return {
        "plan_item_key": key,
        "objective_mode": mode,
        "question_identity_key": question,
        "question_revision": revision,
        "global_ordinal": ordinal,
        "commitment_kind": commitment,
        "brand_exposure": "named",
        "authorized_article_capacity": capacity,
        "minimum_articles": minimum_articles,
        "minimum_publications": minimum_publications,
    }


def plan(items, **minimums) -> dict:
    base = {
        "questions": len(
            {
                (i["question_identity_key"], i["question_revision"])
                for i in items
                if i.get("commitment_kind") == "contract"
            }
        ),
        "articles": sum(
            i["minimum_articles"] for i in items if i.get("commitment_kind") == "contract"
        ),
        "publications": sum(
            i["minimum_publications"]
            for i in items
            if i.get("commitment_kind") == "contract"
        ),
        "distinct_public_media": 1,
        "distinct_root_domains": 0,
    }
    base.update(minimums)
    return {
        "schema_version": SCHEMA_VERSION,
        "campaign_mode": "defensive",
        "contract_minimums": base,
        "items": items,
    }


# --------------------------------------------------------------- 正样本(必须绿)

def test_wellformed_plan_passes():
    """两格两问题的最小合法计划必须通过。

    这条是全组的**活性自证**:它红了说明夹具本身坏了,
    下面所有「必须转红」的判据就都失去意义(红得没有区分力)。
    """
    verdict = validate_plan(plan([item("a", "q1", 1), item("b", "q2", 2)]))
    assert verdict.ok, verdict.violations
    assert verdict.violations == ()


def test_bonus_item_does_not_count_toward_header_minimum():
    """§4.4 L497:bonus item 不得计入 header minimum。

    header 仍按 1 个 contract item 计;bonus 那格额外存在但不改最低量。
    """
    items = [item("a", "q1", 1), item("b", "q2", 2, commitment="bonus")]
    verdict = validate_plan(plan(items))
    assert verdict.ok, verdict.violations
    # 容量算全部格(2),最低量只算 contract(1)—— DEL-01 的「分开」就是这个意思。
    assert capacity_of(plan(items)) == 2
    assert contract_minimums_of(plan(items))["articles"] == 1


# --------------------------------------------- DEL-11:publication<=article<=capacity

def test_del11_zero_article_one_publication_rejected():
    """§4.4 L494 逐字禁止「0 稿 1 发布」。"""
    bad = plan([item("a", "q1", 1, minimum_articles=0, minimum_publications=1),
                item("b", "q2", 2)])
    verdict = validate_plan(bad)
    assert not verdict.ok
    assert "item_publication_exceeds_article" in verdict.blocking_codes()


def test_del11_article_exceeds_capacity_rejected():
    bad = plan([item("a", "q1", 1, minimum_articles=2, capacity=1)])
    verdict = validate_plan(bad)
    assert not verdict.ok
    assert "item_article_exceeds_capacity" in verdict.blocking_codes()


def test_del11_header_exceeds_total_capacity_rejected():
    """header 层不等式**独立于** item 层:两格各自合法,整单仍可能超容量。

    这条与上面那条不是重复 —— 去掉 header 检查后本条转红、上条仍绿。
    """
    items = [item("a", "q1", 1), item("b", "q2", 2)]
    bad = plan(items, articles=3)          # 两格容量共 2,却承诺 3 篇
    verdict = validate_plan(bad)
    assert not verdict.ok
    codes = verdict.blocking_codes()
    assert "header_article_exceeds_capacity" in codes


def test_del11_violation_carries_shortfall_and_repair():
    """DEL-02 逐字要求「不足显示差额和修复」—— 没有出口的违规不许存在。"""
    bad = plan([item("a", "q1", 1)], articles=5)
    verdict = validate_plan(bad)
    assert not verdict.ok
    gap = next(
        v for v in verdict.violations if v.code == "header_article_exceeds_capacity"
    )
    assert gap.shortfall == 4
    assert gap.repair                       # 恒非空
    for violation in verdict.violations:
        assert violation.repair, f"{violation.code} 没有修复出口"


# ------------------------------------------------- DEL-12:合同格问题身份必须互不相同

def test_del12_duplicate_question_cannot_pad_minimum():
    """§4.4 L494:复制同一问题 12 次不能冒充 12 个问题。"""
    bad = plan([item("a", "q1", 1), item("b", "q1", 2)], questions=2)
    verdict = validate_plan(bad)
    assert not verdict.ok
    assert "duplicate_contract_question_identity" in verdict.blocking_codes()


def test_del12_same_key_different_revision_is_still_one_question():
    """身份相同、版本不同 = **两个**冻结身份(key+revision 是一对)。

    这条是上一条的反向对照:证明判据认的是 (key, revision) 元组,
    而不是「只要 key 重复就拒」—— 后者会把合法的改题版本误杀。
    """
    good = plan([item("a", "q1", 1), item("b", "q1", 2, revision=2)])
    verdict = validate_plan(good)
    assert verdict.ok, verdict.violations


# -------------------------------------------------------- DEL-01:容量与最低量不共用出口

def test_del01_capacity_and_minimum_are_separate_readouts():
    """§19 第 11 发变异 =「用 required_articles 同时表示最低和上限」。

    这里的判据形态:两个 getter 必须能给出**不同**的数。
    如果有人把它们合并成一个出口,这条立刻转红。
    """
    items = [item("a", "q1", 1, minimum_articles=0, minimum_publications=0),
             item("b", "q2", 2, minimum_articles=1, minimum_publications=1)]
    p = plan(items)
    assert capacity_of(p) == 2              # 两格各 1 篇容量
    assert contract_minimums_of(p)["articles"] == 1   # 只承诺 1 篇
    assert capacity_of(p) != contract_minimums_of(p)["articles"]


def test_del01_legacy_required_articles_key_is_rejected_loudly():
    """`required_articles` 只属于 confirmed_keywords 且只表示容量(§4.3)。

    交付计划里出现它 = 有人正在把两个语义合并 —— 必须当场炸,
    不能静默忽略(静默忽略正是让 §19 第 11 发变异存活的方式)。
    """
    items = [item("a", "q1", 1), item("b", "q2", 2)]
    items[0]["required_articles"] = 1
    verdict = validate_plan(plan(items))
    assert not verdict.ok
    assert "forbidden_legacy_key" in verdict.blocking_codes()


def test_question_key_alias_is_rejected():
    """§0.5.1-7:与现役 question_key 同名异义,落地必须改名。"""
    items = [item("a", "q1", 1), item("b", "q2", 2)]
    items[0]["question_key"] = "looks-innocent"
    verdict = validate_plan(plan(items))
    assert not verdict.ok
    assert "forbidden_legacy_key" in verdict.blocking_codes()


# ------------------------------------------------------------------ 形态与分母

def test_media_indicators_are_not_interchangeable():
    """§4.4 L497:不同公开媒体与不同 root domain 是两个指标,不得互换。"""
    items = [item("a", "q1", 1), item("b", "q2", 2)]
    assert not validate_plan(
        plan(items, distinct_public_media=9)
    ).ok
    assert not validate_plan(
        plan(items, distinct_public_media=1, distinct_root_domains=5)
    ).ok
    # 反向对照:合法组合必须绿,证明上面两条不是恒红。
    assert validate_plan(plan(items, distinct_public_media=2, distinct_root_domains=1)).ok


def test_v1_capacity_is_fixed_at_one():
    """§4.4 L493:v1 固定 1;写 2 是把一格撑成隐形多槽。"""
    assert V1_ITEM_CAPACITY == 1
    bad = plan([item("a", "q1", 1, capacity=2)])
    assert "item_capacity_not_v1" in validate_plan(bad).blocking_codes()


def test_item_objective_mode_cannot_be_hybrid():
    """§4.2 L454:hybrid 只描述报价头,item 只能是 defensive|offensive。"""
    assert "hybrid" not in OBJECTIVE_MODES
    bad = plan([item("a", "q1", 1, mode="hybrid")])
    assert "item_bad_objective_mode" in validate_plan(bad).blocking_codes()


def test_bool_cannot_impersonate_a_count():
    """``True`` 是 int 子类。不挡掉的话 ``minimum_articles=True`` 会当 1 用。"""
    bad = plan([item("a", "q1", 1)])
    bad["items"][0]["minimum_articles"] = True
    assert "item_bad_minimum_articles" in validate_plan(bad).blocking_codes()


def test_contract_minimum_keys_are_the_denominator():
    """分母从模块常量取,不手抄 —— 少一个键判据也要跟着变。"""
    assert set(CONTRACT_MINIMUM_KEYS) == {
        "questions", "articles", "publications",
        "distinct_public_media", "distinct_root_domains",
    }
    reported = contract_minimums_of(plan([item("a", "q1", 1)]))
    assert set(reported) == set(CONTRACT_MINIMUM_KEYS)


@pytest.mark.parametrize("bad_version", ["geo-delivery-plan-v2", "", None, "legacy"])
def test_foreign_schema_version_is_not_validated_as_v1(bad_version):
    """非本版计划不走本版规则 —— ACT-01「legacy 行为零漂移」的前半段。"""
    p = plan([item("a", "q1", 1)])
    p["schema_version"] = bad_version
    verdict = validate_plan(p)
    assert not verdict.ok
    assert verdict.blocking_codes() == {"plan_bad_schema_version"}
