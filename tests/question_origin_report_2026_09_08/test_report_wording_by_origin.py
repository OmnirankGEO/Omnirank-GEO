"""#149 返修 · 报告文案的**真渲染**行为臂(独立成包)。

## 为什么单独一个包

`build_module_3_raw_ai_appendix` 的真渲染与 `import server` **同一个 pytest 会话**
时,会话结束时 pytest 拆 capture 炸
`ValueError: I/O operation on closed file`,其后所有用例 setup 失败。

🔴 归属已证:**不是本单改出来的** —— 最小 repro(一条 `import server`
   + 一条真调 `build_module_3_raw_ai_appendix`)在本尖与父臂 cff1346bb
   **都通过**;触发要更细的组合。本仓对这类会话级干扰的既有做法就是按包隔离
   (各包锁自己的 DSN;r567 那个包干脆不 import 工作流)。
   我按同一做法隔离,并把这个隐患报给了 Review —— 不是绕过去当没看见。

⚠️ 这类崩溃**不会给出读数**(终端摘要根本没打出来),
   而「没有读数」看起来和「没有问题」一模一样。本包的存在就是不让它再发生。
"""

from __future__ import annotations

import pytest

Q_AI1 = "深圳 AI 搜索优化哪家好?"
Q_AI2 = "GEO 优化一般怎么收费?"
Q_CUST = "我们这种做工业设备的适合投吗?"

def _render(origins, questions, verbatim=True):
    """跑**真**的报告段,不是打计数函数。"""
    from services.report_writer_v2 import build_module_3_raw_ai_appendix

    return build_module_3_raw_ai_appendix({
        "diagnosis_data": {"ai_visibility_data": {
            "is_custom_mode": verbatim,
            "diagnosis_mode": "verbatim" if verbatim else "default",
            "question_origins": dict(origins),
            "detail_table": [
                {"question": q,
                 "results": {"kimi": {"detected": True, "answer": "占位回答"}}}
                for q in questions],
        }},
    })["rendered_md"]


def test_an_all_ai_report_does_not_claim_you_filled_these_in():
    """🔴 毒 P4 那一格:全 AI 题单的**渲染输出**不含「您填写的问题」。

    毒:标题条件改回 `if is_verbatim_mode` ⇒ 红。
    打真输出,不打计数函数 —— 计数对了而文案没接上,一样是假话。
    """
    md = _render({Q_AI1: "ai_suggested", Q_AI2: "ai_suggested"}, [Q_AI1, Q_AI2])
    assert "您填写的问题" not in md, "全 AI 题单仍被写成「您填写的」:\n%s" % md[:400]
    assert "全部由 AI 为您出题" in md, "没说清题是 AI 出的:\n%s" % md[:400]


def test_a_legacy_report_still_says_you_filled_these_in():
    """🔴 正样本臂:老记录(无 origins)仍写「您填写的问题」。

    只证「全 AI 不说那句」不够 —— 一个把那句话整个删掉的实现同样能让
    上一条变绿,而那会让真正自己填过题的用户失去这句确认。
    """
    md = _render({}, [Q_CUST])
    assert "您填写的问题" in md, "老记录的文案倒退了:\n%s" % md[:400]


def test_a_mixed_report_names_both_halves():
    """AI + 自己:两半都要报数,不许只说一半。"""
    md = _render({Q_AI1: "ai_suggested", Q_CUST: "customer"}, [Q_AI1, Q_CUST])
    assert "AI 出的 1 道" in md and "您填的 1 道" in md, md[:400]
    assert "您填写的问题" not in md


def test_a_non_verbatim_report_is_untouched():
    """非 verbatim(系统出题)⇒ 标题维持原样,本单不该碰它。"""
    md = _render({}, [Q_AI1], verbatim=False)
    assert md.startswith("## 原始测试数据\n") or "## 原始测试数据" in md
    assert "您填写的问题" not in md and "本次题单" not in md
