# -*- coding: utf-8 -*-
"""WO_241 乙 · 「生成标题」取价失败统一 fail-closed。

改前两处对**同一种失败**处置相反:
  · `api_generate_titles`   `(_topic_pricing_row or {}).get("cost_points", 390)`
    ⇒ 取价没走通就**静默按写死的 390 扣钱**(而 390 是 05-24 前的错价副本);
  · `api_regenerate_titles` 裸 `get_feature_pricing("topic_gen")`
    ⇒ 查不到行 `raise ValueError` ⇒ **500**「服务器错误」。

同一个功能、同一个价、同一种失败,一个静默收错钱、一个崩,
而**「哪种是本意」从代码里读不出来**。

Owner 09-19 裁定统一成第三种(`08_billing.md:427`「必须 fail-closed」、
`:616`「证据未知时 fail-closed」):**取不到价 ⇒ 503 `PRICING_UNAVAILABLE`,
不出确认卡、不扣费、不用默认值。**
"""
from __future__ import annotations

import ast
import io
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

SERVER = REPO / "server.py"
TITLE_HANDLERS = ("api_generate_titles", "api_regenerate_titles")


def _tree():
    return ast.parse(io.open(SERVER, encoding="utf-8").read(), "server.py")


def _fn(name):
    for n in ast.walk(_tree()):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def _code_only(fn):
    """剥掉 docstring 再 unparse —— **文本计数分不出代码与说明**。

    历史说明里正当地会提到旧价(「改前是按 390 扣钱」),
    那不是"代码里还有写死的价"。数之前先把它摘出去。
    """
    body = list(fn.body)
    if body:
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            body = body[1:]
    return " ".join(ast.unparse(n) for n in body)


# ══════════════════════════════════════════════════════════════════
# 一、行为:取不到价 ⇒ 503,不扣
# ══════════════════════════════════════════════════════════════════

def _guard():
    import server
    return server._require_topic_pricing


@pytest.mark.parametrize("row", [
    None,                       # 取价返回 None
    {},                         # 空 dict
    {"other": 1},               # 缺 cost_points
    {"cost_points": None},      # 键在,值是 None
    {"cost_points": "abc"},     # 键在,值不是数
    {"cost_points": -5},        # 负价:不是"暂时取不到",但同样不能拿去扣钱
])
def test_every_unusable_pricing_row_raises_503(row):
    """🔴 一律 503 `PRICING_UNAVAILABLE` —— **不许回落到任何默认值**。"""
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _guard()(row, where="test")
    assert ei.value.status_code == 503, (row, ei.value.status_code)
    assert ei.value.detail["code"] == "PRICING_UNAVAILABLE", (row, ei.value.detail)


@pytest.mark.parametrize("points", [0, 1, 80, 390, 1040])
def test_a_usable_row_passes_through_untouched(points):
    """🔴 反向臂:能取到就原样返回,**包括 0**(0 是「免费」,不是「取不到」)。

    少了这一条,把守卫写成"一律 503"也全绿 —— 而那会把生成标题整个打死。
    """
    assert _guard()({"cost_points": points}, where="test") == points


def test_the_503_carries_retry_after():
    """「暂时取不到」要让客户端知道可以再来 —— 这和"你输错了"不是一回事。"""
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _guard()(None, where="test")
    assert (ei.value.headers or {}).get("Retry-After"), "503 没带 Retry-After"


# ══════════════════════════════════════════════════════════════════
# 二、两处都走同一个单点,且都不再有默认值
# ══════════════════════════════════════════════════════════════════

#: 每个 handler 里**应有几处**取价 —— 连"几处"一起钉,别只钉"调过"。
#: 🔴 `api_generate_titles` 是 **2**:个人路径一处 + **组织路径一处**。
#:    工单点名「两处」指的是两个 handler;而同一个函数里组织路径还有一次取价,
#:    改前是 `503` + 裸字符串 detail(无 code)且 `int(...)` 仍可能 500。
#:    是本包 `test_the_guard_is_not_reachable_around` 把它抓出来的。
GUARD_CALLS = {"api_generate_titles": 2, "api_regenerate_titles": 1}


@pytest.mark.parametrize("name", TITLE_HANDLERS)
def test_both_title_paths_go_through_the_single_guard(name):
    """🔴 同一个谓词只写一处 —— 各写一遍迟早有一处跟不上。

    连**处数**一起钉:少一处 = 有条路绕开了守卫,多一处 = 有人又加了一条取价路。
    """
    fn = _fn(name)
    assert fn is not None, "找不到 %s —— 判据前提变了" % name
    calls = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "_require_topic_pricing"]
    assert len(calls) == GUARD_CALLS[name], (
        "%s 调守卫 %d 次(应 %d 次),行 %s" % (name, len(calls), GUARD_CALLS[name], calls))


@pytest.mark.parametrize("name", TITLE_HANDLERS)
def test_no_hardcoded_390_left_in_either_title_path(name):
    """🔴 工单判据逐字:生成标题两处的 `390` 为 **0**。

    只数**代码**(见 `_code_only`):docstring 里讲历史时提到旧价是正当的。
    """
    assert "390" not in _code_only(_fn(name)), (
        "%s 的代码里还有写死的 390" % name)


@pytest.mark.parametrize("name", TITLE_HANDLERS)
def test_neither_title_path_still_uses_a_priced_default(name):
    """🔴 没有默认值了 ⇒ 也不该再有 `priced(...)` 的发射点。

    这是「被观测的对象消失了」,不是「把锁改小」——
    发射面花名册同步由 `tests/fallback_observable_2026_09_18` 那条钉住(19 → 17)。
    """
    fn = _fn(name)
    hits = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
            and (getattr(n.func, "id", None) or getattr(n.func, "attr", None))
            in ("priced", "_fb_priced")]
    assert not hits, "%s 还留着 priced() 默认值发射点,行 %s" % (name, hits)


def test_the_guard_is_not_reachable_around():
    """🔴 守卫之外不许再有第二条取 `cost_points` 的路。

    否则"统一"只是多了一个函数,而旧路还开着 ——
    本仓 the-fix-that-was-never-wired-guards-nothing-but-looks-like-it-does。
    """
    for name in TITLE_HANDLERS:
        src = _code_only(_fn(name))
        assert "cost_points" not in src, (
            "%s 里还有直接读 cost_points 的地方 —— 它绕开了守卫" % name)
