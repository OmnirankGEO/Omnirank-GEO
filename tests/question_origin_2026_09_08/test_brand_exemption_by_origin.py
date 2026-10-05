"""#149 §2.3 承重墙 · 品牌定向豁免只对**客户自己写的**题生效。

## 缺陷

AI 出的候选直接进题单之后,verbatim 模式那条豁免

    aggregate_dimension_stats(..., brand_name=("" if mode_verbatim else brand_name))

就不成立了。Owner 截图第一道候选是「全域上榜(深圳)科技有限公司是做什么的?」——
这种品牌定向题**必然命中自己**;沿用整体豁免会让它进竞争格局分母,
把提及率顶高(WO_BRAND_QUESTION_LEAK 2026-08-05 §2.2 / 报告 551 那类病)。

## 判据按状态空间,不按清单

题单来源 ∈ {全 AI、AI+自己、只自己(无 meta)、空}
  × 元数据 ∈ {有 layer、无 layer} × 题面 ∈ {含品牌名、不含}
"""

from __future__ import annotations

import ast
import io
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

BRAND = "全域上榜"
Q_BRAND = "全域上榜(深圳)科技有限公司是做什么的?"
Q_PLAIN = "深圳做 AI 搜索优化的公司哪家靠谱?"


def _ai(origins, verbatim=True):
    return {"is_custom_mode": verbatim, "question_origins": dict(origins)}


def _detail(*questions):
    """两道题各一个引擎、都命中 —— 分层错了才会体现在**哪个桶**上。"""
    return [{"question": q, "results": {"e1": {"detected": True, "mentioned": True}}}
            for q in questions]


def _stats(ai_data, questions, types=None):
    from services.diagnosis_identity_review import aggregate_dimension_stats
    from services.diagnosis_question_origin import brand_filter_exempt_questions
    return aggregate_dimension_stats(
        _detail(*questions),
        types or {q: "super_tier1" for q in questions},
        brand_name=BRAND,
        exempt_questions=brand_filter_exempt_questions(ai_data, questions),
    )


# ─────────────────────────────────────────── 状态空间:来源 × 题面

def test_all_ai_brand_question_leaves_the_competitive_denominator():
    """🔴 本单那一格:全 AI 题单,含品牌名那道**归品牌认知层**,不进竞争分母。

    毒:把豁免谓词改回 `is_custom_mode`(整体豁免)⇒ 本条红。
    """
    ai = _ai({Q_BRAND: "ai_suggested", Q_PLAIN: "ai_suggested"})
    buckets = _stats(ai, [Q_BRAND, Q_PLAIN])
    assert buckets["brand_awareness"]["total"] == 1, (
        "AI 出的品牌定向题没被归到品牌认知层:%r" % buckets)
    assert buckets["super_tier1"]["total"] == 1, (
        "非品牌题不该被挪走:%r" % buckets)


def test_customer_brand_question_keeps_the_exemption():
    """正样本臂:**客户自己写的**品牌题沿用豁免(与今天同)。

    只证「AI 题被纠正」不够 —— 一个把所有题都纠正的实现同样能让上一条变绿,
    而那会把客户自己想问的题也改掉归层。
    """
    ai = _ai({Q_BRAND: "customer", Q_PLAIN: "customer"})
    buckets = _stats(ai, [Q_BRAND, Q_PLAIN])
    assert buckets["brand_awareness"]["total"] == 0, (
        "客户自己写的品牌题被夺走了豁免:%r" % buckets)
    assert buckets["super_tier1"]["total"] == 2


def test_mixed_plan_treats_each_question_by_its_own_origin():
    """AI + 自己:**逐题**分治,不是整批一个口径。"""
    q_cust_brand = "全域上榜的服务怎么收费?"
    ai = _ai({Q_BRAND: "ai_suggested", q_cust_brand: "customer"})
    buckets = _stats(ai, [Q_BRAND, q_cust_brand])
    assert buckets["brand_awareness"]["total"] == 1, (
        "AI 那道该被纠正、客户那道该保留豁免:%r" % buckets)
    assert buckets["super_tier1"]["total"] == 1


