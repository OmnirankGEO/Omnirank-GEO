"""题单校验被拒时,她必须看到**哪一处**不对 + 有下一步可点。

Owner 线上实撞(生产尖 bd5fc709d):hybrid 体检下系统题全落 defensive 侧,
`validate_plan` 抛「hybrid 两侧都必须有题」,而 API 把**所有** PlanIdentityError
一律映成 ``reason_key=None`` ⇒ 通用句「这次提交的内容有一处填得不对,返回改一下
就能继续」。她不知道是哪一处、也没有按钮可点。开发原则:提示二选一 ——
要么有明细 + 有修复动作,要么不显示。

🔴 本包**不碰** ``validate_plan`` 的规则本体(那是身份/计价安全线),只钉「表达」:
   哪条规则 → 哪个 code → 哪个 reason_key → 哪一句人话 → target 带不带侧别。
"""

from __future__ import annotations

import ast
import io
import logging
import os

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _read(*parts: str) -> str:
    with io.open(os.path.join(REPO, *parts), encoding="utf-8", newline="") as fh:
        return fh.read()


def _q(seq: int, side: str, *, text: str = "这家公司靠谱吗", ordinal: int | None = None,
       revision: int = 1, key: str | None = None):
    from services.defensive_geo.question_plan import PlannedQuestion
    return PlannedQuestion(
        question_identity_key=key or f"k{seq}",
        question_revision=revision,
        global_ordinal=seq if ordinal is None else ordinal,
        text=text,
        mode_side=side,
        family_key="f1",
        brand_exposure="named",
        origin="system",
        classifier_version=None,
    )


# ══════════════════════════════════════════════════════════════════════════
# ① 每条规则 → 它自己的 code(和侧别)
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("mode,questions,code,side", [
    ("defensive", (), "plan_empty", None),
    # 🔴 Owner 撞的就是这一条:hybrid 只剩 defensive 侧 ⇒ 缺的是「会搜的」
    ("hybrid", (_q(1, "defensive"),), "plan_hybrid_needs_both_sides", "offensive"),
    # 反过来:只剩 offensive ⇒ 缺的是「会问的」
    ("hybrid", (_q(1, "offensive"),), "plan_hybrid_needs_both_sides", "defensive"),
    ("defensive", (_q(1, "offensive"),), "plan_side_mismatch", "offensive"),
    ("offensive", (_q(1, "defensive"),), "plan_side_mismatch", "defensive"),
    ("defensive", (_q(1, "defensive", text="   "),), "plan_question_blank", "defensive"),
    ("defensive", (_q(1, "defensive", key="dup"), _q(2, "defensive", key="dup")),
     "plan_identity", None),
    ("defensive", (_q(1, "defensive", ordinal=2), _q(2, "defensive", ordinal=3)),
     "plan_identity", None),
    ("defensive", (_q(1, "defensive", revision=0),), "plan_identity", None),
])
def test_each_rule_carries_its_own_code(mode, questions, code, side):
    """分类走**异常自带的 code**,不走消息文本。

    🔴 为什么不按消息文本分类:那是裸串匹配 —— 有人改一个字,提示就悄悄退回
       通用句,而且没有任何判据会红。code 是机器可读的,改它必须显式改。
    """
    from services.defensive_geo.question_plan import PlanIdentityError, validate_plan

    with pytest.raises(PlanIdentityError) as ei:
        validate_plan(mode, questions)
    assert ei.value.code == code, f"{mode}/{code}:实得 code={ei.value.code!r}"
    assert getattr(ei.value, "side", None) == side, (
        f"{mode}/{code}:侧别应为 {side!r},实得 {getattr(ei.value, 'side', None)!r} —— "
        f"按钮要靠它落到出问题的那一侧")


def test_a_valid_plan_still_passes():
    """必须不命中的那一半:合法题单不许被拒。

    没有这条,把 validate_plan 改成"永远抛"也能让上面 9 条全绿。
    """
    from services.defensive_geo.question_plan import validate_plan
    validate_plan("hybrid", (_q(1, "defensive"), _q(2, "offensive")))
    validate_plan("defensive", (_q(1, "defensive"),))


