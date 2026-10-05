"""【A-2 = Codex fix-of-fix2 P1-4】延期端点没有对象归属校验 —— **接线 census**。

Codex 原文
----------
    ``POST /api/keyword-selection/{token}/extend`` 没有 ``Request``,也不调用
    ``_require_session_owner_access``。全局 JWT / quote 模块权限只说明
    「能用报价模块」,不能证明「拥有这个 session」。任何 quote-enabled 登录用户
    拿到其他租户的客户分享 token 后,都可触达该写入。

我的证伪结果:坐实。底 ``5f5884893`` 上 ``extend_session(token: str)``
连 ``Request`` 形参都没有 —— 不是校验写错了,是这一格根本没接。

为什么光修一处不够
------------------
「11 个端点接了、1 个漏了」这种形状,下一次还会以同样的方式复发:
新加一个 ``{token}`` 写端点时,没有任何东西会提醒作者接归属校验,
而漏掉的那一个**不会让任何判据变红**。所以除了补 extend,
还把「本文件里所有 ``{token}`` 写端点」做成一个**机械分母**,
每一个要么接了校验、要么落在显式冻结的豁免集里。
"""
from __future__ import annotations

import pytest

# 🔴 [工单 V5-B · Codex fix-of-fix3 P2-NEW-4] 按**文件路径**载入,不走
#    `from tests.defgeo_v5a_2026_08_28 import _census`。
#    反例:宿主装了一个正规的 `site-packages/tests` 包、而本仓 `tests/` 又不是包时,
#    那句 import 会解析到**别人的** tests,collection 直接 ModuleNotFoundError。
#    失败关闭不是假绿,但它会让整包在别的机器上跑不起来。
#    路径载入对 sys.path 顺序免疫;载入后再断言它确实来自本仓(见 test_00)。
import importlib.util as _ilu
import pathlib as _pl

_spec = _ilu.spec_from_file_location(
    "defgeo_v5a_census", _pl.Path(__file__).with_name("_census.py"))
C = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(C)

SELECTION_API = "api/selection_api.py"

#: 🔴 显式豁免集 —— ``/s/{token}`` 是**C 端 token-only 页面**。
#:
#: 客户不登录(CLAUDE.md「用户定位铁律」:客户 = 链接接收方 · token-only · 不登录),
#: 所以这些 handler 里根本没有"当前用户"可以拿来跟 session 的 owner 比。
#: 它们的授权模型是**持有 token 即授权**,那是这一层的设计,不是漏掉的校验。
#:
#: 集合钉死 + 大小钉死:再多一个 ``/s/{token}`` 写端点必须由人确认它真是 C 端,
#: 而不是把一个销售端端点错放进了 C 端前缀下。
FROZEN_CEND_EXEMPT = {
    "/s/{token}/submit-keywords",
    "/s/{token}/submit-business-lines",
    "/s/{token}/withdraw-keywords",
    "/s/{token}/add-keywords",
    "/s/{token}/cancel-add-keywords",
    "/s/{token}/confirm-quote",
    "/s/{token}/events",
    # [#178 · 2026-09-12 · 窗口 C 声明] 客户在「暂无可交付问法」卡片上点
    # 「让报价方补充」时调它。确认过它真是 C 端:调用方是**未登录**的客户页,
    # handler 里没有当前用户可比;副作用上限是给品牌 owner 的 outbox 里加**至多一条**
    # 通知(event_key 唯一约束兜幂等),且带状态守卫——会话不在"全排除"态时 400。
    # 本条正是这道闸设计来拦的东西:新加 {token} 写端点必须由人确认它属于哪一类。
    "/s/{token}/notify-no-deliverable",
}


@pytest.fixture(scope="module")
def routes():
    return C.token_write_routes(SELECTION_API)


def test_00a_census_module_comes_from_this_repo() -> None:
    """载进来的 `_census` 必须是**本仓这一份**。

    路径载入已经排除了第三方 `tests` 包遮蔽,这条把结果钉死:
    万一哪天有人把它改回名字 import,而机器上又恰好有个同名包,
    判据会拿着别人的扫描器给本仓打分 —— 那种绿最贵。
    """
    import pathlib

    here = pathlib.Path(__file__).resolve()
    mod = pathlib.Path(C.__file__).resolve()
    assert mod.parent == here.parent, "_census 不是本目录这一份:%s" % mod
    assert C.REPO == here.parents[2], "_census.REPO 指向了别的树:%s" % C.REPO


def test_00_denominator_is_alive(routes) -> None:
    """分母活性:真的扫到了一堆 ``{token}`` 写路由,而不是 0 条恒绿。"""
    assert len(routes) >= 15, "只扫到 %d 条 {token} 写路由 —— 分母塌了" % len(routes)
    assert {r.method for r in routes} >= {"POST", "DELETE"}, \
        "只扫到一种写方法 —— 装饰器扫描可能漏了 put/patch/delete"


