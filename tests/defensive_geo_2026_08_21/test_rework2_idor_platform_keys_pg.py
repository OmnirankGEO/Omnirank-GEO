"""返修②:对象级归属(IDOR)· 平台集去重 · 幂等 hash 覆盖平台集。

三条被测缺陷都不是「形状不好看」,都是能算出后果的
----------------------------------------------------
① ``/reports/{diagnosis_id}/presentation`` 原来只验「登录了没」。``diagnosis_id``
   是自增整数,可枚举 —— 任何已登录用户 for 循环一遍就能拿到全站每份报告的
   campaign_mode / 题单 id / 题单版本。那是别人客户的商业信息。
② ``planned_cells = 题数 × 平台数`` 直接决定报价与冻结额。``["kimi","kimi"]``
   把同一格算两遍:客户为一份工作付两份钱,真跑的时候平台就那么一个。
③ 幂等唯一约束是 ``(tenant, idempotency_key, canonical_request_hash)``,而平台集
   原来不进 hash。同一把幂等键换个平台集会命中旧行 —— 她选了 4 个平台,
   拿到的是 1 个平台的报价与题单,响应里还写着「已受理」。

🔴 本文件**不 patch RBAC**
--------------------------
同目录其它判据为了验 façade 合同会 ``monkeypatch(ba.require_brand_access)``。
那在这里是致命的:被测对象**就是**归属校验本身,patch 掉等于把被测代码摘了
再验它。所以本文件全程走真 RBAC(靠 conftest 真种的 ``brands.owner_user_id``),
并且用 ``test_this_file_never_patches_the_rbac`` 把这一点钉死 ——
将来谁顺手加一行 patch,那条判据先红。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]

#: conftest 真种的那一对:brands.id=901 的 owner_user_id 就是 8401。
OWNER = 8401
STRANGER = 8402
OWN_BRAND = 901
#: 本文件自己种,用于「两次真 confirm 比冻结额」——
#: ``uq_diag_active_per_brand`` 不允许同一品牌同时两条活跃 run。
SECOND_BRAND = 9021
#: 不存在的 id。下面有一条判据真的去库里确认它不存在(否则「同形」是巧合)。
GHOST_DIAGNOSIS_ID = 2_000_000_000


def _conn():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn


@pytest.fixture(scope="module")
def client():
    """一个宿主挂两个 router:façade(平台集/幂等)与 report(归属)。

    中间件写 ``user_id`` —— 与生产 ``auth/middleware.py`` 同形。
    (本仓 2026-08-20 踩过「夹具用的键是生产从来不会发的键」:
     生产写 user_id、模块读 user["id"],整片端点生产必 500 而判据全绿。)
    """
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


@pytest.fixture(scope="module", autouse=True)
def _seed_second_brand():
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO brands (id, name, owner_user_id, industry) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                (SECOND_BRAND, "判据品牌-defgeo-2", OWNER, "本地生活服务"))
        conn.commit()
    finally:
        conn.close()
    yield


@pytest.fixture(autouse=True)
def _clean_denominator():
    """清掉本租户的活跃 run。

    ``uq_diag_active_per_brand`` 是生产真约束:一个品牌同时只能有一条活跃诊断。
    上一条判据 confirm 留下的活跃 run 会让下一条**合法地** 409 ——
    那时判据红得莫名其妙,而被测代码毫无问题。
    """
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM diagnosis_runs WHERE owner_user_id = ANY(%s)",
                        ([OWNER, STRANGER],))
        conn.commit()
    finally:
        conn.close()
    yield


def _h(tenant=OWNER, idem=None):
    h = {"X-Test-Tenant": str(tenant)}
    if idem:
        h["Idempotency-Key"] = idem
    return h


def _seed_diagnosis(brand_id) -> int:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO diagnosis_records (session_id, brand_name, industry, brand_id) "
                "VALUES (%s,%s,%s,%s) RETURNING id",
                ("sess-" + uuid.uuid4().hex[:12], "判据品牌-defgeo", "本地生活服务", brand_id))
            did = cur.fetchone()["id"]
        conn.commit()
        return int(did)
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
# ① IDOR —— 分母从 router census 机械导出
# ══════════════════════════════════════════════════════════════════════════
def _diagnosis_id_routes():
    """机械分母:**从 router 对象自己**枚举带 ``{diagnosis_id}`` 的路由。

    手抄清单的问题很具体:新加一个按 diagnosis_id 取数的端点时,清单不会自己
    长出来,判据照绿,而那个端点没有归属校验 —— 正是本次要修的那个洞的复制品。
    """
    from fastapi.routing import APIRoute

    import api.defensive_geo_report_api as mod

    out = []
    for route in mod.router.routes:
        if isinstance(route, APIRoute) and "{diagnosis_id}" in route.path:
            out.append(route)
    return out


def test_diagnosis_id_route_census_is_real_and_probeable():
    """🔴 零分母守卫 + 探针可用性守卫。

    分母为空时下面每条「他人被拒」都会退化成空循环全绿。
    另外:探针只会造 ``GET /...{diagnosis_id}...`` 这一种请求,所以一旦出现
    别的方法或别的路径参数,这条判据**先炸**,逼下一个人去扩探针 ——
    而不是让那条新路由静悄悄地没人验。
    """
    routes = _diagnosis_id_routes()
    assert routes, "一条按 diagnosis_id 取数的路由都没扫到 —— 归属判据会恒绿"
    for r in routes:
        assert r.methods == {"GET"}, (
            f"{r.path} 的方法是 {r.methods},本文件的探针只会打 GET。"
            "请扩探针,别把这条路由留在分母外面。")
        params = set(re.findall(r"{([^}:]+)", r.path))
        assert params == {"diagnosis_id"}, (
            f"{r.path} 还有别的路径参数 {params},探针不知道拿什么填。请扩探针。")


def test_ghost_id_really_does_not_exist():
    """「同形」要有意义,前提是那个 id 真的不存在。"""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 AS x FROM diagnosis_records WHERE id=%s", (GHOST_DIAGNOSIS_ID,))
            assert cur.fetchone() is None, "GHOST id 竟然存在 —— 换一个,否则同形判据是巧合"
    finally:
        conn.close()


def test_owner_can_read_every_diagnosis_route(client):
    """成对判据的**放行**半边。

    只验「他人被拒」是危险的:把端点改成无条件 404 也能全绿,
    而那等于把功能删了。
    """
    did = _seed_diagnosis(OWN_BRAND)
    for r in _diagnosis_id_routes():
        resp = client.get(r.path.replace("{diagnosis_id}", str(did)), headers=_h(OWNER))
        assert resp.status_code == 200, f"{r.path} 对创建者返回 {resp.status_code}: {resp.text}"


def test_stranger_is_refused_on_every_diagnosis_route(client):
    """🔴 成对判据的**拒绝**半边:别人的诊断,不许读。"""
    did = _seed_diagnosis(OWN_BRAND)
    for r in _diagnosis_id_routes():
        resp = client.get(r.path.replace("{diagnosis_id}", str(did)), headers=_h(STRANGER))
        assert resp.status_code in (403, 404), (
            f"{r.path} 让 {STRANGER} 读到了 {OWNER} 的诊断 {did}:"
            f"{resp.status_code} {resp.text[:200]}")


def test_stranger_and_nonexistent_are_byte_identical(client):
    """跨租户与不存在必须**逐字节同形**,否则 id 可枚举。

    差一个字都够:``诊断记录不存在`` vs ``资源不存在`` 就是一个可用的 oracle,
    攻击者据此能把全站有效 id 扫出来。所以这里比的是 ``content``,不是 ``status_code``。
    """
    did = _seed_diagnosis(OWN_BRAND)
    for r in _diagnosis_id_routes():
        foreign = client.get(r.path.replace("{diagnosis_id}", str(did)), headers=_h(STRANGER))
        ghost = client.get(
            r.path.replace("{diagnosis_id}", str(GHOST_DIAGNOSIS_ID)), headers=_h(STRANGER))
        assert foreign.status_code == ghost.status_code, (
            f"{r.path}:他人 {foreign.status_code} vs 不存在 {ghost.status_code} —— id 可枚举")
        assert foreign.content == ghost.content, (
            f"{r.path} 的两种拒绝响应体不同,可用来判断 id 是否存在:\n"
            f"  他人  : {foreign.text[:200]}\n  不存在: {ghost.text[:200]}")


def test_null_brand_diagnosis_is_fail_closed(client):
    """``brand_id`` 为 NULL 的残缺行:无归属线索 ⇒ 拒,不是放行。

    这是 GEO-R1-CAN-139 修过的同一类洞(当时默认 ``allow_null=True``,
    对所有已登录非管理员 fail-open)。本端点显式传 ``allow_null=False``,
    这条判据钉住那个显式值 —— 有人把它改回 True,这里先红。
    """
    orphan = _seed_diagnosis(None)
    for r in _diagnosis_id_routes():
        resp = client.get(r.path.replace("{diagnosis_id}", str(orphan)), headers=_h(STRANGER))
        assert resp.status_code in (403, 404), (
            f"{r.path} 对 NULL-brand 残缺行 fail-open 了:{resp.status_code}")


def test_refusal_uses_the_typed_envelope_not_a_raw_detail(client):
    """拒绝走 §15.8 信封,不是 FastAPI 的裸 detail 字符串。

    裸 detail 有两个问题:①「资源不存在」这种内部文案会直接上屏;
    ② 它随抛出点而变 —— 那正是上一条判据在防的枚举 oracle 的来源。
    """
    did = _seed_diagnosis(OWN_BRAND)
    resp = client.get(f"/api/defensive-geo/reports/{did}/presentation", headers=_h(STRANGER))
    body = resp.json()
    assert isinstance(body.get("detail"), dict), f"拒绝返回了裸 detail:{resp.text[:200]}"
    detail = body["detail"]
    assert detail.get("code") == "NOT_FOUND", detail
    assert detail.get("publicExplanation"), "没有人话解释 —— 她会看到一片空白然后去猜"
    assert detail.get("nextAction", {}).get("kind"), "没有 typed nextAction"


def test_authorization_runs_before_any_data_read(client, monkeypatch):
    """🔴 授权必须在**取数之前**。

    先读后授权即使最终拒绝,数据也已经出了库;任何一次 except 漏网就会把它带出去。
    从外面看不出先后,所以这里在取数函数上装探针:
    他人请求 ⇒ 探针一次都不许被调用;本人请求 ⇒ 探针必须被调用
    (后半句是**反向对照**:没有它,把整个端点改成永远 404 也能让前半句全绿)。
    """
    import services.defensive_geo.presentation.report_binding as rb

    calls = []
    real = rb.resolve_binding
    monkeypatch.setattr(
        rb, "resolve_binding", lambda cur, did: (calls.append(did), real(cur, did))[1])

    did = _seed_diagnosis(OWN_BRAND)
    client.get(f"/api/defensive-geo/reports/{did}/presentation", headers=_h(STRANGER))
    assert calls == [], f"他人请求已经把数据读出来了(resolve_binding 被调用 {len(calls)} 次)"

    client.get(f"/api/defensive-geo/reports/{did}/presentation", headers=_h(OWNER))
    assert calls == [did], f"本人请求没走到取数 —— 探针失效,上一句断言等于没验:{calls}"


def test_this_file_never_patches_the_rbac():
    """自锁:本文件一旦 patch 掉 RBAC,归属判据就全是假的。"""
    src = Path(__file__).read_text(encoding="utf-8")
    body = src.split('"""', 2)[-1]          # 去掉模块 docstring(里面会提到这些词)
    for forbidden in ("require_brand_access", "require_diagnosis_access"):
        assert f'setattr(ba, "{forbidden}"' not in body and \
               f"monkeypatch.setattr(ba, '{forbidden}'" not in body, \
            f"本文件 patch 掉了 {forbidden} —— 归属判据会变成自证自话"


