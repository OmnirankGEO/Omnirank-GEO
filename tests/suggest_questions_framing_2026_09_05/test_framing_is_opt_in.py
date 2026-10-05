"""#84 §2 · 出题 framing 是**可选加项**,线上路径逐字节不变。

订正十五② 走的是「给共享生产者加可选参数」而不是「在端点里做后处理」。
那条路的全部风险集中在一句话上:**不传就得跟改前一模一样**。
本文件把这句话变成可执行的。

🔴 一条**做不到的**:Review 要求「防守线返回的题须通过 `is_brand_directed_text`、
   增长线不通过」—— 那是**真 LLM 的行为**,单测里只能靠假夹具冒充,而假夹具
   证明的是我自己写的字符串,不是模型的输出。**如实留给真跑验收**,不在这里假绿。
"""
from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _src(*parts) -> str:
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


@pytest.mark.parametrize("framing", [None, "", "growth", "defensive", "BRAND_DIRECTED", "x"])
def test_only_the_exact_token_produces_a_block(framing):
    """除了逐字 `brand_directed`,任何值都返回空串。

    🔴 大小写变体也必须返回空:模糊匹配会让「拼错了」静默变成「生效了」,
       而两者在 prompt 上的差别是一整段指令。
    """
    from tools.keyword_generator import _render_question_framing_block
    assert _render_question_framing_block(framing) == ""


def test_the_exact_token_produces_a_non_empty_block():
    """🔁 正样本臂:没有它,上面那条全绿也可能是因为**函数恒返空**(功能整个没接上)。"""
    from tools.keyword_generator import _render_question_framing_block
    block = _render_question_framing_block("brand_directed")
    assert block.strip(), "brand_directed 没产出任何框架 —— 参数接了个寂寞"
    assert "点名品牌" in block


def test_default_is_none_so_existing_callers_are_untouched():
    """签名默认值必须是 None —— 默认值一变,所有现存调用点的 prompt 全变。"""
    from tools.keyword_generator import analyze_client_business
    sig = inspect.signature(analyze_client_business)
    assert "question_framing" in sig.parameters, "参数没加上"
    assert sig.parameters["question_framing"].default is None


def test_the_live_diagnosis_call_site_does_not_pass_framing():
    """🔴 线上诊断那一处**不许**传 framing —— 传了就是把防守口径灌进所有诊断。

    分母是 `diagnosis_workflow.py` 里对 `analyze_client_business` 的**全部**调用点,
    机械枚举,不手写清单。
    """
    src = _src("workflows", "diagnosis_workflow.py")
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", getattr(n.func, "attr", None)) == "analyze_client_business"]
    assert calls, "一个调用点都没找到 —— 分母为空,这条判据什么都没验"
    for c in calls:
        names = [k.arg for k in c.keywords]
        assert "question_framing" not in names, (
            f"线上调用点传了 framing(第 {c.lineno} 行)—— 防守口径会灌进所有诊断")


def test_the_block_is_appended_adjacent_so_empty_changes_nothing():
    """🔴 织入必须与 flywheel 块**紧邻**:`{_flywheel_block}{_framing_block}`。

    这是「不传即逐字节不变」的**结构证明** —— 空串与相邻拼接,
    在 prompt 里加不出任何字符。中间若隔了换行/空格,不传也会多出字符,
    而那正是「零行为变化」这句承诺破掉的方式,且**不会有任何别的判据变红**。
    """
    src = _src("tools", "keyword_generator.py")
    assert "{_flywheel_block}{_framing_block}" in src, (
        "织入点不是紧邻拼接 —— 不传时 prompt 会多出字符")


def test_the_renderer_has_exactly_one_injection_site():
    """同一谓词只许有一处织入:两处织入 = 不传时也可能只改了一处的那种漂移。"""
    src = _src("tools", "keyword_generator.py")
    assert src.count("_render_question_framing_block(") == 2, (
        "期望恰好两处:定义 1 + 调用 1;多出来的是第二个织入点")