def test_01_route_scanner_has_discriminating_power() -> None:
    """扫描器自证:合成一个**漏接**的写端点,census 必须把它标成 guarded=False;
    合成一个接了的,必须标成 True;并且 ``asyncio.to_thread`` 这种
    「guard 作实参」的形态也必须算接了(现役 ``approve_quote`` 就是这个形态,
    只认 callee 的话会把它误判成漏接 —— 假红同样是判据坏了)。"""
    import ast
    import pathlib
    import tempfile

    src = "\n".join([
        "import asyncio",
        "from fastapi import APIRouter, Request",
        "router = APIRouter()",
        "def _require_session_owner_access(request, token): ...",
        "",
        '@router.post("/x/{token}/naked")',
        "async def naked(token: str, request: Request):",
        "    return 1",
        "",
        '@router.post("/x/{token}/direct")',
        "async def direct(token: str, request: Request):",
        "    _require_session_owner_access(request, token)",
        "    return 1",
        "",
        '@router.post("/x/{token}/threaded")',
        "async def threaded(token: str, request: Request):",
        "    await asyncio.to_thread(_require_session_owner_access, request, token)",
        "    return 1",
        "",
        '@router.get("/x/{token}/read")',
        "async def read_only(token: str, request: Request):",
        "    return 1",
    ])
    with tempfile.TemporaryDirectory() as d:
        rel = "fake_selection_api.py"
        p = pathlib.Path(d) / rel
        p.write_text(src, encoding="utf-8")
        # token_write_routes 走 REPO 相对路径,这里直接复用它的内部实现
        tree_rel = p.relative_to(pathlib.Path(d)).as_posix()
        old_repo = C.REPO
        try:
            C.REPO = pathlib.Path(d)
            got = {r.handler: r.guarded for r in C.token_write_routes(tree_rel)}
        finally:
            C.REPO = old_repo
    assert set(got) == {"naked", "direct", "threaded"}, \
        "写方法枚举错了(GET 不该进来,写方法不该漏):%s" % sorted(got)
    assert got["naked"] is False, "漏接的端点没被认出来 —— 判据没有区分力"
    assert got["direct"] is True, "直接调用的形态被误判成漏接"
    assert got["threaded"] is True, "to_thread(guard, …) 被误判成漏接 —— 现役 approve_quote 就是这个形态"
    assert ast.parse(src) is not None


def test_02_every_token_write_route_is_guarded_or_explicitly_exempt(routes) -> None:
    """全集分区:**接了校验** ⊎ **C 端 token-only 豁免**,不留第三类。

    这条是本单里最"防复发"的一条:将来新加一个销售端 ``{token}`` 写端点
    而忘了接归属校验,它既不在 guarded 里、也不在冻结豁免集里 ⇒ 当场红。
    """
    guarded = {r.path for r in routes if r.guarded}
    exempt = {r.path for r in routes if not r.guarded}
    unaccounted = exempt - FROZEN_CEND_EXEMPT
    assert not unaccounted, (
        "这些 {token} 写端点既没接 %s、也不在 C 端豁免集里:\n  %s\n"
        "它们能被任何拿到 token 的登录用户触达。"
        % (C.OWNER_GUARD, sorted(unaccounted)))
    stale = FROZEN_CEND_EXEMPT - exempt
    assert not stale, (
        "豁免集里这几条已经不再是「未接校验的 C 端端点」了:%s —— 冻结集过期了,"
        "过期的豁免集会把新漏洞藏在里面。" % sorted(stale))
    assert guarded, "一条接了校验的都没有 —— 分母塌了"


def test_03_extend_is_now_guarded(routes) -> None:
    """本条 finding 的**正样本**:extend 那一格现在接上了。

    单独列出来而不是靠上面那条集合断言兜住 —— 集合断言的失败信息里
    看不出"就是这一条",而这条一红就直接指到出问题的端点。
    """
    hit = [r for r in routes if r.path.endswith("/extend")]
    assert len(hit) == 1, "extend 路由不见了或变成多条:%s" % hit
    assert hit[0].guarded, (
        "POST %s 仍然没接 %s —— 任何 quote-enabled 登录用户拿到别人的分享 token "
        "就能把那个会话延期(并把 expired 拉回 selecting 重新开放选词)。"
        % (hit[0].path, C.OWNER_GUARD))


def test_04_extend_handler_takes_request(routes) -> None:
    """它必须收 ``Request`` —— 没有 Request 就拿不到当前用户,校验无从谈起。

    这是「接了校验」之外的另一半:光有一行调用而形参没接上,是 import 当调用的近亲。
    """
    import ast

    src = (C.REPO / SELECTION_API).read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "extend_session":
            names = [a.arg for a in n.args.args]
            annos = [getattr(a.annotation, "id", getattr(a.annotation, "attr", None))
                     for a in n.args.args]
            assert "request" in names, "extend_session 没有 request 形参:%s" % names
            assert "Request" in annos, "request 形参没有 Request 注解:%s" % annos
            return
    raise AssertionError("找不到 extend_session —— 锚点过期")


def test_05_guard_is_called_before_any_write(routes) -> None:
    """校验必须在**写入之前**。校验在写之后 = 越权已经发生,只是事后报了个错。"""
    import ast

    src = (C.REPO / SELECTION_API).read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if n.name != "extend_session":
            continue
        guard_at = write_at = None
        for sub in ast.walk(n):
            if not isinstance(sub, ast.Call):
                continue
            f = sub.func
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
            if name == C.OWNER_GUARD and guard_at is None:
                guard_at = sub.lineno
            if name == "extend_selection_session_atomically" and write_at is None:
                write_at = sub.lineno
        assert guard_at is not None, "extend_session 里没有归属校验调用"
        assert write_at is not None, "extend_session 不再调原子写入原语 —— 锚点过期"
        assert guard_at < write_at, (
            "归属校验在第 %d 行、写入在第 %d 行 —— 校验排在写入后面,越权已经发生了"
            % (guard_at, write_at))
        return
    raise AssertionError("找不到 extend_session —— 锚点过期")
