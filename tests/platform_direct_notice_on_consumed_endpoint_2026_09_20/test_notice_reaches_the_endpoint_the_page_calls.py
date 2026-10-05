# -*- coding: utf-8 -*-
"""WO_254 · 明示放错了端点,屏幕上到不了。

WO_241 甲我把「平台直营账号不需要进货」挂在
`GET /api/agent/inventory/purchase-options` 上,409 + 原话,实测无误。
**而进货中心那一屏根本不打这个端点** —— 它打
`GET /api/pricing/procurement/catalog`,那边对 admin 回
`PRICE_CONFIGURATION_UNAVAILABLE`「价格配置暂不可用」。
屏幕实得是前端的诚实兜底句「尚未取到平台口径说明」,**明示原话一次都没到过屏幕**。

🔴 两层病:
  ① **信号由做事方发出**:"端点回了 409 + 原话"只证明"我们发出去的东西是对的";
     要证的是"那一屏上出现了这句话",而那一屏读的是**另一条路**。
  ② **作者的毒来自作者的判据**:我的判据拿夹具直接喂这个 code 证"逐字出现",
     夹具没有经过页面真正打的那条路 ⇒ 接错线这件事**出不了题**。

所以本包的重点不是"catalog 会不会回这个码",而是
**把「页面打哪个端点」这件事变成机器能读的断言** ——
前端入口契约 `frontend/src/contracts/pricing-entry-contracts.json` 里
`agent-inventory-purchase.catalog_endpoint` 就是那条路,拿它去核后端。
"""
from __future__ import annotations

import ast
import asyncio
import io
import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from fastapi import HTTPException  # noqa: E402

from services.platform_direct_procurement import (  # noqa: E402
    PLATFORM_DIRECT_NOTICE_ENDPOINTS,
    PLATFORM_DIRECT_NO_PROCUREMENT_CODE,
    PLATFORM_DIRECT_NO_PROCUREMENT_MESSAGE,
)

CONTRACTS = REPO / "frontend" / "src" / "contracts" / "pricing-entry-contracts.json"
PRICING_API = REPO / "api" / "pricing_ssot_api.py"
WORKBENCH_API = REPO / "api" / "agent_workbench_api.py"


class _FakeState:
    def __init__(self, user):
        self.user = user


class _FakeRequest:
    """只提供被测代码真正用到的东西:`request.state.user`。"""
    def __init__(self, user):
        self.state = _FakeState(user)


def _call_catalog(user):
    from api.pricing_ssot_api import procurement_catalog
    return asyncio.run(procurement_catalog(_FakeRequest(user)))


# ══════════════════════════════════════════════════════════════════
# 一、行为:页面真正打的那个端点,对 admin 出声
# ══════════════════════════════════════════════════════════════════

def test_the_catalog_endpoint_itself_says_it():
    """🔴 本单的全部意义:出声点要在**页面读的那条路**上。"""
    with pytest.raises(HTTPException) as ei:
        _call_catalog({"user_id": 112, "is_admin": True})
    exc = ei.value
    assert exc.status_code == 409, "得到 %s" % exc.status_code
    assert exc.detail["code"] == PLATFORM_DIRECT_NO_PROCUREMENT_CODE
    assert exc.detail["message"] == PLATFORM_DIRECT_NO_PROCUREMENT_MESSAGE


def test_a_non_admin_is_not_caught_by_the_new_branch():
    """🔴 反臂:服务商走原路。

    这里不断言它成功(那需要价目表与库),只断言**它不是被这条新分支拦下的** ——
    少了这一条,把判断写成"所有人都拒"也能让上面那条绿。
    """
    try:
        _call_catalog({"user_id": 113, "is_admin": False})
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        assert detail.get("code") != PLATFORM_DIRECT_NO_PROCUREMENT_CODE, (
            "服务商被平台直营那条分支拦下了")
    except Exception:
        pass        # 缺库/缺价目表都行:本格只管那条新分支没误伤