def test_legacy_plan_without_meta_behaves_exactly_like_today():
    """🔴 老前端兼容臂:不送 `question_meta` ⇒ 全部按 customer ⇒ 整体豁免。

    这一格是「行为保持」的承重处:老前端零改动上线,一道题的归层都不该变。
    """
    ai = _ai({})  # 没有任何 origin 信息
    buckets = _stats(ai, [Q_BRAND, Q_PLAIN])
    assert buckets["brand_awareness"]["total"] == 0, (
        "老前端提交的题单归层变了 —— 这是不兼容:%r" % buckets)
    assert buckets["super_tier1"]["total"] == 2


def test_non_verbatim_mode_exempts_nothing():
    """非 verbatim(系统出题)⇒ 一道都不豁免,与改前一致。"""
    from services.diagnosis_question_origin import brand_filter_exempt_questions
    ai = {"is_custom_mode": False, "diagnosis_mode": "default",
          "question_origins": {Q_BRAND: "customer"}}
    assert brand_filter_exempt_questions(ai, [Q_BRAND]) == ()
    buckets = _stats(ai, [Q_BRAND, Q_PLAIN])
    assert buckets["brand_awareness"]["total"] == 1


def test_empty_plan_is_not_special_cased():
    """空题单 ⇒ 空豁免集,不抛。(0 题可启动,不加 0 题闸 —— #143 ④ 教训。)"""
    from services.diagnosis_question_origin import brand_filter_exempt_questions
    assert brand_filter_exempt_questions(_ai({}), []) == ()
    assert brand_filter_exempt_questions(None, []) == ()


# ─────────────────────────────────────────── 归层:有 layer 的题不再花 LLM

def test_questions_carrying_a_layer_do_not_need_the_classifier():
    """AI 出的题带层 ⇒ 不进 `need`;客户手写的没层 ⇒ 进。"""
    from services.diagnosis_question_origin import split_questions_needing_layers
    meta = [{"text": Q_BRAND, "origin": "ai_suggested", "layer": "brand_awareness"},
            {"text": Q_PLAIN, "origin": "customer"}]
    known, need = split_questions_needing_layers(meta, [Q_BRAND, Q_PLAIN])
    assert known == {Q_BRAND: "brand_awareness"}
    assert need == [Q_PLAIN], "只该把没层的那道交给 LLM:%r" % need


def test_a_plan_with_no_layers_still_uses_the_classifier():
    """🔴 正样本臂:全 customer 无 layer ⇒ `need` = 全部题。

    分类器调用点必须**保留** —— 把它整个砍掉会让「客户手写题不归层」
    那个老缺陷(report 325 总分被低估的根因)悄悄回来。
    """
    from services.diagnosis_question_origin import split_questions_needing_layers
    known, need = split_questions_needing_layers(
        [{"text": Q_PLAIN, "origin": "customer"}], [Q_BRAND, Q_PLAIN])
    assert known == {}
    assert need == [Q_BRAND, Q_PLAIN]


def test_the_workflow_still_has_a_classifier_call_site():
    """结构臂:工作流里那个 LLM 归层调用点**还在**。

    上一条打的是拆分函数,证不了工作流真的还会调它 ——
    「机制存在」不是「机制生效」。
    """
    src = io.open(ROOT / "workflows" / "diagnosis_workflow.py", encoding="utf-8").read()
    calls = [n for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "_classify_questions_to_funnel_layers"]
    assert calls, "LLM 归层调用点没了 —— 客户手写题会全部 fallback super_tier1"


# ─────────────────────────────────────────── 谓词只准一处

def test_nobody_hand_writes_the_verbatim_predicate_any_more():
    """🔴 数据流锁:`is_custom_mode ... or ... == "verbatim"` 只准写在 origin 模块里。

    毒:任何消费点把那一行抄回去 ⇒ 红。四份「应该等价」的实现漂开那天
    不会有任何东西变红,而表现是同一次诊断报告说的分层和算分的分层不一样。
    """
    offenders = []
    for path in list((ROOT / "services").rglob("*.py")) + \
                list((ROOT / "workflows").rglob("*.py")):
        if path.name == "diagnosis_question_origin.py":
            continue                      # 唯一允许写它的地方
        src = io.open(path, encoding="utf-8").read()
        for i, line in enumerate(src.splitlines(), 1):
            if "is_custom_mode" in line and "verbatim" in line and "or" in line:
                offenders.append("%s:%d" % (path.relative_to(ROOT).as_posix(), i))
    assert not offenders, "谓词又被抄了一份:%s" % offenders
