"""#149 §2.2/§2.3.3 · `question_meta` 契约、计价不变、报告文案按 origin 改写。

三件事各一个失败面:
  · 契约:meta 里有不在题单里的题 ⇒ **422 且明说没扣算力**;
  · 计价:题单形状没动 ⇒ 同一题集同一价(650 / 750),数字一个没改;
  · 文案:「您填写的问题」在全 AI 题单上是**假话**,而假话和真话在屏幕上一样。
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

Q = ["深圳 AI 搜索优化哪家好?", "GEO 优化怎么收费?"]


# ───────────────────────────────────────── §2.2 契约

def test_meta_must_be_a_subset_of_the_plan():
    """🔴 毒①那一格:meta 多一条不在题单里的题 ⇒ 拒。

    放行的话:报告按 meta 说「本次 N 道」,实跑却少几道,
    而两边各自看都正常。
    """
    import server
    with pytest.raises(Exception) as got:
        server.DiagnosisRequest(
            brand_name="B", industry="I", custom_questions=list(Q),
            question_meta=[{"text": Q[0], "origin": "ai_suggested"},
                           {"text": "这道题不在题单里", "origin": "ai_suggested"}])
    msg = str(got.value)
    assert "不在题单" in msg, msg
    # 她最关心的是「我是不是被扣钱了」—— 报文必须回答这件事。
    assert "算力" in msg, "422 没说明未扣算力:%s" % msg


def test_meta_subset_is_accepted_and_normalised():
    """正样本臂:合法 meta 放行(证上一条不是恒抛)。"""
    import server
    req = server.DiagnosisRequest(
        brand_name="B", industry="I", custom_questions=list(Q),
        question_meta=[{"text": Q[0], "origin": "ai_suggested",
                        "side": "growth", "layer": "brand_awareness"},
                       {"text": Q[1], "origin": "customer"}])
    assert len(req.question_meta) == 2
    assert req.question_meta[0].layer == "brand_awareness"


def test_missing_meta_is_allowed_old_frontend():
    """老前端不送 meta ⇒ 放行(缺元数据按 customer 处理)。"""
    import server
    req = server.DiagnosisRequest(brand_name="B", industry="I",
                                  custom_questions=list(Q))
    assert req.question_meta is None


def test_layer_is_not_a_restricted_literal():
    """🔴 `layer` 不受限取值。

    在 DTO 写死 Literal,生成器新增一层那天**整页 500**(#139 那种炸法),
    而判据在响应层之下完全看不见。
    """
    import server
    req = server.DiagnosisRequest(
        brand_name="B", industry="I", custom_questions=[Q[0]],
        question_meta=[{"text": Q[0], "origin": "ai_suggested",
                        "layer": "some_future_layer_nobody_declared_yet"}])
    assert req.question_meta[0].layer == "some_future_layer_nobody_declared_yet"


# ───────────────────────────────────────── 计价:数字一字不动

@pytest.mark.parametrize("n,expect_extra", [(8, 0), (9, 100)])
def test_pricing_is_untouched_by_this_card(n, expect_extra):
    """题单形状没动 ⇒ 同一题集同一价。8 内不加价、第 9 道 +100。

    本单**不许**碰价格数字;这条锁住「我没顺手改」。
    """
    from services.diagnosis_question_pricing import (
        EXTRA_POINTS_PER_QUESTION, FREE_CUSTOM_QUESTIONS,
        extra_points_for_questions)
    assert FREE_CUSTOM_QUESTIONS == 8 and EXTRA_POINTS_PER_QUESTION == 100
    assert extra_points_for_questions(n, ai_optimized=False) == expect_extra


def test_question_meta_does_not_enter_the_price_hash():
    """价哈希只绑**题集 + 计价规则** —— meta 不进,否则前后端一改就 409。"""
    from services.diagnosis_question_pricing import price_preview_id
    kw = dict(base_points=650, ai_optimized=False, feature_code="geo_diagnosis")
    assert price_preview_id(list(Q), **kw) == price_preview_id(list(Q), **kw)
    # 反向:题集变了 id 必须变(证上面不是恒等)。
    assert price_preview_id(list(Q) + ["第三道"], **kw) != price_preview_id(list(Q), **kw)


# ───────────────────────────────────────── §2.3.3 报告文案

def _wording(origins, questions):
    """只取标题与那句话,不跑整篇报告。"""
    from services.diagnosis_question_origin import (
        has_customer_questions, origin_counts)
    ai = {"is_custom_mode": True, "question_origins": dict(origins)}
    c = origin_counts(ai, questions)
    return c, has_customer_questions(ai, questions)


def test_all_ai_plan_is_not_described_as_what_you_filled_in():
    """🔴 毒④那一格:全 AI 题单不许说「您填写的问题」。"""
    counts, has_cust = _wording({q: "ai_suggested" for q in Q}, Q)
    assert counts["ai_suggested"] == 2 and counts["customer"] == 0
    assert not has_cust, "全 AI 题单被当成「您填写的」"


def test_mixed_plan_counts_both_halves():
    counts, has_cust = _wording({Q[0]: "ai_suggested", Q[1]: "customer"}, Q)
    assert (counts["ai_suggested"], counts["customer"]) == (1, 1)
    assert has_cust


def test_legacy_plan_still_reads_as_customer_written():
    """老记录(无 origins)⇒ 仍是「您填写的问题」,文案不倒退。"""
    counts, has_cust = _wording({}, Q)
    assert counts["customer"] == 2 and has_cust


def test_the_report_no_longer_hardcodes_the_customer_phrasing():
    """结构臂:报告不再无条件写死那句话。

    行为臂打的是计数函数,证不了报告**用**了它 ——
    「机制存在」不是「机制生效」。
    """
    src = io.open(ROOT / "services" / "report_writer_v2.py", encoding="utf-8").read()
    # 🔴 锚要唯一:`原始测试数据` 在本文件里出现多次(附录里也有一处),
    #    `src.index` 命中的是**第一处** —— 那不是本单改的地方。
    #    钉本单**新引入**的那个标题分支,它在全文件唯一。
    anchor = "## 原始测试数据(本次题单)"
    assert src.count(anchor) == 1, "标题锚不唯一:%d 次" % src.count(anchor)
    i = src.index(anchor)
    head = src[i - 1200:i + 900]
    assert "origin_counts" in head and "has_customer_questions" in head, (
        "报告标题那一带没接 origin 计数,文案仍可能说假话")
