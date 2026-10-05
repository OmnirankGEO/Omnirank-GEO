"""WO_213 c1 判据 · 图文总闸盖住整条链。

Deploy 2026-09-14 逐路由实测的病:`GEO_DOUYIN_PIPELINE_ENABLED` 是生产上唯一的
图文开关,却只盖当时 38 条路由里的 9 条 —— 合同链那 9 条(含**花供应商钱**的
`publish-batch`)一条都不受闸,`run_tick` 与两份 scheduler 也不认。
结论是:今天没有「只停图文、代价可控」的开关。
(那个 38 是 2026-09-14 当时的路由数;0913e 合成树上 204-c1b 又加了 4 条,
 现行分母见 K1 的 `FROZEN` —— 别拿正文里的历史数字当现行分母。)

🔴 本包的分母是**两个路由器的全部路由**,不是我挑出来的几条 ——
   这个病本身就是"挑出来的几条"造成的(9 条自己判了,后加的 9 条没人记得加)。
"""
import ast
import io
import os
import pathlib

import pytest
from fastapi import HTTPException

DOUYIN_SRC = pathlib.Path("api/geo_douyin_api.py")
IMGNOTE_SRC = pathlib.Path("api/geo_image_note_api.py")


@pytest.fixture
def gate_closed(monkeypatch):
    monkeypatch.setenv("GEO_DOUYIN_PIPELINE_ENABLED", "0")


@pytest.fixture
def gate_open(monkeypatch):
    monkeypatch.setenv("GEO_DOUYIN_PIPELINE_ENABLED", "1")


def _routers():
    from api.geo_douyin_api import router as douyin
    from api.geo_image_note_api import router as imgnote
    return {"geo_douyin": douyin, "geo_image_note": imgnote}


# ═══════════════════════════════════════════════════════════════
# K1 · 闸关 ⇒ 42/42 都拦住(0913e 集成:204-c1b 新增 4 条,38→42)
# ═══════════════════════════════════════════════════════════════
def test_every_route_in_both_routers_carries_the_gate():
    """结构臂:两个路由器都挂了**路由器级**依赖,分母 = 全部路由。

    🔴 挂在路由器上而不是逐条加,是因为**将来新增的路由**要自动受闸 ——
       合同链那 9 条正是后加的,逐条加的那套没人记得给它们加。
       毒「路由改挂裸 router」打的就是这一条。
    """
    from services.geo_douyin.pipeline_gate import pipeline_gate

    counts = {}
    for name, router in _routers().items():
        deps = [d.dependency for d in (router.dependencies or [])]
        assert pipeline_gate in deps, "%s 路由器没挂总闸" % name
        counts[name] = len([r for r in router.routes])

    # 🔴 [0913e 集成] 分母**逐路由器冻**,不只冻总数。
    #    只冻总数时,一个路由器 +1、另一个 -1 仍然是 42,分母变了却没人红。
    #    数字由实测来:2026-09-15 在合成树 f5ab62718(树 eab1b6ad7)上量得
    #    geo_douyin 33 / geo_image_note 9。204-c1b 加了 4 条(38→42)。
    #    改动使它变化时,下面的报错会**把两边的数都打出来**,
    #    不用再去翻"到底是哪个路由器多了"。
    FROZEN = {"geo_douyin": 33, "geo_image_note": 9}
    assert counts == FROZEN, (
        "路由分母变了:实测 %s,冻结 %s(合计 %d → %d)。"
        "分母变了就要重新解释:新增的路由自动受闸是对的,"
        "但「多了几条、多在哪个路由器」必须是有人看过的"
        % (counts, FROZEN, sum(FROZEN.values()), sum(counts.values())))


def test_the_gate_refuses_with_503_and_a_machine_readable_code(gate_closed):
    """行为臂:闸关 ⇒ 503 + PIPELINE_DISABLED,且带一句人话。

    🔴 用 503 不用 200:老的 `_COMING_SOON` 返 200,于是"功能关了"与
       "功能跑通了"在状态码上分不出来 —— 网关、埋点、前端的错误分支全看不见它。
    """
    import asyncio

    from services.geo_douyin.pipeline_gate import (PIPELINE_DISABLED_CODE,
                                                   pipeline_gate)

    with pytest.raises(HTTPException) as e:
        asyncio.run(pipeline_gate())
    assert e.value.status_code == 503
    detail = e.value.detail or {}
    assert detail.get("code") == PIPELINE_DISABLED_CODE
    assert detail.get("message"), "闸关了却没有一句给人看的话"
    # 与老 `_COMING_SOON` 形状对齐:前端既有分支认 status 这一键
    assert detail.get("status") == "coming_soon"


