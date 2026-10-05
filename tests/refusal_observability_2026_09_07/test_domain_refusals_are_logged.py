"""#142 · 领域拒绝必须在服务端留痕。

## 缺陷

`BUSINESS_IDENTITY_SSOT_UNAVAILABLE` 这一类拒绝生产上「24 小时零次」——
不是没发生,是**没人写**。翻译点把领域异常变成 `HTTPException` 直接抛,
整条路径一行日志都没有。

🔴 被正确捕获的错误不留痕:它在日志里与「从未发生过」**完全同形**。
   于是「零次」既可能是真没发生、也可能是发生了很多次,
   而这两个结论导向完全相反的处置。

## 分母不是「一个函数」

机械枚举发现能吐出这个码的是**四个 funnel**,其中两个是**内联**的
(`agent_workbench_api` / `partner_api`),不走共享翻译器。
只修翻译器就只盖住 2/4 —— 所以下面的结构锁**从代码反查分母**,
不写死清单:哪天有人加第五个 funnel,它会红。
"""

from __future__ import annotations

import ast
import io
import logging
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: 领域拒绝异常族。捕获了它们、又要返 4xx 的函数,就是本锁的分母。
_DOMAIN_EXCEPTIONS = {
    "GovernanceValidationError",
    "GovernanceVersionConflict",
    "GovernanceNotFound",
    "InventoryAdminError",
}


def _handler_functions():
    """**从代码反查**分母:api/ 下所有 catch 了领域拒绝异常的函数。

    🔴 手写清单会漏项,而漏掉的那一项不会让任何判据变红(本仓栽过多次)。
       所以分母是枚举出来的,数字只当下限。
    """
    found = []
    for path in sorted((ROOT / "api").rglob("*.py")):
        try:
            tree = ast.parse(io.open(path, encoding="utf-8").read())
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            names = set()
            for node in ast.walk(fn):
                if isinstance(node, ast.ExceptHandler) and node.type is not None:
                    for t in (node.type.elts if isinstance(node.type, ast.Tuple)
                              else [node.type]):
                        nm = getattr(t, "id", None) or getattr(t, "attr", None)
                        if nm:
                            names.add(nm)
                # 翻译器不是 except,而是 isinstance 分派 —— 也算进分母
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "isinstance":
                    for a in node.args[1:]:
                        nm = getattr(a, "id", None) or getattr(a, "attr", None)
                        if nm:
                            names.add(nm)
            if names & _DOMAIN_EXCEPTIONS:
                found.append((path.relative_to(ROOT).as_posix(), fn))
    return found


def test_every_domain_refusal_funnel_goes_through_the_logging_gate():
    """🔴 承重锁:处理领域拒绝的函数里,不许有**裸** `raise HTTPException`。

    毒:把任何一个 funnel 改回裸 `raise HTTPException(...)` ⇒ 红。

    为什么锁「零裸抛」而不是「有 refuse 调用」:后者在**部分**改造的情况下
    照样绿 —— 一个函数里三处抛、只有一处走了门,它仍然"有 refuse 调用"。
    """
    funnels = _handler_functions()
    assert len(funnels) >= 4, "分母塌了,只找到 %d 个 funnel" % len(funnels)

    offenders = []
    for path, fn in funnels:
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
                continue
            f = node.exc.func
            nm = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if nm == "HTTPException":
                offenders.append("%s::%s:%d" % (path, fn.name, node.lineno))

    assert not offenders, (
        "这些拒绝点绕过了日志门,生产上会再次「零次」:\n  " + "\n  ".join(offenders))


def test_the_gate_is_actually_imported_where_it_is_used():
    """结构臂:用了 `refuse` 的模块必须真导入它。

    没有这条,上一条锁可以被一个**根本跑不起来**的模块骗过
    (裸抛没了、但 `refuse` 是个 NameError)。

    🔴 按 **AST 取真实 Call 节点**,不扫源码文本:
       `refusal_log.py` 自己的 docstring 里就写着 `raise refuse(`,
       扫文本会把它算成调用点(本仓在 #139 栽过同一个坑)。
    """
    checked = 0
    for path in sorted((ROOT / "api").rglob("*.py")):
        src = io.open(path, encoding="utf-8").read()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "refuse"]
        if not calls:
            continue
        checked += 1
        imported = any(
            isinstance(n, ast.ImportFrom) and n.module == "api.refusal_log"
            and any(a.name == "refuse" for a in n.names)
            for n in ast.walk(tree))
        assert imported, "%s 有 %d 处 refuse 调用却没导入它" % (path.name, len(calls))
    assert checked >= 4, "分母塌了:只有 %d 个模块用了这道门" % checked


