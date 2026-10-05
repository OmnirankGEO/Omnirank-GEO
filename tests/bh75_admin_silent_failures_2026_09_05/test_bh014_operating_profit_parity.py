# -*- coding: utf-8 -*-
"""BH-014 · 营业利润:导出值必须等于屏幕值。

## 缺陷是什么(实测)

同一 period / 同一 basis 下,财务中心屏幕与 CSV 导出对「营业利润」给出**不同的数**:

    屏幕  api/finance_api.py:565   op_profit = round(gross - opex_total - gateway_fee, 2)
    导出  api/finance_api.py:1289  w.writerow(["营业利润", round(gross - opex_total, 2)])

CSV 恒**高出** `gateway_fee`,而 CSV 里连「支付渠道费」这一行都没有 ⇒ 读者无法自洽核对。

## 🔴 不是「写公式时漏打一项」

`gateway_fee` 在 `export_csv` 作用域、其嵌套 `_do` 作用域、模块全局里**都不存在**
(symtable 查过)—— 这条链**从取数层**就没把渠道费拿回来。
所以修法是「**先取再减**」,不是「加个减号」。

## 修法与本组判据的对应

公式抽成 `_operating_profit(gross, opex_total, gateway_fee)` 一处定义、两处共用;
取数抽成 `_gateway_fee_yuan(since, until)` 一处定义、三处共用
(pnl / cashflow / export —— cashflow 那份原本是第三份逐字副本)。

## 🔴 全组的地基是 `w01` 活性断言,不是「导出 == 屏幕」

「导出 == 屏幕」这句话,在 `_operating_profit` 把第三个形参**整个忽略**时**照样成立**
(两边都不减渠道费,依然相等)—— 也就是说,少了活性断言,这一整组判据会在
`gateway_fee == 0` 或形参被忽略时,**对着有 bug 的代码全绿**。
`w01` 钉的就是「这个参数真的在参与运算」。
"""
from __future__ import annotations

import ast
import contextlib
import importlib
import io
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
TARGET = ROOT / "api" / "finance_api.py"

#: 🔴 冻结豁免集 —— **大小钉死为 0**。
#:    「这一处渠道费 SQL / 这一处营业利润公式可以自己写一份」是一个**需要理由的主张**,
#:    必须在这里显式登记并写清谁保证它与共用件同口径,不许靠沉默表达。
#:    两份手写副本**正是 BH-014 的成因**;拿豁免名单当分母 ⇒ 新副本永远在分母之外。
DUPLICATE_BY_DESIGN: frozenset[str] = frozenset()


def _src() -> str:
    return io.open(TARGET, encoding="utf-8", newline="").read()


# ══════════════════════════════════════════════════════════════════
# 纯判定件 —— 每条只写一处。
# 锁在真文件上跑,`w07` 拿同一批判定件去跑**合成坏源码**,证明它们有牙。
# (同一谓词写两处 ⇒ 必有一处没人验 —— 所以这里不复制粘贴。)
# ══════════════════════════════════════════════════════════════════
def _fn(tree: ast.AST, name: str):
    return next((n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name), None)


def bare_formula_sites(src: str) -> list[tuple[int, str]]:
    """`_operating_profit` **之外**、形状像营业利润的减法。

    分母 = 文件里**所有** `BinOp`(机械枚举),不是手写的两处坐标 ——
    将来谁新加第三处手写公式,会**自动**进分母。
    """
    tree = ast.parse(src)
    op = _fn(tree, "_operating_profit")
    inside = {id(n) for n in ast.walk(op)} if op else set()
    return [(n.lineno, ast.unparse(n))
            for n in ast.walk(tree)
            if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Sub)
            and any(re.search("opex", x.id, re.I) for x in ast.walk(n) if isinstance(x, ast.Name))
            and id(n) not in inside]


def gateway_sql_literals(src: str) -> list[int]:
    """渠道费聚合 SQL 的字面量出现点(行号)。修好后应恰 1 处,在 helper 里。"""
    return [n.lineno for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and "SUM(gateway_fee_cents),0) AS v" in n.value]


