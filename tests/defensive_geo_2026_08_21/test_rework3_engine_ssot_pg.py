"""返修③:引擎单源 · 平台白名单 · UUID 形态 · 单一错误信封。

被测缺陷(门四实证)
--------------------
① 计价平台集与真跑引擎集是**两套互不相干的东西**:
   前端按 ``['qwen','deepseek','kimi','doubao']`` 计价扣费,
   管线真跑 ``["dashscope","deepseek","doubao","yuanbao"]``,
   真跑 diagnosis_id=3 的 ai_visibility 里 kimi 零出现。
   而且 ``'qwen'`` 这个键在后端任何注册表里都不存在 —— 它不是"跑错了",
   是"根本没这个东西"。``platformKeys`` 也没有白名单:传
   ``["totally-fake-platform"]`` 返 200 并真按 1 个平台定价。
② 非 UUID 路径参数 → 裸 500(``Internal Server Error`` 纯文本,连 JSON 都不是)。
③ 同一 router 家族里三种错误信封并存(full typed / minimal typed / framework bare)。

🔴 先证伪工单给的根因
---------------------
工单说「建立单一引擎 SSOT」。但 ``config/ai_engines.py`` **已经是**那个 SSOT
(2026-07-26 Owner 裁定统一五引擎,含 Kimi)。真正的缺口有两个:
  · 它的 docstring 点名的守卫判据
    ``tests/test_ai_engine_registry_single_source_2026_07_26.py``
    **全仓、全分支从未存在过** —— 「不得在别处硬编码引擎列表」自落地起零执行;
  · 诊断侧缺少监测侧早就有的那一对概念:授权面(卖了几个)vs 执行面(真跑几个)。
    监测有 ``MONITORING_ENGINES`` / ``MONITORING_RUN_CELL_PLATFORMS``,诊断只有前者。
所以本包不新建第二个 SSOT(那正是缺陷本身),而是补上诊断侧的执行面常量
``DEFENSIVE_GEO_BILLABLE_ENGINES``,并把缺失的那把守卫真的写出来。

Owner 2026-08-23 口径(本文件按它写判据)
----------------------------------------
  · 计价集 = 执行集 = ``["dashscope","deepseek","doubao","kimi"]``,Kimi 加回并真跑;
  · 元宝**保持现状**:诊断侧继续跑(管线默认里留着),但不进计价、不进对客承诺、
    不进交付链 —— 所以它对客户是个**不能点的键**,``unknown_engines`` 会拒。
"""

from __future__ import annotations

import ast
import io
import re
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
OWNER = 8401
OWN_BRAND = 901

FRONTEND_ENGINES_TS = ROOT / "frontend/src/lib/defensiveGeoEngines.ts"

#: 「不许出现第二份引擎清单」的扫描作用域 = 诊断/防御型 GEO 这条链。
#: 逐个文件列出而不是 glob 目录:glob 会把作用域悄悄扩到别人的地界,
#: 而作用域一变、判据红了,红因与被测行为无关。
_SCAN_ROOTS = (
    "api/defensive_geo_api.py",
    "api/defensive_geo_report_api.py",
    "services/defensive_geo/run_executor.py",
    "services/defensive_geo/plan_store.py",
    "workflows/diagnosis_workflow.py",
    "frontend/src/lib/defensiveGeoEngines.ts",
    "frontend/src/pages/Diagnosis/NewDiagnosis.tsx",
    "frontend/src/components/defensiveGeo/LaunchPanel.tsx",
)


def frontend_selectable_keys() -> list[str]:
    """现读前端可选集。**不手抄** —— 手抄的清单不会跟着前端变。

    也被 test_rework2 的兼容自证复用:两处引用同一个读取谓词,
    免得哪天前端换了位置只改一处。
    """
    src = FRONTEND_ENGINES_TS.read_text(encoding="utf-8")
    m = re.search(r"DEFENSIVE_GEO_PLATFORM_KEYS[^=]*=\s*\[([^\]]+)\]", src)
    assert m, f"{FRONTEND_ENGINES_TS} 里没找到 DEFENSIVE_GEO_PLATFORM_KEYS —— 先核对再改判据"
    return [s.strip().strip("'\"") for s in m.group(1).split(",") if s.strip()]


