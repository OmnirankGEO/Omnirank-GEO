"""#157 · 题面超限在**冻结时**就拦;派发遇不可重试异常判**终态**。

## 实测出来的缺陷(2026-09-08,#153 A 段取证)

品牌名 96 字 ⇒ 系统题 `{品牌名}是做什么的?` = **102 字** > 100:

  · 冻结题单时 `validate_plan` **根本不查长度**(全文件无长度判断);
  · 到 `run_executor` 派发时构造 `DiagnosisRequest` ⇒ pydantic `ValidationError`;
  · 而那里**没有 HTTP 出口**把它翻成 422 —— 它落进 `except Exception`,
    `release_claim` 归还 marker、attempts +1、**反复重试**,
    直到耗尽由 sweeper 退款。

⇒ 用户看到的是「一直没跑起来,最后退了钱」,**没有一句话说是题太长**;
  日志里只有一坨 pydantic 报文。

## 判据钉三件

  · **口径一份**:上限只在 `diagnosis_question_pricing` 定义,两个出口都用它;
  · **冻结时拦**:96 字品牌名的题单在 `validate_plan` 就被拒,且报文是人话;
  · **一次即终**:绕过冻结直接派发 ⇒ **不归还 marker**、attempts 不再涨、原因落库;
    正样本:可重试异常仍然归还 marker、仍然重试。
"""

from __future__ import annotations

import ast
import io
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

LONG_BRAND = "全" * 96
LONG_Q = f"{LONG_BRAND}是做什么的?"          # 102 字


# ══════════════════════════════════ 口径只有一份

def test_the_limit_lives_in_exactly_one_place():
    """🔴 上限只在 SSOT 里,两个出口都从它取。

    毒:在 `question_plan` 里写死 100 ⇒ 本条红。
    两份上限漂开时,表现是「冻结放过、提交拒绝」,而两边各自看都正常。
    """
    from services.diagnosis_question_pricing import MAX_QUESTION_CHARS
    assert MAX_QUESTION_CHARS == 100

    for rel in ("services/defensive_geo/question_plan.py", "server.py"):
        src = io.open(ROOT / rel, encoding="utf-8").read()
        assert "MAX_QUESTION_CHARS" in src, "%s 没走上限 SSOT" % rel

    qp = io.open(ROOT / "services" / "defensive_geo" / "question_plan.py",
                 encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(qp))
              if isinstance(n, ast.FunctionDef) and n.name == "validate_plan")
    for node in ast.walk(fn):
        if isinstance(node, ast.Constant) and node.value == 100:
            pytest.fail("validate_plan 里又写死了一个 100 —— 上限必须只有一份")


def test_the_violation_helper_reports_the_actual_length():
    """helper 只回答「超没超、超多少」;措辞由各自出口决定。"""
    from services.diagnosis_question_pricing import question_length_violation
    assert question_length_violation("短题") is None
    assert question_length_violation(LONG_Q) == len(LONG_Q) == 102


# ══════════════════════════════════ 冻结时就拦

def _plan(text):
    from services.defensive_geo.question_plan import PlannedQuestion
    return (PlannedQuestion(
        question_identity_key="k1", question_revision=1, global_ordinal=1,
        text=text, mode_side="defensive", family_key="f1",
        brand_exposure="explicit", origin="system", classifier_version=None),)


def test_an_over_long_question_is_refused_at_freeze_time():
    """🔴 本单那一格:96 字品牌名的题单在冻结时就被拒。

    毒:去掉 `validate_plan` 里的长度闸 ⇒ 本条红(题会被冻进去,留到派发才炸)。
    """
    from services.defensive_geo.question_plan import PlanIdentityError, validate_plan

    with pytest.raises(PlanIdentityError) as got:
        validate_plan("defensive", _plan(LONG_Q))
    msg = str(got.value)
    # 人话三要素:哪道题、多少字、怎么办
    assert "第 1 题" in msg, msg
    assert "102 字" in msg, msg
    assert "改短" in msg, msg
    assert got.value.code == "question_too_long"