# ══════════════════════════════════════════════════════════════════════════
# ② 平台集去重 —— 价格与冻结额都按唯一键算
# ══════════════════════════════════════════════════════════════════════════
def _make_plan(client, brand_id=OWN_BRAND, tenant=OWNER):
    r = client.post(
        "/api/defensive-geo/question-plans/preview",
        json={"clientRequestId": "creq-" + uuid.uuid4().hex[:10], "brandId": brand_id,
              "profileRevisionId": "prof-1", "mode": "defensive",
              "questions": [{"text": "这个牌子靠谱吗", "modeSide": "defensive",
                             "familyKey": "identity_check", "brandExposure": "named"}]},
        headers=_h(tenant))
    assert r.status_code == 200, r.text
    return r.json()


def _make_preview(client, plan, platform_keys, *, idem=None, tenant=OWNER):
    return client.post(
        "/api/defensive-geo/run-previews",
        json={"questionPlanId": plan["planId"], "questionPlanRevision": 1,
              "profileRevisionId": "prof-1", "platformKeys": platform_keys},
        headers=_h(tenant, idem=idem or ("idem-" + uuid.uuid4().hex[:10])))


#: 与身份无关的易变字段,比较价格时剔除。
_VOLATILE = ("previewId", "expiresAt", "nextAction", "idempotentReplay")