def test_it_refuses_before_pricing_not_after():
    """🔴 要在**取价之前**拒。

    走到算价再拒,会拿一个结算异常去表达一件业务上不存在的事,
    而那个异常的文案会把厂家/买方结构说出去(WO_241 甲原话)。
    这里按行号钉:新分支的 raise 必须早于本函数里任何取目录/算价调用。
    """
    tree = ast.parse(PRICING_API.read_text(encoding="utf-8"), "pricing_ssot_api.py")
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "procurement_catalog")
    raises = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Raise)
              and "platform_direct_no_procurement" in ast.unparse(n)]
    assert raises, "catalog 里没有这条拒绝"
    pricing = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
               and any(k in ast.unparse(n.func) for k in
                       ("get_published_catalog", "build_procurement_quote_preview"))]
    assert pricing, "找不到取价调用 —— 判据前提变了"
    assert min(raises) < min(pricing), (
        "拒绝在取价之后(raise@%s vs 取价@%s)" % (min(raises), min(pricing)))


# ══════════════════════════════════════════════════════════════════
# 二、跨层:页面打哪个端点,由契约数据说了算
# ══════════════════════════════════════════════════════════════════

def _entry(entry_id):
    data = json.loads(CONTRACTS.read_text(encoding="utf-8"))
    entries = data["entries"] if isinstance(data, dict) else data
    for e in entries:
        if e.get("entry_id") == entry_id:
            return e
    raise AssertionError("入口契约里没有 %s" % entry_id)


def _paths_raising_the_helper():
    """全仓:哪些路由的 handler 会抛这条明示。

    🔴 要走**一跳**:`purchase-options` 自己不抛,它调
    `_reject_platform_direct_procurement(a)`,抛在那个 helper 里。
    只看 handler 自己的函数体会把它判成"不会出声" —— 那是仪器少扫,
    不是它真的不出声(第一版就这么红过一次)。
    """
    found = set()
    for p in sorted(REPO.rglob("api/*.py")):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"), p.name)
        except Exception:
            continue
        fns = {f.name: f for f in ast.walk(tree)
               if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
        speakers = {n for n, f in fns.items()
                    if "platform_direct_no_procurement" in ast.unparse(f)}
        for fn in fns.values():
            body = ast.unparse(fn)
            says = "platform_direct_no_procurement" in body or any(
                getattr(c.func, "id", None) in speakers
                for c in ast.walk(fn) if isinstance(c, ast.Call))
            if not says:
                continue
            for d in fn.decorator_list:
                if isinstance(d, ast.Call) and d.args and isinstance(d.args[0], ast.Constant):
                    found.add(str(d.args[0].value))
    return found


def test_the_endpoint_the_page_actually_calls_is_one_that_says_it():
    """🔴 这一格就是 WO_241 甲**当时缺的那一格**。

    页面打哪条路不靠我记忆、也不靠读 tsx 猜:入口契约 JSON 里
    `agent-inventory-purchase.catalog_endpoint` 写着。拿它去核后端。
    这条链上任何一环挪了位置,这里都会红 —— 而"出声点和被消费点错位"
    正是它唯一要问的事。
    """
    catalog_endpoint = _entry("agent-inventory-purchase")["catalog_endpoint"]
    assert catalog_endpoint == "/api/pricing/procurement/catalog", (
        "契约里的 catalog 端点变了:%s —— 先确认页面现在打哪条路" % catalog_endpoint)
    # 后端路由前缀 + 装饰器路径
    raising = _paths_raising_the_helper()
    assert any(catalog_endpoint.endswith(p) for p in raising), (
        "页面打的 %s 不在会出声的端点里(会出声的:%s)" % (catalog_endpoint, sorted(raising)))


def test_the_declared_notice_endpoints_all_really_say_it():
    """🔴 花名册:模块里登记的每一个端点都要真的会抛,不能只是写着。"""
    raising = _paths_raising_the_helper()
    missing = [ep for ep in PLATFORM_DIRECT_NOTICE_ENDPOINTS
               if not any(ep.endswith(p) for p in raising)]
    assert not missing, "登记了却不会出声的端点:%s" % (missing,)


def test_the_frontend_branches_on_this_code_from_the_catalog_result():
    """🔴 被服务方那一端:页面确实按这个 code 分支,且读的是 catalog 的结果。

    (渲染那一半归 A;这一格只钉住"后端这条码有人接" ——
     没人接的码等于没出声,和放错端点是同一种空。)
    """
    src = (REPO / "frontend" / "src" / "pages" / "Agent" / "InventoryCenter.tsx").read_text(
        encoding="utf-8")
    hit = [ln for ln in src.splitlines()
           if PLATFORM_DIRECT_NO_PROCUREMENT_CODE in ln and "catalogResult" in ln]
    assert hit, "页面没有从 catalogResult 上按这个 code 分支"


# ══════════════════════════════════════════════════════════════════
# 三、一份文案
# ══════════════════════════════════════════════════════════════════

def test_the_approved_sentence_is_not_silently_reworded():
    """🔴 Owner 09-19 裁定 ⑦「明示」定的就是这一句。

    这一格把那句话**独立写一遍**,不从模块 import ——
    上面几格比较的都是「端点回的 message == 同一个常量」,
    常量一改两边同时改,**那种比较永远为真**(拿 A 比 A)。
    注毒实测:改一个字,那些格全绿。文案是对客承诺,得有一格咬得住。
    """
    assert PLATFORM_DIRECT_NO_PROCUREMENT_MESSAGE == "平台直营账号不需要进货", (
        "对客明示文案被改了:%r —— 这句是 Owner 裁定的原话,要改先过 Owner"
        % PLATFORM_DIRECT_NO_PROCUREMENT_MESSAGE)
    assert PLATFORM_DIRECT_NO_PROCUREMENT_CODE == "PLATFORM_DIRECT_NO_PROCUREMENT", (
        "code 改名了 —— 前端按它分支,两边要一起改")


def test_the_sentence_exists_exactly_once_in_the_backend():
    """🔴 同一句话写两处,迟早有一处跟不上,而两处各自看都"对"。"""
    hits = []
    for p in sorted(REPO.rglob("*.py")):
        rel = p.relative_to(REPO).as_posix()
        if rel.startswith(("tests/", "scripts/")) or "node_modules" in rel:
            continue
        try:
            txt = p.read_text(encoding="utf-8")
        except Exception:
            continue
        n = txt.count('"%s"' % PLATFORM_DIRECT_NO_PROCUREMENT_MESSAGE)
        if n:
            hits.append((rel, n))
    assert hits == [("services/platform_direct_procurement.py", 1)], (
        "这句话的字面量出处应当只有一处,实得:%s" % (hits,))


@pytest.mark.parametrize("rel,fn_name", [
    ("api/pricing_ssot_api.py", "procurement_catalog"),
    ("api/agent_workbench_api.py", "_reject_platform_direct_procurement"),
])
def test_neither_endpoint_carries_its_own_copy(rel, fn_name):
    """🔴 两处都必须**从同一处取**,不许各自写字面量(工单点名)。"""
    tree = ast.parse((REPO / rel).read_text(encoding="utf-8"), rel)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn_name)
    body = ast.unparse(fn)
    assert "platform_direct_no_procurement" in body, "%s 没走共用 helper" % fn_name
    assert PLATFORM_DIRECT_NO_PROCUREMENT_MESSAGE not in body, "%s 里还有自己的一份文案" % fn_name
    assert PLATFORM_DIRECT_NO_PROCUREMENT_CODE not in body, "%s 里还有自己的一份码" % fn_name


