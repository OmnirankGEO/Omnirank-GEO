# -*- coding: utf-8 -*-
"""WO_236-c1b · verbatim 只关掉「重新分层」,不再关掉「题面判据」。

Review 裁定(2026-09-17):那条豁免把**两个不同的决定**混成了一个 ——

  ① 「不替客户重新分层她自己写的题」 —— 合理,verbatim 的本意。
  ② 「不把它排除出竞争分母」         —— 不合理。
     **一道以品牌名开头的题必然命中自己,与题是谁写的无关。**

谁写的决定「归哪一层」;题面本身决定「算不算竞争样本」。
原实现一见 `verbatim=True` 就 `return (False, False, False)`,
= 用第 ① 条的理由去决定第 ② 件事。

生产实证(Deploy 只读):#700/#726 两份**真客户**报告
`diagnosis_mode="verbatim"` + `question_origins` 缺失/空 ⇒ 全部题豁免 ⇒
三道品牌定向题 × 4 平台 = 12 条全进竞争分母,页面给出「被提及 8/10 次」+ 竞品排行。

🔴 本单**只撤 verbatim 对题面判据的短路**,`is_brand_directed_text` 一个字没动 ——
   同一根轴上放宽会把真竞争题一起误剔
   (本仓 fixing-a-false-positive-by-sliding-the-same-axis-kills-true-positives)。

════════════════════════════════════════════════════════════════════
🔴🔴 **本组判据钉的是「题面文本判据」这一根轴 —— 它可能被换掉**
════════════════════════════════════════════════════════════════════
题单里现成有 `brand_exposure: "named" | "unnamed"`,是**出题层自标**的逐题标记
(Review 生产只读:防御型 131/131 全 `named`),而它**全链路零消费**。
它比题面文本判据硬:文本判据要猜品牌名的缩写形态,自标不用猜。

⇒ 若将来改用 `brand_exposure` 作判据(已排队:236-c1c 之后只读定性,不动码),
  **本组要重新指向** —— 那时这里断言的「`by_text` 为真」将不再是
  「这道题会被排除」的理由,而变成一个恰好同时成立的副产品。
  改指时请像本组这样**写明为什么改指**,别让下一个人看到一组绿判据
  就以为文本判据还在承重。

(现在就写下来,是因为 WO_233 那次 c1↔c3 的耦合没当场写,补了一笔 c3'。)
"""
from __future__ import annotations

import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from services.brand_directed_question import (                        # noqa: E402
    is_brand_directed_text, resolve_brand_directed,
)

BRAND = "广东星衍朗科技有限公司"

#: #700 / #726 现场的真实题面(Deploy 只读导出的三道)。
REAL_DEFENSIVE_QUESTIONS = [
    "广东星衍朗科技有限公司靠谱吗",
    "广东星衍朗科技有限公司是做什么的",
    "广东星衍朗科技有限公司更适合哪些客户",
]

#: 真竞争题:verbatim 与否都**不该**被剔。
REAL_COMPETITIVE_QUESTIONS = [
    "光伏板清洗多少钱",
    "分布式光伏 EPC 哪家好",
    "工商业储能怎么选",
]


@pytest.mark.parametrize("question", REAL_DEFENSIVE_QUESTIONS)
def test_a_verbatim_brand_directed_question_is_still_excluded(question):
    """🔴 主判据:verbatim + 无分层 + 品牌定向题 ⇒ 仍判为品牌定向(要被排除)。

    夹具按 #700/#726 的真实形态构造:`layer_key=None`(题单没带分层)、
    `verbatim=True`(origin 缺省按 customer ⇒ 全部题豁免)。
    """
    verdict = resolve_brand_directed(
        question, layer_key=None, brand_name=BRAND, verbatim=True)
    assert verdict.is_brand_directed, (
        "verbatim 又把题面判据一起关掉了:%r" % question)
    assert verdict.by_text is True, verdict
    assert verdict.by_label is False, (
        "verbatim 下不该用标签判据 —— 那是「替客户重新分层」,本单保留不做")


@pytest.mark.parametrize("question", REAL_DEFENSIVE_QUESTIONS)
def test_the_same_question_with_a_layer_key_is_also_excluded(question):
    """带 `brand_awareness` 分层时同样排除(非 verbatim 路径没被改坏)。"""
    verdict = resolve_brand_directed(
        question, layer_key="brand_awareness", brand_name=BRAND, verbatim=False)
    assert verdict.is_brand_directed, verdict
    assert verdict.by_label is True, verdict