def _pricing_face(body: dict) -> dict:
    return {k: v for k, v in body.items() if k not in _VOLATILE}


def test_duplicate_platform_keys_do_not_inflate_the_price(client):
    """🔴 重复键请求与去重后请求,价格面**逐字节同**。

    用**同一份题单**发两次,所以除了 previewId/expiresAt/nextAction,
    整个响应(含 plannedCells / basePoints / extraPoints / exactTotalPoints /
    costUserLabel / canonicalHash)必须一模一样。
    """
    plan = _make_plan(client)
    dup = _make_preview(client, plan, ["doubao", "doubao", "kimi", "doubao"])
    clean = _make_preview(client, plan, ["doubao", "kimi"])
    assert dup.status_code == 200 and clean.status_code == 200, (dup.text, clean.text)
    assert _pricing_face(dup.json()) == _pricing_face(clean.json()), (
        "重复平台键把价格算高了:\n"
        f"  重复: {json.dumps(_pricing_face(dup.json()), ensure_ascii=False)}\n"
        f"  去重: {json.dumps(_pricing_face(clean.json()), ensure_ascii=False)}")


def test_price_really_moves_with_the_platform_set(client):
    """🔴 区分力自证:平台集**真不同**时 plannedCells 必须不同;价格按题不按平台,**相等**。

    区分力只留在 plannedCells 严格小于上 —— 它才是「平台集真的进了计划」的证据。
    价格那一半翻成相等:Owner 2026-09-02 定「按题不按平台」(见
    tests/defgeo_flat_pricing_2026_09_02/test_question_based_pricing.py::
    test_platform_count_never_enters_pricing 及其后继锁)。本判据原先写「平台多就更贵」,
    09-02 改价后它就过期了;7 = conftest 种的 geo_diagnosis 基价(一题,不超免费额)。
    """
    plan = _make_plan(client)
    one = _make_preview(client, plan, ["doubao"]).json()
    two = _make_preview(client, plan, ["doubao", "kimi"]).json()
    assert one["plannedCells"] < two["plannedCells"], (one, two)
    assert one["exactTotalPoints"] == two["exactTotalPoints"], (
        f"1 个平台 {one['exactTotalPoints']} 与 2 个平台 {two['exactTotalPoints']} 不同价 —— "
        "平台数进了计价,违反 09-02「按题不按平台」")


