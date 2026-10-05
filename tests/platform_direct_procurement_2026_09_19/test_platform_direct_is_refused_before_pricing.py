# -*- coding: utf-8 -*-
"""WO_241 甲 · 平台直营账号进「进货」三个入口:进 handler 即拒,不进算价。

事实(台账 09-19 13:35,Review 在回滚事务里复现):
admin 经 `_require_agent` 把经营主体换成**平台直营账号 136**;
`build_cash_anchored_quote_terms` 对它抛 `ResaleError「厂家与最终买方身份非法」`
—— 平台自己既是厂家又是最终买方。链路 `ResaleError` → `QuoteError` →
`purchase-options` **没有 try** ⇒ **500**。对照:真服务商 89 三档全通;全站 7 个 admin 都会撞。

🔴 本包钉两件,分别对应两种不同的缺陷:
1. **业务上不存在的事要在入口说清楚** —— 平台直营没有「进货」这件事,
   不该走到算价再用一个结算异常去表达它;
2. **同一种异常三个入口处置必须一致** —— 改前 preview/下单 是 422、
   options 是 500,而三者调的是同一条报价链。
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

API = REPO / "api" / "agent_workbench_api.py"

#: 三个入口 —— 工单逐字点名的那三个
PROCUREMENT_HANDLERS = {
    "agent_inventory_purchase_options": "/inventory/purchase-options",
    "agent_inventory_purchase_preview": "/inventory/purchase-preview",
    "agent_inventory_purchase": "/inventory/purchase",
}


def _tree():
    return ast.parse(io.open(API, encoding="utf-8").read(), "agent_workbench_api.py")


def _fn(name):
    for n in ast.walk(_tree()):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


# ══════════════════════════════════════════════════════════════════
# 一、行为:平台直营 → 409,且**不进算价**
# ══════════════════════════════════════════════════════════════════

def _guard():
    from api.agent_workbench_api import _reject_platform_direct_procurement
    return _reject_platform_direct_procurement


def test_platform_direct_is_refused_with_the_exact_code():
    """🔴 code 逐字相等 —— 前端按 code 分支,文案可改、code 不可。"""
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _guard()({"user_id": 136, "operating_context": "platform_direct"})
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "PLATFORM_DIRECT_NO_PROCUREMENT"
    assert ei.value.detail["message"] == "平台直营账号不需要进货"


@pytest.mark.parametrize("ctx", [None, "", "organization_seat", "self"])
def test_every_other_operating_context_passes_through(ctx):
    """🔴 反向臂:真服务商 / 组织席位 **一律不受影响**。

    没有这一条,把守卫写成 `raise` 无条件拒绝也全绿 ——
    而那会把所有服务商的进货一起打死。
    """
    _guard()({"user_id": 89, "operating_context": ctx})     # 不抛即通过


def test_the_guard_reads_the_operating_context_not_the_admin_flag():
    """🔴 按**经营身份**判,不按 `is_admin` 判。

    `_require_agent` 已经把 admin 换成了平台直营主体;
    再去看 `is_admin` 等于绕过那一层,且组织席位代作业时会判错
    (本仓 defining-the-population-by-the-wrong-attribute-flips-the-conclusion)。
    """
    src = ast.unparse(_fn("_reject_platform_direct_procurement"))
    assert "operating_context" in src
    assert "is_admin" not in src, "守卫读了 is_admin —— 换了个属性圈人"


# ══════════════════════════════════════════════════════════════════
# 二、接线:三个入口都挂了守卫,且都挂在算价之前
# ══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("handler", sorted(PROCUREMENT_HANDLERS))
def test_every_procurement_entry_calls_the_guard(handler):
    """🔴 三个入口一个都不能漏 —— 工单点名的就是这三个。"""
    fn = _fn(handler)
    assert fn is not None, "找不到 %s —— 判据前提变了" % handler
    calls = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "_reject_platform_direct_procurement"]
    assert len(calls) == 1, "%s 调守卫 %d 次(应 1 次)" % (handler, len(calls))


@pytest.mark.parametrize("handler", sorted(PROCUREMENT_HANDLERS))
def test_the_guard_runs_before_any_pricing_call(handler):
    """🔴 「不进算价」是工单原话 —— 守卫必须排在所有取价/报价调用**之前**。

    只断言"调了守卫"是不够的:挂在算价后面时,那个 500 照样先发生。
    这里按**行号顺序**判(同一函数体内,行号即执行顺序的必要条件)。
    """
    fn = _fn(handler)
    guard_line = min(n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
                     and getattr(n.func, "id", None) == "_reject_platform_direct_procurement")
    # 🔴 每个入口都必须在这张表里有**至少一个**算价调用。
    #    第一版我漏了下单入口的 `_create_quoted_inventory_order`,于是那一条
    #    `pytest.skip` 了 —— **被跳过的判据什么都不保护**,而读数里它长得像"通过"。
    #    所以这里不 skip:表里没有它的算价调用 = 表过期了,必须红。
    PRICING = {"_published_procurement_view", "issue_procurement_quote",
               "issue_procurement_custom_amount_quote", "_compute_purchase_options",
               "quote_order_pricing_snapshot", "_create_quoted_inventory_order",
               "merge_pricing_config", "normalize_catalog_options"}
    pricing_lines = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
                     and (getattr(n.func, "id", None) or getattr(n.func, "attr", None)) in PRICING]
    assert pricing_lines, (
        "%s 里一个已知算价调用都没数到 —— 要么它改名了,要么 PRICING 表过期了。"
        "不许 skip:跳过的判据什么都不保护,读数里却长得像通过。" % handler)
    assert guard_line < min(pricing_lines), (
        "%s 的守卫在行 %d,而最早的算价在行 %d —— 守卫排在算价后面,500 照样先发生"
        % (handler, guard_line, min(pricing_lines)))


# ══════════════════════════════════════════════════════════════════
# 三、QuoteError 三个入口同一种处置(不许再漏成 500)
# ══════════════════════════════════════════════════════════════════

def _handles_quote_error(fn):
    for n in ast.walk(fn):
        if not isinstance(n, ast.Try):
            continue
        for h in n.handlers:
            if h.type is not None and "QuoteError" in ast.unparse(h.type):
                return True
    return False


@pytest.mark.parametrize("handler", sorted(PROCUREMENT_HANDLERS))
def test_every_entry_maps_quote_error_instead_of_letting_it_become_500(handler):
    """🔴 改前:preview 与下单接了 `QuoteError` → 422,**只有 options 裸着** → 500。

    同一条报价链、同一种异常,三个入口两种结果 —— 这个不对称本身就是缺陷。
    """
    fn = _fn(handler)
    assert _handles_quote_error(fn), (
        "%s 没有接 QuoteError —— 它会漏成 500" % handler)


def test_the_quote_error_reply_is_422_and_leaks_no_settlement_detail():
    """🔴 422,且**公开文案是固定的**,不把异常原文交出去。

    ⚠️ 与工单字面不同,理由写在交付单:工单写
       `422 {"code":"QUOTE_UNAVAILABLE","message":<异常文案>}`。
       我保留既有 `_public_procurement_quote_error`(code `PROCUREMENT_QUOTE_INVALID`
       + 固定文案),因为:
       ① 它的 docstring 就是「结算细节只进服务器日志,不进公开错误」——
          供应商/结算结构零暴露是常驻红线;
       ② 改 code 名是**前端可见的契约变更**,preview 与下单今天已经在回
          `PROCUREMENT_QUOTE_INVALID`,改名会让 A 那边静默失配。
       两点都已报 Review,由他决定是否仍要改名。
    """
    from api.agent_workbench_api import _public_procurement_quote_error
    exc = _public_procurement_quote_error(RuntimeError("当前采购规则无法形成金额锚定报价"),
                                          "options")
    assert exc.status_code == 422
    assert exc.detail["code"] == "PROCUREMENT_QUOTE_INVALID"
    assert "采购规则" not in exc.detail["message"], "异常原文被交到了公开文案里"
    assert exc.detail["message"] == "当前进货报价已失效或不可用，请刷新后重试"