def workflow_default_engines() -> list[str]:
    """现读管线默认清单(``run_diagnosis_workflow`` 里 ``ai_engines is None`` 那一支)。

    用 AST 找那个赋值,不 grep 字面量:字面量 grep 在"清单已经改成读常量"之后
    会什么都找不到,而"找不到"和"清单是空的"长得一样。
    """
    src = (ROOT / "workflows/diagnosis_workflow.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.AsyncFunctionDef) and node.name == "run_diagnosis_workflow"):
            continue
        for sub in ast.walk(node):
            if not (isinstance(sub, ast.Assign) and len(sub.targets) == 1):
                continue
            tgt = sub.targets[0]
            if not (isinstance(tgt, ast.Name) and tgt.id == "ai_engines"):
                continue
            val = sub.value
            if isinstance(val, ast.List):          # 老形态:就地写死的清单
                return [e.value for e in val.elts if isinstance(e, ast.Constant)]
            if isinstance(val, ast.Call):          # 新形态:list(常量)
                from config.ai_engines import DIAGNOSIS_RUNTIME_ENGINES

                name = getattr(val.args[0], "id", None) if val.args else None
                assert name == "DIAGNOSIS_RUNTIME_ENGINES", (
                    f"管线默认取的是 {name!r},不是 SSOT 常量 —— 又出现了第二份清单")
                return list(DIAGNOSIS_RUNTIME_ENGINES)
    raise AssertionError("在 run_diagnosis_workflow 里没找到 ai_engines 默认赋值 —— 先核对再改判据")


@pytest.fixture(scope="module")
def client():
    from api.defensive_geo_api import router as facade_router
    from api.defensive_geo_report_api import router as report_router

    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):
        tid = request.headers.get("X-Test-Tenant")
        if tid:
            request.state.user = {"user_id": int(tid), "is_admin": False}
        return await call_next(request)

    app.include_router(facade_router)
    app.include_router(report_router)
    return TestClient(app, raise_server_exceptions=False)


def _h(tenant=OWNER, idem=None):
    h = {"X-Test-Tenant": str(tenant)}
    if idem:
        h["Idempotency-Key"] = idem
    return h


def _make_plan(client):
    r = client.post(
        "/api/defensive-geo/question-plans/preview",
        json={"clientRequestId": "creq-" + uuid.uuid4().hex[:10], "brandId": OWN_BRAND,
              "profileRevisionId": "prof-1", "mode": "defensive",
              "questions": [{"text": "这个牌子靠谱吗", "modeSide": "defensive",
                             "familyKey": "identity_check", "brandExposure": "named"}]},
        headers=_h())
    assert r.status_code == 200, r.text
    return r.json()


def _preview(client, plan, keys):
    return client.post(
        "/api/defensive-geo/run-previews",
        json={"questionPlanId": plan["planId"], "questionPlanRevision": 1,
              "profileRevisionId": "prof-1", "platformKeys": keys},
        headers=_h(idem="idem-" + uuid.uuid4().hex[:10]))


# ══════════════════════════════════════════════════════════════════════════
# ① 同源锁 —— 从 SSOT 删一个引擎,三处必须同时红
# ══════════════════════════════════════════════════════════════════════════
def test_ssot_denominator_is_real():
    """零分母守卫:SSOT 不能是空的,也不能只剩一个。

    空清单会让下面三条「集合相等」退化成 [] == [],全绿而什么都没证明。
    """
    from config.ai_engines import DEFENSIVE_GEO_BILLABLE_ENGINES

    assert len(DEFENSIVE_GEO_BILLABLE_ENGINES) >= 2, DEFENSIVE_GEO_BILLABLE_ENGINES
    assert len(set(DEFENSIVE_GEO_BILLABLE_ENGINES)) == len(DEFENSIVE_GEO_BILLABLE_ENGINES), "SSOT 自己有重复"


#: Owner 2026-08-23 原话转录,作为**独立锚**。
#: 🔴 为什么不能直接用 SSOT 常量当期望值:第一版就是那么写的,变异「从 SSOT 删掉
#:    kimi」当场**存活** —— 判据拿同一个被改的常量既造输入又造期望,两边一起变,
#:    格数永远对得上。判据必须有一个**不随被测对象移动**的锚,否则它测的是
#:    「常量等于它自己」。(同一形态本包已踩过第二次:返修② 的 M9 也是这个。)
OWNER_RULED_BILLABLE = ("dashscope", "deepseek", "doubao", "kimi")


