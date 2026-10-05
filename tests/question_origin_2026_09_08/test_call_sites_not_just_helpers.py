"""#149 返修 · 锁移到**调用点**那一侧。

## 为什么加这个文件

第一版判据把逻辑抽进 helper、锁也钉在 helper 上,**毒和锁落在同一侧**:

  · 毒 P2:调用点改成 `_known_layers, _need = {}, list(questions)`
    (绕过分工、全题重分类)⇒ **21 条全绿**;
  · 毒 P4:报告标题条件改回 `if is_verbatim_mode`
    (全 AI 题单仍写「您填写的问题」)⇒ **21 条全绿**。

`test_questions_carrying_a_layer` 只打 `split_questions_needing_layers`,
`test_all_ai_plan` 只打 `origin_counts` —— 两条都证明「helper 是对的」,
一条都不证明「调用它的人用对了」。

## 两种臂各守一半

  · **行为臂**打归层入口的真实分工(分类器实际收到了哪几道题);
  · **结构臂**打工作流那个赋值:`sales_question_types` 只准从**白名单**里
    的两种来源产出。P2 那种「就地手写一个 dict」正是白名单挡的东西 ——
    黑名单(「不许出现 `_need`」)对换个变量名的写法结构性失明。

🔴 本文件**不 import `workflows/diagnosis_workflow`**:本仓约定判据不导它
   (它 import 就连库,见 r567 包的 docstring);实测与 import server 的文件
   同跑还会让 pytest 拆 capture 时炸 `I/O operation on closed file`。
   所以归层入口住在 `services/diagnosis_question_origin`,分类器由调用方注入。
"""

from __future__ import annotations

import ast
import asyncio
import io
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

Q_AI1 = "深圳 AI 搜索优化哪家好?"
Q_AI2 = "GEO 优化一般怎么收费?"
Q_CUST = "我们这种做工业设备的适合投吗?"


# ══════════════════ ① 归层入口:行为臂(桩掉分类器,数它实际收到哪几道题)

def _run_types(meta, questions):
    """跑**真**的归层入口,只把 LLM 分类器换成计数桩。"""
    from services.diagnosis_question_origin import resolve_verbatim_question_types

    seen = {"calls": 0, "questions": None}

    async def _fake(qs):
        seen["calls"] += 1
        seen["questions"] = list(qs)
        return {q: "regional_industry" for q in qs}

    types = asyncio.run(
        resolve_verbatim_question_types(meta, questions, classify=_fake))
    return types, seen


def test_questions_with_a_layer_are_never_sent_to_the_classifier():
    """🔴 毒 P2 那一格:带层的题**不进**分类器的入参。

    打的是「分类器实际收到了哪几道题」,不是「拆分函数返回了什么」。
    """
    meta = [{"text": Q_AI1, "origin": "ai_suggested", "layer": "brand_awareness"},
            {"text": Q_AI2, "origin": "ai_suggested", "layer": "super_tier1"},
            {"text": Q_CUST, "origin": "customer"}]
    types, seen = _run_types(meta, [Q_AI1, Q_AI2, Q_CUST])
    assert seen["calls"] == 1, "该调一次(有一道无层的题):%r" % seen
    assert seen["questions"] == [Q_CUST], (
        "分类器收到了带层的题 —— 分工被绕过了:%r" % seen["questions"])
    assert types[Q_AI1] == "brand_awareness"      # 生成器给的层不被覆盖
    assert types[Q_AI2] == "super_tier1"
    assert types[Q_CUST] == "regional_industry"


def test_an_all_ai_plan_does_not_call_the_classifier_at_all():
    """全部带层 ⇒ 分类器**一次都不调**(省一次 LLM)。"""
    meta = [{"text": Q_AI1, "origin": "ai_suggested", "layer": "brand_awareness"},
            {"text": Q_AI2, "origin": "ai_suggested", "layer": "super_tier1"}]
    types, seen = _run_types(meta, [Q_AI1, Q_AI2])
    assert seen["calls"] == 0, "全带层还调了分类器:%r" % seen
    assert types == {Q_AI1: "brand_awareness", Q_AI2: "super_tier1"}


def test_a_legacy_plan_sends_every_question_to_the_classifier():
    """🔴 正样本臂:无 meta ⇒ **全部**题进分类器(与今天逐字节同行为)。

    没有这条,一个「永远不调分类器」的实现也能让上面两条变绿 ——
    而那会让客户手写题全部 fallback super_tier1(report 325 被低估的根因)。
    """
    types, seen = _run_types(None, [Q_AI1, Q_CUST])
    assert seen["calls"] == 1
    assert seen["questions"] == [Q_AI1, Q_CUST], seen
    assert types == {Q_AI1: "regional_industry", Q_CUST: "regional_industry"}


#: `sales_question_types` **只准**从这两种来源产出(白名单)。
#: 🔴 黑名单挡不住 P2:它就地手写一个 dict,换个变量名就绕过去了。
_ALLOWED_TYPE_SOURCES = ("resolve_verbatim_question_types", "get")


def test_the_growth_branch_only_gets_its_layers_from_the_entry_point():
    """🔴 结构臂(P2 那一格):`sales_question_types` 的每一次赋值都必须来自白名单。

    verbatim 那支只准是 `await resolve_verbatim_question_types(...)`;
    非 verbatim 那支是 `business_context_result.get("question_types", {})`。
    别的写法(比如就地拼一个 dict 再全题送分类器)一律红 ——
    行为臂打的是入口,谁在调用点旁边另起一套,行为臂看不见。
    """
    src = io.open(ROOT / "workflows" / "diagnosis_workflow.py", encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "task_ai_visibility")
    sources = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "sales_question_types"
                   for t in node.targets):
            continue
        v = node.value
        if isinstance(v, ast.Await):
            v = v.value
        name = None
        if isinstance(v, ast.Call):
            f = v.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        sources.append(name or type(v).__name__)
    assert sources, "找不到 sales_question_types 的赋值 —— 分母塌了,不是通过"
    bad = [x for x in sources if x not in _ALLOWED_TYPE_SOURCES]
    assert not bad, (
        "归层来源不在白名单里:%r。若确是新的合法来源,先把它加进 "
        "_ALLOWED_TYPE_SOURCES 并写明理由。" % bad)
    assert "resolve_verbatim_question_types" in sources, (
        "增长线没走归层入口 —— 上面那些行为臂守的是一段没人调的代码")