def test_frozen_payload_stores_the_deduped_set(client):
    """冻结面里存的也必须是去重后的集合 —— 它是审计与执行的入参。"""
    plan = _make_plan(client)
    prev = _make_preview(client, plan, ["doubao", "doubao", "kimi"]).json()
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT frozen_payload, planned_cells FROM "
                        "defgeo_diagnosis_run_previews WHERE preview_id=%s", (prev["previewId"],))
            row = cur.fetchone()
    finally:
        conn.close()
    keys = row["frozen_payload"]["platformKeys"]
    assert keys == ["doubao", "kimi"], f"冻结面里的平台集没去重/没定序:{keys}"
    assert row["planned_cells"] == len(keys), (
        f"planned_cells={row['planned_cells']} 与唯一平台数 {len(keys)} 对不上")


def test_duplicate_keys_freeze_the_same_amount_as_deduped(client):
    """🔴 真冻结额:重复键与去重后**冻同样多的钱**。

    这一条走两次真 confirm、比 ``point_freezes.amount_total``。
    只比预览价是不够的 —— 本仓记过「预览 470、冻结 390」那种两处不同源的形态,
    钱最终按哪个数走必须自己去库里看。
    (两条 run 落在两个品牌上:``uq_diag_active_per_brand`` 不允许同品牌两条活跃。)

    ⚠️ 09-02 改为「按题不按平台」后,冻结额本来就与平台集无关 —— 这一条比金额相等的**牙**
       现在不来自价格,只来自 planned_cells / 冻结面里的平台集(test_frozen_payload_stores_the_deduped_set
       与本文件的 plannedCells 严格小于判据)。金额相等在这里是必要条件,不再单独有区分力。
    """
    plan_a = _make_plan(client, OWN_BRAND)
    plan_b = _make_plan(client, SECOND_BRAND)
    dup = _make_preview(client, plan_a, ["doubao", "doubao", "kimi"]).json()
    clean = _make_preview(client, plan_b, ["doubao", "kimi"]).json()

    amounts = {}
    for label, prev in (("dup", dup), ("clean", clean)):
        r = client.post(
            f"/api/defensive-geo/run-previews/{prev['previewId']}/confirm",
            json={"expectedHash": prev["canonicalHash"]},
            headers=_h(OWNER, idem="rw2-" + uuid.uuid4().hex[:8]))
        assert r.status_code == 200, f"{label} confirm 失败:{r.status_code} {r.text[:300]}"
        run_token = r.json()["diagnosisCommandId"]
        conn = _conn()
        try:
            with conn.cursor() as cur:
                # [A-1 · 2026-08-25] task_ref 从 "defgeo_"+token 收口成 run 行自己的
                #   freeze_task_ref("diag_"+token)—— 两个串曾经不相等,凡是按三元组
                #   (id+user+task_ref)定位冻结的地方全查空。判据这里也**不再手拼**,
                #   直接调那唯一的产出源,免得两边再各拼一遍。
                from services.diagnosis_runs import freeze_task_ref as _ftr
                cur.execute("SELECT amount_total FROM point_freezes WHERE task_ref=%s",
                            (_ftr(run_token),))
                rows = cur.fetchall()
        finally:
            conn.close()
        assert len(rows) == 1, f"{label} 落了 {len(rows)} 条冻结,要求恰一条"
        amounts[label] = int(rows[0]["amount_total"])

    assert amounts["dup"] > 0, "冻结额是 0 —— 这条判据没有判别力(0 既像对也像错)"
    assert amounts["dup"] == amounts["clean"], (
        f"重复平台键多冻了钱:重复 {amounts['dup']} vs 去重 {amounts['clean']}")


