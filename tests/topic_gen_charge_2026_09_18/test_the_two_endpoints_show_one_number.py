# -*- coding: utf-8 -*-
"""WO_218-c1b · 「点击前看到的数」与「实扣的数」必须是同一次计算。

🔴 **这一格是整单的目的**,其余判据都是它的支撑。

背景:后端交了字段、前端没接 —— 或者接了,但两边各算一份而碰巧现在一样 ——
是本仓反复出现的那一类失败:**两边各自都绿,只有页面上看得见**。
所以本包不满足于"两个端点都有 charge",而是钉:
它们共用**同一个函数、同一个份数定义、同一个计费判断、同一份 keywords**。
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
    charge_card, is_billable, keyword_count, preview, total_points,
)

SERVER = REPO / "server.py"
DETAIL_FN = "api_get_writing_project_detail"
TITLES_FN = "api_generate_titles"


def _fn(name):
    tree = ast.parse(io.open(SERVER, encoding="utf-8").read(), "server.py")
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


# ══════════════════════════════════════════════════════════════════
# 一、同一个数(纯函数层)
# ══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("base,n_keywords", [(80, 1), (80, 4), (80, 17), (130, 3)])
def test_the_card_says_exactly_what_will_be_charged(base, n_keywords):
    """🔴 详情端点显示的 `estimated_points` == generate-titles 实扣。

    两边都是 `charge_card(keywords, ...)`,所以这一条等价于:
    同一份 keywords 进去,出来的数和 `total_points` 一致。
    """
    kws = [{"id": i} for i in range(n_keywords)]
    card = charge_card(kws, billable=True, lookup=lambda _c: {"cost_points": base})
    assert card["estimated_points"] == total_points(base, n_keywords)
    assert card["estimated_points"] == preview(base, n_keywords)["estimated_points"]
    assert card["keyword_count"] == n_keywords
    assert card["ceiling_points"] >= card["estimated_points"]


def test_the_number_changes_when_the_keyword_count_changes():
    """🔴 反臂:加了一个关键词,那个数**必须变**。

    不变 = 缓存住了一个过期价 —— 比不显示更糟,客户按过期价同意了扣费。
    """
    lookup = lambda _c: {"cost_points": 80}          # noqa: E731
    before = charge_card([{"id": 1}, {"id": 2}], billable=True, lookup=lookup)
    after = charge_card([{"id": 1}, {"id": 2}, {"id": 3}], billable=True, lookup=lookup)
    assert after["estimated_points"] != before["estimated_points"]
    assert after["keyword_count"] == before["keyword_count"] + 1


def test_the_number_does_not_change_when_the_keyword_count_does_not():
    """🔴 反臂之二(Review 加的):份数没变时那个数**不许变**。

    少了这一条,"每次都重取一遍"会把过期问题**掩盖**成"反正总在刷",
    而下一次真过期时没有任何东西看得出来。
    """
    lookup = lambda _c: {"cost_points": 80}          # noqa: E731
    kws = [{"id": 1}, {"id": 2}]
    first = charge_card(kws, billable=True, lookup=lookup)
    second = charge_card(list(kws), billable=True, lookup=lookup)
    assert first == second


def test_not_billable_means_no_price_at_all_not_a_price_of_zero():
    """🔴 `None` ≠ `0`。`None` = 本次不走计费(不显示价);`0` = 免费。"""
    assert charge_card([{"id": 1}], billable=False,
                       lookup=lambda _c: {"cost_points": 80}) is None
    free = charge_card([{"id": 1}], billable=True, lookup=lambda _c: {"cost_points": 0})
    assert free is not None and free["estimated_points"] == 0


def test_an_unavailable_catalog_row_yields_none_not_a_made_up_price():
    """取不到价 ⇒ `None`(不显示),**不许编一个数**。"""
    assert charge_card([{"id": 1}], billable=True, lookup=lambda _c: None) is None


@pytest.mark.parametrize("keywords,expected", [
    (None, 0), ([], 0), ([{"id": 1}], 1), ([{"id": 1}, {"id": 2}], 2),
])
def test_the_keyword_count_definition_lives_in_one_place(keywords, expected):
    """份数定义 = 要出题的词数(WO_317 第三笔起不设下限 1),两个端点共用。"""
    assert keyword_count(keywords) == expected


@pytest.mark.parametrize("org,uid,expected", [
    (False, None, False), (False, 7, True), (True, None, True), (True, 7, True),
])
def test_the_billable_predicate_lives_in_one_place(org, uid, expected):
    assert is_billable(is_organization_member=org, bill_user_id=uid) is expected


# ══════════════════════════════════════════════════════════════════
# 二、接线腿:两个端点真的都走了同一个函数
# ══════════════════════════════════════════════════════════════════

def _calls_named(fn, names):
    out = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            nm = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            if nm in names:
                out.append(n.lineno)
    return out


def test_both_endpoints_call_the_same_charge_helper():
    """🔴 「两边各写一遍、碰巧现在一样」正是本单要消灭的东西。"""
    for name in (DETAIL_FN, TITLES_FN):
        fn = _fn(name)
        assert fn is not None, "找不到 %s —— 判据前提变了" % name
        hits = _calls_named(fn, {"charge_card", "_topic_charge_card"})
        # 🔴 只钉「走了同一个 helper」,**不钉处数**。
        #    详情端点自 WO_241 丙补起有两处(整表 + 新词子集),处数钉在
        #    `tests/topic_gen_charge_basis_2026_09_19` —— 那是它的主人。
        #    同一个事实在两个包各钉一遍,下一次改动必然一处红一处绿
        #    (我今天已经在守卫处数上栽过一次,这是第二次同形)。
        assert hits, "%s 没有走 charge_card" % name


def test_the_detail_endpoint_actually_returns_the_field():
    """后端"算了"不等于"交出去了"。"""
    fn = _fn(DETAIL_FN)
    keys = {k.value for n in ast.walk(fn) if isinstance(n, ast.Dict)
            for k in n.keys if isinstance(k, ast.Constant)}
    assert "charge" in keys, "详情端点回包里没有 charge —— 前端拿不到"


def test_neither_endpoint_recomputes_the_multiplication_itself():
    """🔴 谁也不许就地再乘一遍 —— 那正是两边分家的入口。"""
    for name in (DETAIL_FN, TITLES_FN):
        src = ast.unparse(_fn(name))
        assert "len(detail['keywords'])" not in src.replace('"', "'"), (
            "%s 里还有就地数关键词的写法,没走 keyword_count()" % name)


def test_the_price_is_still_looked_up_exactly_once_in_generate_titles():
    """接线改造不许把"只取一次价"弄丢(别名照旧要先解)。"""
    fn = _fn(TITLES_FN)
    local = {"get_feature_pricing"}
    for n in ast.walk(fn):
        if isinstance(n, ast.ImportFrom):
            for a in n.names:
                if a.name == "get_feature_pricing":
                    local.add(a.asname or a.name)
    hits = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
            and (getattr(n.func, "id", None) or getattr(n.func, "attr", None)) in local]
    assert len(hits) == 1, "取价 %d 次(应 1 次),行 %s" % (len(hits), hits)


def test_the_detail_endpoint_never_breaks_the_page_for_a_price():
    """🔴 取不到价不许把只读详情打成 500。

    为了显示一个数而让整个项目页挂掉,方向反了。
    """
    fn = _fn(DETAIL_FN)
    guarded = False
    for n in ast.walk(fn):
        if isinstance(n, ast.Try):
            body = ast.unparse(n.body)
            if "charge_card" in body or "_topic_charge_card" in body:
                guarded = True
    assert guarded, "详情端点的取价没有包在 try 里 —— 取价一崩整页 500"


# ══════════════════════════════════════════════════════════════════
# 三、覆盖面:charge 不是"这颗按钮的价"
# ══════════════════════════════════════════════════════════════════

def test_the_contract_states_which_face_is_free():
    """🔴 契约必须写清**哪一面免费**。

    ⚠️ [WO_241 丙 2026-09-19 退役重写] 本条原来断言的是
       「`isNewKwOnly` 那一面一分不收」—— 那个映射**已被 Owner/Review 裁掉**:
       新词改走批量端点**并显示价**,免费只剩「缺题补救」那一面。
       「仍然存在」型断言必须**与修法同班退役**,否则它会在改对之后继续报红,
       下一个人只能靠删它来过 —— 那时真正该守的东西也一起没人看了。
       (本仓 a-lock-that-asserts-a-defect-is-still-open-must-be-retired-with-the-fix。)

    现在为真、也是该守的:免费的是**补救**那一面,而且它**要出示证明**。
    """
    doc = io.open(REPO / "services" / "topic_gen_charge.py", encoding="utf-8").read()
    assert "generate-topic" in doc, "契约没写明补救那条路径"
    assert "isNewKwOnly" in doc, "契约没写明按钮的第二个面孔"
    for phrase in ("已经付过费", "从没付过费", "TITLE_RECOVERY_NOT_AUTHORIZED"):
        assert phrase in doc, "契约缺这句:%s" % phrase


def test_the_per_keyword_generate_topic_route_is_still_uncharged():
    """🔴 钉住"它现在不收钱"这个**事实**。

    哪天有人给它加上计费,这条会红 —— 那时该做的是**同时**更新契约里那段
    覆盖面声明,而不是让前端继续按老理解显示价。
    """
    src = io.open(SERVER, encoding="utf-8").read()
    tree = ast.parse(src, "server.py")
    target = None
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in n.decorator_list:
            if isinstance(dec, ast.Call) and dec.args and \
                    isinstance(dec.args[0], ast.Constant) and \
                    "keywords/{keyword_id}/generate-topic" in str(dec.args[0].value):
                target = n
    assert target is not None, "找不到逐词补生成端点 —— 判据前提变了"

    # 🔴 不能只扫端点函数自身。第一版就是这么写的,而它调的外部 worker
    #    `_auto_generate_topics_for_new_keyword` **不在扫描范围内** ——
    #    有人把计费加进 worker,这条锁不会红(本仓 nobody-verified-the-line-where-a-calls-b)。
    #    改成沿 server.py 内部的调用边做闭包:端点 → 它调的 → 它们调的 → …
    #    这样管的是「这条路径上的任何一处」,不是「我点名的那一处」。
    defs = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.setdefault(n.name, n)

    seen, queue, reached = set(), [target], []
    while queue:
        fn = queue.pop()
        if id(fn) in seen:
            continue
        seen.add(id(fn))
        reached.append(fn)
        for n in ast.walk(fn):
            if isinstance(n, ast.Call):
                callee = getattr(n.func, "id", None)
                if callee in defs and id(defs[callee]) not in seen:
                    queue.append(defs[callee])

    assert any(f.name == "_auto_generate_topics_for_new_keyword" for f in reached), (
        "闭包没走到 worker `_auto_generate_topics_for_new_keyword` —— "
        "要么它改名了,要么端点不再调它;两种情况这条锁都失去了它守的东西")

    BILLING = ("deduct_points", "check_balance_only", "reserve_charge",
               "charge_subscription_entitlement", "_bill_ctx", "_bill_feature_ctx",
               "freeze_points", "consume_subscription_entitlement")
    hits = []
    for fn in reached:
        src = ast.unparse(fn)
        for billing in BILLING:
            if billing in src:
                hits.append((fn.name, billing))
    assert not hits, (
        "逐词补生成这条路径上出现了计费调用 %s —— 它不再免费,"
        "契约里那段覆盖面声明必须同步更新(否则前端会继续按「这一面免费」显示)" % (hits,))