def test_pricing_cells_follow_the_ssot(client):
    """同源锁 1/3 —— **计价**。

    锚是 Owner 口径那四个,不是 SSOT 自己。从 SSOT 删掉 kimi 之后:
      · 下面第一句(SSOT == Owner 口径)红;
      · 而且 kimi 会被白名单拒 → 这一发请求直接 422,格数断言也拿不到 200。
    """
    from config.ai_engines import DEFENSIVE_GEO_BILLABLE_ENGINES

    assert tuple(DEFENSIVE_GEO_BILLABLE_ENGINES) == OWNER_RULED_BILLABLE, (
        f"计价集 {tuple(DEFENSIVE_GEO_BILLABLE_ENGINES)} 与 Owner 2026-08-23 口径 "
        f"{OWNER_RULED_BILLABLE} 不一致 —— 改计价集要 Owner 拍板,不是改一行常量")

    plan = _make_plan(client)
    resp = _preview(client, plan, list(OWNER_RULED_BILLABLE))
    assert resp.status_code == 200, (
        f"Owner 口径那四个引擎过不了计价:{resp.status_code} {resp.text[:200]}")
    body = resp.json()
    assert body["plannedCells"] == plan["counts"]["total"] * len(OWNER_RULED_BILLABLE), (
        f"计划格数 {body['plannedCells']} != {plan['counts']['total']} × {len(OWNER_RULED_BILLABLE)}")


def test_frontend_selectable_set_equals_the_ssot():
    """同源锁 2/3 —— **前端可选集**(跨语言)。

    这是三处里最容易漂的一处:前端改一行没人会去看 Python。
    门四实证的 ``'qwen'`` 就是这么来的 —— 它连后端的键空间都不在。
    """
    from config.ai_engines import DEFENSIVE_GEO_BILLABLE_ENGINES

    assert frontend_selectable_keys() == list(DEFENSIVE_GEO_BILLABLE_ENGINES), (
        f"前端可选集 {frontend_selectable_keys()} 与 SSOT "
        f"{list(DEFENSIVE_GEO_BILLABLE_ENGINES)} 不一致 —— 客户会为一个我们不跑的平台付钱")


def test_workflow_default_still_carries_yuanbao_unchanged():
    """Owner 口径「元宝保持现状」的锁 + 搬迁的**兼容自证**。

    管线默认清单是 legacy 入口用的,Owner 明确它**不变**:元宝继续在诊断侧跑,
    只是不进计价/对客承诺/交付链。所以这条断言两件事:
      ① 它仍然逐字节等于搬迁前的 ``["dashscope","deepseek","doubao","yuanbao"]``
         —— 把清单搬进 SSOT 那一步只该搬位置,不该改内容;
      ② 它与计价集**刻意不同**(元宝在这边、Kimi 在那边)。这不是漂移,是两个
         不同的问题:「我们自己观测哪几个」vs「客户买了哪几个」。
         防御型 GEO 那条链不吃这个默认(执行器显式传付费集),所以两者不同
         不会再变成「客户为 A 付钱、系统跑 B」。
    """
    from config.ai_engines import DEFENSIVE_GEO_BILLABLE_ENGINES, DIAGNOSIS_RUNTIME_ENGINES

    got = workflow_default_engines()
    assert got == ["dashscope", "deepseek", "doubao", "yuanbao"], (
        f"管线默认值变了:{got} —— Owner 口径是元宝保持现状")
    assert got == list(DIAGNOSIS_RUNTIME_ENGINES), f"管线默认 {got} 与 SSOT 常量不一致"
    assert "yuanbao" not in DEFENSIVE_GEO_BILLABLE_ENGINES, (
        "元宝进了计价集 —— Owner 口径是它不进计价/不进对客承诺/不进交付链")
    assert "kimi" in DEFENSIVE_GEO_BILLABLE_ENGINES, "Kimi 不在计价集 —— Owner 口径是加回并真跑"


