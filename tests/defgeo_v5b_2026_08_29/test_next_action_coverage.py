"""【V5-B · Codex fix-of-fix3 P2-NEW-5】nextAction 的**跨层** census。

被测缺陷(Codex 原话)
--------------------
① 真实 ``POLICY_UNAVAILABLE`` 分支给 ``new_preview``,但通用 reason 文案仍说
   「帮你转给平台客服处理」;用户会同时看到"转客服"和"重新看报价"。
② 余额不足返回 ``top_up``/wallet target,但 LaunchPanel 只对 ``new_preview``、
   ``retry`` 画可点击按钮,``top_up`` 等只显示文字。

为什么这条判据只能在 pytest 里
------------------------------
它要同时读**后端**(服务端能发出哪些 kind、每个 kind 配的是哪句话)与**前端**
(哪些 kind 被接上了真实路由)。``frontend/scripts/*.mjs`` 在 Dockerfile 的
frontend-builder 阶段够不到后端源码,``verify-no-backend-refs-in-build-chain``
会把读后端的脚本判红(判得对)。所以跨层那一半照本仓惯例搬到 pytest
(与 ``tests/defensive_geo_pkgh_2026_08_23/test_pkgh_cross_layer.py`` 同一处置)。

分母怎么来
----------
服务端能塞进错误信封的 nextAction kind = 三处的并集,**机械枚举**:
  ⓐ ``_ERROR_ACTIONS`` 默认表里每个 code 配的 kind;
  ⓑ 所有 ``_action("字面量", …)`` 调用;
  ⓒ ``_action(<变量>, …)`` 那一处的候选集 —— 资金矩阵每一格的
     ``insufficient_actions`` 全集。**必须算进来**:本仓记过「出口 census
     含变量形参调用点」,只扫字面量会把 ``top_up`` 整个漏掉,
     而 ``top_up`` 正是本条 finding 的主角。
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
API = REPO / "api" / "defensive_geo_api.py"
ASSIST = REPO / "api" / "defensive_geo_assist_api.py"
NEXT_ACTION_TS = REPO / "frontend" / "src" / "components" / "defensiveGeo" / "nextActionRoute.ts"
APP_TSX = REPO / "frontend" / "src" / "App.tsx"

#: 🔴 前端**刻意**只出文字、不给按钮的 kind —— 每一条都要写清"为什么没有落点"。
#: 冻结集:新增一个 kind 落进这里,必须由人确认站内**真的**没有它的落点,
#: 而不是"忘了接"。
FROZEN_NO_DESTINATION: dict[str, str] = {
    "wait": "不是一个动作,是一句「稍等」;这一屏没有可点的东西",
    "view_existing_command": "已开始那次体检的进度/结果由外层容器接管,不由本面板跳",
    "request_approval": "defgeo 侧现役没有审批页(只有社媒的 /s/approval/:token,不同板块)",
    "request_budget_approval": (
        "服务端给它的 target 是个人钱包页;把组织成员导到个人钱包"
        "等于让他自己掏钱替组织付 —— 在站内出现真正的团队预算入口前不接"),
    "reduce_plan": "缩题单是**这一页上方**题单编辑区的动作,不是另一个页面",
    "review_question_plan": "同上,题单就在这一页上方",
    "fix_input": "「返回修改」指的也是本页表单;跳走反而把她填的东西丢了",
    "retry_later": "「稍后再试一次」是一句建议,不是一个落点",
    "switch_to_agent_account": (
        "换账号意味着重新登录。站内有 /login,但从一个错误框把她直接踢去登录页"
        "会丢掉她这一页填的东西 —— 标签本身已经把该做的事说清楚了,这里不代她做"),
}


# ══════════════════════════════════════════════════════════════════════════
# 分母:服务端能发出的 kind 全集
# ══════════════════════════════════════════════════════════════════════════
def _literal_action_kinds(path: pathlib.Path) -> set[str]:
    """``_action("字面量", …)`` 的第一个实参。"""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    out: set[str] = set()
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
        if name != "_action" or not n.args:
            continue
        a0 = n.args[0]
        if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
            out.add(a0.value)
    return out


def _variable_action_call_sites(path: pathlib.Path) -> list[int]:
    """``_action(<非字面量>, …)`` 的行号 —— 这些点的候选集必须另行补进分母。"""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    out: list[int] = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
        if name != "_action" or not n.args:
            continue
        a0 = n.args[0]
        if not (isinstance(a0, ast.Constant) and isinstance(a0.value, str)):
            out.append(n.lineno)
    return out


def _default_table_kinds() -> set[str]:
    """``_ERROR_ACTIONS`` 之类的默认表:每个 code 配的 (reason, kind, target)。"""
    from api import defensive_geo_api as api

    out: set[str] = set()
    for name in dir(api):
        val = getattr(api, name)
        if not isinstance(val, dict) or not val:
            continue
        for v in val.values():
            if isinstance(v, tuple) and len(v) == 3 and all(
                    isinstance(x, str) for x in v[:2]):
                out.add(v[1])
    return out


def _funding_matrix_kinds() -> set[str]:
    from services.defensive_geo import funding_projection as fp

    out: set[str] = set()
    for name in dir(fp):
        val = getattr(fp, name)
        if isinstance(val, dict):
            for cell in val.values():
                acts = getattr(cell, "insufficient_actions", None)
                if acts:
                    out.update(acts)
    return out


#: LaunchPanel 只渲染**这两个端点**的错误信封(`createRunPreview` / `confirmRunPreview`)。
#: 别的端点(Z-3.1 补齐、Z-3.3 广告法、发布链)有它们自己的面板与自己的动作集。
ENTRY_FUNCS = ("create_run_preview", "confirm_run_preview")


def _same_module_reachable(path: pathlib.Path, roots: tuple[str, ...]) -> set[str]:
    """从 ``roots`` 出发,在**同一模块内**取传递闭包的函数名集合。

    🔴 第一版把分母开成了「全仓所有 _action 调用」,于是 Z-3.1 / Z-3.3 /
       发布链的 14 个 kind 也被要求"前端必须分类" —— 而 LaunchPanel 根本
       收不到那些信封。判据的作用域必须**等于结论的作用域**:
       本条的结论是「LaunchPanel 面对的 kind 全都有人决定」,
       那分母就只能是这两个端点(及它们在本模块内调到的东西)能发出的那些。
    """
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    calls: dict[str, set[str]] = {}
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names: set[str] = set()
        for sub in ast.walk(n):
            if isinstance(sub, ast.Call):
                f = sub.func
                names.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
        calls[n.name] = names
    reach = {r for r in roots if r in calls}
    assert reach, "两个入口函数一个都没找到 —— 锚点过期:%s" % roots
    changed = True
    while changed:
        changed = False
        for fn in list(reach):
            for nxt in calls.get(fn, ()):
                if nxt in calls and nxt not in reach:
                    reach.add(nxt)
                    changed = True
    return reach


def _kinds_in_functions(path: pathlib.Path, funcs: set[str]) -> set[str]:
    """这些函数体内 ``_action("字面量")`` + 无显式 next_action 的
    ``_safe_error("CODE")`` 经默认表映射出来的 kind。"""
    from api import defensive_geo_api as api

    default = {}
    for name in dir(api):
        val = getattr(api, name)
        if isinstance(val, dict):
            for code, v in val.items():
                if isinstance(v, tuple) and len(v) == 3 and all(
                        isinstance(x, str) for x in v[:2]):
                    default[code] = v[1]

    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    out: set[str] = set()
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) or n.name not in funcs:
            continue
        for sub in ast.walk(n):
            if not isinstance(sub, ast.Call):
                continue
            f = sub.func
            fname = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
            if fname == "_action" and sub.args and isinstance(sub.args[0], ast.Constant):
                out.add(sub.args[0].value)
            if fname == "_safe_error" and sub.args and isinstance(sub.args[0], ast.Constant):
                has_explicit = any(kw.arg == "next_action" for kw in sub.keywords)
                if not has_explicit and sub.args[0].value in default:
                    out.add(default[sub.args[0].value])
    return out


def server_action_kinds() -> set[str]:
    """LaunchPanel 能收到的 nextAction kind 全集。

    三块的并集:
      ⓐ 两个端点(及它们在本模块内调到的函数)里的 ``_action("字面量")``;
      ⓑ 同一批函数里**没有**显式 next_action 的 ``_safe_error("CODE")``
         经默认表映射出来的 kind;
      ⓒ ``_action(<变量>, …)`` 那一处的候选集 = 资金矩阵每格的
         ``insufficient_actions`` 全集(``top_up`` 只从这里进来)。
    """
    reachable = _same_module_reachable(API, ENTRY_FUNCS)
    return _kinds_in_functions(API, reachable) | _funding_matrix_kinds()


# ══════════════════════════════════════════════════════════════════════════
# 前端:三档分类
# ══════════════════════════════════════════════════════════════════════════
def _frontend_tiers() -> tuple[set[str], dict[str, str]]:
    """(in_panel kinds, {kind: href})。从 TS 源码机械抽,不手抄第二份。"""
    ts = NEXT_ACTION_TS.read_text(encoding="utf-8", errors="replace")
    in_panel_block = re.search(r"IN_PANEL_KINDS[^=]*=\s*\[(.*?)\]", ts, re.S)
    assert in_panel_block, "抽不出 IN_PANEL_KINDS —— 锚点过期"
    in_panel = set(re.findall(r"'([a-z_]+)'", in_panel_block.group(1)))

    routes_block = re.search(r"KIND_ROUTES[^=]*=\s*\{(.*?)\n\};", ts, re.S)
    assert routes_block, "抽不出 KIND_ROUTES —— 锚点过期"
    routes: dict[str, str] = {}
    for m in re.finditer(r"^\s*([a-z_]+):\s*(.+?),\s*$", routes_block.group(1), re.M):
        routes[m.group(1)] = m.group(2).strip()
    return in_panel, routes


# ══════════════════════════════════════════════════════════════════════════
# 00 · 分母活性
# ══════════════════════════════════════════════════════════════════════════
def test_00_denominator_is_alive() -> None:
    kinds = server_action_kinds()
    assert len(kinds) >= 12, "服务端 kind 全集只扫到 %d 个 —— 分母塌了:%s" % (
        len(kinds), sorted(kinds))
    assert "new_preview" in kinds and "contact_support" in kinds, \
        "连最常见的两个 kind 都没扫到:%s" % sorted(kinds)


def test_01_variable_action_call_sites_are_covered() -> None:
    """``_action(<变量>, …)`` 的候选集必须真的进了分母。

    🔴 本仓记过「出口 census 含变量形参调用点」。只扫字面量的话,
       ``top_up`` 会整个漏掉 —— 而它正是本条 finding 的主角
       (它只从资金矩阵经变量传进 ``_action``)。
    """
    sites = _variable_action_call_sites(API)
    assert sites, "一个变量形参的 _action 调用点都没有 —— 锚点过期,本条恒真"
    assert "top_up" not in _literal_action_kinds(API), \
        "top_up 现在有字面量调用点了 —— 这条断言的前提变了,回来复核"
    assert "top_up" in _funding_matrix_kinds(), "资金矩阵里没有 top_up —— 分母缺了变量那一半"
    assert "top_up" in server_action_kinds(), "top_up 没进分母 —— census 会漏掉整条 finding"


# ══════════════════════════════════════════════════════════════════════════
# 02 · 全集分区:in_panel ⊎ navigate ⊎ 冻结的"无落点",不留第四类
# ══════════════════════════════════════════════════════════════════════════
def test_02_every_server_kind_is_classified() -> None:
    kinds = server_action_kinds()
    in_panel, routes = _frontend_tiers()
    unclassified = sorted(k for k in kinds
                          if k not in in_panel and k not in routes
                          and k not in FROZEN_NO_DESTINATION)
    assert not unclassified, (
        "服务端能发出这些 nextAction kind,而前端既没接落点、也没登记「站内无落点」:\n"
        "  %s\n"
        "落进第三档不是问题,**没人决定**才是问题:她会看到一句"
        "「点这里做什么什么」而屏幕上什么都不会发生。" % unclassified)


def test_03_no_stale_entries_in_the_frozen_set() -> None:
    """冻结集不许留过期项 —— 过期的豁免集会把新洞藏在里面。"""
    kinds = server_action_kinds()
    stale = sorted(k for k in FROZEN_NO_DESTINATION if k not in kinds)
    assert not stale, (
        "这几个 kind 服务端已经不再发了,却还留在「站内无落点」冻结集里:%s" % stale)


def test_04_top_up_is_wired_to_a_real_route() -> None:
    """本条 finding 的正样本:余额不足那一档**接上了**真实路由。"""
    _in_panel, routes = _frontend_tiers()
    assert "top_up" in routes, (
        "top_up 没有落点 —— 站内明明有 /wallet,只显示「去充值算力」五个字"
        "等于把她推回去自己找路")
    assert "/wallet" in routes["top_up"], "top_up 的落点不是钱包页:%r" % routes["top_up"]
    app = APP_TSX.read_text(encoding="utf-8", errors="replace")
    assert re.search(r'path="wallet"', app), "App.tsx 里没有 wallet 路由 —— 接上去会 404"


def test_05_org_budget_is_not_wired_to_the_personal_wallet() -> None:
    """配对的必须不命中:组织预算不足**不许**导到个人钱包。

    服务端那一处 ``_action(options[0], target={"kind":"page","page":"wallet"})``
    对组织格给的是 ``request_budget_approval`` + **个人钱包** target。
    前端照着 target 接的话,组织成员会被送去用自己的钱充值 ——
    那正是资金矩阵注释里写着「绝不给个人钱包充值」要防的事。
    """
    _in_panel, routes = _frontend_tiers()
    assert "request_budget_approval" not in routes, \
        "request_budget_approval 被接上了路由:%r" % routes.get("request_budget_approval")
    assert "request_budget_approval" in FROZEN_NO_DESTINATION, \
        "它必须显式登记在「站内无落点」里并写清理由,而不是悄悄不接"


# ══════════════════════════════════════════════════════════════════════════
# 06 · 文案与动作必须一致(P2-NEW-5 ①)
# ══════════════════════════════════════════════════════════════════════════
#: reason key → 它的文案里**不许**出现的字眼(因为配的动作不是那个意思)。
#: 只放"指向另一条路"的词,不做泛泛的措辞审查。
REASON_ACTION_CONSISTENCY = {
    "pricing_deactivated_after_preview": ("客服", "转给"),
}


def test_06_confirm_pricing_branch_uses_its_own_reason_key() -> None:
    """confirm 里「预览之后价目停用」那一支必须用**自己**的 reason key。

    改动前它复用 ``policy_unavailable``,而那句话写的是「帮你转给平台客服处理」——
    屏幕上于是同时出现"等人处理"和一颗"重新发起体检"的按钮,两条指令打架。
    """
    src = API.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    found = False
    for n in ast.walk(tree):
        if not isinstance(n, ast.ExceptHandler):
            continue
        names = set()
        if n.type is not None:
            targets = n.type.elts if isinstance(n.type, ast.Tuple) else [n.type]
            names = {getattr(t, "id", getattr(t, "attr", "")) for t in targets}
        if "PricingCatalogUnreadable" not in names:
            continue
        reasons, kinds = set(), set()
        for sub in ast.walk(n):
            if not isinstance(sub, ast.Call):
                continue
            f = sub.func
            fname = getattr(f, "id", getattr(f, "attr", None))
            if fname == "_safe_error":
                for kw in sub.keywords:
                    if kw.arg == "reason_key" and isinstance(kw.value, ast.Constant):
                        reasons.add(kw.value.value)
            if fname == "_action" and sub.args and isinstance(sub.args[0], ast.Constant):
                kinds.add(sub.args[0].value)
        if "new_preview" not in kinds:
            continue                      # 另一支(preview 阶段)配的是 contact_support,不在本条
        found = True
        assert reasons == {"pricing_deactivated_after_preview"}, (
            "给 new_preview 的那一支用的 reason key 是 %s —— 它必须有自己的那一句,"
            "不能复用写着「转给平台客服」的通用文案" % (reasons or "无"))
    assert found, "找不到「PricingCatalogUnreadable + new_preview」那一支 —— 锚点过期"


@pytest.mark.parametrize("reason_key,banned", sorted(REASON_ACTION_CONSISTENCY.items()))
def test_07_reason_copy_does_not_point_somewhere_else(reason_key, banned) -> None:
    """那句话里不许出现"去找别人"的指向 —— 它配的动作是"你自己再来一次"。"""
    from services.defensive_geo.copy_registry import census

    text = census()["entries"]["reason"].get(reason_key)
    assert text, "registry 里没有 %s —— 锚点过期" % reason_key
    hit = [w for w in banned if w in text]
    assert not hit, (
        "%s 的文案里出现了 %s:%r —— 它配的 nextAction 是「重新发起体检」,"
        "文案却把她指向别处,两条指令打架" % (reason_key, hit, text))
    assert "没有扣除任何算力" in text, \
        "涉钱失败必须显式说清钱没动:%r" % text


def test_08_generic_policy_copy_still_matches_its_own_action() -> None:
    """配对的必须命中:通用那一句**仍然**配 contact_support,不许被顺手改掉。

    现役五个 POLICY_UNAVAILABLE 出口里有四个真的是"我们这边读不出来",
    转客服是对的。本单只拆出第五个,不动那四个。
    """
    from api import defensive_geo_api as api
    from services.defensive_geo.copy_registry import census

    text = census()["entries"]["reason"]["policy_unavailable"]
    assert "客服" in text, "通用 policy_unavailable 的文案被改得不再指向客服了:%r" % text
    table = {v[0]: v for name in dir(api)
             for v in (getattr(api, name).values() if isinstance(getattr(api, name), dict) else [])
             if isinstance(v, tuple) and len(v) == 3 and all(isinstance(x, str) for x in v[:2])}
    assert table.get("policy_unavailable", (None, None))[1] == "contact_support", \
        "默认表里 policy_unavailable 不再配 contact_support —— 那四个出口的文案就也对不上了"