def op_call_arg_signatures(src: str) -> list[tuple[int, str]]:
    """每个 `_operating_profit(...)` 调用点的实参签名(逐字 unparse)。

    两个调用点的签名必须**完全相同** —— 这才是「导出与屏幕吃同一组量」的证据。
    """
    return [(n.lineno, ast.unparse(ast.Tuple(elts=n.args, ctx=ast.Load())))
            for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "_operating_profit"]


def gateway_binding_sources(src: str) -> dict[int, str]:
    """每处 `gateway_fee = <expr>` 赋值的右侧(行号 → unparse)。

    钉**同一性**(右侧必须就是 `_gateway_fee_yuan(...)` 调用),不是包含判定 ——
    包含判定会被 `_gateway_fee_yuan(...) if X else 0` 这种短路写法糊弄过去。
    """
    out = {}
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 \
                and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "gateway_fee":
            out[n.lineno] = ast.unparse(n.value)
    return out


def csv_gateway_row(src: str):
    """CSV 里「支付渠道费」那一行 —— 返回 (行号, 第二元素的 AST 节点) 或 None。"""
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "writerow" and n.args and isinstance(n.args[0], ast.List):
            elts = n.args[0].elts
            if len(elts) == 2 and isinstance(elts[0], ast.Constant) \
                    and str(elts[0].value) == "支付渠道费":
                return n.lineno, elts[1]
    return None


# ══ w01 · 地基:第三个形参必须真的参与运算(活性) ═══════════════════
def test_w01_gateway_fee_is_load_bearing_in_the_real_function():
    """🔴 全组的地基。

    调**真 import 的**函数,不是我在判据里重写一遍公式
    —— 重写等于判据在跟自己对话,被测代码写成什么样都不影响读数。

    三个形参**逐一**扰动(分母机械取自函数签名),每个都必须改变输出。
    少了这条:一个把 `gateway_fee` 整个忽略的实现,能让本文件其余每一条都变绿。
    """
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        mod = importlib.import_module("api.finance_api")
    f = mod._operating_profit

    base = dict(gross=8500.0, opex_total=2000.0, gateway_fee=0.0)
    #: 分母**机械取自函数签名**,不是手写的三个名字 —— 将来加第四个形参会自动进分母
    sig_params = [a.arg for a in _fn(ast.parse(_src()), "_operating_profit").args.args]
    assert sig_params == list(base), (
        f"`_operating_profit` 的形参变成了 {sig_params} —— 本条的探针字典没跟上,"
        f"请同笔更新 `base`,而不是删掉这条。")

    ref = f(**base)
    dead = []
    for k in sig_params:
        probe = dict(base)
        probe[k] = base[k] + 34.56
        if f(**probe) == ref:
            dead.append(k)
    assert not dead, (
        f"这些形参对输出**没有影响**:{dead}。\n"
        f"    `gateway_fee` 在列表里 ⇒ 营业利润根本没减渠道费,\n"
        f"    而「导出 == 屏幕」在两边都不减时**照样成立** ——\n"
        f"    本文件其余每一条都会对着有 bug 的代码变绿。这条是全组的地基。")

    # 数值自证:差额必须**恰好**是渠道费,不是「变了就行」
    assert f(8500.0, 2000.0, 34.56) == pytest.approx(f(8500.0, 2000.0, 0.0) - 34.56), \
        "渠道费参与了运算,但不是**减去** —— 符号或口径错了"


# ══ w02 · 公式只有一处定义 ═══════════════════════════════════════
def test_w02_no_hand_written_operating_profit_formula_outside_the_helper():
    sites = bare_formula_sites(_src())
    assert not sites, (
        "`_operating_profit` 之外又出现了手写的营业利润公式:\n    "
        + "\n    ".join(f"L{l}: {s}" for l, s in sites)
        + "\n    两份手写副本**正是 BH-014 的成因**(屏幕减了渠道费、导出没减)。\n"
          "    请改调 `_operating_profit(gross, opex_total, gateway_fee)`。")