def test_customer_cannot_buy_yuanbao(client):
    """元宝「不进计价、不进对客承诺」的**可执行**那一面。

    光在常量里排除它是不够的 —— 得证明客户真的点不动:
    把 yuanbao 当平台键提交,必须被白名单 typed 拒绝,而不是照单收钱。
    """
    plan = _make_plan(client)
    r = _preview(client, plan, ["dashscope", "yuanbao"])
    assert r.status_code != 200, f"客户买到了元宝:{r.text[:200]}"
    assert isinstance(r.json().get("detail"), dict), r.text[:200]


def test_executor_passes_the_paid_platform_set_to_the_pipeline():
    """同源锁的下游:执行器**真的把付费平台集传给管线**。

    ``ai_engines`` 这个参数一直存在,但从来没人传 —— 于是管线永远回落到
    自己的默认清单。这条打的是接线:关键字必须出现在**调用**里,
    且值来自冻结面而不是现算的默认。
    """
    import inspect

    import services.defensive_geo.run_executor as ex

    src = inspect.getsource(ex.dispatch_one)
    tree = ast.parse(src.lstrip())
    passed = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fname = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if fname != "run_diagnosis_task":
                continue
            for kw in node.keywords:
                if kw.arg == "ai_engines":
                    passed.append(ast.dump(kw.value))
    assert passed, "run_diagnosis_task 调用里没有 ai_engines —— 客户买的检索面没交给管线"
    assert "platform_keys" in passed[0], f"ai_engines 不是来自冻结面:{passed[0][:120]}"


def test_pipeline_actually_accepts_the_parameter():
    """反向对照:上一条是**源码锁**,只能证明"写了"。

    这条证明"传进去有人接":两层签名(server.run_diagnosis_task /
    workflows.run_diagnosis_workflow)都必须真的有 ai_engines 形参 ——
    否则调用当场 TypeError,而源码锁照绿。
    """
    import inspect

    from server import run_diagnosis_task
    from workflows.diagnosis_workflow import run_diagnosis_workflow

    for fn in (run_diagnosis_task, run_diagnosis_workflow):
        assert "ai_engines" in inspect.signature(fn).parameters, (
            f"{fn.__name__} 没有 ai_engines 形参 —— 传参会 TypeError")


def test_no_second_engine_list_anywhere():
    """🔴 补上那把**从未存在过**的守卫。

    ``config/ai_engines.py`` 的 docstring 写着「新增硬编码即转红」并点名
    ``tests/test_ai_engine_registry_single_source_2026_07_26.py`` ——
    那个文件全仓、全分支从来没有过。所以「不得在别处硬编码引擎列表」
    自 2026-07-26 落地起就是一句没有执行力的话,而 'qwen' 那份清单
    正是在这段空白里长出来的。

    扫描口径:源码里出现「同一个字面量列表里有 ≥3 个已知引擎键」即判红。
    ≥3 是为了不误伤"提到两个引擎"的普通代码。

    🔴 作用域 = **诊断/防御型 GEO 这条链**,写在 ``_SCAN_ROOTS`` 里,不是全仓
    ------------------------------------------------------------------------
    全仓扫是错的,不是偷懒:监测侧有**另一个**合法的引擎集
    (``MONITORING_ENGINES`` / ``MONITORING_RUN_CELL_PLATFORMS``,与诊断计价集
    不同且必须不同)。一把全仓尺子会去要求监测 UI 消费诊断的常量 —— 那是把
    两个不同的问题硬压成一个,比现在更糟。

    🔴 扫描时**跳过注释行**:本判据与前端那份 SSOT 的文档里都逐字引用了
    出问题的那两份清单(讲清缺陷长什么样)。引用裁决原文把裸串结构锚判红,
    本仓 2026-08 记过这个形态。收紧匹配面(只看代码行)比加白名单硬 ——
    白名单会把真的第二份清单也一起放过。

    ⚠️ 作用域外**已发现但本包不改**的三处(已在交付单如实列明,给 Owner 决定):
       frontend/src/sandbox/mockData.ts · 顾问团详情抽屉([开源 E3 · 前端 · 2026-10-01 · WO_322] 已随顾问团前端删)
       · frontend/src/pages/Monitoring/components/ProgressPanel.tsx
    它们属于监测/顾问/沙箱面,不在本单授权范围内,静默改别人的地界比不改更贵。
    """
    from config.ai_engines import ENGINE_LABELS

    known = set(ENGINE_LABELS) | {"qwen"}
    pattern = re.compile(r"\[[^\[\]\n]{0,200}\]")
    comment = re.compile(r"^\s*(#|//|\*|/\*)")
    offenders: list[str] = []
    for rel in _SCAN_ROOTS:
        path = ROOT / rel
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if comment.match(line):
                continue
            for lit in pattern.findall(line):
                hits = {k for k in known if f"'{k}'" in lit or f'"{k}"' in lit}
                if len(hits) >= 3:
                    offenders.append(f"{rel}: {lit.strip()[:90]}")
    assert not offenders, (
        "这些地方又写了一份引擎清单(必须改成消费 config/ai_engines):\n  "
        + "\n  ".join(offenders))