def test_the_message_template_lives_only_in_the_copy_registry():
    """🔴 [#157 v11] 那句话**只在注册表**出现一次,`validate_plan` 不许内联。

    A 的 CTA 要在提交前就禁用并显示同一句 —— 后端内联 f-string 的话,
    前端只能照抄一遍,而两份漂开时的表现是
    「弹窗说 100 字、按钮说别的」,两边各自看都正常。
    毒:把模板抄回 `validate_plan` 的 f-string ⇒ 本条红。
    """
    from services.defensive_geo.copy_registry import user_label

    tpl = user_label("reason", "question_too_long")
    for ph in ("{ordinal}", "{chars}", "{limit}", "{excerpt}"):
        assert ph in tpl, "模板缺占位符 %s:%r" % (ph, tpl)

    qp = io.open(ROOT / "services" / "defensive_geo" / "question_plan.py",
                 encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(qp))
              if isinstance(n, ast.FunctionDef) and n.name == "validate_plan")
    body = ast.unparse(fn)
    assert "user_label(" in body, "validate_plan 没从注册表取文案"
    for leaked in ("题太长了", "改短一点", "客户名很长"):
        assert leaked not in body, (
            "文案被内联回 validate_plan 了(%r)—— 前端会照抄出第二份" % leaked)


def test_validate_plan_output_equals_the_formatted_template():
    """🔴 输出 == 模板 format 结果 —— 逐字相同,不是「意思差不多」。

    毒:改 `validate_plan` 的填空参数名/顺序 ⇒ 本条红。
    """
    from services.defensive_geo.copy_registry import user_label
    from services.defensive_geo.question_plan import PlanIdentityError, validate_plan
    from services.diagnosis_question_pricing import MAX_QUESTION_CHARS

    with pytest.raises(PlanIdentityError) as got:
        validate_plan("defensive", _plan(LONG_Q))
    expected = user_label("reason", "question_too_long").format(
        ordinal=1, chars=len(LONG_Q), limit=MAX_QUESTION_CHARS,
        excerpt=LONG_Q[:24])
    assert str(got.value) == expected, (
        "输出与模板不一致 · 实得=%r 应为=%r" % (str(got.value), expected))


def test_the_frontend_mirror_carries_the_same_template():
    """🔴 前端镜像里有**同一份**模板(它是生成物,不是手抄)。

    没有这条,后端改了文案而没重跑生成器时,前端仍显示旧句 ——
    而 build 链那道门只在 CI 跑,判据要在这里先说话。
    """
    ts = io.open(ROOT / "frontend" / "src" / "lib" / "defensiveGeoCopy.ts",
                 encoding="utf-8").read()
    from services.defensive_geo.copy_registry import (
        COPY_REGISTRY_VERSION, user_label)
    assert COPY_REGISTRY_VERSION in ts, "镜像版本与后端不一致 —— 生成器没重跑"
    assert user_label("reason", "question_too_long") in ts, "镜像里没有这句模板"


def test_a_normal_question_still_passes_freeze():
    """正样本臂:正常长度照常签发。

    只证「超长被拒」不够 —— 一个把所有题单都拒掉的实现同样能让上一条变绿。
    """
    from services.defensive_geo.question_plan import validate_plan
    validate_plan("defensive", _plan("深圳做 GEO 优化哪家好?"))


# ══════════════════════════════════ 派发:一次即终态

def _executor_ast():
    src = io.open(ROOT / "services" / "defensive_geo" / "run_executor.py",
                  encoding="utf-8").read()
    return src, ast.parse(src)


def test_the_terminal_branch_is_checked_before_the_generic_one():
    """🔴 承重结构臂:`except _NON_RETRYABLE_DISPATCH` 必须排在 `except Exception` **前**。

    排在后面的话它**永远走不到** —— 而顺利路径(不抛这类异常时)读数完全相同,
    行为臂分不出来。毒是「把两个 handler 对调」,不是「删掉它」。
    """
    _src, tree = _executor_ast()
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "dispatch_one")
    orders = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Try) and len(node.handlers) > 1:
            names = [getattr(h.type, "id", None) or
                     (ast.unparse(h.type) if h.type else "bare")
                     for h in node.handlers]
            if any("NON_RETRYABLE" in (n or "") for n in names):
                orders.append(names)
    assert orders, "派发处没有不可重试分支 —— 结构上跑不起来的单仍会被无限重投"
    for names in orders:
        i = next(k for k, n in enumerate(names) if "NON_RETRYABLE" in (n or ""))
        j = next((k for k, n in enumerate(names) if n == "Exception"), len(names))
        assert i < j, "终态分支排在 except Exception 之后 ⇒ 永远走不到:%r" % names