# ══ w03 · 取数只有一处定义 ═══════════════════════════════════════
def test_w03_gateway_fee_sql_has_exactly_one_definition():
    lines = gateway_sql_literals(_src())
    assert len(lines) == 1, (
        f"渠道费聚合 SQL 出现在 {len(lines)} 处(L{lines}),应恰 1 处(在 `_gateway_fee_yuan` 里)。\n"
        f"    多一份副本 = 多一处可以独立漂走的口径。")
    fn = _fn(ast.parse(_src()), "_gateway_fee_yuan")
    assert fn and fn.lineno <= lines[0] <= fn.end_lineno, \
        f"那唯一一处 SQL(L{lines[0]})不在 `_gateway_fee_yuan` 体内"


def test_w03b_no_duplicate_is_exempted_without_a_written_reason():
    assert DUPLICATE_BY_DESIGN == frozenset(), (
        f"有人往 `DUPLICATE_BY_DESIGN` 里加了 {sorted(DUPLICATE_BY_DESIGN)} 却没改本条。\n"
        f"    要加就同笔写清:谁保证这份副本与共用件同口径、口径变了谁负责同步改。")


# ══ w04 · 两个调用点吃**同一组量**(导出 == 屏幕的真正证据) ═══════
def test_w04_both_call_sites_pass_identical_arguments():
    sigs = op_call_arg_signatures(_src())
    assert len(sigs) == 2, f"`_operating_profit` 调用点 {len(sigs)} 处(应 2:pnl + export),实测 {sigs}"
    uniq = {s for _, s in sigs}
    assert len(uniq) == 1, (
        f"两个调用点传的实参**不同**:{sigs}\n"
        f"    「导出值 == 屏幕值」的前提就是它们吃同一组量;不同就说明又漂开了。")


def test_w04b_every_gateway_fee_binding_comes_from_the_shared_helper():
    """钉**同一性**:每处 `gateway_fee = ...` 的右侧必须就是 `_gateway_fee_yuan(...)` 调用。"""
    binds = gateway_binding_sources(_src())
    assert binds, "文件里一处 `gateway_fee = ...` 赋值都没有 —— 分母为空,本条无意义,去查发生了什么"
    bad = {l: v for l, v in binds.items() if not re.fullmatch(r"_gateway_fee_yuan\(.+\)", v)}
    assert not bad, (
        "这些 `gateway_fee` 不是从共用件取的:\n    "
        + "\n    ".join(f"L{l}: {v}" for l, v in bad.items())
        + "\n    (含 `_gateway_fee_yuan(...) if X else 0` 这类短路写法 —— 它会让某条分支悄悄取 0。)")
    assert len(binds) == 3, (
        f"`gateway_fee` 赋值点 {len(binds)} 处(应 3:pnl / cashflow / export)。\n"
        f"    多出来的那处要么是新副本,要么是新链路 —— 两种都要人看一眼。实测 {binds}")


# ══ w05 · CSV 必须把渠道费**列出来**,且用同一个量 ═════════════════
def test_w05_csv_shows_the_gateway_fee_row_from_the_same_variable():
    row = csv_gateway_row(_src())
    assert row is not None, (
        "CSV 里没有「支付渠道费」这一行。\n"
        "    没有它,读者拿到 CSV 时**无法自洽核对**:毛利 − OpEx ≠ 营业利润,而差额没有出处。")
    lineno, node = row
    assert isinstance(node, ast.Name) and node.id == "gateway_fee", (
        f"L{lineno} 的「支付渠道费」写的不是那个 `gateway_fee` 变量,而是 "
        f"`{ast.unparse(node)}`。\n"
        f"    必须是**同一个量** —— 重新取一次数或写字面量,都可能与营业利润里减掉的那份不同。")


