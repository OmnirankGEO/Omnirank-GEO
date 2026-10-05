"""【P0-3】最小样本合同必须读**生产真的会发**的那个形状。

这是整个缺陷的真因,也是最容易再犯的一条:

`evaluate_sample` 原来只从 `ai_visibility["summary"]` 取计数,而两个真实产出方
(`workflows/diagnosis_workflow.py` 销售版 / 技术版)都把计数放在 `ai_visibility` 的**顶层**,
全文件没有一处给它造过 `summary` 子字典。于是生产每一单读到的都是 planned=0 →
落进"老结构不臆断失败"那一档 → `sufficient / billable_ratio=1.0` → **四引擎全挂也全额扣费**。

而老的合同测试全绿,因为它的夹具 `_av()` 自己造了 `av["summary"]` ——
**夹具用的键是生产从来不会发的键**,这种判据无论实现怎么错都不会红。

所以这里的锁分两层:
  · 结构层 —— 从 `diagnosis_workflow.py` 真正的 `ai_data = {...}` 字面量里取键名当分母,
    生产形状变了(比如哪天真加了 summary)这条锁会先红,而不是等资金出事;
  · 行为层 —— 拿那个形状喂 `evaluate_sample`,断言三档判定各自正确。
"""
from __future__ import annotations

import ast
import io
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = REPO / "workflows" / "diagnosis_workflow.py"

COUNTER_KEYS = ("total_tests", "total_planned", "total_failed")