# ------------------------------------------------------------------ 行为臂

def test_refuse_writes_one_warning_carrying_the_code(caplog):
    """🔴 本单那一格:拒绝**真的**写了一行日志,而且带着码。

    打行为不打「有没有调用 logger」—— 后者证不了这行能被 grep 到。
    毒:把 `refuse` 里的 `logger.warning` 删掉 ⇒ 红。
    """
    from api.refusal_log import refuse

    log = logging.getLogger("test-refusal")
    with caplog.at_level(logging.WARNING, logger="test-refusal"):
        refuse(log, status=409, code="BUSINESS_IDENTITY_SSOT_UNAVAILABLE",
               detail={"code": "BUSINESS_IDENTITY_SSOT_UNAVAILABLE", "message": "x"},
               context={"user": 174})

    recs = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(recs) == 1, "拒绝没留下**恰好一行** WARNING:%r" % recs
    text = recs[0].getMessage()
    assert "BUSINESS_IDENTITY_SSOT_UNAVAILABLE" in text, text
    assert "user=174" in text, "上下文没进日志,运维查不到是谁被挡了:%s" % text


def test_refuse_does_not_reshape_the_detail(caplog):
    """🔴 `detail` **原样**进响应 —— 本单只多写一行日志,不动用户可见契约。

    今天有的调用点传 dict、有的传**字符串**(partner_api 的版本冲突那条)。
    把字符串包成 dict 会让前端读到的形状变掉,而那不是本单的授权范围。
    毒:让 `refuse` 把 detail 统一成 dict ⇒ 红。
    """
    from api.refusal_log import refuse

    log = logging.getLogger("test-refusal-shape")
    with caplog.at_level(logging.WARNING, logger="test-refusal-shape"):
        as_dict = refuse(log, status=409, code="C", detail={"code": "C", "message": "m"})
        as_str = refuse(log, status=409, code="C", detail="账号身份正在被其他操作变更")

    assert as_dict.detail == {"code": "C", "message": "m"}
    assert as_str.detail == "账号身份正在被其他操作变更"
    assert isinstance(as_str.detail, str), "字符串 detail 被改了形状"
    assert as_dict.status_code == 409 and as_str.status_code == 409


def test_a_real_governance_refusal_logs_and_keeps_its_status(caplog):
    """端到端那一格:走**真的**翻译器,不是直接调 `refuse`。

    只测 `refuse` 本身证不了它被接在拒绝路径上 —— 那是「机制存在」
    不是「机制生效」。
    """
    from api.admin_user_governance_api import _raise_governance_error
    from services.admin_user_governance import GovernanceValidationError
    from fastapi import HTTPException

    exc = GovernanceValidationError(
        "BUSINESS_IDENTITY_SSOT_UNAVAILABLE", "用户业务身份暂时无法确认")

    with caplog.at_level(logging.WARNING, logger="GEO-Admin-User-Governance"):
        with pytest.raises(HTTPException) as got:
            _raise_governance_error(exc)

    assert got.value.status_code == 409, got.value.status_code
    assert got.value.detail["code"] == "BUSINESS_IDENTITY_SSOT_UNAVAILABLE"
    hits = [r for r in caplog.records
            if "BUSINESS_IDENTITY_SSOT_UNAVAILABLE" in r.getMessage()]
    assert hits, "真实拒绝路径仍然没留痕 —— 生产会继续「零次」"


def test_the_translator_still_reraises_unknown_exceptions():
    """反向臂:不认识的异常必须**原样抛**,不许被门吞掉。

    一道把所有异常都变成 4xx 的门,会把真 bug 伪装成业务拒绝。
    """
    from api.admin_user_governance_api import _raise_governance_error

    boom = RuntimeError("不是领域拒绝")
    with pytest.raises(RuntimeError):
        _raise_governance_error(boom)