def test_the_scan_scope_is_not_empty():
    """零分母守卫:作用域里的文件必须真的存在。

    路径写错时 ``path.exists()`` 会静静跳过,扫描器变成"扫了 0 个文件"——
    那时上一条恒绿,而它本来是唯一在守「不许出现第二份清单」的东西。
    """
    missing = [rel for rel in _SCAN_ROOTS if not (ROOT / rel).exists()]
    assert not missing, f"扫描作用域里这些路径不存在:{missing}"
    assert len(_SCAN_ROOTS) >= 4, f"作用域只有 {len(_SCAN_ROOTS)} 个文件,分母可疑"


def test_the_hardcode_detector_has_discriminating_power(tmp_path):
    """反向对照:上一条的扫描器必须真的认得出一份硬编码清单。

    没有这一条,把 pattern 写错(比如永远匹配不到)也能让它恒绿 ——
    本仓 2026-08-21 刚记过「加宽 pattern 没加正样本 = 加宽没判据在守」。
    """
    from config.ai_engines import ENGINE_LABELS

    known = set(ENGINE_LABELS) | {"qwen"}
    poison = "platformKeys={['qwen', 'deepseek', 'kimi', 'doubao']}"   # 门四那一行原样
    lit = re.search(r"\[[^\[\]\n]{0,200}\]", poison).group(0)
    hits = {k for k in known if f"'{k}'" in lit or f'"{k}"' in lit}
    assert len(hits) >= 3, f"扫描器认不出门四那份真实硬编码清单:命中 {hits}"


# ══════════════════════════════════════════════════════════════════════════
# ② 白名单锁 —— 未知平台键 typed 拒绝 + 反向对照
# ══════════════════════════════════════════════════════════════════════════
def test_unknown_platform_key_is_typed_refusal_not_a_silent_charge(client):
    """🔴 门四原样复现:``["totally-fake-platform"]`` 曾经返 200 并真定价。

    客户为一个不存在的检索面付了钱,而系统永远不会去跑它。
    """
    plan = _make_plan(client)
    r = _preview(client, plan, ["totally-fake-platform"])
    assert r.status_code != 200, f"未知平台键被静默接受并定价了:{r.text[:200]}"
    assert r.status_code != 500, f"未知平台键打出了 500:{r.text[:200]}"
    detail = r.json().get("detail")
    assert isinstance(detail, dict), f"不是 typed 信封:{r.text[:200]}"
    assert detail.get("publicExplanation"), detail
    assert detail.get("nextAction", {}).get("label"), detail


def test_partially_unknown_platform_set_is_refused_too(client):
    """混着一个未知键也必须拒 —— 不许"把认识的留下、悄悄丢掉不认识的"。

    静默丢弃最坏:她选了 4 个、按 3 个计价、报告里出现 3 个,
    三处各自都"对",没有任何一处会说出那一个去哪了。
    """
    from config.ai_engines import DEFENSIVE_GEO_BILLABLE_ENGINES

    plan = _make_plan(client)
    r = _preview(client, plan, [DEFENSIVE_GEO_BILLABLE_ENGINES[0], "nope-engine"])
    assert r.status_code != 200, f"混入未知键仍被接受:{r.text[:200]}"


def test_legal_keys_still_pass_at_the_same_price(client):
    """🔴 白名单的反向对照:合法键仍 200,**且价格不变**。

    只验"未知键被拒"是危险的:把端点改成一律拒也能全绿,而那等于把功能关了。
    """
    from config.ai_engines import DEFENSIVE_GEO_BILLABLE_ENGINES

    plan = _make_plan(client)
    two = list(DEFENSIVE_GEO_BILLABLE_ENGINES[:2])
    r = _preview(client, plan, two)
    assert r.status_code == 200, f"合法平台键被拒了:{r.status_code} {r.text[:200]}"
    body = r.json()
    assert body["plannedCells"] == plan["counts"]["total"] * 2, body
    assert body["exactTotalPoints"] > 0, "价格是 0 —— 这条反向对照没有判别力"