# ── 反向对照:真竞争题一条都不许被误剔 ─────────────────────────────

@pytest.mark.parametrize("question", REAL_COMPETITIVE_QUESTIONS)
@pytest.mark.parametrize("verbatim", [True, False], ids=["verbatim", "planned"])
def test_a_real_competitive_question_is_never_excluded(question, verbatim):
    """🔴 反向对照:不带品牌名的真竞争题,两种模式下都**不**被剔。

    没有这一条,把 `resolve_brand_directed` 改成恒真(全部剔光)也会让
    上面每一条绿 —— 那样竞争分母会变成 0,是比虚高更彻底的破坏。
    """
    verdict = resolve_brand_directed(
        question, layer_key=None, brand_name=BRAND, verbatim=verbatim)
    assert not verdict.is_brand_directed, (
        "真竞争题被误剔(verbatim=%s):%r" % (verbatim, question))


def test_verbatim_still_does_not_relabel_by_layer():
    """🔴 第 ① 条决定**保留**:verbatim 下不看标签。

    构造一道**题面不是品牌定向**、但被上游打了 `brand_awareness` 标签的题:
    · verbatim ⇒ 不剔(尊重"她自己写的题不替她重新分层");
    · 非 verbatim ⇒ 剔(标签判据生效)。
    两种模式给出**不同**结果,才说明第 ① 条真的还在,而不是被我一起撤掉了。
    """
    question = "分布式光伏 EPC 哪家好"
    assert not is_brand_directed_text(question, BRAND), "样本前提不成立"

    as_verbatim = resolve_brand_directed(
        question, layer_key="brand_awareness", brand_name=BRAND, verbatim=True)
    as_planned = resolve_brand_directed(
        question, layer_key="brand_awareness", brand_name=BRAND, verbatim=False)

    assert as_verbatim.is_brand_directed is False, (
        "verbatim 下用了标签判据 —— 那是替客户重新分层,本单不做")
    assert as_planned.is_brand_directed is True, as_planned
    assert as_verbatim.is_brand_directed != as_planned.is_brand_directed, (
        "两种模式给出同一结果 —— 第 ① 条没有分辨力了")


def test_the_text_judgement_itself_was_not_loosened():
    """🔴 `is_brand_directed_text` 一个字没动 —— 没有顺手放宽词面判据。

    钉法:拿一组**不含品牌名**的题面直接问文本判据本人。
    它们要是开始返回 True,说明有人为了让 c1b 好看而放宽了轴,
    那会把真竞争题一起剔掉(同轴放宽杀真阳性)。
    """
    for question in REAL_COMPETITIVE_QUESTIONS:
        assert is_brand_directed_text(question, BRAND) is False, question
    for question in REAL_DEFENSIVE_QUESTIONS:
        assert is_brand_directed_text(question, BRAND) is True, question


def test_an_abbreviated_brand_name_in_a_verbatim_question_is_caught():
    """缩写公司名的 verbatim 题也要认出来(客户很少写全称)。"""
    verdict = resolve_brand_directed(
        "广东星衍朗科技怎么样", layer_key=None, brand_name=BRAND, verbatim=True)
    assert verdict.is_brand_directed, verdict


# ══════════════════════════════════════════════════════════════════
# 被服务方那一侧 —— 钉后果,不只钉谓词
# ══════════════════════════════════════════════════════════════════
#
# 🔴 复审点名:上面 26 条**全长在 `resolve_brand_directed` 这一侧**,没有一条
#    问被服务方。今天整件事的教训就是「做事方全绿而页面照旧骗客户」,
#    不能在同一天再犯一次(本仓 the-signal-is-emitted-by-the-wrong-party,第十二件)。
#
# 后果比"排除数不再是 0"彻底得多:`report_writer_v2` 两处 `continue` 让定向题
# **既不进 `valid_total`**(:1802)**也不进 `top_brands`**(:1739)。
# 防御型题单 131/131 全是品牌定向题 ⇒ 两者同时归零 ⇒
# `public_report_presentation` 命中 `not competitors and not valid_total`
# ⇒ **竞争块整块变成 empty**。
# 也就是说:每一份防御型报告的竞品板块会整块消失。
# Review 认为这个结果是对的(建立在必然命中题上的排行本来就没意义),
# 但它是报 Owner 的两个选项之一,**Owner 未拍板前本笔不上车**。