def test_the_gate_lets_everything_through_when_open(gate_open):
    """反向对照:闸开 ⇒ 放行。

    少了它,「永远 503」也能让上面那条绿,而那等于把图文永久关掉。
    """
    import asyncio

    from services.geo_douyin.pipeline_gate import pipeline_gate

    assert asyncio.run(pipeline_gate()) is None


def test_no_per_endpoint_gate_left_behind():
    """逐端点判定必须**删干净**:留着就是同一谓词两处。

    🔴 两处并存的坏法不是"多判一次",是**漂**:哪天有人改了闸的语义
       (比如加一个 admin 豁免),只改一处 —— 而两处各自都"看起来对"。
    """
    src = io.open(DOUYIN_SRC, encoding="utf-8").read()
    assert "return _COMING_SOON" not in src, "还留着逐端点的 _COMING_SOON 早退"
    assert "_COMING_SOON = {" not in src, "死常量没删 —— 下一个人会以为那还是现行说法"


def test_read_endpoints_are_gated_too():
    """读端点也拦:GET 不在豁免之列。

    读端点看起来无害,但它们把"这个功能还活着"的样子展示给用户,
    用户接着就去点那个已经关掉的按钮。闸是"今天不提供这个功能",
    不是"今天不许写"。
    """
    gets = []
    for name, router in _routers().items():
        for r in router.routes:
            methods = getattr(r, "methods", set()) or set()
            if "GET" in methods:
                gets.append((name, getattr(r, "path", "?")))
    assert gets, "一条 GET 都没有 —— 锚过期了"
    # 路由器级依赖对所有方法生效,这里只需确认 GET 确实在这两个路由器里
    from services.geo_douyin.pipeline_gate import pipeline_gate
    for name, _ in gets:
        deps = [d.dependency for d in (_routers()[name].dependencies or [])]
        assert pipeline_gate in deps


# ═══════════════════════════════════════════════════════════════
# K3 · worker / scheduler 侧:停领新活,**但不停收敛**
# ═══════════════════════════════════════════════════════════════
def test_run_tick_stops_claiming_when_the_gate_is_closed(gate_closed, monkeypatch):
    """闸关 ⇒ 三条 lane 一件不领。"""
    import asyncio

    from services.geo_douyin import contract_worker

    claimed = {"n": 0}

    async def _never(*a, **kw):
        claimed["n"] += 1
        return None

    for fn in ("run_production_once", "run_artifact_prepare_once",
               "run_publish_submit_once"):
        monkeypatch.setattr(contract_worker, fn, _never)
    # 收敛器全部桩成空转,把判读聚焦在"领没领"
    monkeypatch.setattr(contract_worker, "raise_wiring_alert", lambda **kw: None)

    out = asyncio.run(contract_worker.run_tick(per_tick=3))
    assert claimed["n"] == 0, "闸关了还在领活"
    assert out.get("pipeline_open") == 0


def test_run_tick_still_claims_when_the_gate_is_open(gate_open, monkeypatch):
    """反向对照:闸开照领。

    少了它,「永远不领」也能让上面那条绿 —— 而那是把图文永久停掉。
    """
    import asyncio

    from services.geo_douyin import contract_worker

    claimed = {"n": 0}

    async def _one(*a, **kw):
        claimed["n"] += 1
        return None          # 领一次就说没有了,免得跑满 per_tick

    for fn in ("run_production_once", "run_artifact_prepare_once",
               "run_publish_submit_once"):
        monkeypatch.setattr(contract_worker, fn, _one)
    monkeypatch.setattr(contract_worker, "raise_wiring_alert", lambda **kw: None)

    asyncio.run(contract_worker.run_tick(per_tick=3))
    assert claimed["n"] == 3, "闸开却没领(三条 lane 各一次)"