def test_live_frontend_platform_set_is_byte_for_byte_unaffected(client):
    """🔴 兼容自证:线上前端真发的那一组平台键,行为与改动前**逐字节相同**。

    平台集不是手抄的,是从前端源码里现读的 —— 手抄的那一份不会跟着前端变,
    「兼容」就成了对一个想象中的入参的兼容。
    改动前的算法是 ``题数 × len(原始列表)``;前端那组无重复,所以规范化后
    长度不变、价格不变。这条把它算出来钉住。
    """
    # [返修③ 起] 前端那份清单已从 .tsx 里的字面量搬进
    # frontend/src/lib/defensiveGeoEngines.ts(P0-2:三处不许各写一份)。
    # 这里跟着搬,仍然是**现读**不手抄 —— 手抄的那一份不会跟着前端变,
    # 「兼容」就成了对一个想象中的入参的兼容。
    from tests.defensive_geo_2026_08_21.test_rework3_engine_ssot_pg import (
        frontend_selectable_keys,
    )

    live_keys = frontend_selectable_keys()
    assert len(live_keys) == len(set(live_keys)), (
        f"前端现在真的会发重复平台键 {live_keys} —— 那是另一回事,去看后端有没有多算")

    plan = _make_plan(client)
    body = _make_preview(client, plan, live_keys).json()
    assert body["plannedCells"] == plan["counts"]["total"] * len(live_keys), (
        f"线上入参的计划格数变了:{body['plannedCells']} != "
        f"{plan['counts']['total']} × {len(live_keys)} —— 既有行为回归了")


