# -*- coding: utf-8 -*-
"""WO_218-c1 · 「生成标题」的计费预估:后端算一次,两条计费路径与前端看到的是同一个数。

工单事实:`POST /api/writing/generate-titles` 按 `topic_gen` **真扣真客户算力**
(组织路径 `reserve_charge`、个人路径 `deduct_points`,两条都是 关键词数 × 基价),
而写作大厅那个按钮**一个数都不显示** —— 违反元指令 2「按钮级确认扣费」与
Owner 09-13「不直观、要人猜的展示不允许」。

🔴 本单只做后端半:把「本次将扣 N(上限 M)」放进回包,**前端不算钱**
(08_billing / 元指令 1)。前端半是 218-a1(A)。
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

from services.topic_gen_charge import (            # noqa: E402
    FEATURE_CODE, extra_points, preview, total_points,
)


@pytest.mark.parametrize("base,count,expected", [
    (80, 1, 80),        # 单关键词 = 基价
    (80, 3, 240),
    (80, 7, 560),
    (130, 4, 520),      # 换个基价,防"把 80 写死进公式"
])
def test_total_is_base_times_keyword_count(base, count, expected):
    """实扣 = 基价 × 关键词数。

    🔴 基价取两个不同值:只用 80 的话,把公式写成 `80 * count` 也全绿
    (本仓 a-criterion-built-from-the-constant-pins-the-mechanism-not-the-number)。
    """
    assert total_points(base, count) == expected


@pytest.mark.parametrize("base,count", [(80, 1), (80, 5), (130, 3)])
def test_the_two_billing_paths_add_extra_on_top_of_base(base, count):
    """🔴 两条计费路径都是「基价 + extra」,所以 extra 必须正好补足差额。

    · 组织路径 `reserve_charge(..., dynamic_ceiling_extra_points=extra)`
    · 个人路径 `deduct_points(..., extra_cost=extra)`
    这一条钉的是 `extra_points` 与 `total_points` **互相自洽** ——
    它们分家时,交给前端的数会和实扣对不上,而两边各自看都"对"。
    """
    assert base + extra_points(base, count) == total_points(base, count)


def test_preview_says_the_same_number_it_charges():
    """🔴 交给前端显示的数 == 实扣的数。本单的全部意义在这一条。"""
    for base, count in ((80, 1), (80, 6), (130, 2)):
        card = preview(base, count)
        assert card["estimated_points"] == total_points(base, count), card
        assert card["ceiling_points"] >= card["estimated_points"], card
        assert card["base_points"] == base and card["keyword_count"] == count, card
        assert card["feature_code"] == FEATURE_CODE, card


def test_the_ceiling_is_never_lower_than_what_will_be_charged():
    """工单口径逐字:「有上限时显示上限,**不许显示比实扣少的数**」。

    显示得比实扣少,比不显示更糟 —— 客户按那个数同意了扣费。
    """
    for base in (0, 1, 80, 130, 999):
        for count in (1, 2, 9, 50):
            card = preview(base, count)
            assert card["ceiling_points"] >= total_points(base, count), (base, count, card)


@pytest.mark.parametrize("count", [0, -3])
def test_a_degenerate_keyword_count_still_charges_one_unit(count):
    """关键词数为 0/负 时按 1 算 —— 与 server 里 `max(1, len(keywords))` 同口径。

    🔴 不许回 0:回 0 会让按钮显示「本次 0 算力」,而后端仍按 1 份扣。
    """
    assert total_points(80, count) == 80
    assert preview(80, count)["keyword_count"] == 1


# ── 接线腿:server.py 真的用了它,而且只取一次价 ──────────────────────

def _server_tree():
    return ast.parse(io.open(REPO / "server.py", encoding="utf-8").read(), "server.py")


def _generate_titles_fn():
    for node in ast.walk(_server_tree()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == "api_generate_titles":
            return node
    return None


def test_generate_titles_returns_the_charge_field():
    """🔴 回包里真的有 `charge` —— 没有它,前端还是只能自己算或者不显示。"""
    fn = _generate_titles_fn()
    assert fn is not None, "找不到 api_generate_titles —— 判据前提变了"
    keys = {
        k.value for n in ast.walk(fn) if isinstance(n, ast.Dict)
        for k in n.keys if isinstance(k, ast.Constant)
    }
    assert "charge" in keys, "回包没有 charge 字段"


def test_the_price_is_looked_up_exactly_once_in_that_function():
    """🔴 一个函数里只取一次价。

    取两次的话,两处迟早跟不上彼此 —— 而客户看到的那个数来自其中一处。
    (改前这个函数里确实有两处 `get_feature_pricing("topic_gen")`。)
    """
    fn = _generate_titles_fn()

    # 🔴 **先解别名再数**。函数里是 `from db.wallet_db import get_feature_pricing
    #    as _get_topic_pricing` —— 按调用名找 `get_feature_pricing` 会数出 **0 次**
    #    而不是 1 次,读起来像"根本没取价"。我第一版就是这么写的,当场红。
    #    这正是本仓那条"按名字认身份"的又一次:别名一换,名字锚就瞎。
    local_names = {"get_feature_pricing"}
    for n in ast.walk(fn):
        if isinstance(n, ast.ImportFrom):
            for a in n.names:
                if a.name == "get_feature_pricing":
                    local_names.add(a.asname or a.name)
    calls = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and (getattr(n.func, "id", None) or getattr(n.func, "attr", None)) in local_names
    ]
    assert len(calls) == 1, "取价 %d 次(应为 1 次),行 %s · 解析到的别名 %s" % (
        len(calls), [n.lineno for n in calls], sorted(local_names))


def test_the_lookup_detector_is_not_fooled_by_an_alias():
    """🔴 正样本臂:同一个探测器对**别名导入**的写法必须认得出来。

    没有这一条,上面那条的 `== 1` 可能只是"什么都没数到"。
    """
    sample = "\n".join([
        "def f():",
        "    from db.wallet_db import get_feature_pricing as _p",
        "    return _p('topic_gen')",
    ])
    tree = ast.parse(sample)
    fn = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)][0]
    names = {"get_feature_pricing"}
    for n in ast.walk(fn):
        if isinstance(n, ast.ImportFrom):
            for a in n.names:
                if a.name == "get_feature_pricing":
                    names.add(a.asname or a.name)
    hits = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
            and (getattr(n.func, "id", None) or getattr(n.func, "attr", None)) in names]
    assert len(hits) == 1, (sorted(names), hits)


def test_both_paths_derive_extra_from_the_shared_helper():
    """🔴 两条计费路径的 extra 都来自同一个 helper,不各写各的乘法。

    没有这一条,谁把其中一处改回 `(n-1)*base` 也不会有判据变红 ——
    而那正是「交给前端的数」与「实扣」分家的入口。
    """
    fn = _generate_titles_fn()
    src = ast.unparse(fn)
    assert src.count("_topic_extra_points(") >= 2, (
        "两条计费路径没有都用共享 helper 算 extra")
    assert "(_gen_titles_multiplier - 1) * " not in src, (
        "还有就地重算的乘法 —— 那一处迟早和前端看到的数分家")
