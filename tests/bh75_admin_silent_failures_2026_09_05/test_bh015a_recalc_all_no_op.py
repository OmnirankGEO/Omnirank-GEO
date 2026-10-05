# -*- coding: utf-8 -*-
"""BH-015a · 批量重算「一行都没动」时必须说得出为什么。

## 缺陷是什么(实测,不是抄工单)

`POST /api/admin/pricing/skus/recalc-all` 在**默认参数**下
(`include_non_standard=False`)`updated_count` **结构性恒为 0**,
而端点照样返回 `{"success": true}`,前端弹「已重算 · 实际变动 0 个算力包」。

admin 会把这句话读成「已经都是最新价了」。真相是「这个模式下永远不会动」。

## 结构:两个条件互斥

    能走到 UPDATE 的行必须同时满足
      (a) `_is_standard_price(points, old, numer, denom)`  ← 否则被当活动包/人工谈价跳过
      (b) `calc_factory_cents(points, numer, denom) != old` ← 否则 `if new != old` 不成立
    而 BH-015b 之后 (a) 的定义**就是** `old == calc_factory_cents(...)` ⇒ (b) 恒假。

下方 `x01` 是这个证明的可执行版,并且它**导入代码里真正那个谓词**,
不是我在判据里重打一遍 —— 谓词改了它会跟着改,不会绿着绿着就与代码脱钩。

> BH-015b 之前 (a) 写作 `points × numer == old × denom`(精确等式),
> 结论同样成立(整除 ⇒ ceil 恰等)。x01 的第一版就是照那个形式手写枚举的,
> 换了谓词之后它**照样全绿** —— 绿不制造任何「要不要去看」的问题,
> 所以现在改成从代码取谓词。

更糟的是这个端点存在的**意义**正是「系数改了,拿新系数重算旧价」——
而按新系数衡量,所有按旧系数定的价都是 `non_standard` ⇒ **全被跳过**。
「需要更新」与「是标准价」在同一套系数下互斥。

## 为什么修法是「说清楚」而不是「真正更新」

`sku_templates` 生产 schema(2026-08-19 快照)13 列里**没有**任何标记能区分
「人工谈价」与「按旧系数算出的价」。加列也回填不了历史行 —— 没有可依据的数据。
所以 D1 做的是**如实说出发生了什么**;真正的谓词修正是 BH-015b,单独处置。

## 🔴 静态 ≠ 运行时

全树 18 个种子 SKU 全是 13 的倍数、全部通过判定 ⇒ **当前目录 0 命中**。
「92% 被跳过」是「points 均匀取值空间」的属性,不是「当前目录」的属性。
本组判据钉的是**规则**,不是「今天这批数据」—— 所以它不会随种子数据变化而失效。
"""
from __future__ import annotations

import ast
import contextlib
import importlib
import io
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
API = ROOT / "api" / "admin_factory_api.py"
FE_API = ROOT / "frontend" / "src" / "lib" / "v35w2Api.ts"
FE_PAGE = ROOT / "frontend" / "src" / "pages" / "Admin" / "PricingCenter.tsx"

#: 系数分母 —— 机械覆盖「整除/不整除」「大/小」几类,不是随手写两组。
COEFFS = [(225, 325), (1, 1), (3, 4), (7, 11), (99, 100), (13, 17), (325, 200)]


def _read(p: pathlib.Path) -> str:
    return io.open(p, encoding="utf-8", newline="").read()


def _mod():
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return importlib.import_module("api.admin_factory_api")


def _fn(src: str, name: str):
    return next((n for n in ast.walk(ast.parse(src))
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name), None)