def test_the_two_places_decide_platform_direct_by_the_same_rule():
    """🔴 两个入口手里的东西不同(一个有 `operating_context`,一个只有登录用户),
    但**判别规则只能有一条**:管理员 ⇒ 平台直营。

    这一格钉住 `_require_agent` 里产出 `platform_direct` 的那个分支确实由
    `is_admin` 守着 —— 哪天它改成别的条件,catalog 这边的 `is_admin` 就跟不上了,
    而两边各自看都"对"。
    """
    tree = ast.parse(WORKBENCH_API.read_text(encoding="utf-8"), "agent_workbench_api.py")
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_require_agent")
    guarded = False
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and "is_admin" in ast.unparse(node.test):
            if '"platform_direct"' in ast.unparse(node) or "'platform_direct'" in ast.unparse(node):
                guarded = True
    assert guarded, (
        "`_require_agent` 里 platform_direct 不再由 is_admin 决定 —— "
        "catalog 那边用的 is_admin 判别就跟不上了")


def test_the_operational_failure_codes_still_mean_operational_failure():
    """🔴 工单边界:`NO_PUBLISHED_PROCUREMENT` / `PRICE_CONFIGURATION_UNAVAILABLE`
    语义不变 —— 运维故障仍是故障,不许被这次改动顺手吞掉。
    """
    src = PRICING_API.read_text(encoding="utf-8")
    assert "NO_PUBLISHED_PROCUREMENT" in src
    assert "PRICE_CONFIGURATION_UNAVAILABLE" in src
