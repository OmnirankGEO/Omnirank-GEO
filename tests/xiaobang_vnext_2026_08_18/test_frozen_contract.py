"""冻结面:错误码 + 四条状态枚举(WP0「冻结 v3 schema、错误码和状态映射」)。

冻结不是"写进文档",是**双向机械核对**:

* 代码里抛了却没登记 → 前端会拿到一个没人认识的码;
* 登记了代码里却从没抛 → 死条目,让人以为存在这条路。

两个方向都要红,只做一个方向等于给自己留了半扇门。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from services import xiaobang_intent as intent

_SOURCES = (
    Path("services/xiaobang_intent.py"),
    Path("api/xiaobang_operations_api.py"),
    # [WO-B ② 2026-08-20] execute adapter 是第三个抛稳定码的地方。
    # 不加进分母的话,它抛的码全部逃过双向核对 —— 前端会拿到没人认识的码。
    Path("services/xiaobang_publish_execute.py"),
)

#: 抛点的**包装函数**。``_refuse("CODE", …)`` 与 ``IntentError("CODE", …)`` 一样是抛点。
#: 🔴 加宽匹配面必须同时加正样本,否则"加宽了"这件事本身没有判据在守 ——
#:    见 test_the_scanner_really_sees_wrapped_raises。
_RAISE_WRAPPERS = ("IntentError", "_refuse")


def _raised_codes() -> set[str]:
    """从**源码**里抠出所有实际抛出的稳定错误码。

    走 AST 而不是正则:正则会把注释和文档字符串里的示例一起算进来,
    而那些不是真的抛出点 —— 分母混进假货,双向判据就不成立了。
    """
    codes: set[str] = set()
    for path in _SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name not in _RAISE_WRAPPERS:
                continue
            if node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str):
                codes.add(node.args[0].value)
    # IntentNotFound 是 IntentError 的子类,码写在它自己的 __init__ 里。
    codes.add("INTENT_NOT_FOUND")
    # 端点里直接 raise HTTPException 的稳定码(不走 IntentError)。
    for path in _SOURCES:
        text = path.read_text(encoding="utf-8")
        codes.update(re.findall(r'"error_code":\s*"([A-Z_]+)"', text))
    return codes


def test_raised_codes_denominator_is_not_empty():
    codes = _raised_codes()
    assert len(codes) >= 15, sorted(codes)


def test_the_scanner_really_sees_wrapped_raises():
    """🔴 正样本:扫描器**真的**看见 ``_refuse(...)`` 包装的抛点。

    加宽匹配面(``_RAISE_WRAPPERS`` 多了一个名字)如果没有正样本盯着,
    等于"加宽了但没有判据在守":哪天包装名改了、或者 execute adapter 换了写法,
    那一整片码会静默退出双向核对,而两个方向的判据都还是绿的。
    """
    codes = _raised_codes()
    for code in ("PUBLISH_CHANNEL_NOT_ELIGIBLE", "PUBLISH_ARTIFACT_NOT_READY"):
        assert code in codes, (
            "扫描器没看见 _refuse 包装的抛点 —— 加宽没有生效或写法变了:"
            + str(sorted(codes)))


def test_every_raised_code_is_registered():
    unregistered = sorted(_raised_codes() - set(intent.ERROR_CODES))
    assert not unregistered, "抛了但没登记:{0}".format(unregistered)


def test_every_registered_code_is_actually_raised():
    """反方向。死条目会让人以为有这条路。"""
    dead = sorted(set(intent.ERROR_CODES) - _raised_codes())
    assert not dead, "登记了但代码里没抛(死条目):{0}".format(dead)


def test_error_code_http_status_is_sane():
    for code, status in intent.ERROR_CODES.items():
        assert status in (400, 402, 403, 404, 409, 500, 503), (code, status)
    # 不泄存在性的那两个必须是 404 —— 换成 403 就等于承认"东西在,你没权限"。
    assert intent.ERROR_CODES["INTENT_NOT_FOUND"] == 404
    assert intent.ERROR_CODES["OPERATION_NOT_FOUND"] == 404


# ── 状态枚举冻结 ──────────────────────────────────────────────────────────
def test_intent_states_are_frozen_exactly():
    assert intent.INTENT_STATES == (
        "prepared", "awaiting_confirmation", "confirmed",
        "approval_pending", "approval_rejected", "executable",
        "execution_linked", "cancelled", "expired", "superseded",
    )
    # §10.5 二选一裁定:executing 已删,不许从任何入口回来。
    assert "executing" not in intent.INTENT_STATES


def test_projection_enums_are_frozen_exactly():
    assert intent.DOMAIN_STATES == (
        "not_started", "queued", "running", "waiting_user", "succeeded",
        "failed", "cancelled", "unknown", "reconciling",
    )
    assert intent.SETTLEMENT_STATES == (
        "none", "quoted", "reserved", "committed", "released",
        "refund_pending", "refunded", "quarantined",
    )
    assert intent.EXTERNAL_STATES == (
        "not_applicable", "not_started", "started", "accepted",
        "verified_succeeded", "verified_failed", "unknown", "reconciling",
    )


# ── §19.1 #23:未知值绝不默认 failed/succeeded ────────────────────────────
def _row(**domain):
    return {
        "intent_state": "prepared", "intent_revision": 1, "side_effect": "external",
        "domain_ref": domain, "compute_quote_amount": None,
        "compute_quote_unit": "算力", "last_observed_at": None,
    }


@pytest.mark.parametrize("raw", ["weird_new_value", "SUCCESS", "Succeeded", "done", "ok"])
def test_unrecognized_domain_state_falls_to_unknown_never_to_a_terminal(raw):
    """🔴 领域给了一个我们没见过的**非空**值时,只能落 unknown。

    落 failed = 替用户宣布失败;落 succeeded = 伪造终态(H0 XB-H0-FAKE-TERMINAL)。
    ``"SUCCESS"`` / ``"Succeeded"`` / ``"done"`` 都必须落 unknown ——
    大小写和近义词都不猜。这条写这么多样本,是因为"看起来像成功"正是最容易
    被顺手翻译成 succeeded 的那一类。
    """
    state = intent.status_projection(_row(state=raw))["domain_projection"]["state"]
    assert state == "unknown", (raw, state)


@pytest.mark.parametrize("raw", [None, "", "   ", "	"])
def test_blank_domain_state_means_not_started_not_unknown(raw):
    """「还没有值」≠「值我不认识」。

    刚 prepare 完的 intent 根本没有领域任务,把它显示成"未知"是错的 ——
    未知是"我查过但说不准",not_started 是"还没开始"。两者对用户的下一步不同。
    """
    state = intent.status_projection(_row(state=raw))["domain_projection"]["state"]
    assert state == "not_started", (raw, state)


@pytest.mark.parametrize("raw", ["succeeded ", " running", "  queued  "])
def test_whitespace_is_the_only_normalization_allowed(raw):
    """两端空白剥掉 = 消除传输噪声,不是猜。

    这条和上面那条是一对:**空白可以归一,别的一律不许**。
    没有这条,实现里可以偷偷加一层"智能映射",而上面那条测不出来。
    """
    state = intent.status_projection(_row(state=raw))["domain_projection"]["state"]
    assert state == raw.strip(), (raw, state)
    assert state in intent.DOMAIN_STATES


def test_unknown_settlement_state_is_quarantined_not_none():
    """资金态认不出来时不许落 none —— none 是「没花过钱」的断言。"""
    state = intent.status_projection(
        _row(settlement_state="???"))["settlement_projection"]["state"]
    assert state == "quarantined"


def test_known_values_pass_through_unchanged():
    """正向对照。没有它,上面几条只证明它恒返回 unknown。"""
    projection = intent.status_projection(
        _row(state="running", settlement_state="reserved", external_state="accepted"))
    assert projection["domain_projection"]["state"] == "running"
    assert projection["settlement_projection"]["state"] == "reserved"
    assert projection["external_projection"]["state"] == "accepted"


def test_external_projection_defaults_by_side_effect():
    """compute_only 没有对外副作用 → not_applicable;external → not_started。"""
    row = _row()
    row["side_effect"] = "compute_only"
    assert intent.status_projection(row)["external_projection"]["state"] == "not_applicable"
    row["side_effect"] = "external"
    assert intent.status_projection(row)["external_projection"]["state"] == "not_started"