def test_frontend_live_set_passes_the_whitelist(client):
    """前端**今天真发的那一组**必须能过白名单。

    这条是白名单与同源锁的交叉自证:两者只要有一处漂,线上就会出现
    「点开体检直接被拒」——那是比多扣钱更响的故障,必须先在这里响。
    """
    plan = _make_plan(client)
    r = _preview(client, plan, frontend_selectable_keys())
    assert r.status_code == 200, f"线上前端那组平台键过不了白名单:{r.status_code} {r.text[:200]}"


# ══════════════════════════════════════════════════════════════════════════
# ③ UUID 形态 —— 分母从 router 机械枚举
# ══════════════════════════════════════════════════════════════════════════
def _uuid_path_routes():
    """机械分母:带 ``{plan_id}`` / ``{preview_id}`` 的路由,从 router 对象枚举。

    手抄清单漏掉的那一条不会让任何判据变红(本仓 2026-08-19 实证)。
    """
    from fastapi.routing import APIRoute

    import api.defensive_geo_api as facade

    out = []
    for route in facade.router.routes:
        if isinstance(route, APIRoute) and re.search(r"\{(plan_id|preview_id)\}", route.path):
            out.append(route)
    return out


def test_uuid_route_census_is_real():
    """零分母守卫。"""
    routes = _uuid_path_routes()
    assert len(routes) >= 2, f"只扫到 {len(routes)} 条 uuid 路由,分母可疑"


def test_no_uuid_route_returns_a_bare_500(client):
    """🔴 门四原样复现:``/question-plans/not-a-uuid?revision=1`` → 裸 500 纯文本。

    ``plan_id`` / ``preview_id`` 在库里是 ``uuid`` 列,非法串直接撞进 SQL。
    任何登录用户都能打出来;她看到"系统坏了"于是重试,而真相只是链接抄错一位。
    """
    for route in _uuid_path_routes():
        path = re.sub(r"\{(plan_id|preview_id)\}", "not-a-uuid", route.path)
        method = "POST" if "POST" in route.methods else "GET"
        params = {"revision": 1} if "{plan_id}" in route.path else None
        resp = (client.get(path, params=params, headers=_h())
                if method == "GET" else
                client.post(path, json={"expectedHash": "x" * 64},
                            headers=_h(idem="u-" + uuid.uuid4().hex[:8])))
        assert resp.status_code != 500, f"{path} 非 UUID 打出 500:{resp.text[:160]}"
        body = resp.json()          # 裸文本会在这里抛,正是要挡的那一种
        assert isinstance(body.get("detail"), dict), f"{path} 返回了非 typed 信封:{resp.text[:160]}"
        assert body["detail"].get("publicExplanation"), body["detail"]


def test_legal_uuid_still_works(client):
    """反向对照:合法 UUID 照常工作 —— 否则"一律 404"也能让上一条全绿。"""
    plan = _make_plan(client)
    r = client.get(f"/api/defensive-geo/question-plans/{plan['planId']}",
                   params={"revision": 1}, headers=_h())
    assert r.status_code == 200, f"合法 UUID 也打不开了:{r.status_code} {r.text[:200]}"


def test_malformed_and_absent_uuid_are_byte_identical(client):
    """形状不合法 与 合法但不存在 必须同形 —— 否则又多一个枚举面。"""
    absent = str(uuid.uuid4())
    bad = client.get("/api/defensive-geo/question-plans/not-a-uuid",
                     params={"revision": 1}, headers=_h())
    gone = client.get(f"/api/defensive-geo/question-plans/{absent}",
                      params={"revision": 1}, headers=_h())
    assert bad.status_code == gone.status_code, (bad.status_code, gone.status_code)
    assert bad.content == gone.content, (bad.text[:160], gone.text[:160])


# ══════════════════════════════════════════════════════════════════════════
# ④ 单一错误信封 —— census
# ══════════════════════════════════════════════════════════════════════════
_PACKAGE_API_FILES = ("api/defensive_geo_api.py", "api/defensive_geo_report_api.py")