# ══ w06 · 🔴 自证:上面每条判定件真的有牙 ═══════════════════════════
def test_w06_the_checkers_actually_bite():
    """把**真源码**做定点损伤,喂给同一批判定件,每种坏法必须被抓到。

    毒下在合成字符串上,**不动真文件**。
    每发都先自证「字节真的变了」—— 否则「注毒后仍绿」有四种解释
    (锁没牙 / 毒没下成 / 毒落在分母外 / 判定件没跑),读数完全同形。
    """
    src = _src()
    cases = []

    # ① 导出侧退回裸公式(BH-014 的原始缺陷现场)
    p1 = src.replace(
        'w.writerow(["营业利润", _operating_profit(gross, opex_total, gateway_fee)])',
        'w.writerow(["营业利润", round(gross - opex_total, 2)])')
    cases.append(("导出退回裸公式", p1, lambda s: len(bare_formula_sites(s)) > 0))

    # ② cashflow 复活第三份 SQL 副本
    p2 = src.replace(
        'gateway_fee = _gateway_fee_yuan(p["since"], p["until"])',
        'gateway_fee = round(float(_safe_scalar("SELECT COALESCE(SUM(gateway_fee_cents),0) AS v '
        'FROM recharge_orders WHERE payment_status=\'paid\'", ())) / 100.0, 2)', 1)
    cases.append(("SQL 副本复活", p2, lambda s: len(gateway_sql_literals(s)) != 1))

    # ③ 两个调用点漂开(导出少传一个量)
    p3 = src.replace(
        'w.writerow(["支付渠道费", gateway_fee])\n'
        '    w.writerow(["营业利润", _operating_profit(gross, opex_total, gateway_fee)])',
        'w.writerow(["支付渠道费", gateway_fee])\n'
        '    w.writerow(["营业利润", _operating_profit(gross, opex_total, 0.0)])')
    cases.append(("调用点实参漂开", p3, lambda s: len({x for _, x in op_call_arg_signatures(s)}) != 1))

    # ④ gateway_fee 改成短路写法(包含判定抓不到,同一性能抓到)
    p4 = src.replace(
        'gateway_fee = _gateway_fee_yuan(p["since"], p["until"])',
        'gateway_fee = _gateway_fee_yuan(p["since"], p["until"]) if basis != "cash" else 0.0', 1)
    cases.append(("短路取数", p4, lambda s: any(
        not re.fullmatch(r"_gateway_fee_yuan\(.+\)", v)
        for v in gateway_binding_sources(s).values())))

    # ⑤ CSV 渠道费行被删
    p5 = src.replace('    w.writerow(["支付渠道费", gateway_fee])\n', "")
    cases.append(("CSV 行删除", p5, lambda s: csv_gateway_row(s) is None))

    # ⑥ CSV 渠道费行改成重新取数(不是同一个量)
    p6 = src.replace('w.writerow(["支付渠道费", gateway_fee])',
                     'w.writerow(["支付渠道费", _gateway_fee_yuan(p["since"], p["until"])])')
    cases.append(("CSV 用了另一个量", p6, lambda s: not isinstance(csv_gateway_row(s)[1], ast.Name)))

    for name, poisoned, detects in cases:
        assert poisoned != src, (
            f"【{name}】这一发**毒没下成**(字节与原文一致)—— 锚点漂了。\n"
            f"    这跟「锁没牙」在读数上完全同形,所以先判这个:去修锚点,别下「锁没牙」的结论。")
        ast.parse(poisoned)  # 坏法必须仍是合法 Python,否则判定件红的是语法而不是我要它抓的那条
        assert detects(poisoned), f"【{name}】毒下成了,但判定件**没抓到** —— 这条锁没牙"

    # 反向对照:未注毒的真源码,六个判定件必须全绿(否则上面的红说明不了任何事)
    assert not bare_formula_sites(src)
    assert len(gateway_sql_literals(src)) == 1
    assert len({x for _, x in op_call_arg_signatures(src)}) == 1
    assert all(re.fullmatch(r"_gateway_fee_yuan\(.+\)", v)
               for v in gateway_binding_sources(src).values())
    assert csv_gateway_row(src) is not None
    assert isinstance(csv_gateway_row(src)[1], ast.Name)
    assert len(cases) == 6, f"自证发数 {len(cases)}(改坏法数量时同笔改这里)"