def _ai_data_literals():
    """把 diagnosis_workflow.py 里所有 `ai_data = {...}` 字面量的**顶层键名**取出来。

    分母是机械枚举出来的(走 AST 找赋值给 ai_data 的 dict 字面量),不是我手写的清单 ——
    手写会漏,漏掉的那一条不会让任何判据变红。
    """
    tree = ast.parse(io.open(WORKFLOW, encoding="utf-8").read())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "ai_data" for t in node.targets):
            continue
        keys = {k.value for k in node.value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        out.append((node.lineno, keys))
    return out


def test_producers_put_counters_at_top_level_and_never_nest_a_summary():
    """生产形状锁:每一处 ai_data 字面量都必须顶层带计数、且**不带** summary。"""
    literals = _ai_data_literals()
    assert len(literals) >= 2, "没找到两条组装路径的 ai_data 字面量,分母取错了: %s" % literals
    for lineno, keys in literals:
        assert set(COUNTER_KEYS) <= keys, (
            "workflows/diagnosis_workflow.py:%d 的 ai_data 顶层缺计数键: %s" % (lineno, sorted(keys)))
        assert "summary" not in keys, (
            "workflows/diagnosis_workflow.py:%d 现在会发 summary 了 —— "
            "合同的取数口径要跟着改,别让它又变成读不到的键" % lineno)


def _prod_shape(total_planned, total_tests):
    """按生产字面量的形状造 payload(计数在顶层,没有 summary)。"""
    return {
        "test_questions": ["q%d" % i for i in range(8)],
        "engines_tested": ["qwen", "deepseek", "kimi", "doubao"],
        "total_tests": total_tests,
        "total_planned": total_planned,
        "total_failed": total_planned - total_tests,
        "detected_count": 0,
        "engine_stats": {},
        "detail_table": [],
        "total_engines": 4,
    }


def test_production_shape_zero_success_is_insufficient_and_bills_zero():
    """真因锁:生产形状 + 全引擎失败 → 必须 insufficient / 计费比例 0。
    这条锁如果红了,就是"四引擎全挂照样全额扣费"那个缺陷又回来了。
    """
    from services.diagnosis_sample_contract import OUTCOME_INSUFFICIENT, evaluate_sample
    v = evaluate_sample(_prod_shape(32, 0))
    assert v.outcome == OUTCOME_INSUFFICIENT, (v.outcome, v.reason_code)
    assert v.billable_ratio == 0.0
    assert v.reason_code != "legacy_shape_no_planned", (
        "又落回'老结构不臆断失败'那一档了 —— 说明计数还是没从顶层读到")


@pytest.mark.parametrize("succeeded,expected_ratio", [(8, 0.25), (24, 0.75)])
def test_production_shape_partial_success_is_degraded_with_that_ratio(succeeded, expected_ratio):
    from services.diagnosis_sample_contract import OUTCOME_DEGRADED, evaluate_sample
    v = evaluate_sample(_prod_shape(32, succeeded))
    assert v.outcome == OUTCOME_DEGRADED, (v.outcome, v.reason_code)
    assert round(v.billable_ratio, 4) == expected_ratio


def test_production_shape_full_success_is_sufficient_and_bills_full():
    """配对的必须不命中:全成功 → 一分不少照收(不误杀)。"""
    from services.diagnosis_sample_contract import OUTCOME_SUFFICIENT, evaluate_sample
    v = evaluate_sample(_prod_shape(32, 32))
    assert v.outcome == OUTCOME_SUFFICIENT
    assert v.billable_ratio == 1.0


def test_nested_summary_shape_still_works_backward_compatible():
    """向后兼容锁:老的嵌套形状(以及老测试)行为不变。"""
    from services.diagnosis_sample_contract import (
        OUTCOME_DEGRADED, OUTCOME_INSUFFICIENT, evaluate_sample,
    )
    assert evaluate_sample(
        {"summary": {"total_planned": 8, "total_tests": 0, "total_failed": 8}}
    ).outcome == OUTCOME_INSUFFICIENT
    assert evaluate_sample(
        {"summary": {"total_planned": 4, "total_tests": 3, "total_failed": 1}}
    ).outcome == OUTCOME_DEGRADED


def test_truly_legacy_shape_without_any_counters_is_still_let_through():
    """向后兼容锁:两处都没有计数的**真**老结构 → 保持放行,绝不误杀。"""
    from services.diagnosis_sample_contract import OUTCOME_SUFFICIENT, evaluate_sample
    v = evaluate_sample({"total_engines": 4})
    assert v.outcome == OUTCOME_SUFFICIENT
    assert v.reason_code == "legacy_shape_no_planned"


def test_verdict_reaches_settlement_through_the_signal_assembler():
    """接线锁:结算侧从快照读 `delivery_verdict`,那完成 payload 就必须真的发它。

    [R2-②] 这条原来断言键名出现在 payload 字面量里 —— Review 亲毒证明那不够
    (留键钉值 None 照样绿)。现在结构那道缝锁「有没有走组装函数」,
    值那道缝由 test_closeout_p03b.py 里 test_settlement_signals_* 四条直接驱动函数验。

    ⚠️⚠️ **这条与 test_closeout_p03b.py 那条是同一把子串锁,拦不住入参被换掉。**
    `settlement_signals_from_result({})` 里函数名照样留在 `ast.dump()` 输出中 ⇒ 本条恒绿,
    而两条结算信号已经在每一单里恒 None。R3 实测(`scratchpad/p03b_attribution.py`):
    「调用处入参换 `{}`」这发变异**只被节点级锁抓到,本条全绿**。

    调用点的真锁 =
      `test_r3_gap_closure.py::test_payload_splat_is_the_assembler_called_with_the_real_result`
    本条只是更粗一档的早报警(连 `**` 展开都没了)。**别只看名字以为它在守调用点。**
    """
    tree = ast.parse(io.open(REPO / "server.py", encoding="utf-8").read())
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if not {"diagnosis_id", "share_token", "terminal"} <= keys:
            continue
        splats = [ast.dump(v) for k, v in zip(node.keys, node.values) if k is None]
        found.append((node.lineno, any("settlement_signals_from_result" in d for d in splats)))
    assert found, "没定位到诊断完成 payload,分母取错了"
    bad = [ln for ln, ok in found if not ok]
    assert not bad, (
        "server.py:%s 的完成 payload 没走 settlement_signals_from_result → "
        "_partial_commit_points 读不到判定,降级交付会全额扣费" % bad)


# ===========================================================================
# ③ 呈现:结算内部改道退款后,调用方绝不能把它当成交
# ===========================================================================

def test_ok_true_but_terminal_released_is_not_a_success():
    """`ok=True` + `terminal=released` = 钱退了。判成成功 → 用户会看到"诊断完成 · 隐形级"。"""
    from services.diagnosis_runs import commit_outcome_is_success
    assert commit_outcome_is_success({"ok": True, "terminal": "released"}) is False


@pytest.mark.parametrize("terminal", ["committed", "completed_exempt"])
def test_real_success_terminals_are_still_success(terminal):
    """配对的必须不命中:真成交的两个终态必须照常判成功(不误杀正常诊断)。"""
    from services.diagnosis_runs import commit_outcome_is_success
    assert commit_outcome_is_success({"ok": True, "terminal": terminal}) is True


@pytest.mark.parametrize("out", [
    {"ok": False, "terminal": "settlement_manual"},
    {"ok": False, "retry": True},
    None, "not-a-dict",
])
def test_non_ok_outcomes_are_never_success(out):
    from services.diagnosis_runs import commit_outcome_is_success
    assert commit_outcome_is_success(out) is False


def test_server_gates_every_billed_success_message_with_the_predicate():
    """接线锁 —— 光有函数没用,server.py 必须**真的**拿它守住结算路径上的成功终态消息。

    (锁接线不锁函数:M8 那一发变异证明过,判据够不到的内联 if 改掉了也没人红。)

    分母怎么取:枚举**每一处** `send_message(session_id, _complete_snap)`,再看它外层的
    if 条件链。只要这条链上出现过 `out`(= 它在结算结果的分支里),就必须也出现
    `commit_outcome_is_success`。`run_token is None` 那条无计费路径不在链上没有 `out`,
    自然被排除 —— 那条路没冻结也没扣费,发完成是对的,不该被这把锁误伤。
    """
    tree = ast.parse(io.open(REPO / "server.py", encoding="utf-8").read())
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def enclosing_tests(node):
        """只收**真正守住这一支**的条件。

        关键细节:`if A: ... elif B: ...` 在 AST 里是 `If(test=A, orelse=[If(test=B)])`,
        内层 If 是外层的子节点。天真地往上收会把 A 也当成 B 那一支的守卫 ——
        于是把 B 改成没有谓词的写法,锁照样绿(M8 第一版实测存活就是栽在这)。
        所以往上走时只在「上一层是从 body 进来的」才算这一层的 test 守住了它。
        """
        tests, cur = [], node
        while cur in parents:
            prev, cur = cur, parents[cur]
            if isinstance(cur, ast.If) and any(prev is st or prev in ast.walk(st)
                                               for st in cur.body):
                tests.append(ast.dump(cur.test))
        return tests

    sends = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "send_message"
             and any(isinstance(a, ast.Name) and a.id == "_complete_snap" for a in n.args)]
    assert sends, "没定位到发送 _complete_snap 的调用,分母取错了"

    billed = [(n.lineno, enclosing_tests(n)) for n in sends]
    billed = [(ln, ts) for ln, ts in billed if any("'out'" in t for t in ts)]
    assert billed, "没有任何 _complete_snap 发送处于结算分支里,分母取错了(或分支被改没了)"
    bad = [ln for ln, ts in billed if not any("commit_outcome_is_success" in t for t in ts)]
    assert not bad, (
        "server.py:%s 在结算分支里发成功终态却没经过 commit_outcome_is_success —— "
        "结算内部改道退款会被当成成交发给用户" % bad)