def _defensive_report_data():
    """防御型题单的最小真实形态:三道品牌定向题 × 4 平台,verbatim、无分层。"""
    questions = REAL_DEFENSIVE_QUESTIONS
    platforms = ("deepseek", "qwen", "kimi", "doubao")
    return {
        "brand_name": BRAND,
        "diagnosis_data": {
            "ai_visibility_data": {
                "diagnosis_mode": "verbatim",
                "question_types": {q: None for q in questions},
                "engine_stats": {},
                "detail_table": [
                    {
                        "question": q,
                        "results": {
                            p: {"answer_summary": "回答里提到了%s。" % BRAND,
                                "brand_detected": True,
                                "mentioned_brands": [BRAND]}
                            for p in platforms
                        },
                    }
                    for q in questions
                ],
            }
        },
    }


def test_a_defensive_plan_yields_an_empty_competitive_section():
    """🔴 被服务方那一格:防御型题单 ⇒ 竞争分母 0、名单空、板块 empty。

    这是本笔**真正的对客后果**。前面那些判据说的是谓词判对了,
    这一条说的是**客户那一页上会发生什么**。
    """
    from services.report_writer_v2 import build_module_3_competition

    module = build_module_3_competition(_defensive_report_data())
    assert module["valid_total"] == 0, (
        "防御型题单仍有 %s 格进了竞争分母 —— 那些格全是必然命中题"
        % module["valid_total"])
    assert module["brand_directed_valid"] == 12, module["brand_directed_valid"]
    assert module["top_brands"] == [], module["top_brands"]

    from services.public_report_presentation import _competitive_section

    section = _competitive_section({"3_competition": module}, brand_name=BRAND)
    assert section.get("status") == "empty", section

    # 🔴 [WO_237 · Owner 拍板] 空态文案要**指路**,不能只说"没有样本"。
    message = section.get("message") or ""
    assert "防御型诊断" in message, message          # ① 为什么没有
    assert "合并版" in message, message              # ② 怎么才能有
    assert "没有形成可比较的竞争品牌样本" not in message, (
        "防御型空态还在用「我们没采到同行」那句 —— 两种空被压成了一句:%r" % message)


def test_the_two_kinds_of_empty_do_not_share_one_sentence():
    """🔴 反臂:**没剔过任何题**的空态仍是旧文案。

    没有这一条,把新文案写成无条件返回也会让上面那格绿 ——
    那会把「我们没采到同行」也说成「你用的是防御型」,**那是另一种骗**:
    客户会去买一个解决不了他问题的合并版诊断。
    两种空的区别只有一个事实说得清:**这次到底剔没剔过品牌定向题**。
    """
    from services.public_report_presentation import _competitive_section

    module = {
        "valid_total": 0,
        "brand_directed_valid": 0,          # ← 一条都没剔过
        "denominator_scope": "all_valid_answers",
        "top_brands": [],
    }
    section = _competitive_section({"3_competition": module}, brand_name=BRAND)
    assert section.get("status") == "empty", section
    message = section.get("message") or ""
    assert "没有形成可比较的竞争品牌样本" in message, message
    assert "防御型诊断" not in message, (
        "没剔过题却说成防御型 —— 这次没采到同行,不是题单的问题:%r" % message)


def test_the_trigger_is_the_actual_exclusion_not_the_mode():
    """🔴 触发条件按**本次实际发生的事**算,不按 `mode == 'defensive'` 硬判。

    钉法:构造一个 `diagnosis_mode` **不是** defensive、却确实剔掉过定向题
    且无竞争样本的 module —— 它仍该拿到指路那句。
    (真实对应:hybrid 题单被客户手动删光增长题。A 实测这条路径存在。)
    硬判 mode 的实现会在这一格红。
    """
    from services.public_report_presentation import _competitive_section

    module = {
        "valid_total": 0,
        "brand_directed_valid": 8,
        "denominator_scope": "excludes_brand_directed_questions",
        "top_brands": [],
        "diagnosis_mode": "hybrid",         # ← 不是 defensive
    }
    section = _competitive_section({"3_competition": module}, brand_name=BRAND)
    assert "防御型诊断" in (section.get("message") or ""), section


