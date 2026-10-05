# -*- coding: utf-8 -*-
"""BH-015b · 「这个进货价标不标准」的判法必须与定价规则一致,且只许有一处。

## 缺陷是什么(实测)

判定侧用**精确等式**,定价侧用 **ceil**:

    定价  services/agent_pricing.py  calc_factory_cents = ceil(points × numer / denom)
    判定  api/admin_factory_api.py   is_standard = (points × numer == wholesale × denom)

两者只在 `denom/gcd(numer,denom)` 整除 points 时一致 ——
默认 225/325 ⇒ **points 必须是 13 的倍数**,否则不管 wholesale 填什么整数,
等式都不可能成立:**系统自己算出来的价会被判成「人工谈价」**。

## 最狠的一处不是软跳过,是硬阻断

`PUT /pricing/skus/{id}`(`admin_pricing_sku_put`)是 `raise HTTPException(400)`。
points 不是 13 倍数的 SKU 通过这个端点**永远改不动**,
而报错还要求 admin「同时改 points_granted 和 wholesale_cents 保持等式」——
**一个做不到的要求**。改后报错必须直接给出该填的数(裁定:带理由和出路)。

## 🔴 静态 ≠ 运行时:定级按运行时收敛

全树 18 个种子 SKU 全是 13 的倍数、全部通过判定 ⇒ **当前目录 0 命中**。
「92% 被跳过」是「points 均匀取值空间」的属性,**不是「当前目录」的属性**。
触发需要 admin 手填一个非 13 倍数的 points(无任何校验拦),或改出厂折扣系数。
本组判据钉的是**规则**,不随种子数据变化 —— 也正因如此它能在 0 命中时仍然有效。

## 🔴 现有判据对这条结构性不可见

AST 普查既有 4 条相关判据的 points 字面量:**10/12 个是 130**(13 的倍数),
仅有的两个 100 都配 `wholesale=999`(真人工谈价值)。
⇒ 那一族判据的**分母本来就漏了一整格**;补一条不够,要按 AST 分母逐点打。
"""
from __future__ import annotations

import ast
import contextlib
import importlib
import io
import math
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
API = ROOT / "api" / "admin_factory_api.py"

#: 🔴 冻结例外集 —— 大小钉死为 0。
#:    「这一处就是该用精确等式」是一个**需要理由的主张**,必须显式登记并写清
#:    谁保证它与 `calc_factory_cents` 同口径,不许靠沉默表达。
EXACT_EQUALITY_BY_DESIGN: frozenset[str] = frozenset()


def _read(p: pathlib.Path) -> str:
    return io.open(p, encoding="utf-8", newline="").read()


def _mod():
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return importlib.import_module("api.admin_factory_api")


def hand_written_ratio_comparisons(src: str) -> list[tuple[int, str]]:
    """手写的量纲等式 —— 分母 = 文件里**所有** `Compare` 节点(机械枚举)。

    将来谁新加第六处手写比较,会**自动**进分母。
    """
    return [(n.lineno, ast.unparse(n)) for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Compare)
            and "numer" in ast.unparse(n) and "denom" in ast.unparse(n)
            and "*" in ast.unparse(n)]


# ══ y01 · 谓词与定价规则同口径(这是缺陷现场) ═══════════════════════
@pytest.mark.parametrize("points", [1, 7, 12, 14, 100, 101, 999, 1234])
def test_y01_rule_output_is_always_judged_standard(points):
    """系统自己算出来的价,**必须**被判成标准价。

    分母是**非 13 倍数**的 points —— 旧写法在这些点上无论 wholesale 填什么都判非标。
    (既有那族判据的 points 字面量 10/12 个是 130,整格漏掉了这里。)
    """
    with contextlib.redirect_stdout(io.StringIO()):
        from services.agent_pricing import calc_factory_cents
    numer, denom = 225, 325
    assert points % 13 != 0 or points in (), \
        f"{points} 是 13 的倍数 —— 它落在旧写法**恰好也对**的那一格,没有区分力"
    rule = calc_factory_cents(points, numer, denom)
    assert _mod()._is_standard_price(points, rule, numer, denom), (
        f"points={points} 时,规则自己算出的 wholesale={rule} 竟被判成非标准价。\n"
        f"    这就是缺陷:系统算的价被当成「人工谈价」,\n"
        f"    在 PUT /pricing/skus/{{id}} 上表现为**永远保存不了**。")
    # 旧写法在这里必然判错 —— 证明本条有区分力,不是「怎么写都绿」
    assert points * numer != rule * denom, (
        f"points={points} 时旧的精确等式**也成立** —— 这个样本没有区分力,换一个")