def _refusal_raises():
    """census:本包 api 模块里所有 ``raise HTTPException(...)`` 的位置。

    分母不手抄:AST 扫。``_safe_error(...)`` 是唯一允许的造信封方式,
    直接 ``raise HTTPException`` 就是"手写第二种信封"那一形态。
    """
    out = []
    for rel in _PACKAGE_API_FILES:
        src = io.open(ROOT / rel, "r", encoding="utf-8", newline="").read().replace("\r\n", "\n")
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                fname = getattr(node.exc.func, "id", None) or getattr(node.exc.func, "attr", None)
                if fname == "HTTPException":
                    out.append(f"{rel}:{node.lineno}")
    return out


def test_no_handwritten_httpexception_in_the_package():
    """🔴 「minimal typed」那一种信封的来源:手写 HTTPException。

    ``{"code": "NOT_AUTHENTICATED"}`` 就是这么来的 —— 没有 publicExplanation
    ⇒ 前端弹窗一片空白;没有 nextAction ⇒ 她读完不知道该干什么。
    """
    handwritten = _refusal_raises()
    assert not handwritten, (
        f"这些地方手写了 HTTPException,绕过 _safe_error 家族:{handwritten}")


def test_every_error_code_renders_a_full_envelope():
    """census:``_ERROR_TABLE`` 里每个 code 造出来都必须是 full typed。

    分母是**表本身**,不是我挑的几个 —— 新加一个 code 忘了配文案,这里先红。
    (``_NEVER_SURFACED_CODES`` 是 §0.5 L124 明令永不上屏的,单独放行。)
    """
    from api.defensive_geo_api import _ERROR_TABLE, _NEVER_SURFACED_CODES, _safe_error

    assert len(_ERROR_TABLE) >= 10, f"错误表只有 {len(_ERROR_TABLE)} 项,分母可疑"
    bad = {}
    for code in _ERROR_TABLE:
        detail = _safe_error(code).detail
        missing = [k for k in ("code", "retryable", "nextAction") if k not in detail]
        if code not in _NEVER_SURFACED_CODES and "publicExplanation" not in detail:
            missing.append("publicExplanation")
        if not detail.get("nextAction", {}).get("label"):
            missing.append("nextAction.label")
        if missing:
            bad[code] = missing
    assert not bad, f"这些 code 造出来是残缺信封:{bad}"


def test_unexpected_exception_becomes_a_typed_envelope(client, monkeypatch):
    """🔴 「framework bare」那一种:谁都没接住的异常 → 裸 500 纯文本。

    这条真的往端点里塞一个异常,不是读源码 —— 源码锁只能证明写了 try,
    证明不了它真的在请求路径上。
    """
    import services.defensive_geo.plan_store as ps

    def _boom(*a, **k):
        raise RuntimeError("判据注入的内部不变式失败")

    monkeypatch.setattr(ps, "get_plan_exact_revision", _boom)
    r = client.get(f"/api/defensive-geo/question-plans/{uuid.uuid4()}",
                   params={"revision": 1}, headers=_h())
    assert r.status_code == 500, r.status_code
    body = r.json()                      # 裸文本会在这里抛
    assert body["detail"]["code"] == "INTERNAL_ERROR", body
    assert body["detail"]["publicExplanation"], body
    assert "判据注入" not in r.text, "内部异常文本泄露到了客户那一面"


def test_request_validation_error_is_typed_but_still_422(client):
    """入参 schema 不过:码仍是 422(既有判据断言的就是这个码),体变成人话。"""
    plan = _make_plan(client)
    r = client.post(
        "/api/defensive-geo/run-previews",
        json={"questionPlanId": plan["planId"], "questionPlanRevision": 1,
              "profileRevisionId": "prof-1", "platformKeys": ["deepseek"], "unknownKey": 1},
        headers=_h(idem="v-" + uuid.uuid4().hex[:8]))
    assert r.status_code == 422, r.status_code
    detail = r.json().get("detail")
    assert isinstance(detail, dict) and detail.get("publicExplanation"), r.text[:200]