def test_a_mixed_plan_still_produces_a_competitive_section():
    """🔴 反向对照:题单里有真竞争题时,竞争块**照常出**。

    没有这一条,把竞争块无条件清空也会让上面那条绿 ——
    那是比虚高更彻底的破坏(客户什么都看不到了)。
    """
    from services.report_writer_v2 import build_module_3_competition

    data = _defensive_report_data()
    data["diagnosis_data"]["ai_visibility_data"]["detail_table"].append({
        "question": "分布式光伏 EPC 哪家好",
        "results": {
            p: {"answer_summary": "推荐甲公司与乙公司。",
                "brand_detected": True, "mentioned_brands": [BRAND, "乙公司"]}
            for p in ("deepseek", "qwen", "kimi", "doubao")
        },
    })
    data["diagnosis_data"]["ai_visibility_data"]["question_types"][
        "分布式光伏 EPC 哪家好"] = None

    module = build_module_3_competition(data)
    assert module["valid_total"] == 4, module["valid_total"]
    assert module["brand_directed_valid"] == 12, module["brand_directed_valid"]

    from services.public_report_presentation import _competitive_section

    section = _competitive_section({"3_competition": module}, brand_name=BRAND)
    assert section.get("status") == "ready", section
    assert section["data"]["excludedBrandDirectedCount"] == 12, section["data"]


def test_the_third_state_flywheel_names_with_a_zero_denominator():
    """🔴 **第三态**:分母为零、而竞品名单**非空**(来自蒸馏飞轮回落)⇒ 仍须 empty + 指路句。

    复审实跑抓到的。我上一版把判断挂在 `not competitors and not valid_total` 上,
    而 `report_writer_v2.py:1827` 在 `top_brands` 为空时会**回落到蒸馏飞轮**
    (`keyword_insights.brands_found`)—— 本单恰恰让防御型的 `top_brands`
    从答案侧变空,**回落必然触发** ⇒ `competitors` 非空 ⇒ 两个空态分支都不进
    ⇒ 页面 `status='ready'`、渲染竞品、样本口径写「已排除 12 条」而一个样本都没有。

    生产可达:品牌 592(`is_test=false`,真客户)41 行飞轮料 + 防御型题单。
    #695/#696/#700/#726 飞轮料为 0,所以它们读不出这一态 ——
    **我原来那格断言 `top_brands == []`,正是这个前提让第三态看不见。**

    为什么不论名字从哪来都要拦:分母为零的排行本就没意义;名字来自飞轮时更糟 ——
    那是另一个时间窗、另一批问题采到的,拼进一份防御型报告
    等于拿历史数据冒充本次实测。
    """
    from services.public_report_presentation import _competitive_section

    module = {
        "valid_total": 0,                   # 本次一条竞争样本都没有
        "brand_directed_valid": 12,         # 剔掉 12 条定向题
        "denominator_scope": "excludes_brand_directed_questions",
        "top_brands": [                     # ← 飞轮回落塞进来的名字
            {"name": "某同行甲", "count": 9},
            {"name": "某同行乙", "count": 4},
        ],
        "competitor_source": "keyword_insights_flywheel",
    }
    section = _competitive_section({"3_competition": module}, brand_name=BRAND)
    assert section.get("status") == "empty", (
        "分母为零却渲染了竞品排行:%r" % section)
    message = section.get("message") or ""
    assert "防御型诊断" in message and "合并版" in message, message


def test_a_real_denominator_with_flywheel_names_still_renders():
    """反臂:**分母非零**时,飞轮来的名字照常渲染 —— 没把回落整条路砍掉。

    没有这一条,把第三态实现成"只要有 brand_directed 就一律 empty"也会绿,
    那会连正常的 hybrid 报告一起清空。
    """
    from services.public_report_presentation import _competitive_section

    module = {
        "valid_total": 8,
        "brand_directed_valid": 12,
        "denominator_scope": "excludes_brand_directed_questions",
        "top_brands": [{"name": "某同行甲", "count": 9}],
        "competitor_source": "keyword_insights_flywheel",
    }
    section = _competitive_section({"3_competition": module}, brand_name=BRAND)
    assert section.get("status") == "ready", section
    assert section["data"]["competitors"], section["data"]
    assert section["data"]["excludedBrandDirectedCount"] == 12, section["data"]