# ══ x01 · 把「结构性不可达」写成会自己更新答案的判据 ═══════════════
def test_x01_default_mode_update_branch_is_provably_unreachable():
    """🔴 这条**不是** `assert True`,也不是写死 `return []`。

    它拿**真** `calc_factory_cents` 机械枚举反例。
    哪天取整规则变了(ceil → round/floor,或加最小价保护),
    这里会**自动**开始返回样本 ⇒ 本条转红,提醒去改 `no_op_reason` 的措辞 ——
    因为那段话届时就成了谎话。

    「结构上不可能」是一个很强的断言,必须配一个**可执行**的证明。
    """
    with contextlib.redirect_stdout(io.StringIO()):
        from services.agent_pricing import calc_factory_cents
    #: 🔴 谓词**从代码取**,不在判据里重打一遍。
    #:    重打的那份不会跟着代码改 —— 它会绿着绿着就与被测对象脱钩(实测发生过一次)。
    is_standard = _mod()._is_standard_price

    counterexamples, sampled = [], 0
    for numer, denom in COEFFS:
        for points in range(4000):
            # old 取遍「规则值」及其邻域 —— 只喂规则值等于替被测谓词把答案先算好了
            for old in {calc_factory_cents(points, numer, denom) + d for d in (-1, 0, 1)}:
                if old < 0:
                    continue
                if not is_standard(points, old, numer, denom):   # (a) 不满足 ⇒ 会被跳过
                    continue
                sampled += 1
                if calc_factory_cents(points, numer, denom) != old:   # (b)
                    counterexamples.append((numer, denom, points, old))

    assert sampled > 1000, (
        f"只取到 {sampled} 个满足 (a) 的样本 —— 分母太小,本条没有说服力。"
        f"「枚举不到反例」与「枚举范围太窄」在读数上同形。")
    assert not counterexamples, (
        f"默认模式下 UPDATE 分支**变得可达了**(反例 {len(counterexamples)} 个,"
        f"前 3 个 {counterexamples[:3]})。\n"
        f"    说明 `calc_factory_cents` 的取整规则、或 `_is_standard_price` 的定义变了。\n"
        f"    `_recalc_summary` 里那句「没被跳过的行……再算一次还是同一个数」\n"
        f"    从此是**谎话**,必须同笔改掉。")

    # 🔴 本条的前提:recalc_all 的跳过分支用的**就是**上面这个谓词。
    #    不钉住它,谓词哪天在端点里被换成别的,x01 会继续绿着测一个没人用的函数。
    fn = _fn(_read(API), "admin_pricing_sku_recalc_all")
    assigns = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
               and any(isinstance(x, ast.Name) and x.id == "is_standard" for x in n.targets)]
    assert len(assigns) == 1, f"recalc_all 里 `is_standard` 赋值 {len(assigns)} 处(应 1)"
    rhs = ast.unparse(assigns[0].value)
    assert re.fullmatch(r"_is_standard_price\(.+\)", rhs), (
        f"recalc_all 的跳过谓词不再是 `_is_standard_price(...)`,而是 `{rhs}`。\n"
        f"    上面那段枚举于是在证一个**端点已经不用**的函数的性质 —— "
        f"绿了也说明不了任何事。")


# ══ x02 · 零更新时必须给出原因(分母 = 机械构造的四种 0 情形) ════════
@pytest.mark.parametrize("case,items,inc,only_active", [
    ("空集合", [], False, False),
    ("空集合·仅上架", [], False, True),
    ("默认模式·全标准", [{"id": 1, "old": 100, "new": 100}], False, False),
    ("默认模式·有跳过且确实要改",
     [{"id": 1, "old": 999, "new": 999, "skipped": True, "would_change": True}], False, False),
    ("默认模式·有跳过但规则值相同",
     [{"id": 1, "old": 100, "new": 100, "skipped": True, "would_change": False}], False, False),
    ("强制覆盖·全等", [{"id": 1, "old": 100, "new": 100}], True, False),
])
def test_x02_zero_update_always_carries_a_reason(case, items, inc, only_active):
    s = _mod()._recalc_summary(items, 225, 325, inc, only_active)
    assert s["updated_count"] == 0, f"【{case}】这个夹具本身就不是零更新,本条无意义"
    assert s["no_op_reason"], (
        f"【{case}】`updated_count == 0` 却没有 `no_op_reason` —— \n"
        f"    这正是缺陷现场:admin 只看到「变动 0 个」,会读成「已经是最新的了」。")
    assert len(s["no_op_reason"]) > 20, f"【{case}】原因只有 {len(s['no_op_reason'])} 字,等于没说"