@pytest.mark.parametrize("points,wholesale", [(130, 999), (260, 1), (13, 0)])
def test_y01b_a_genuinely_negotiated_price_is_still_non_standard(points, wholesale):
    """🔴 反臂:真·人工谈价必须仍判非标。

    没有这条,一个 `return True` 的实现能让上面每条都绿。
    """
    with contextlib.redirect_stdout(io.StringIO()):
        from services.agent_pricing import calc_factory_cents
    numer, denom = 225, 325
    assert wholesale != calc_factory_cents(points, numer, denom), "这个夹具本身就是规则值,不是谈价"
    assert not _mod()._is_standard_price(points, wholesale, numer, denom), (
        f"points={points} wholesale={wholesale} 明明不是规则算出来的,却判成了标准价 —— "
        f"那 recalc-all 会把人工谈价当标准包覆盖掉。")


def test_y01c_the_two_writings_disagree_on_a_measured_share_not_a_quoted_one():
    """把「两种写法分歧多大」量出来,而不是引用工单里的 92%。

    同时钉住:分歧**存在**。哪天它变成 0,说明 `calc_factory_cents` 改了取整规则,
    本组判据的前提没了,要重新论证而不是继续绿着。
    """
    with contextlib.redirect_stdout(io.StringIO()):
        from services.agent_pricing import calc_factory_cents
    numer, denom = 225, 325
    isp = _mod()._is_standard_price
    disagree = 0
    N = 2000
    for points in range(1, N + 1):
        rule = calc_factory_cents(points, numer, denom)
        old_verdict = (points * numer == rule * denom)      # 旧精确等式
        new_verdict = isp(points, rule, numer, denom)       # 现谓词
        if old_verdict != new_verdict:
            disagree += 1
    share = disagree / N
    step = denom // math.gcd(numer, denom)                  # = 13
    expected = 1 - 1 / step
    assert disagree > 0, (
        "两种写法在 2000 个 points 上**完全一致** —— "
        "说明 `calc_factory_cents` 不再是 ceil,本组判据的前提没了,去重新论证。")
    assert abs(share - expected) < 0.01, (
        f"分歧率实测 {share:.1%},按 denom/gcd={step} 推算应约 {expected:.1%} —— "
        f"两者对不上,先查是我的推算错了还是系数变了,别直接改这个数字")


# ══ y02 · 谓词只有一处(五份手写副本正是它铺开到五处的原因) ═════════
def test_y02_no_hand_written_ratio_equality_remains():
    sites = hand_written_ratio_comparisons(_read(API))
    assert not sites, (
        "又出现了手写的量纲等式:\n    "
        + "\n    ".join(f"L{l}: {s}" for l, s in sites)
        + "\n    改之前这个谓词在本文件手写了**五份**,五份是同一个错法 ——\n"
          "    同一个谓词写五处,必有一处在下次改动时被漏掉。\n"
          "    请改调 `_is_standard_price(points, wholesale, numer, denom)`。")


def test_y02b_all_five_sites_go_through_the_shared_predicate():
    src = _read(API)
    tree = ast.parse(src)
    defs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_is_standard_price"]
    calls = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "_is_standard_price"]
    assert len(defs) == 1, f"`_is_standard_price` 定义 {len(defs)} 处(应 1)"
    assert len(calls) == 5, (
        f"`_is_standard_price` 调用 {len(calls)} 处(应 5:put / _factory_and_standard / "
        f"center / save / recalc_all),实测 @ L{sorted(calls)}。\n"
        f"    少了 = 有判定点没走共用件;多了 = 新增了判定点,要人看一眼是不是同一件事。")


def test_y02c_no_site_is_exempted_without_a_written_reason():
    assert EXACT_EQUALITY_BY_DESIGN == frozenset(), (
        f"有人往 `EXACT_EQUALITY_BY_DESIGN` 里加了 {sorted(EXACT_EQUALITY_BY_DESIGN)} 却没改本条。\n"
        f"    要加就同笔写清:谁保证这一处与 `calc_factory_cents` 同口径。")


def test_y02d_the_predicate_delegates_to_the_pricing_rule_not_a_reimplementation():
    """谓词体内必须**调** `calc_factory_cents`,不是自己再实现一遍 ceil。

    自己实现 = 又一份会独立漂走的副本,只是从五份变成两份。
    """
    fn = next(n for n in ast.parse(_read(API)).body
              if isinstance(n, ast.FunctionDef) and n.name == "_is_standard_price")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "calc_factory_cents"]
    assert len(calls) == 1, (
        f"`_is_standard_price` 里 `calc_factory_cents` 调用 {len(calls)} 处(应 1)——"
        f"体内是 {ast.unparse(fn)[:200]}")


