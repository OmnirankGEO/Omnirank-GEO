# -*- coding: utf-8 -*-
"""WO_241 丙(已裁定的那半)· 计费份数 = **本次真正要出题的词数**。

事实:扣费侧原来是 `max(1, len(detail["keywords"]))` —— **整张关键词表的长度**;
而出题侧 `writing/keyword_topic_generator.py:322` 只为
`_required_article_count(kw) > 0` 的词出题。
⇒ 带 `per_keyword_plan` 把旧词置 0、只留新词时,
**出题只出新词、收费收整张表**(10 个词的项目出 2 个新词 ⇒ 收 10 份)。

🔴 这不是"现在正在超收":前端今天**根本不发** `per_keyword_plan`
(`grep per_keyword_plan frontend/src` = 0 命中)⇒ 整表出题 = 整表收费,
两种算法逐值相等。**它是「按工单那条路去做,上线当天开始超收」** ——
而 WO_241 丙 的 B 方案(按钮走批量端点带子集)做的恰恰就是开始发子集。
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
    charge_card, keyword_count, plannable_keywords,
)

SERVER = REPO / "server.py"


def _kw(i, **extra):
    row = {"id": i}
    row.update(extra)
    return row


# ══════════════════════════════════════════════════════════════════
# 一、基数本身
# ══════════════════════════════════════════════════════════════════

def test_zero_planned_keywords_are_not_charged():
    """🔴 本单的全部意义:置 0 的词不出题,也就不该收它的钱。"""
    kws = [_kw(1, planned_count=3), _kw(2, planned_count=0), _kw(3, planned_count=0)]
    assert keyword_count(kws) == 1, "置 0 的词仍被计费"


def test_the_charge_basis_equals_the_generation_basis():
    """🔴 与出题侧同一口径 —— 这条是「两边不许分家」的直接表达。"""
    cases = [
        [_kw(1), _kw(2), _kw(3)],
        [_kw(1, planned_count=0), _kw(2, planned_count=5)],
        [_kw(1, planned_posts_default=0), _kw(2, planned_posts_default=2)],
        [_kw(1, required_articles=0), _kw(2, required_articles=1)],
        [_kw(1, planned_count=0, planned_posts_default=9)],   # 优先级:planned_count 赢
    ]
    for kws in cases:
        assert keyword_count(kws) == len(plannable_keywords(kws)), kws   # WO_317 第三笔:不设下限 1


def test_an_explicit_zero_is_not_a_missing_value():
    """🔴 显式 0 ≠ 缺失。三个键都要用 `is None` 判缺失,不许用真值判断。

    用真值判断的话 `planned_count=0` 会被当成"没填",落回下一个键 ⇒
    这个词又被计费了,而它一条题都不会出。
    """
    assert keyword_count([_kw(1, planned_count=0, required_articles=7)]) == 0   # WO_317 第三笔:显式 0 ⇒ 0 份
    assert plannable_keywords([_kw(1, planned_count=0, required_articles=7)]) == []


def test_no_plan_means_everything_is_charged_exactly_as_before():
    """🔴 反向臂 · **向后兼容**:不带计划时,新算法与旧算法**逐值相等**。

    这一条是"这次改动不动任何现有扣费"那句话的判据形式。
    少了它,把基数改成别的(比如恒 1)也能让上面几条全绿。
    """
    for n in (1, 2, 5, 17):
        kws = [_kw(i) for i in range(n)]
        assert keyword_count(kws) == len(kws) == n


@pytest.mark.parametrize("kws", [None, [], [_kw(1, planned_count=0)]])
def test_an_empty_batch_counts_zero_and_shows_no_price(kws):
    """[Review 09-28 · WO_317 第三笔] 全被置 0 / 空表 ⇒ 0 份、卡片 None(不显示价)。

    原来按下限 1 份:按钮写「将扣 80」,点了冻 80、出 0 条、退 80。现在 generate-titles
    在冻结之前 400「没有需要出题的关键词」,详情端点给 None。
    """
    assert keyword_count(kws) == 0
    assert charge_card(kws, billable=True, lookup=lambda _c: {"cost_points": 80}) is None


def test_the_card_and_the_basis_agree_on_a_subset():
    """交给前端显示的那张卡,也必须按子集算。"""
    kws = [_kw(1, planned_count=2), _kw(2, planned_count=0), _kw(3, planned_count=0)]
    card = charge_card(kws, billable=True, lookup=lambda _c: {"cost_points": 80})
    assert card["keyword_count"] == 1
    assert card["estimated_points"] == 80


# ══════════════════════════════════════════════════════════════════
# 二、运行期铁律锁:份数 != 真正出题数 ⇒ 拒绝扣费
# ══════════════════════════════════════════════════════════════════

def _fn(name):
    tree = ast.parse(io.open(SERVER, encoding="utf-8").read(), "server.py")
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def test_the_handler_refuses_to_charge_on_a_basis_mismatch():
    """🔴 Review 裁定逐字:不等即拒绝扣费(503,不扣)。

    为什么运行期还要再断言一次 —— 判据钉的是「`keyword_count` 怎么算」,
    这里防的是**出题侧口径哪天改了、计费侧没跟上**:
    那时两个函数各自都对,只有客户多付的钱是错的,
    而静态判据看不见这种**跨模块分家**。
    """
    fn = _fn("api_generate_titles")
    assert fn is not None, "找不到 api_generate_titles"
    # 🔴 不能只查字符串在不在:注毒把判别条件改成 `if False:` 之后,
    #    `CHARGE_BASIS_MISMATCH` 这个串**照样在**(只是那一支永不执行)⇒ 判据全绿。
    #    与 WO_242 里影子目录那次 `if True:` 是同一个形状:
    #    **两个分支都"存在"不等于两个分支都"到得了"**。
    #    所以钉**判别式本身**:那个 if 的条件必须真的在
    #    「计费份数」与「真正出题词数」之间做比较。
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "_require_charge_basis_matches"]
    assert len(calls) == 1, "handler 调铁律锁 %d 次(应 1 次)" % len(calls)
    args = " ".join(ast.unparse(a) for a in calls[0].args)
    assert "_gen_titles_multiplier" in args, "没把计费份数喂给它:%s" % args
    assert "plannable" in args, "没把真正出题的词数喂给它:%s" % args


# ── 闸的**行为**:直接调,不靠 AST 推 ────────────────────────────

def _basis_guard():
    import server
    return server._require_charge_basis_matches


@pytest.mark.parametrize("multiplier,plannable_n", [(2, 1), (10, 2), (1, 3), (5, 0)])
def test_the_guard_actually_refuses_on_mismatch(multiplier, plannable_n):
    """🔴 不等 ⇒ 真的抛 503 CHARGE_BASIS_MISMATCH。

    🔴 这一格是注毒逼出来的:闸原来写在 handler 里,**只能靠 AST 判据看它**,
       而 AST 看不见"它到底会不会拦"。把 `_plannable_now` 换成 `[]` 之后,
       比较照样在、串照样在、判据照样全绿,**而闸的行为已经变了**。
       抽成独立函数就能直接调、直接验。
    """
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _basis_guard()(multiplier, [{"id": i} for i in range(plannable_n)])
    assert ei.value.status_code == 503
    assert ei.value.detail["code"] == "CHARGE_BASIS_MISMATCH"


@pytest.mark.parametrize("plannable_n", [0, 1, 2, 9])
def test_the_guard_lets_a_matching_basis_through(plannable_n):
    """🔴 反向臂:相等就放行。少了它,"一律拦"也能让上面全绿 —— 那会把功能打死。"""
    kws = [{"id": i} for i in range(plannable_n)]
    _basis_guard()(plannable_n, kws)        # 不抛即通过(WO_317 第三笔:份数 == 真实词数,不设下限)


def test_the_mismatch_guard_sits_before_any_charging_call():
    """🔴 「不扣」要靠**排在扣费之前**,不是靠事后退费。"""
    fn = _fn("api_generate_titles")
    lines = {}
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            nm = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            if nm in ("deduct_points", "check_balance_only", "reserve_charge"):
                lines.setdefault(nm, n.lineno)
    # 闸抽成独立函数之后,handler 里的锚是**那次调用**,不再是那个串。
    guard = None
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_require_charge_basis_matches":
            guard = n.lineno
    assert guard is not None, "handler 里找不到对铁律锁的调用"
    assert lines, "这个函数里一个扣费调用都没数到 —— 判据失去参照物"
    assert guard < min(lines.values()), (
        "铁律锁在行 %d,而最早的扣费在行 %d —— 它排在扣费后面,那就不是「不扣」"
        % (guard, min(lines.values())))


def test_the_generation_side_definition_has_not_drifted():
    """🔴 我这份口径是照抄出题侧的。**出题侧改了,这条必须红**。

    否则「两边同口径」会在某天变成一句没人会红的话 ——
    而那正是本单要消灭的那种失败(两边各自都对,只有客户看到的数是错的)。
    """
    src = io.open(REPO / "writing" / "keyword_topic_generator.py",
                  encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)
               and n.name == "_required_article_count"), None)
    assert fn is not None, "出题侧那个函数不见了 —— 我的口径失去了参照物"
    body = ast.unparse(fn)
    for key in ("planned_count", "planned_posts_default", "required_articles"):
        assert key in body, "出题侧不再看 %s —— 计费口径要跟着改" % key
    assert "is not None" in body, "出题侧改用真值判断了 —— 显式 0 的语义变了"


# ══════════════════════════════════════════════════════════════════
# 新词面的子集报价(WO_241 丙补 · Review 裁定 2026-09-19)
# ══════════════════════════════════════════════════════════════════

from services.topic_gen_charge import keywords_without_topics   # noqa: E402

LOOKUP = lambda _c: {"cost_points": 80}          # noqa: E731


def _topic(kid, title=None):
    return {"keyword_id": kid, "optimized_title": title}


def test_a_keyword_with_any_topic_row_is_not_new():
    """🔴 「有没有选题」按 topics 行的 `keyword_id` 判,**不按标题是否为空**。

    受理凭据行(`optimized_title IS NULL`)也算这个词已经进过批次 ——
    它是「缺题的词」,走**免费补救**那一面。
    把它当成「新词」会让**已经付过费的词又被报一次价**。
    """
    kws = [_kw(1), _kw(2), _kw(3)]
    topics = [_topic(1, "成品标题"), _topic(2, None)]     # 2 是缺题(凭据行)
    assert [k["id"] for k in keywords_without_topics(kws, topics)] == [3]


def test_both_charges_come_from_the_same_function():
    """🔴 整表价与子集价由**同一个 `charge_card()`** 产出,不是两套算法。"""
    kws = [_kw(1), _kw(2), _kw(3)]
    topics = [_topic(1, "x")]
    whole = charge_card(kws, billable=True, lookup=LOOKUP)
    subset_kws = keywords_without_topics(kws, topics)
    subset = charge_card(subset_kws, billable=True, lookup=LOOKUP)
    assert whole["feature_code"] == subset["feature_code"]
    assert whole["base_points"] == subset["base_points"]
    assert subset["estimated_points"] == 80 * len(subset_kws)


def test_a_strict_subset_is_strictly_cheaper():
    """🔴 子集 ⊂ 整表 ⇒ `estimated_points` **严格小于**。"""
    kws = [_kw(1), _kw(2), _kw(3)]
    topics = [_topic(1, "x")]                     # 只有 1 出过题 ⇒ 子集 {2,3}
    whole = charge_card(kws, billable=True, lookup=LOOKUP)
    subset = charge_card(keywords_without_topics(kws, topics),
                         billable=True, lookup=LOOKUP)
    assert subset["estimated_points"] < whole["estimated_points"]


def test_an_equal_set_is_byte_for_byte_equal():
    """🔴 子集 == 整表(一个词都还没出题)⇒ 两个报价**逐字相等**。

    这一格和上一格**必须都有**:只有"严格小于"时,把子集写成"恒小一份"也能绿;
    只有"相等"时,把子集写成"恒等于整表"也能绿。两格夹住才唯一。
    """
    kws = [_kw(1), _kw(2), _kw(3)]
    whole = charge_card(kws, billable=True, lookup=LOOKUP)
    subset = charge_card(keywords_without_topics(kws, []),
                         billable=True, lookup=LOOKUP)
    assert subset == whole


def test_an_empty_new_set_yields_none_not_zero():
    """🔴 一个新词都没有 ⇒ `None`(不显示价),**不是 `0`**(有新词但免费)。"""
    kws = [_kw(1), _kw(2)]
    topics = [_topic(1, "x"), _topic(2, "y")]
    new = keywords_without_topics(kws, topics)
    assert new == []
    assert charge_card(new, billable=bool(new), lookup=LOOKUP) is None


def test_the_detail_endpoint_returns_both_charges_from_one_helper():
    """🔴 接线腿:详情端点回包两个价,且**都走同一个 charge_card**。"""
    fn = _fn("api_get_writing_project_detail")
    assert fn is not None, "找不到详情端点"
    src = ast.unparse(fn)
    keys = {k.value for n in ast.walk(fn) if isinstance(n, ast.Dict)
            for k in n.keys if isinstance(k, ast.Constant)}
    assert "charge" in keys and "charge_new_keywords_only" in keys, keys
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and (getattr(n.func, "id", None) in ("charge_card", "_topic_charge_card"))]
    assert len(calls) == 2, "charge_card 调 %d 次(应 2 次:整表 + 子集)" % len(calls)

    # 🔴 **钉那次调用本身,别钉整段源码里有没有那个名字。**
    #    第一版我查的是 `"keywords_without_topics" in src` —— 而 **import 行**
    #    里就有这个名字,于是把子集换成 `detail.get("keywords")`(恒等于整表)
    #    之后判据**照样绿**。锚落在了一条不是它要守的行上。
    # 实参通常是个变量,要先把它**解回来源**:找 `<名字> = keywords_without_topics(...)`。
    filtered_vars = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
            callee = getattr(n.value.func, "id", None) or ""
            if callee in ("keywords_without_topics", "_kw_no_topics"):
                for t_ in n.targets:
                    filtered_vars.add(ast.unparse(t_))
    assert filtered_vars, (
        "没有任何变量是由 keywords_without_topics 筛出来的 —— 子集不是筛的")
    subset_call = None
    for c in calls:
        if c.args and ast.unparse(c.args[0]) in filtered_vars:
            subset_call = c
    assert subset_call is not None, (
        "没有任何一次 charge_card 吃的是那个筛出来的子集(筛了但没用上)"
        " —— 子集价仍来自第二套算法")

    # 🔴 空集必须走 `None` 而不是 `0`:`billable` 要由「有没有新词」决定。
    billable = [k.value for k in subset_call.keywords if k.arg == "billable"]
    assert billable, "子集那次调用没给 billable"
    bsrc = ast.unparse(billable[0])
    assert "_new_kws" in bsrc, (
        "billable=%s 没有看「有没有新词」—— 空集时会给出 0 而不是 None" % bsrc)