# ══════════════════════════════════════════════════════════════════════════
# ③ 幂等 hash 覆盖平台集
# ══════════════════════════════════════════════════════════════════════════
def test_same_idem_key_different_platform_sets_do_not_override_each_other(client):
    """🔴 同幂等键 + 不同平台集 ⇒ 各自成单,谁也不顶谁。

    修之前:第二次请求命中 ``(tenant, idem_key, hash)`` 旧行,端点把**第一次**
    那份 preview 当幂等重放返回 —— 她选了两个平台,拿到的是一个平台的报价,
    而 ``idempotentReplay`` 还写着 true,前端没有任何理由怀疑。
    """
    plan = _make_plan(client)
    idem = "same-key-" + uuid.uuid4().hex[:8]
    first = _make_preview(client, plan, ["doubao"], idem=idem).json()
    second = _make_preview(client, plan, ["doubao", "kimi"], idem=idem).json()

    assert first["previewId"] != second["previewId"], (
        "换了平台集却拿回同一份 preview —— 第二次请求被第一次顶掉了")
    assert first["idempotentReplay"] is False and second["idempotentReplay"] is False, (
        first["idempotentReplay"], second["idempotentReplay"])
    assert second["plannedCells"] > first["plannedCells"], (
        f"第二份 preview 的格数 {second['plannedCells']} 没随平台集涨 —— "
        "它拿的可能还是第一份的冻结面")

    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT preview_id, canonical_hash, canonical_request_hash FROM "
                "defgeo_diagnosis_run_previews WHERE tenant_owner_user_id=%s AND idempotency_key=%s "
                "ORDER BY preview_id", (OWNER, idem))
            rows = cur.fetchall()
    finally:
        conn.close()
    assert len(rows) == 2, f"同幂等键下应有两行(平台集不同),实得 {len(rows)}"
    assert rows[0]["canonical_request_hash"] != rows[1]["canonical_request_hash"], \
        "两行的幂等 hash 相同 —— 平台集没进 hash"
    assert rows[0]["canonical_hash"] == rows[1]["canonical_hash"], (
        "题单身份 hash 被平台集污染了 —— 同一份题单在两个阶段会算出两个身份")


def test_same_idem_key_same_platform_set_still_replays(client):
    """🔴 兼容自证:平台集**相同**(哪怕换序、带重复)时,幂等重放照旧。

    这是上一条的另一半。只加「不同 ⇒ 分开」而不验「相同 ⇒ 仍合并」,
    很可能把幂等整个做没了(每次都新建),那是更贵的缺陷:重复扣费。
    """
    plan = _make_plan(client)
    idem = "replay-" + uuid.uuid4().hex[:8]
    first = _make_preview(client, plan, ["doubao", "kimi"], idem=idem).json()
    again = _make_preview(client, plan, ["doubao", "kimi", "doubao"], idem=idem).json()

    assert again["previewId"] == first["previewId"], (
        "同一把幂等键 + 同一个平台集(换序/带重复)却建出了第二份 preview")
    assert again["idempotentReplay"] is True, "没有如实标记这是重放"


def test_idempotency_hash_is_exactly_plan_hash_plus_platform_set(client):
    """幂等 hash 的构成可复算:``run_request_hash(题单 hash, 规范化平台集)``。

    这条同时是**兼容自证**:``canonical_hash`` 那一列(以及响应里的
    ``canonicalHash``)仍然逐字节是题单本体身份,平台集只进 ``canonical_request_hash``。
    两者一旦被合成一个,这里立刻红。
    """
    from services.defensive_geo.question_plan import run_request_hash

    plan = _make_plan(client)
    sent = ["doubao", "doubao", "kimi"]
    prev = _make_preview(client, plan, sent).json()

    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT canonical_hash, canonical_request_hash FROM "
                        "defgeo_diagnosis_run_previews WHERE preview_id=%s", (prev["previewId"],))
            row = cur.fetchone()
    finally:
        conn.close()

    assert prev["canonicalHash"] == row["canonical_hash"], "响应里的 canonicalHash 不是题单身份"
    assert row["canonical_request_hash"] == run_request_hash(
        plan_content_hash=row["canonical_hash"], platform_keys=sent), \
        "幂等 hash 不等于 run_request_hash(题单 hash, 平台集) —— 构成变了"
    assert row["canonical_request_hash"] != row["canonical_hash"], \
        "幂等 hash 与题单 hash 相同 —— 平台集没进去"


def test_confirm_still_accepts_the_response_canonical_hash(client):
    """兼容自证:confirm 的 ``expectedHash`` 仍然吃响应里那个 ``canonicalHash``。

    我改的是**另一列**。如果哪天有人图省事把两个 hash 合成一个,
    前端拿到的 canonicalHash 与 confirm 校验的那个就会分叉 ——
    表现是「点确认永远说快照变了」,而两边各自都算得对。
    """
    plan = _make_plan(client)
    prev = _make_preview(client, plan, ["doubao", "kimi"]).json()
    r = client.post(
        f"/api/defensive-geo/run-previews/{prev['previewId']}/confirm",
        json={"expectedHash": prev["canonicalHash"]},
        headers=_h(OWNER, idem="compat-" + uuid.uuid4().hex[:8]))
    assert r.status_code == 200, f"用响应里的 canonicalHash 确认失败了:{r.status_code} {r.text[:300]}"