def test_x02b_reason_is_absent_when_something_actually_changed():
    """🔴 反臂。没有这条,`no_op_reason` 可以永远是一句固定的话,
    上面每个参数化用例照样全绿 —— 那证明不了它在**回答**什么。"""
    s = _mod()._recalc_summary([{"id": 1, "old": 100, "new": 123}], 225, 325, False, False)
    assert s["updated_count"] == 1
    assert s["no_op_reason"] is None, \
        f"确实更新了 1 行,却还在说「为什么没动」:{s['no_op_reason']!r}"


def test_x02c_the_reason_distinguishes_the_two_default_mode_situations():
    """两种默认模式的 0,原因**必须不同** —— 否则这段话没有区分力,
    等于把「没有需要处理的」和「有 3 个需要处理但被跳过了」说成同一件事。"""
    nothing = _mod()._recalc_summary(
        [{"id": 1, "old": 100, "new": 100}], 225, 325, False, False)["no_op_reason"]
    pending = _mod()._recalc_summary(
        [{"id": 1, "old": 100, "new": 100},
         {"id": 2, "old": 999, "new": 999, "skipped": True, "would_change": True}],
        225, 325, False, False)["no_op_reason"]
    assert nothing != pending, "两种情形给了同一句话 —— 这段原因没有区分力"
    assert "1" in pending, f"有 1 个确实需要处理,原因里却没有这个数:{pending!r}"


def test_x02d_skipped_would_change_is_counted_not_guessed():
    s = _mod()._recalc_summary(
        [{"id": i, "old": 999, "new": 999, "skipped": True, "would_change": i % 2 == 0}
         for i in range(10)], 225, 325, False, False)
    assert s["skipped_count"] == 10
    assert s["skipped_would_change"] == 5, \
        f"10 个跳过里 5 个 would_change,却数出 {s['skipped_would_change']}"


# ══ x03 · 接线:端点真的走共用件,且响应真的带原因 ═══════════════════
def test_x03_endpoint_delegates_to_the_shared_summary():
    src = _read(API)
    fn = _fn(src, "admin_pricing_sku_recalc_all")
    assert fn, "`admin_pricing_sku_recalc_all` 不见了"
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "_recalc_summary"]
    assert len(calls) == 1, f"端点里 `_recalc_summary` 调用 {len(calls)} 处(应 1)"

    # 响应字典里 no_op_reason 的值必须**取自那次调用的结果**,不是字面量/另算一遍
    ret = next((n for n in ast.walk(fn) if isinstance(n, ast.Return)), None)
    assert isinstance(ret.value, ast.Dict), "端点不再返回字典字面量,本条的读法要跟着改"
    got = {k.value: ast.unparse(v) for k, v in zip(ret.value.keys, ret.value.values)
           if isinstance(k, ast.Constant)}
    for key in ("no_op_reason", "skipped_would_change", "skipped_count"):
        assert key in got, f"响应里没有 `{key}` —— 前端拿不到,修复停在函数层"
        assert re.fullmatch(r"s\[['\"]" + key + r"['\"]\]", got[key]), (
            f"`{key}` 不是取自 `_recalc_summary` 的结果,而是 `{got[key]}`。\n"
            f"    另算一遍 = 同一谓词写两处,必有一处漂走。")


def test_x03b_skipped_rows_carry_would_change():
    """`no_op_reason` 里那个「其中 N 个确实要改」的 N,来源必须真的存在。"""
    src = _read(API)
    fn = _fn(src, "admin_pricing_sku_recalc_all")
    appends = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Attribute) and n.func.attr == "append"
               and n.args and isinstance(n.args[0], ast.Dict)]
    skip_rows = [d for d in appends
                 if any(isinstance(k, ast.Constant) and k.value == "skipped" for k in d.args[0].keys)]
    assert skip_rows, "找不到构造「被跳过行」的那处 append —— 分母为空,去查发生了什么"
    for d in skip_rows:
        keys = {k.value for k in d.args[0].keys if isinstance(k, ast.Constant)}
        assert "would_change" in keys, (
            f"L{d.lineno} 的被跳过行没带 `would_change` —— "
            f"那 `no_op_reason` 里的数字就没有来源。实测 keys={sorted(keys)}")