# ══════════════════════════════════════════════════════════════════════════
# ② code → reason_key → 一句人话(行为臂:跑生产那段 except 的源码原文)
# ══════════════════════════════════════════════════════════════════════════
def _run_the_real_except_block(exc, caplog):
    """把端点里那段 ``except PlanIdentityError`` 的**源码原文**取出来执行。

    🔴 不重写等价物:重写的是我的理解,不是生产的行为。这里
       ``ast.get_source_segment`` 取的是 ``api/defensive_geo_api.py`` 里那几行本身,
       命名空间给生产模块的真 ``_safe_error`` / ``_action`` /
       ``_PLAN_REASON_BY_CODE`` 和真 logger —— 所以拿回来的 payload
       就是她会收到的那一份。
    """
    import textwrap

    from fastapi import HTTPException

    import api.defensive_geo_api as mod

    src = _read("api", "defensive_geo_api.py")
    tree = ast.parse(src)
    handlers = [h for n in ast.walk(tree) if isinstance(n, ast.Try)
                for h in n.handlers
                if isinstance(h.type, ast.Name) and h.type.id == "PlanIdentityError"]
    assert len(handlers) == 1, f"PlanIdentityError 的 except 不唯一:{len(handlers)}"
    body = "\n".join(textwrap.dedent(ast.get_source_segment(src, st))
                     for st in handlers[0].body)
    assert "_PLAN_REASON_BY_CODE" in body, "端点没按 code 选 reason —— 分类没接上"

    ns = {
        "_PLAN_REASON_BY_CODE": mod._PLAN_REASON_BY_CODE,
        "_safe_error": mod._safe_error,
        "_action": mod._action,
        "logger": mod.logger,
        "Any": object,
        "exc": exc,
    }
    with caplog.at_level(logging.INFO, logger=mod.logger.name):
        with pytest.raises(HTTPException) as ei:
            exec(compile(body, "<except-block>", "exec"), ns)   # noqa: S102
    return ei.value.detail


@pytest.mark.parametrize("code,side,must_contain", [
    ("plan_empty", None, "一道题都没有"),
    ("plan_hybrid_needs_both_sides", "offensive", "客户会搜的"),
    ("plan_side_mismatch", "offensive", "混进了另一类"),
    ("plan_question_blank", "defensive", "还是空的"),
    ("plan_identity", None, "编号对不上"),
])
def test_each_code_reaches_her_as_a_specific_sentence(code, side, must_contain, caplog):
    """每个 code 都要落到一句**说人话**的提示,而不是那句通用的。

    🔴 断言"含某个词"钉的是**这一句而不是另一句**;同时反向断言它**不是**通用句 ——
       只钉"有 publicExplanation"的话,全部落通用句也全绿。
    """
    from services.defensive_geo.question_plan import PlanIdentityError

    payload = _run_the_real_except_block(
        PlanIdentityError("规则文本", code=code, side=side), caplog)

    assert payload["code"] == "VALIDATION_FAILED"
    exp = payload.get("publicExplanation", "")
    assert must_contain in exp, f"{code} 的句子不对:{exp!r}"
    assert "这次提交的内容有一处填得不对" not in exp, (
        f"{code} 仍落通用句 —— 她还是不知道哪一处不对")
    assert payload["nextAction"]["label"], "没有可点的下一步"
    if side:
        assert payload["nextAction"]["target"].get("side") == side, (
            f"nextAction 没带侧别 —— 前端一键补题落不到那一侧:"
            f"{payload['nextAction']['target']}")
    else:
        assert "side" not in payload["nextAction"]["target"]


def test_an_unknown_code_still_falls_back_to_the_generic_sentence(caplog):
    """反向臂:认不出的 code 仍落通用句,不许炸也不许空白。

    向前兼容:将来有人在 validate_plan 加一条 raise 忘了给 code,
    表现回到 2026-09-02 之前,而不是 500 或一片空白。
    """
    from services.defensive_geo.question_plan import PlanIdentityError

    payload = _run_the_real_except_block(
        PlanIdentityError("将来某条新规则", code="plan_something_new_2027"), caplog)
    assert "这次提交的内容有一处填得不对" in payload.get("publicExplanation", "")
    assert payload["nextAction"]["label"]