def test_the_terminal_path_does_not_return_the_marker():
    """🔴 终态 = **不归还 marker**。

    归还 = 允许重投。对"结构上跑不起来"的单,重投只是把同一行炸 N 次,
    而用户看到的只是「一直没跑起来,最后退了钱」。
    毒:在终态分支里加回 `release_claim` ⇒ 本条红。
    """
    src, tree = _executor_ast()
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "dispatch_one")
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Try) and len(node.handlers) > 1):
            continue
        for h in node.handlers:
            nm = getattr(h.type, "id", None) or ""
            if "NON_RETRYABLE" not in nm:
                continue
            body = ast.unparse(ast.Module(body=h.body, type_ignores=[]))
            assert "release_claim" not in body, (
                "终态分支归还了 marker —— 那就还会被重投:\n%s" % body[:300])
            assert "mark_dispatch_terminal" in body, "终态没落原因,前端看不见"


def test_the_retryable_path_still_returns_the_marker():
    """🔴 正样本臂:可重试失败**仍然**归还 marker。

    没有这条,一个把所有失败都判终态的实现也能让上一条变绿 ——
    而那会把一次库抖动变成一次退款。
    """
    src, tree = _executor_ast()
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "dispatch_one")
    found = False
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Try) and len(node.handlers) > 1):
            continue
        for h in node.handlers:
            if (getattr(h.type, "id", None) or "") != "Exception":
                continue
            body = ast.unparse(ast.Module(body=h.body, type_ignores=[]))
            if "release_claim" in body:
                found = True
    assert found, "可重试分支不再归还 marker —— 一次抖动就变成一次退款"


def test_non_retryable_set_excludes_transient_failures():
    """🔴 只收**结构性**失败。

    把超时/连接错判成终态 = 把一次偶发变成一次退款。
    """
    from services.defensive_geo.run_executor import _NON_RETRYABLE_DISPATCH
    from pydantic import ValidationError

    assert ValidationError in _NON_RETRYABLE_DISPATCH
    assert not any(issubclass(t, (TimeoutError, ConnectionError, OSError))
                   for t in _NON_RETRYABLE_DISPATCH), _NON_RETRYABLE_DISPATCH
    # 反向:不许把 Exception 本身塞进来(那等于所有失败都终态)
    assert Exception not in _NON_RETRYABLE_DISPATCH


def test_the_terminal_reason_is_human_readable():
    """终态原因是**人话**,不是 pydantic 报文 —— 它会被前端看到。"""
    from services.defensive_geo.run_executor import _humanize_dispatch_failure

    msg = _humanize_dispatch_failure(ValueError("自定义问题不能超过 100 字符: 全全全..."))
    assert "100 字" in msg and "改短" in msg, msg
    assert "ValidationError" not in msg and "pydantic" not in msg


# ══════════════════════════════════ 迁移

def test_the_reason_column_migration_is_registered_and_dml_free():
    """057 进 manifest,零 DML,且**不复用** `reaped_reason`。"""
    mig = ROOT / "db" / "migration_057_defgeo_dispatch_error_2026_09_08.sql"
    assert mig.exists()
    sql = io.open(mig, encoding="utf-8").read()
    ddl = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
    for verb in ("INSERT ", "UPDATE ", "DELETE "):
        assert verb not in ddl.upper(), "迁移体内出现 DML:%s" % verb
    assert "defgeo_dispatch_error" in ddl
    assert "reaped_reason" not in ddl, "借用了 sweeper 的原因列 —— 两件事会分不开"
    manifest = io.open(ROOT / "db" / "migration_manifest.py", encoding="utf-8").read()
    assert mig.name in manifest