def test_normalization_is_a_single_predicate():
    """结构锚:去重只此一处。

    ``planned_cells``、报价、冻结额、幂等 hash 全部从 ``body.platform_keys`` 派生。
    任一处自己再 ``set()`` 一遍,就会把「规范化被摘掉」这件事遮住 ——
    两把锁叠在同一条路径上时,变异存活但判据全绿(本仓 2026-08 实证过两次)。
    """
    import inspect

    import api.defensive_geo_api as mod

    src = inspect.getsource(mod)
    assert "normalize_platform_keys(value)" in src, "DTO 里的规范化调用不见了"
    assert "set(body.platform_keys)" not in src, (
        "端点里又去重了一遍 —— 会遮住 DTO 规范化被摘掉这件事")


def test_distinct_platform_cap_counts_distinct_not_submitted(client):
    """上限 12 限的是「几个不同平台」,不是「提交了几个条目」。

    这条钉住规范化的**位置**:``min_length/max_length`` 是核心 schema 约束,
    ``mode="after"`` 的校验器跑在它们之后 —— 那时 13 个重复条目会先把上限占满,
    她提交 ``["doubao"]*12 + ["doubao"]`` 只会拿到一个 422,而她真正要的是两个平台。
    放在 ``mode="before"``,上限才是它该有的意思。
    """
    plan = _make_plan(client)
    resp = _make_preview(client, plan, ["doubao"] * 12 + ["kimi"])
    assert resp.status_code == 200, (
        f"13 个条目(去重后 2 个平台)被拒了:{resp.status_code} {resp.text[:200]} —— "
        "上限算的是提交条目数,不是不同平台数")
    assert resp.json()["plannedCells"] == plan["counts"]["total"] * 2, resp.json()


def test_run_request_hash_really_varies_with_the_platform_set():
    """🔴 这条是被变异逼出来的。

    上面那条「幂等 hash = run_request_hash(题单 hash, 平台集)」自己**调**了
    ``run_request_hash`` 去算期望值 —— 于是把平台集从那个函数的 payload 里删掉,
    等号两边一起变,判据照绿(变异 M9 当场存活)。
    它锁的其实是**端点接线**(M8 退回旧值时它确实红了),锁不住函数内部。
    所以在函数这一层单独打一条:纯输入输出,不借被测函数造期望值。
    """
    from services.defensive_geo.question_plan import run_request_hash as h

    assert h(plan_content_hash="p", platform_keys=["a"]) != \
        h(plan_content_hash="p", platform_keys=["a", "b"]), "换了平台集 hash 不变"
    assert h(plan_content_hash="p", platform_keys=["b", "a"]) == \
        h(plan_content_hash="p", platform_keys=["a", "a", "b"]), "同一个集合换序/带重复应同 hash"
    assert h(plan_content_hash="p", platform_keys=["a"]) != \
        h(plan_content_hash="q", platform_keys=["a"]), "换了题单 hash 不变"


def test_run_request_hash_golden_vector():
    """固定向量:hash 构成一旦漂移,在途 preview 的幂等身份会**集体失效**。

    「同一把幂等键第二次请求」会突然不再命中旧行,于是重复建单 —— 而两个 hash
    各自都算得对,没有任何别的判据会红。
    改构成是允许的,但必须**同时**升 ``RUN_REQUEST_HASH_VERSION`` 并更新这里的向量;
    这条判据的作用就是逼那一步发生,而不是让它悄悄发生。
    """
    from services.defensive_geo.question_plan import RUN_REQUEST_HASH_VERSION, run_request_hash

    assert RUN_REQUEST_HASH_VERSION == "defgeo-run-request-v1"
    assert run_request_hash(plan_content_hash="plan-abc", platform_keys=["doubao", "kimi"]) == \
        "368827922d785112b788a944f8c8c7c31c597b7a0e6ee91045d7ed087c8c04e5"