def test_a_rejection_always_leaves_exactly_one_log_line_with_the_rule_text(caplog):
    """Deploy 实证:这条拒绝以前**一行日志都不打**(两容器 90 分钟命中 0)。

    运维侧完全不可诊断:线上有人被拦下,我们既不知道撞的是哪条规则、也不知道几次。
    """
    from services.defensive_geo.question_plan import PlanIdentityError

    caplog.clear()
    _run_the_real_except_block(
        PlanIdentityError("hybrid 计划两侧都必须有题,实得 ['defensive']",
                          code="plan_hybrid_needs_both_sides", side="offensive"), caplog)
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 1, f"应当正好一行,实得 {len(lines)}:{lines}"
    assert "hybrid 计划两侧都必须有题" in lines[0], f"日志没带规则文本:{lines[0]!r}"


# ══════════════════════════════════════════════════════════════════════════
# ③ 分母 + 隐私(机械枚举,不手抄)
# ══════════════════════════════════════════════════════════════════════════
def test_every_rule_in_validate_plan_carries_a_code():
    """分母 = ``validate_plan`` 里**每一个** raise,机械枚举。

    手抄清单漏掉的那一条不会让任何判据变红 —— 而漏掉的那条就会落回通用句。
    """
    src = _read("services", "defensive_geo", "question_plan.py")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "validate_plan")
    raises = [n for n in ast.walk(fn) if isinstance(n, ast.Raise)]
    assert len(raises) >= 8, f"只找到 {len(raises)} 处 raise —— 分母塌了"
    missing = [(ast.get_source_segment(src, r) or "").splitlines()[0]
               for r in raises
               if not (isinstance(r.exc, ast.Call)
                       and any(k.arg == "code" for k in r.exc.keywords))]
    assert not missing, f"这些 raise 没带 code,会落回通用句:{missing}"


def test_no_rule_message_can_leak_the_question_text():
    """规则文本进日志,所以它**不许**含用户输入的题文。

    现在的消息插的是 ``question_identity_key``(由 plan_id + 序号派生)与侧别集合,
    都不是用户内容。这条锁住这一点:将来有人为了"更好定位"把 ``q.text`` 插进去,
    等于把用户题文写进日志。
    """
    src = _read("services", "defensive_geo", "question_plan.py")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "validate_plan")
    bad = [seg for r in ast.walk(fn) if isinstance(r, ast.Raise)
           for seg in [ast.get_source_segment(src, r) or ""]
           if "q.text" in seg]
    assert not bad, f"规则消息里插了题文,会随日志泄漏:{bad}"


def test_every_mapped_reason_key_has_a_sentence():
    """映射表里每个 reason_key 都必须在 registry 里有句子。

    没有的话 ``_safe_error`` 的构造期 fail-closed 会在**运行时**炸;
    这条把它提前到判据里,而且分母是映射表自己,不是我手抄的名单。
    """
    from services.defensive_geo.copy_registry import try_user_label

    import api.defensive_geo_api as mod

    for code, reason in mod._PLAN_REASON_BY_CODE.items():
        assert try_user_label("reason", reason), f"{code} → {reason} 没有句子"


def test_a_forgotten_code_is_distinguishable_from_an_unknown_one(caplog):
    """「漏带 code」与「code 认不出」必须分得开 —— 而且漏带的那条不许被安错句子。

    🔴 `code` 的默认值是 `None` 而**不是** `"plan_identity"`。这个选择是有代价对比的:
       默认 `plan_identity` 的话,一条忘了给 code 的新规则会被安上
       「题单的编号对不上了(可能是刚才删改时漏了一步)」—— 一句**具体但错误**的话,
       把她指向一个根本不存在的问题。默认 `None` 则落通用句,诚实地说"有一处不对"。
       "不知道" 与 "知道且是身份问题" 是两件事。

    两条路各由谁抓:
      · 漏带     → `test_every_rule_in_validate_plan_carries_a_code`(AST 分母,机械枚举)
      · 认不出   → `test_an_unknown_code_still_falls_back_to_the_generic_sentence`
    这条钉的是**它们的行为差别**:漏带落通用句,而不是落 plan_identity 那句。
    """
    from services.defensive_geo.question_plan import PlanIdentityError

    forgotten = PlanIdentityError("将来某条新规则忘了给 code")
    assert forgotten.code is None, (
        "漏带 code 时默认成了具体值 —— 她会看到一句具体但错误的提示")

    payload = _run_the_real_except_block(forgotten, caplog)
    exp = payload.get("publicExplanation", "")
    assert "这次提交的内容有一处填得不对" in exp, f"漏带 code 没落通用句:{exp!r}"
    assert "编号对不上" not in exp, (
        "漏带 code 被安上了 plan_identity 那句 —— 具体但错误比笼统更坏")
