"""扣费信封 `charged`(#79 · 2026-09-05 · 用户点名批准动 `middleware/billing.py`)。

**分母不是「返回 charged 的地方」,也不是「全部扣费点」**:
```
全部 freeze/charge 调用   99 处  ← 其中 81 处在 worker/调度,根本没有 HTTP 响应
返回 "charged" 的地方     24 处  ← 用答案定义题目
会扣费的路由处理器         7 个  ← 唯一同时「真动钱」且「有响应可带」的集合
```
改造前覆盖率 **0/7**:七个会扣费的路由**没有一个**在响应里说钱动没动,
而那 24 处 `charged` 与它们**零重叠** —— 「我们有 charged 信封」这句话成立,
却对真正扣钱的七条路**一次都没生效**。
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from middleware.billing import (
    CHARGED_NO,
    CHARGED_UNKNOWN,
    CHARGED_YES,
    charged_envelope,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHARGE_CALL = re.compile(r"\b(freeze_points|charge_on_success|commit_freeze|release_freeze)\s*\(")

#: 🔴 冻结豁免:这两个路由的扣费不在函数体内直接赋值(在嵌套/被调方里),
#:    本轮**没有**接线。**显式冻结 + 配对臂**,不让「没做」躲在「覆盖率」后面。
#: [开源 E3 · B3c G4 · 2026-09-28] cancel_task 随 C 端 GEO 方案任务 API 删除,移出豁免(豁免只剩真存在的那条)
FROZEN_UNWIRED = frozenset({"confirm_run_preview"})


def _charging_route_functions() -> dict:
    """机械枚举:带路由装饰器 且 函数体内有 freeze/charge 调用的处理器。"""
    out = {}
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith(("tests/", "scripts/", ".git/")) or "middleware/billing.py" in rel:
            continue
        try:
            src = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not CHARGE_CALL.search(src):
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            is_route = any(
                isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                and d.func.attr in ("get", "post", "put", "delete", "patch")
                for d in n.decorator_list)
            if not is_route:
                continue
            seg = ast.get_source_segment(src, n) or ""
            if CHARGE_CALL.search(seg):
                out[n.name] = (rel, seg)
    return out


# ══════════════════════════════════════════════════════════════════════════
# 信封语义:三态,且没有真值陷阱
# ══════════════════════════════════════════════════════════════════════════

def test_state_is_a_string_enum_not_a_mixed_type():
    """🔴 `happened ∈ {true,false,"unknown"}` 那种混类型会掉进**真值陷阱**。

    前端 `if (charged.happened)` 会把 `"unknown"` 当成「扣了」——
    而 unknown 的语义恰恰是「我们也不知道」。三态压回布尔是本仓记过账的病。
    所以用纯字符串枚举:三个取值都是 str,谁都不为真也不为假地"顺便"成立。
    """
    for v in (CHARGED_YES, CHARGED_NO, CHARGED_UNKNOWN):
        assert isinstance(v, str) and v
    assert len({CHARGED_YES, CHARGED_NO, CHARGED_UNKNOWN}) == 3


@pytest.mark.parametrize("result,kw,want", [
    (None, {}, CHARGED_NO),
    ({}, {}, CHARGED_NO),
    ({"free": True, "amount": 0}, {}, CHARGED_NO),
    ({"admin_exempt": True, "amount": 0}, {}, CHARGED_NO),
    ({"freeze_id": 9, "amount": 0}, {}, CHARGED_NO),
    ({"freeze_id": 9, "amount": 650}, {}, CHARGED_YES),
    (None, {"unknown": True}, CHARGED_UNKNOWN),
    ("不认识的形状", {}, CHARGED_UNKNOWN),
])
def test_envelope_states(result, kw, want):
    assert charged_envelope(result, **kw)["state"] == want


def test_unknown_is_never_collapsed_into_no():
    """🔴 把 unknown 压成 `no` 是在**承诺一件我们没验证的事**。"""
    env = charged_envelope(None, unknown=True)
    assert env["state"] == CHARGED_UNKNOWN
    assert env["reason"], "unknown 必须带一句对客的话,否则她只看到一个词"


def test_envelope_is_pure():
    """信封是纯派生:同样输入两次结果相同,且不改动入参。"""
    src = {"freeze_id": 9, "amount": 650}
    before = dict(src)
    a, b = charged_envelope(src), charged_envelope(src)
    assert a == b and src == before


# ══════════════════════════════════════════════════════════════════════════
# 覆盖:分母机械求出,豁免显式冻结
# ══════════════════════════════════════════════════════════════════════════

def test_the_route_path_list_covers_every_charging_route():
    """🔴 `CHARGING_ROUTE_PATHS` 是手写的,但**分母是机械求出的** ——

    手写名单漏掉的那一条不会让任何判据变红,除非有人去数真集。
    """
    import server

    found = _charging_route_functions()
    # [开源 E3 · B3c G4 · 2026-09-28] 7 → 5:geo-plan start_task / cancel_task 随 C 端 GEO 方案任务 API 删除;
    #   剩 start_diagnosis / run_monitoring / api_redraw_card / self_serve / confirm_run_preview
    assert len(found) >= 5, "机械枚举只找到 %d 个会扣费的路由 —— 分母可疑:%s" % (
        len(found), sorted(found))
    assert len(server.CHARGING_ROUTE_PATHS) >= len(found), (
        "路径清单 %d 条 < 会扣费的路由 %d 个 —— 有路由的错误响应不会带信封"
        % (len(server.CHARGING_ROUTE_PATHS), len(found)))


def _assigns_charged_envelope(seg: str) -> bool:
    """函数体内是否**真的**给 `<something>.state.charged_envelope` 赋过值。"""
    try:
        tree = ast.parse(seg)
    except SyntaxError:
        tree = ast.parse(seg.strip())
    for n in ast.walk(tree):
        if not isinstance(n, ast.Assign):
            continue
        for tg in n.targets:
            if (isinstance(tg, ast.Attribute) and tg.attr == 'charged_envelope'
                    and isinstance(tg.value, ast.Attribute)
                    and tg.value.attr == 'state'):
                return True
    return False


def test_the_assignment_detector_is_not_fooled_by_an_import():
    """🔴 正样本自证:光有 import 不算接线(毒 H6 就活在这个缝里)。"""
    only_import = ('def h(request):' + chr(10)
                   + '    from middleware.billing import charged_envelope as _c' + chr(10)
                   + '    return 1' + chr(10))
    real = ('def h(request):' + chr(10)
            + '    request.state.charged_envelope = 1' + chr(10))
    assert _assigns_charged_envelope(only_import) is False, '只有 import 却算成接线了'
    assert _assigns_charged_envelope(real) is True, '真赋值却没认出来 —— 尺子坏了'


def test_every_charging_route_records_what_it_charged():
    """🔴 路由必须在扣费调用之后**赋值** `state.charged_envelope`。

    不写 ⇒ 处理器兜底 `no`。**扣完之后再报错的那一格会说「没扣」—— 那是错的**,
    而它看起来完全正常。

    🔴 判「有没有赋值」而不是「文本里有没有这个名字」:实测毒 H6 只删掉赋值行、
    留下 `from middleware.billing import charged_envelope as _chg_env` 这行 import,
    **包含判定照绿**。这是同一个病今天的第六张脸 ——
    短路 / 旁路 / 常量右值 / 换赋值目标 / 判据自构 / **import 行顶住名字**。
    """
    found = _charging_route_functions()
    unwired = sorted(name for name, (_rel, seg) in found.items()
                     if not _assigns_charged_envelope(seg))
    extra = set(unwired) - FROZEN_UNWIRED
    assert not extra, '这些会扣费的路由没记录扣费结果:%s' % sorted(extra)


def test_the_frozen_exemptions_are_not_dead_entries():
    """🔴 配对臂:冻结豁免名单里的每一项都必须**仍然存在且仍未接线**。

    豁免一旦变成死条目,它就成了一个「看起来在管、其实什么都不管」的名单 ——
    比没有名单更坏。
    """
    found = _charging_route_functions()
    for name in FROZEN_UNWIRED:
        assert name in found, "豁免项 %s 已不是会扣费的路由 —— 该把它从名单里删掉" % name
        assert not _assigns_charged_envelope(found[name][1]), (
            "豁免项 %s 其实已经接线了 —— 名单过期,应当移出豁免" % name)


def test_the_handler_passes_through_non_charging_paths():
    """🔴 全局 handler 只允许一个:**非扣费路径必须原样交回 FastAPI 默认实现**。

    与仓里那个 422 handler 同一条纪律 —— 顺手改掉别的模块的错误形状是回归。
    """
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_charged_envelope_handler":
            seg = ast.unparse(n)
            assert "CHARGING_ROUTE_PATHS" in seg and "_default_http_exc" in seg
            assert "not in CHARGING_ROUTE_PATHS" in seg, "没有 pass-through 分支"
            return
    pytest.fail("找不到 _charged_envelope_handler")


def test_billing_change_is_additive_only():
    """🔴 保护文件只许**加派生**,不许碰冻结/扣费/退费任何语义。

    这条钉住那几个函数仍在(签字条要的「最小 diff」由 Review 逐行核,
    这里守的是「没被顺手删掉/改名」)。
    """
    import middleware.billing as B

    for fn in ("freeze_points", "commit_freeze", "release_freeze",
               "charge_on_success", "refund_points", "deduct_points"):
        assert callable(getattr(B, fn, None)), "billing.%s 不见了" % fn