def test_convergers_keep_running_when_the_gate_is_closed(gate_closed, monkeypatch):
    """🔴 闸关**不停收敛器** —— 这条比上面两条更重要。

    我第一版写的是整个 `run_tick` 早退。那是错的,而且危险:
    三条 lane 之后还有五个收敛器 + 回填,它们负责把**已经在途**的任务走完,
    包括把冻结的算力结清。整体早退 = 关闸那一刻,在途任务连同冻结一起卡死。
    调度注册处早就写过这句警告(「已经在途的任务仍必须被收敛」)。

    闸的语义是:**不再开始新的、不再花新的钱**;已经花出去的,照样走到终态。
    """
    import asyncio

    from services.geo_douyin import contract_worker

    ran = {"converge": 0}

    async def _never_claim(*a, **kw):
        return None

    for fn in ("run_production_once", "run_artifact_prepare_once",
               "run_publish_submit_once"):
        monkeypatch.setattr(contract_worker, fn, _never_claim)
    monkeypatch.setattr(contract_worker, "raise_wiring_alert", lambda **kw: None)

    # 把第一个收敛器换成计数桩;它跑到了,就说明闸关没把收敛这一段停掉
    import inspect
    src = inspect.getsource(contract_worker.run_tick)
    assert "_guarded(" in src, "收敛器的调用形状变了 —— 锚过期,先看代码"

    # 🔴 桩要用**真实形状**:这些收敛器是**同步**函数,由 `asyncio.to_thread`
    #    包起来调,返回 dict。第一版我写成 async 且返 0,被测代码当场
    #    `'coroutine' object has no attribute 'get'` —— 那种红看起来像被测对象坏了,
    #    其实是桩的形状错(本仓踩过好几次)。
    assert hasattr(contract_worker, "project_failed_publish_posts"),         "锚过期:收敛器改名了,先看代码"

    def _count_sync(*a, **kw):
        ran["converge"] += 1
        return {"failed_projected": 0}

    monkeypatch.setattr(contract_worker, "project_failed_publish_posts", _count_sync)

    asyncio.run(contract_worker.run_tick(per_tick=2))
    assert ran["converge"] > 0, (
        "闸关把收敛器也停了 —— 在途任务与冻结会卡死在半路")


def test_the_convergence_scheduler_job_is_deliberately_ungated():
    """`job_geo_douyin_publish_converge` **故意不认闸**,而且写明了理由。

    🔴 这条锁的是"下一个人别顺手补上"。它只读 item 终态、写回作品终态,
       不下单不扣费;关掉它 = 在途的 publishing 作品永远卡住。
       判据钉住**注释里的理由在**,因为这是一处"看起来漏了、其实是有意的"。
    """
    src = io.open("scheduler.py", encoding="utf-8").read()
    i = src.find("def job_geo_douyin_publish_converge")
    assert i > 0, "锚过期:找不到该 job"
    block = src[i:i + 1400]
    assert "WO_213" in block and "故意不认总闸" in block, (
        "该 job 没有写明为什么不认闸 —— 没写理由的例外,下一个人会当成漏加")


# ═══════════════════════════════════════════════════════════════
# K4 · 对照:分清"闸生效"与"服务挂了"
# ═══════════════════════════════════════════════════════════════
def test_other_routers_are_untouched_by_the_gate(gate_closed):
    """闸关时,**别的路由器**一条依赖都没多。

    🔴 这条是「闸生效」与「服务挂了」的分界:如果闸关连登录、钱包价目一起
       503,那用户看到的就不是"图文关了"而是"系统坏了",
       而排障的人也分不出是哪一种。
    """
    from services.geo_douyin.pipeline_gate import pipeline_gate

    import api.auth_api as auth_api
    import api.wallet_api as wallet_api

    for name, mod in (("auth", auth_api), ("wallet", wallet_api)):
        r = getattr(mod, "router", None)
        if r is None:
            continue
        deps = [d.dependency for d in (r.dependencies or [])]
        assert pipeline_gate not in deps, "%s 路由器被顺手挂上了图文总闸" % name


def test_gate_reads_one_switch_not_two():
    """HTTP 侧与 worker 侧读**同一个**开关。

    两处各读各的环境变量名,就会出现"HTTP 关了、后台还在跑"的半开状态 ——
    而那正是本单要根治的病的一个变种。
    """
    import inspect

    from services.geo_douyin import pipeline_gate as gate_mod

    src = inspect.getsource(gate_mod)
    # 只看**代码行**:docstring 里提到环境变量名是在解释它,不是在读它。
    # (我第一版直接扫全文,被自己的 docstring 打红 —— 与本单要修的
    #  「按注释判事实」是同一个病。)
    code_lines = [l for l in src.splitlines()
                  if not l.lstrip().startswith(('#',))
                  and '"""' not in l]
    code = chr(10).join(code_lines)
    assert code.count("is_pipeline_enabled") >= 2
    assert "os.getenv" not in code and "os.environ" not in code, (
        "闸模块自己又读了一次环境变量 —— 应当只调 config.is_pipeline_enabled")


