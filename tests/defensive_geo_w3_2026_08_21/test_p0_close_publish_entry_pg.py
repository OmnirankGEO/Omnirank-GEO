"""终审 P0-1(关闭发布链客户入口)+ 窗D P1-3(进度端点存在性)判据 —— 真 HTTP + 真 PG16。

═══════════════════════════════════════════════════════════════════════
P0-1 关的是什么
═══════════════════════════════════════════════════════════════════════
执行侧没开发完:``dispatch_once`` / ``claim_outbox`` / ``reconcile_once``
全仓零调用点(排 tests 后只剩定义处),调度器里跟发布有关的只有只读告警。
而 ``confirm`` 是**真冻钱**的。客户视角 =
确认发布 → 冻算力 → 什么都没发生 → 12 小时后 zombie sweeper 把钱莫名退回。

所以处置是**关入口**,不是补执行器。

═══════════════════════════════════════════════════════════════════════
🔴 分母是机械导出的,不是手抄的
═══════════════════════════════════════════════════════════════════════
"哪几个端点必须关"如果由我手写一个名单,漏掉的那一个**不会让任何判据变红**
(本仓 2026-08 记过)。所以 :func:`test_00_...` 用 AST 现扫
``api/defensive_publish_api.py``:凡是函数体里出现 ``_funding.freeze_exact``
或 ``_funding.check_and_lock_budget`` 的 POST handler,**全部**必须把
``_assert_customer_publish_entry_open()`` 放在第一条语句。

这条也正是我为什么多关了一个终审单没点名的端点:单子说
「其余 publish 只读端点可保留(**不冻钱**)」—— 那是判据不是名单,
而 ``retry-child`` 会冻钱。

═══════════════════════════════════════════════════════════════════════
🔴 「零冻结」必须先证明尺子是活的
═══════════════════════════════════════════════════════════════════════
"调用后 point_freezes 增量 = 0" 在**表不存在 / 连错库 / 查询写错**时同样成立。
所以 :func:`test_04_...` 在同一把尺子上做反向对照:真插一行,增量必须变 1。
不做这一步的话,那条零冻结锁跟"没查"是一回事。

跑法:pytest tests/defensive_geo_w3_2026_08_21/test_p0_close_publish_entry_pg.py -q
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from typing import Any, Iterator, Mapping

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

# 🔴 身份字典**复用**窗C 那份(它是逐字照抄 auth/middleware.py 的,
#    并且有一条 AST 判据钉着它与生产键名一致)。这里再抄一份 =
#    同一谓词写两处,必有一处没人验。
from tests.defensive_geo_w3_2026_08_21.test_wp5_publish_http_pg import (
    _IDENTITIES, BRAND_A, TENANT_A,
)

ROOT = Path(__file__).resolve().parents[2]
PUBLISH_API = ROOT / "api" / "defensive_publish_api.py"

#: 会动钱的 ``_funding`` 入口。命中其一 = 这个 handler 属于必须关闭的集合。
_FUNDING_MOVERS = ("freeze_exact", "check_and_lock_budget")

_GUARD = "_assert_customer_publish_entry_open"


# ══════════════════════════════════════════════════════════════════════════
# app / client
# ══════════════════════════════════════════════════════════════════════════
class _SkipOnClosed(TestClient):
    """[fix-of-fix P1-B] 撞上指定的关闭信封 ⇒ **当场 skip**,不是红。

    与 P0-1 那把 ``_EntryClosed`` 同款:判定放在**响应**上,不手挑名单。
    撞不上的照常跑、照常红;入口一旦重开,这些判据**自动复活并必须通过**。
    """

    skip_code: str | None = None

    def request(self, *args: Any, **kwargs: Any):                # noqa: ANN201
        resp = super().request(*args, **kwargs)
        if self.skip_code and resp.status_code == 403:
            try:
                if (resp.json().get("detail") or {}).get("code") == self.skip_code:
                    pytest.skip(
                        f"{self.skip_code}:该入口当前是关的 —— 本条判据驱动的是"
                        "还没接线的行为。重开后它会自动复活并必须通过。"
                    )
            except ValueError:
                pass
        return resp


def _mk_client(module_name: str, *, skip_on: str | None = None) -> Iterator[TestClient]:
    import importlib

    mod = importlib.import_module(module_name)
    app = FastAPI()
    app.include_router(mod.router)

    @app.middleware("http")
    async def _inject_user(request: Request, call_next):        # noqa: ANN001
        who = request.headers.get("X-Test-Identity", "a")
        request.state.user = dict(_IDENTITIES[who])
        return await call_next(request)

    client = _SkipOnClosed(app)
    client.skip_code = skip_on
    with client as c:
        yield c


@pytest.fixture(scope="module")
def pub() -> Iterator[TestClient]:
    yield from _mk_client("api.defensive_publish_api")


@pytest.fixture(scope="module")
def mon() -> Iterator[TestClient]:
    # 🔴 [包F ② 2026-08-23] 重开哨兵**已摘**。
    #    一期这里挂着 ``skip_on="MONITORING_PROGRESS_CLOSED"``:入口关着就 skip,
    #    "账本接线、入口重开后自动复活并必须通过"。包F ① 接线 + ② 重开 ⇒
    #    复活条件已满足,哨兵随之摘掉。
    #
    #    🔴 摘掉哨兵这件事本身需要一条**活性自证** ——
    #    ``test_p1_3_progress_criteria_are_no_longer_skipped`` 断言这两条判据
    #    真的跑起来了。没有它的话,"复活了"与"哨兵还在静默 skip"
    #    在报告里长得一模一样(本仓记过:skip 掉的判据和通过了长得一样)。
    yield from _mk_client("api.defensive_monitoring_api")


def _closed(resp) -> dict[str, Any]:
    """关闭态断言:**typed 信封**,不是 500、不是 501、不是 200。"""
    assert resp.status_code != 500, f"500 = 当成故障了,而这不是故障:{resp.text[:500]}"
    assert resp.status_code != 501, f"501 被终审单明确禁止:{resp.text[:500]}"
    assert resp.status_code != 200, f"200 = 入口根本没关:{resp.text[:500]}"
    assert resp.status_code == 403, f"期望 403 实得 {resp.status_code}:{resp.text[:500]}"
    body = resp.json()
    detail = body.get("detail", body)
    assert detail.get("code") == "PUBLISH_ENTRY_CLOSED", detail
    explanation = detail.get("publicExplanation") or ""
    # 🔴 说钱:她刚点了一个"确认发布",最想知道的是钱动没动。
    assert "没有扣除任何算力" in explanation, f"文案没把钱说死:{explanation!r}"
    # 🔴 不许把内部枚举摆到她面前
    assert "PUBLISH_ENTRY_CLOSED" not in explanation, explanation
    return detail


# ══════════════════════════════════════════════════════════════════════════
# 00 · 机械分母:凡会动钱的 POST handler,第一条语句必须是关闭闸
# ══════════════════════════════════════════════════════════════════════════
def _route_handlers() -> list[tuple[str, str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    tree = ast.parse(PUBLISH_API.read_text(encoding="utf-8"))
    out = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and isinstance(dec.func.value, ast.Name) and dec.func.value.id == "router"):
                path = dec.args[0].value if dec.args and isinstance(dec.args[0], ast.Constant) else "?"
                out.append((dec.func.attr.upper(), str(path), node))
    return out


def _calls_in(node) -> set[str]:
    """这个函数体里调了哪些**模块级函数名** + 哪些 ``_funding.*``。"""
    out: set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        f = sub.func
        if isinstance(f, ast.Name):
            out.add(f.id)
        elif (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
              and f.value.id == "_funding"):
            out.add("_funding." + f.attr)
    return out


def _money_reachable_functions() -> set[str]:
    """会动钱的函数集合 —— **传递闭包**,不是"handler 体里有没有那行"。

    🔴 第一版我就是照后者写的,当场只认出 1 条(retry-child),漏掉了
       preview 与 confirm —— 因为它们的资金调用在模块级 helper
       ``_build_new_preview`` / ``_do_confirm`` 里,handler 只是调用方。
       "分母漏掉的那一项不会让任何判据变红":如果我当时把 >=3 改成 >=1
       让它绿了,这条锁就只在守 retry-child,而真正冻钱的 confirm
       可以被人随手打开而没有任何东西会红。
    """
    tree = ast.parse(PUBLISH_API.read_text(encoding="utf-8"))
    funcs = {n.name: n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    edges = {name: _calls_in(node) for name, node in funcs.items()}
    money = {name for name, cs in edges.items()
             if any(("_funding." + m) in cs for m in _FUNDING_MOVERS)}
    changed = True
    while changed:                       # 传递闭包
        changed = False
        for name, cs in edges.items():
            if name not in money and (cs & money):
                money.add(name)
                changed = True
    return money


def _guard_lines(fn) -> list[int]:
    return [n.lineno for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == _GUARD]


def _money_lines(fn, money_fns: set[str]) -> list[int]:
    """这个 handler 体内,**最早**能走到钱的那些调用在哪几行。"""
    out = []
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Name) and f.id in money_fns:
            out.append(n.lineno)
        elif (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
              and f.value.id == "_funding" and f.attr in _FUNDING_MOVERS):
            out.append(n.lineno)
    return out


def _guard_precedes_money(fn, money_fns: set[str]) -> bool:
    """关闸必须排在**任何**资金调用之前。

    🔴 [fix-of-fix P1-A 2026-08-23] 这条原本写的是"必须是第一条语句"。
       P1-A 在关闸**之前**插了一次只读幂等重放(已经确认过、钱已经冻住的那一次
       不该被 403 从她眼前抹掉),于是关闸不再是第一条 —— 但承重的东西没变:
       **在钱动之前**关闸必须已经跑过。
       所以是**改断言**,不是把这条锁退役:位置放松了,不变式收紧成它真正的样子。
    """
    guards = _guard_lines(fn)
    if not guards:
        return False
    money = _money_lines(fn, money_fns)
    if not money:
        return True                          # 这个 handler 自己不直接碰钱
    return max(guards) < min(money)


def test_00_every_money_moving_endpoint_is_closed() -> None:
    handlers = _route_handlers()
    assert len(handlers) >= 10, f"路由枚举只找到 {len(handlers)} 条 —— AST 走空了(零分母)"

    money_fns = _money_reachable_functions()
    money = [(m, p, f) for (m, p, f) in handlers if f.name in money_fns]
    # 🔴 零分母 = 判据没被测对象。实测正好 3 条:preview / confirm / retry-child。
    assert len(money) == 3, (
        f"可达资金的路由是 {sorted(p for _, p, _ in money)} —— 期望恰好 3 条。"
        "多了:有新的冻钱入口没被关;少了:检测器漏了(分母塌了,锁会恒绿)。"
    )

    unguarded = [f"{m} {p}" for (m, p, f) in money if not _guard_precedes_money(f, money_fns)]
    assert not unguarded, (
        f"这些会动钱的端点的关闭闸没有排在资金调用之前:{unguarded}。"
        "承重的是『钱动之前闸已经跑过』—— 零冻结才能由阅读证明。"
    )

    # 反向对照 ①:检测器必须分得清"会动钱"和"不会动钱",否则分母没有意义。
    not_money = [p for (m, p, f) in handlers if m == "POST" and f.name not in money_fns]
    assert not_money, "所有 POST 都被判成会动钱 —— 检测器是恒真的"
    # 反向对照 ②:不动钱的端点**不许**被顺手关掉(超出终审单授权)。
    guarded_but_free = [p for (m, p, f) in handlers
                        if f.name not in money_fns and _guard_lines(f)]
    assert not guarded_but_free, (
        f"这些不动钱的端点也被关了:{guarded_but_free} —— 超出终审单授权范围"
    )


#: 重开的前置条件,逐字抄自 P0-1 当初写死的那句话:
#: 「dispatch_once / claim_outbox / reconcile_once 至少各有一个生产调用点」。
#: 分母不手写成"应该有哪几个文件在调" —— 那是拿结论当分母;
#: 这里只写**被调方的函数名**,调用者集合由 AST 现扫。
_EXECUTOR_FUNCTIONS = ("dispatch_once", "claim_outbox", "reconcile_once")

#: 调度器里必须有的**推进** job(不是只读告警)。
_ADVANCING_JOB_IDS = (
    "defgeo_publish_dispatch",
    "defgeo_publish_reconcile",
    "defgeo_activation_materialize",
)


def _production_callers(function_name: str) -> set[str]:
    """全仓(排 tests)真调用了这个函数的文件。**只认 ast.Call**。

    不用裸符号名 grep:那样 import 语句、注释、docstring 都会命中,
    而"import 了"不等于"调用了"(本仓记过:census 裸符号名 = 把 import 当调用)。
    """
    hits: set[str] = set()
    for d in ("db", "api", "services", "workflows", "agents", "tools"):
        for path in (ROOT / d).rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = (fn.id if isinstance(fn, ast.Name)
                        else fn.attr if isinstance(fn, ast.Attribute) else None)
                if name == function_name:
                    # 定义处所在文件不算调用者(它自己 def 了这个名字)
                    if f"def {function_name}(" in path.read_text(
                            encoding="utf-8", errors="replace"):
                        continue
                    hits.add(rel)
    return hits


def test_00b_entry_is_open_only_with_real_executor_wiring() -> None:
    """[包E 2026-08-24] 现状锁**翻面** —— 改断言,不是退役。

    ═══════════════════════════════════════════════════════════════════
    🔴 为什么不是简单地把 False 改成 True
    ═══════════════════════════════════════════════════════════════════
    P0-1 那一版的价值在于「开关不能被顺手一拨」。如果这里只写
    ``assert ... is True``,下一个人把执行器接线拆了、开关照样 True,
    这条判据一声不吭 —— 那才是真正的退役。

    所以这一条把**开关**与**它当初写死的重开条件**绑在同一个断言里:
      · 开关 True  ⇒ 三个执行侧函数必须各有生产调用点,且三条推进 job 注册得上;
      · 开关 False ⇒ 反过来要求"确实还没接线"(否则就该开了,别让它白关着)。
    两个方向都会红。
    """
    from api import defensive_publish_api as mod

    callers = {fn: _production_callers(fn) for fn in _EXECUTOR_FUNCTIONS}
    unwired = sorted(fn for fn, c in callers.items() if not c)

    if mod._CUSTOMER_PUBLISH_ENTRY_OPEN:
        assert not unwired, (
            f"入口开着,但这些执行侧函数零生产调用点:{unwired}。"
            "P0-1 逐字写死的重开条件是三个函数各有一个生产调用点 —— "
            f"实测 {({k: sorted(v) for k, v in callers.items()})}"
        )
        registered = _registered_job_ids()
        missing = [j for j in _ADVANCING_JOB_IDS if j not in registered]
        assert not missing, (
            f"入口开着,但调度器里缺这些**推进** job:{missing}。"
            f"注册中断原因 = {_LAST_REGISTRATION_ABORT['reason']!r}"
            f"(实注册 {len(registered)} 条)。只有只读告警的调度器 = P0-1 原样复发"
        )
    else:
        assert unwired, (
            "入口关着,但执行侧三个函数都已经有生产调用点了 —— "
            "关闭闸的前提条件已经不成立,应该重开而不是继续关着"
        )


def _registered_job_ids() -> set[str]:
    """🔴 **运行期**接线锁:用桩 scheduler 真跑一遍注册,收作业 id。

    不用 AST / grep 扫 ``add_job``:那种锁杀不掉 ``if False:`` 这类变异
    (语法树里那条 add_job 还在,只是永远不执行)。门三 G9 的教训逐字。
    这里让 ``register_v32_core_tasks`` **真的执行**,谁没被执行到谁就不在集合里。
    """
    import api.scheduler as sched

    class _StubScheduler:
        def __init__(self) -> None:
            self.jobs: dict[str, Any] = {}

        def get_job(self, job_id):                       # noqa: ANN001
            return self.jobs.get(job_id)

        def add_job(self, func, **kw):                   # noqa: ANN001
            job_id = kw.get("id")
            self.jobs[job_id] = func
            return job_id

        def remove_job(self, job_id):                    # noqa: ANN001
            self.jobs.pop(job_id, None)

    stub = _StubScheduler()
    original = sched.get_scheduler
    sched.get_scheduler = lambda: stub                   # type: ignore[assignment]
    aborted: str | None = None
    try:
        try:
            sched.register_v32_core_tasks()
        except Exception as exc:                         # noqa: BLE001
            # 🔴 本仓有若干 **fail-closed** 的注册块(缺 DB 种子行就 raise,
            #    刻意让带病的 cron 起不来)。在一次性判据库上它们确实会抛。
            #    抛之前已经加进去的 job 仍然算数,所以这里**不吞也不当场红**:
            #    把中断原因带出去,让调用方在"我关心的那几条 job 不在"时
            #    能分清是"没注册"还是"根本没走到那里"——
            #    两者都必须红,但报错要说得出是哪一种。
            aborted = f"{type(exc).__name__}: {exc}"
    finally:
        sched.get_scheduler = original                   # type: ignore[assignment]

    assert len(stub.jobs) > 10, (
        f"桩 scheduler 只收到 {len(stub.jobs)} 条 job(中断原因 {aborted})—— "
        "注册函数多半没真跑起来,那样『缺哪条 job』的断言会恒绿"
    )
    _LAST_REGISTRATION_ABORT["reason"] = aborted
    return set(stub.jobs)


#: 上一次桩注册的中断原因。放模块级是为了让断言消息能说清
#: 「这条 job 不在」到底是没注册,还是注册在到它之前就中断了。
_LAST_REGISTRATION_ABORT: dict[str, str | None] = {"reason": None}


# ══════════════════════════════════════════════════════════════════════════
# 01-03 · 真 HTTP:**合法**入参也必须拿到 typed 拒绝
# ══════════════════════════════════════════════════════════════════════════
def _not_closed(resp) -> None:
    """[包E] ``_closed`` 的反面:这一格**不许**再返回入口关闭信封。

    刻意只否掉 ``PUBLISH_ENTRY_CLOSED``,不断言具体成功码 ——
    这三条判据管的是"门开没开",不是"门后面那一步的业务结论"
    (后者由 test_wp5_publish_http_pg 那 20 条真链判据管)。
    但 500 一并否掉:门开着却必 500,等于门没开。
    """
    assert resp.status_code != 500, f"入口开了却在真库上跑不完:{resp.text[:500]}"
    body = resp.json() if resp.content else {}
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, Mapping):
        assert detail.get("code") != "PUBLISH_ENTRY_CLOSED", (
            f"这一格还在返回入口关闭信封,但开关已经是 True:{resp.text[:400]}"
        )


def test_01_preview_not_closed(pub: TestClient) -> None:
    """[包E] 改断言不退役:P0-1 断言"必须关",包E 断言"必须不再关"。"""
    resp = pub.post(
        "/api/defensive-geo/publish/decision-snapshots/preview",
        headers={"Idempotency-Key": "close-preview-" + uuid.uuid4().hex},
        json={
            "planItemKey": "item-1", "articleRevisionId": "rev-1",
            "expectedArticleHash": "h" * 64, "acceptedSnapshotId": 1,
            "serviceProjectionId": "svc-1", "brandId": BRAND_A,
        },
    )
    _not_closed(resp)


def test_02_confirm_not_closed(pub: TestClient) -> None:
    resp = pub.post(
        "/api/defensive-geo/publish/decision-snapshots/dsnap_x/confirm",
        headers={"Idempotency-Key": "close-confirm-" + uuid.uuid4().hex},
        json={"expectedHash": "h" * 64, "expectedVersion": 1},
    )
    _not_closed(resp)


def test_03_retry_child_not_closed(pub: TestClient) -> None:
    resp = pub.post(
        "/api/defensive-geo/publish/commands/pcmd_x/retry-child",
        headers={"Idempotency-Key": "close-retry-" + uuid.uuid4().hex},
        json={"expectedStatusVersion": 1, "expectedCommandHash": "h" * 64},
    )
    _not_closed(resp)


# ══════════════════════════════════════════════════════════════════════════
# 04 · 零冻结 —— 先证明尺子是活的
# ══════════════════════════════════════════════════════════════════════════
def _freeze_count(db) -> int:
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM public.point_freezes")
        return int(cur.fetchone()["n"])


def test_04_rejected_calls_freeze_nothing(pub: TestClient, db) -> None:
    """[包E] 零冻结锁**换靶不退役**。

    ═══════════════════════════════════════════════════════════════════
    🔴 原来这条打的是"入口关着 ⇒ 零冻结"。入口开了,那个前提没了。
       但它守的那件事没变、而且更重要了:**被拒绝的调用一分钱都不许冻**。
       所以靶子从"关闭信封"换成"三条会动钱的端点在被拒绝时" ——
       尺子(``point_freezes`` 计数 + 反向对照)一个字不改。
       退役掉它等于从此没有任何东西看着"拒绝路径会不会顺手冻钱"。

    这三发拒绝各自走的是不同的拒绝路径,刻意不挑同一种:
      · preview      → acceptedSnapshotId=1 不属于本 tenant ⇒ OBJECT_NOT_FOUND(包E 新加的归属闸)
      · confirm      → 快照不存在                            ⇒ OBJECT_NOT_FOUND
      · retry-child  → 命令不存在                            ⇒ OBJECT_NOT_FOUND
    """
    before = _freeze_count(db)

    rejected = []
    for call in (
        lambda: pub.post(
            "/api/defensive-geo/publish/decision-snapshots/preview",
            headers={"Idempotency-Key": "zf-p-" + uuid.uuid4().hex},
            json={"planItemKey": "item-1", "articleRevisionId": "rev-1",
                  "expectedArticleHash": "h" * 64, "acceptedSnapshotId": 1,
                  "serviceProjectionId": "svc-1", "brandId": BRAND_A}),
        lambda: pub.post(
            "/api/defensive-geo/publish/decision-snapshots/dsnap_x/confirm",
            headers={"Idempotency-Key": "zf-c-" + uuid.uuid4().hex},
            json={"expectedHash": "h" * 64, "expectedVersion": 1}),
        lambda: pub.post(
            "/api/defensive-geo/publish/commands/pcmd_x/retry-child",
            headers={"Idempotency-Key": "zf-r-" + uuid.uuid4().hex},
            json={"expectedStatusVersion": 1, "expectedCommandHash": "h" * 64}),
    ):
        resp = call()
        # 🔴 分母自证:这三发必须真的**被拒绝**了。如果哪一发意外成功了,
        #    "零冻结"就变成了"零判别力"(成功路径本来就该冻钱)。
        assert 400 <= resp.status_code < 500, (
            f"这一发没有被拒绝(实得 {resp.status_code}),零冻结断言失去被测对象:"
            f"{resp.text[:400]}")
        assert resp.status_code != 500, f"拒绝路径变成 500 了:{resp.text[:400]}"
        rejected.append(resp.status_code)
    assert len(rejected) == 3

    db.rollback()                       # 别让本连接的快照挡住别处的写
    after = _freeze_count(db)
    assert after == before, f"被拒绝的调用仍产生了 {after - before} 行冻结"

    # 🔴 反向对照:同一把尺子必须**能**动。不做这一步,上面那个 0 与"没查"无法区分。
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO public.point_freezes (user_id, feature_code, amount_total) "
            "VALUES (%s, %s, %s) RETURNING id",
            (TENANT_A, "p0close_ruler_liveness", 1))
        inserted = cur.fetchone()["id"]
    moved = _freeze_count(db)
    assert moved == before + 1, (
        f"往 point_freezes 真插了一行,计数却是 {moved}(应为 {before + 1})—— "
        "这把尺子根本没在数,上面那条『零冻结』是空的"
    )
    with db.cursor() as cur:
        cur.execute("DELETE FROM public.point_freezes WHERE id=%s", (inserted,))
    db.rollback()                       # 一次性库也不留残留(污染后续判据)


# ══════════════════════════════════════════════════════════════════════════
# 05 · 反向对照:没有把整个 router 关掉
# ══════════════════════════════════════════════════════════════════════════
def test_05_admin_settlement_review_still_reachable(pub: TestClient) -> None:
    """admin 只读队列必须还在。

    终审单明确禁止「把 router 整个从 server.py 摘掉(会连带 admin 队列)」——
    这条就是那件事的反向对照:关闭闸只关会动钱的那几条,不是把门焊死。
    """
    resp = pub.get(
        "/api/defensive-geo/publish/admin/settlement-review",
        headers={"X-Test-Identity": "admin"},
    )
    assert resp.status_code != 404, "admin 队列路由不见了 —— router 被整个摘掉了"
    assert resp.status_code != 500, f"admin 队列在真库上跑不完:{resp.text[:400]}"
    body = resp.json()
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        assert detail.get("code") != "PUBLISH_ENTRY_CLOSED", (
            "admin 只读队列被关闭闸误伤了 —— 它不产生新冻结,不该关"
        )
    assert resp.status_code == 200, f"期望 200 实得 {resp.status_code}:{resp.text[:400]}"


# ══════════════════════════════════════════════════════════════════════════
# 10-11 · 窗D P1-3:进度端点存在性(成对)
# ══════════════════════════════════════════════════════════════════════════
_PROGRESS = "/api/defensive-geo/monitoring/runs/{tid}/progress"


def test_09_progress_projection_tables_present(db) -> None:
    """判据活性:P1-3 用到的三张表必须都在。

    缺一张的话,下面那对判据会以 UndefinedTable 炸掉或者恒红 ——
    两种都不是"被测代码有问题",是夹具没装起来。
    """
    for table in ("monitoring_tasks", "monitoring_run_cells", "defgeo_monitoring_attempts"):
        with db.cursor() as cur:
            cur.execute("SELECT to_regclass(%s) AS r", (f"public.{table}",))
            assert cur.fetchone()["r"] is not None, f"{table} 不在 —— 夹具没装起来"


@pytest.mark.parametrize("task_id", [-1, 0, 999999999999999999999999999999999])
def test_10_progress_unknown_task_is_typed_not_found(mon: TestClient, task_id: int) -> None:
    """不存在 ⇒ typed NOT_FOUND。

    改之前这三个**全部** 200 + 全零 payload —— "这次跑了 0 格、完成 0%" 与
    "根本没有这次跑" 在客户眼里是两件完全不同的事,而我们把后者说成了前者。
    """
    resp = mon.get(_PROGRESS.format(tid=task_id), params={"brandId": BRAND_A})
    assert resp.status_code != 500, f"越界/不存在的 id 把端点打成了 500:{resp.text[:400]}"
    assert resp.status_code != 200, (
        f"不存在的 task 仍返回 200(无中生有的谎报):{resp.text[:300]}")
    assert resp.status_code == 404, f"期望 404 实得 {resp.status_code}:{resp.text[:300]}"
    detail = resp.json().get("detail", {})
    assert detail.get("code") == "NOT_FOUND", detail


def test_11_progress_existing_task_still_200_and_nonzero(mon: TestClient, db) -> None:
    """🔴 正样本 —— 没有它,"存在也返 404" 不会被任何判据发现。

    造一条**真** task + 两格**真** cell(两个不同 plan_hash),
    进度投影的 ``plannedCells`` 直接 = 不同 plan_hash 的个数,
    所以这里必须拿到 2,不是 0。
    """
    task_id = 987654
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO public.monitoring_tasks (id, client_id, brand_id, status) "
            "VALUES (%s, %s, %s, 'running') ON CONFLICT (id) DO NOTHING",
            (task_id, "p0close-client", BRAND_A))
        for n in (1, 2):
            cur.execute(
                "INSERT INTO public.monitoring_run_cells ("
                "  task_id, brand_id, keyword_id, keyword_source, keyword_snapshot,"
                "  question_snapshot, target_brand_snapshot, platform, is_planned,"
                "  entitlement_snapshot, order_snapshot, fulfillment_credential, plan_hash"
                ") VALUES (%s,%s,%s,'contract','kw','q','b','kimi',true,"
                "          '{}'::jsonb,'{}'::jsonb, gen_random_uuid(), %s)",
                (task_id, BRAND_A, n, str(n) * 64))
    db.commit()

    try:
        resp = mon.get(_PROGRESS.format(tid=task_id), params={"brandId": BRAND_A})
        assert resp.status_code != 404, (
            "存在的 task 被判成 404 —— 存在性闸把正常场景一起杀了")
        assert resp.status_code != 500, f"真 task 把端点打成 500:{resp.text[:400]}"
        assert resp.status_code == 200, f"实得 {resp.status_code}:{resp.text[:300]}"
        body = resp.json()
        assert body["plannedCells"] == 2, (
            f"plannedCells={body['plannedCells']},期望 2 —— "
            "正样本没造起来的话,『不存在返 404』那条就是恒绿的")
    finally:
        with db.cursor() as cur:
            cur.execute("DELETE FROM public.monitoring_run_cells WHERE task_id=%s", (task_id,))
            cur.execute("DELETE FROM public.monitoring_tasks WHERE id=%s", (task_id,))
        db.commit()