def test_both_routers_share_the_single_envelope_route_class():
    """两个 router 必须挂同一个 route_class —— 少挂一个就又回到三形态。"""
    import api.defensive_geo_api as facade
    import api.defensive_geo_report_api as report
    from services.defensive_geo.typed_error_route import TypedErrorRoute

    for mod in (facade, report):
        assert mod.router.route_class is TypedErrorRoute, (
            f"{mod.__name__} 的 router 没挂 TypedErrorRoute")


# ══════════════════════════════════════════════════════════════════════════
# ⑤ Owner 判据③ —— Kimi 真跑锁
# ══════════════════════════════════════════════════════════════════════════
def test_kimi_is_a_real_branch_in_the_engine_dispatch():
    """🔴 Owner 判据③ 的**接线**那一半(零成本,每次都跑)。

    ``batch_query_ai_engines`` 用 if/elif 逐个引擎分派,不认识的键返回
    ``{"error": "Unknown engine"}`` —— 也就是说「计价里有、分派里没有」
    会安静地退化成一条 error 记录,而总数照算。这条把 kimi 那一支钉死:
    把 ``elif engine == "kimi"`` 删掉,这里立刻红。

    桩替的是**被调方**(真 LLM),不是被测代码(分派)。真跑取证另见交付单
    (那一次真花钱,不适合放进每次都跑的判据里)。
    """
    import asyncio
    import json

    import tools.ai_visibility.ai_tester as T
    from config.ai_engines import DEFENSIVE_GEO_BILLABLE_ENGINES

    assert "kimi" in DEFENSIVE_GEO_BILLABLE_ENGINES, "计价集里没有 kimi —— 前提就不成立"

    class _Stub:
        def __init__(self, text):
            self.content = [{"type": "text", "text": text}]

    async def _fake(query, check_brand="", **kw):
        return _Stub(json.dumps({"answer": "桩答案", "brand_mentioned": True,
                                 "references": []}, ensure_ascii=False))

    originals = {}
    try:
        for name in ("query_kimi_search", "query_dashscope_search",
                     "query_doubao_search", "query_deepseek", "query_yuanbao"):
            if hasattr(T, name):
                originals[name] = getattr(T, name)
                setattr(T, name, _fake)
        out = asyncio.new_event_loop().run_until_complete(
            T.batch_query_ai_engines("这个牌子靠谱吗", "判据品牌", engines=["kimi"]))
    finally:
        for name, fn in originals.items():
            setattr(T, name, fn)

    payload = json.loads(out.content[0]["text"])
    assert payload["total_engines"] == 1, payload
    assert payload["successful_engines"] == 1, (
        f"kimi 没能成功分派 —— 很可能落进了 Unknown engine 分支:{payload}")
    assert payload["results"] and "error" not in payload["results"][0], payload["results"]


def test_unknown_engine_really_degrades_silently():
    """反向对照:证明上一条**认得出**「分派里没有」这件事。

    没有这一条,上一条可能只是"随便传什么都成功"——那它什么都没证明。
    这里传一个真的不存在的引擎,必须落进 Unknown engine 分支。
    """
    import asyncio
    import json

    import tools.ai_visibility.ai_tester as T

    out = asyncio.new_event_loop().run_until_complete(
        T.batch_query_ai_engines("这个牌子靠谱吗", "判据品牌", engines=["no-such-engine"]))
    payload = json.loads(out.content[0]["text"])
    assert payload["successful_engines"] == 0, (
        f"不存在的引擎居然分派成功了 —— 上一条判据没有判别力:{payload}")


def test_the_paid_set_reaches_the_engine_dispatch_intact():
    """付费集 → 管线 → 分派,中间没人替换。

    执行器传 ``ai_engines`` 给 ``run_diagnosis_task``,管线再往下传到检索层。
    这条打的是**管线签名的默认分支不会吃掉显式传参**:显式传了就得用传的那份。
    """
    import ast
    import inspect

    from workflows.diagnosis_workflow import run_diagnosis_workflow

    src = inspect.getsource(run_diagnosis_workflow)
    tree = ast.parse(src.lstrip())
    guarded = False
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
            left = node.test.left
            if isinstance(left, ast.Name) and left.id == "ai_engines" and \
                    any(isinstance(c, ast.Is) for c in node.test.ops):
                guarded = True
    assert guarded, (
        "管线里找不到 `if ai_engines is None:` 那道闸 —— "
        "默认值可能无条件覆盖显式传参,那样执行器传什么都没用")