# ═══════════════════════════════════════════════════════════════
# K5 · c1b:路由器级依赖护得住「同一个 router 上的新路由」,
#          护不住「同一个文件里新建的**另一个** router」
# ═══════════════════════════════════════════════════════════════
def test_every_apirouter_object_in_both_modules_carries_the_gate():
    """🔴 分母改成「模块里**所有** APIRouter 对象」,不是那个叫 router 的。

    Review 09-14 的缺口 Q2:在同一个文件里新建一个裸 `APIRouter()` 再挂一条路由,
    冻结的那几个数一条不少、`router` 仍挂着闸 —— 我原来那条结构臂**全程是绿的**。
    原因是它按**名字**取路由器(`from … import router`),而名字看不见邻居。

    所以这条按**对象**取:凡是模块级的 APIRouter 实例,都得挂闸。
    新建一个没挂闸的,不管它叫什么,这里当场红。
    """
    import importlib

    from fastapi import APIRouter

    from services.geo_douyin.pipeline_gate import pipeline_gate

    for modname in ("api.geo_douyin_api", "api.geo_image_note_api"):
        mod = importlib.import_module(modname)
        routers = [(n, v) for n, v in vars(mod).items()
                   if isinstance(v, APIRouter)]
        assert routers, "%s 里一个 APIRouter 都没有 —— 锚过期了" % modname
        for name, r in routers:
            deps = [d.dependency for d in (r.dependencies or [])]
            assert pipeline_gate in deps, (
                "%s.%s 是个没挂总闸的 APIRouter —— 挂到 app 上它那些路由就不受闸"
                % (modname, name))


def test_neither_file_builds_a_second_apirouter():
    """源码臂:两个文件各只许**构造一次** APIRouter。

    上面那条按对象判,已经比按名字判有牙;但它只看得见**模块级变量**。
    这条按 AST 数 `APIRouter(…)` 的构造次数 —— 连"建了但先不赋值给模块级名字"
    的写法也拦住,把 Q2 那种加法挡在更早的地方。

    🔴 用 AST 不用 grep:注释或字符串里写一句 `APIRouter(` 会骗过 grep,
       而被骗的方向是**放行**(数多了 ⇒ 红,数少了才危险)—— 但同样地,
       真构造写成 `fastapi.APIRouter(...)` 时 grep 的锚也可能对不上。AST 两头都准。
    """
    import ast
    import io

    for path in (DOUYIN_SRC, IMGNOTE_SRC):
        tree = ast.parse(io.open(path, encoding="utf-8").read())
        made = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if (isinstance(f, ast.Name) and f.id == "APIRouter") or \
                    (isinstance(f, ast.Attribute) and f.attr == "APIRouter"):
                made.append(getattr(node, "lineno", -1))
        assert len(made) == 1, (
            "%s 里构造了 %d 个 APIRouter(行 %s)。多出来的那个不会自动带总闸 —— "
            "要么把路由挂回原来那个 router,要么给它也挂上 pipeline_gate 并改这条判据"
            % (path, len(made), made))


def test_server_mounts_nothing_but_the_gated_router_from_these_two_modules():
    """`server.py` 从这两个模块**挂上去**的,只能是那个受闸的 `router`。

    🔴 光锁住文件里只有一个 router 还不够:那条锁看的是**定义侧**。
       哪天有人在模块里加了 `public_router` 又在 server.py 把它 include 上去,
       定义侧那条会红 —— 但如果新 router 是从别的文件搬来的,定义侧就看不见了。
       这条钉**挂载侧**:凡是从这两个模块取来的名字,被 include 的只能是 `router`。

    🔴 钉的是「被 include 的是什么」,不是「import 了什么」:
       import 一个纯 helper(比如 `image_note_daily_limit`)是无害的,
       禁掉它只会让这条锁被下一个人放宽 —— 而放宽之后它也不再为真原因转红。
    """
    import ast
    import io

    tree = ast.parse(io.open("server.py", encoding="utf-8").read())
    targets = {"api.geo_douyin_api", "api.geo_image_note_api"}
    # 本地名 → (模块, 模块里的原名)
    bound = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in targets:
            for a in node.names:
                bound[a.asname or a.name] = (node.module, a.name)
    assert {m for m, _ in bound.values()} == targets, (
        "server.py 没有 import 这两个模块(%s)—— 锚过期了,先看代码"
        % (sorted({m for m, _ in bound.values()}),))

    bad = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "include_router"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Name) and arg.id in bound:
                mod, orig = bound[arg.id]
                if orig != "router":
                    bad.append((mod, orig, node.lineno))
    assert not bad, (
        "server.py 把这两个模块里 `router` 之外的东西挂上去了:%s。"
        "路由器级总闸挂在 `router` 上,别的 router 挂上去就是一条不受闸的链" % (bad,))