# ══ y03 · 硬阻断的 400 必须带出路(裁定:带理由和出路) ═══════════════
def test_y03_the_hard_block_tells_the_admin_what_value_would_be_accepted():
    """改之前它只说等式不成立,还要求「同时改两个字段保持等式」——
    而 points 不是 13 倍数时那是**做不到**的:admin 会一直改一直被拒。
    """
    fn = next(n for n in ast.walk(ast.parse(_read(API)))
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "admin_pricing_sku_put")
    raises = [n for n in ast.walk(fn) if isinstance(n, ast.Raise)
              and isinstance(n.exc, ast.Call) and "HTTPException" in ast.unparse(n.exc.func)
              and n.exc.args and isinstance(n.exc.args[0], ast.Constant)
              and n.exc.args[0].value == 400]
    assert raises, "`admin_pricing_sku_put` 里找不到 400 硬阻断 —— 分母为空,去查发生了什么"

    msgs = [ast.unparse(r.exc.args[1]) for r in raises if len(r.exc.args) > 1]
    ratio_msg = [m for m in msgs if "wholesale_cents" in m]
    assert ratio_msg, f"400 报错里不提 wholesale_cents,实测 {msgs}"
    body = ratio_msg[0]

    # 出路必须是一个**算出来的具体数**,不是「请保持等式」这种做不到的要求
    assert "_want" in body, (
        "400 报错里没有插入「该填的那个数」。\n"
        "    只说等式不成立 = 没有出路;points 不是 13 倍数时 admin 无论怎么改都过不了。")
    assert "保持等式" not in body, (
        "400 报错还在要求 admin「保持等式」—— 这在 points 非 13 倍数时做不到,"
        "正是本 bug 让人最抓狂的地方。")

    # `_want` 必须真的由规则算出,不是随手取的变量
    src_fn = ast.unparse(fn)
    assert re.search(r"_want\s*=\s*calc_factory_cents\(", src_fn), (
        "`_want` 不是由 `calc_factory_cents` 算出来的 —— 那报错给的数就不可信")


def test_y03b_the_hard_block_still_blocks_a_genuinely_wrong_price():
    """🔴 反臂:别为了「有出路」把闸门拆了。

    钉住:`admin_pricing_sku_put` 里那个 400 仍然挂在
    `not _is_standard_price(...)` 这个条件下,不是被改成恒不触发。
    """
    fn = next(n for n in ast.walk(ast.parse(_read(API)))
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "admin_pricing_sku_put")
    guarded = [n for n in ast.walk(fn) if isinstance(n, ast.If)
               and re.fullmatch(r"not _is_standard_price\(.+\)", ast.unparse(n.test))
               and any(isinstance(x, ast.Raise) for x in ast.walk(n))]
    assert len(guarded) == 1, (
        f"400 不再挂在 `not _is_standard_price(...)` 上(命中 {len(guarded)} 处)—— "
        f"要么闸门被拆了,要么条件被改成别的东西。")


# ══ y04 · 🔴 自证:上面每条判定真的有牙 ════════════════════════════
def test_y04_the_checks_actually_bite():
    src = _read(API)
    cases = []

    # ① 任一处退回手写精确等式
    p1 = src.replace("is_standard = _is_standard_price(points, old_wholesale, numer, denom)",
                     "is_standard = points * numer == old_wholesale * denom", 1)
    cases.append(("退回手写等式", p1, lambda s: len(hand_written_ratio_comparisons(s)) > 0))

    # ② 谓词自己重实现 ceil(不再委托定价规则)
    p2 = src.replace("    return int(wholesale) == calc_factory_cents(int(points), numer, denom)",
                     "    return int(wholesale) == (int(points) * numer + denom - 1) // denom", 1)
    cases.append(("谓词重实现 ceil", p2, lambda s: len(
        [n for n in ast.walk(next(x for x in ast.parse(s).body
                                  if isinstance(x, ast.FunctionDef) and x.name == "_is_standard_price"))
         if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
         and n.func.id == "calc_factory_cents"]) != 1))

    # ③ 400 报错退回「保持等式」的老措辞
    p3 = src.replace('f"把 wholesale_cents 改成 {_want} 即可保存;"',
                     'f"必须同时改 points_granted 和 wholesale_cents 保持等式;"', 1)
    cases.append(("报错退回老措辞", p3,
                  lambda s: "保持等式" in s and "_want} 即可保存" not in s))

    # ④ 闸门被拆(条件恒假)
    p4 = src.replace("        if not _is_standard_price(final_points, final_wholesale, _numer, _denom):",
                     "        if False:", 1)
    cases.append(("闸门恒假", p4, lambda s: not [
        n for n in ast.walk(next(x for x in ast.walk(ast.parse(s))
                                 if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef))
                                 and x.name == "admin_pricing_sku_put"))
        if isinstance(n, ast.If) and re.fullmatch(r"not _is_standard_price\(.+\)", ast.unparse(n.test))
        and any(isinstance(y, ast.Raise) for y in ast.walk(n))]))

    for name, poisoned, detects in cases:
        assert poisoned != src, (
            f"【{name}】这一发**毒没下成**(字节与原文一致)—— 锚点漂了。\n"
            f"    这跟「锁没牙」读数同形,先判这个,别下「锁没牙」的结论。")
        ast.parse(poisoned)
        assert detects(poisoned), f"【{name}】毒下成了,但判定件**没抓到** —— 这条锁没牙"

    # 反向对照:未注毒的真源码全绿,否则上面的红说明不了任何事
    assert not hand_written_ratio_comparisons(src)
    assert len(cases) == 4, f"自证发数 {len(cases)}(改坏法数量时同笔改这里)"