def test_x03c_no_dead_skip_counter_left_in_the_endpoint():
    """「这一行被跳过」这个谓词只许有一处 —— 端点内不许再留只写不读的计数器。"""
    fn = _fn(_read(API), "admin_pricing_sku_recalc_all")
    stale = [n.lineno for n in ast.walk(fn)
             if isinstance(n, ast.Name) and n.id == "skipped_count"]
    assert not stale, (
        f"端点内又出现了本地 `skipped_count`(L{stale})。\n"
        f"    汇总已由 `_recalc_summary` 从 items 派生;再维护一个本地计数器 = "
        f"同一谓词两处,必有一处没人验。")


# ══ x04 · 前端接线:原因必须真的被显示出来 ══════════════════════════
def test_x04_frontend_surfaces_the_reason_at_every_call_site():
    """🔴 后端说清楚了、前端不显示 ⇒ 用户看到的仍是「变动 0 个」,修复等于没做。

    分母**机械枚举**:页面里每一处 `pricingRecalcAll(` 调用,
    都必须有对应的 `no_op_reason` 分支 —— 不是「文件里出现过这个词」就算。
    """
    assert "no_op_reason" in _read(FE_API), \
        "`v35w2Api.ts` 的响应类型里没有 `no_op_reason` —— TS 侧读不到这个字段"

    page = _read(FE_PAGE)
    call_sites = len(re.findall(r"pricingRecalcAll\(", page))
    guarded = len(re.findall(r"updated_count\s*===\s*0\s*&&\s*\w+\.no_op_reason", page))
    assert call_sites >= 1, "页面里找不到 `pricingRecalcAll(` 调用 —— 分母为空,本条无意义"
    assert guarded == call_sites, (
        f"页面有 {call_sites} 处批量重算调用,只有 {guarded} 处会显示 `no_op_reason`。\n"
        f"    没被守住的那处,用户看到的还是一句不解释任何事的「变动 0 个」。")


# ══ x05 · 🔴 自证:上面的判定真的有牙 ══════════════════════════════
def test_x05_the_checks_actually_bite():
    """毒下在**合成对象/字符串**上,不动真文件。
    每发先证「确实变了」—— 否则「注毒后仍绿」有四种解释且读数同形。
    """
    m = _mod()
    real = m._recalc_summary

    # ① 把 no_op_reason 恒设为 None(即回到静默返回 0)
    def poisoned_silent(items, numer, denom, inc, oa):
        r = dict(real(items, numer, denom, inc, oa)); r["no_op_reason"] = None; return r
    assert poisoned_silent([], 225, 325, False, False)["no_op_reason"] is None
    assert real([], 225, 325, False, False)["no_op_reason"] is not None, \
        "真实现对空集合就没给原因 —— 上面 x02 的绿是假的,先查这个"

    # ② 把 no_op_reason 设成一句固定话(x02c 必须抓到)
    def poisoned_fixed(items, numer, denom, inc, oa):
        r = dict(real(items, numer, denom, inc, oa))
        if r["no_op_reason"]:
            r["no_op_reason"] = "没有需要更新的算力包。"
        return r
    a = poisoned_fixed([{"id": 1, "old": 100, "new": 100}], 225, 325, False, False)["no_op_reason"]
    b = poisoned_fixed([{"id": 1, "old": 100, "new": 100},
                        {"id": 2, "old": 999, "new": 999, "skipped": True, "would_change": True}],
                       225, 325, False, False)["no_op_reason"]
    assert a == b, "固定话术这一发没下成"
    ra = real([{"id": 1, "old": 100, "new": 100}], 225, 325, False, False)["no_op_reason"]
    rb = real([{"id": 1, "old": 100, "new": 100},
               {"id": 2, "old": 999, "new": 999, "skipped": True, "would_change": True}],
              225, 325, False, False)["no_op_reason"]
    assert ra != rb, "真实现对这两种情形给了同一句话 —— x02c 抓不到区分力缺失"

    # ③ 前端分母锁:少守一处必须红
    page = _read(FE_PAGE)
    holed = page.replace("r2.updated_count === 0 && r2.no_op_reason", "false", 1)
    assert holed != page, "前端这一发毒没下成(锚点漂了)—— 先修锚点,别下「锁没牙」的结论"
    assert len(re.findall(r"updated_count\s*===\s*0\s*&&\s*\w+\.no_op_reason", holed)) \
        < len(re.findall(r"pricingRecalcAll\(", holed)), "前端分母锁没牙"
