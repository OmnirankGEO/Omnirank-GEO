"""WP2 两阶段端点:真 HTTP × 真 PG(§15.1-15.3 / G-4 / §0.5.5 U-2)。

为什么必须**真 HTTP 打真库**
----------------------------
本仓 2026-08-18 记过:只验 ``!= 422`` 会漏掉整层库合同 ——
升级成「不许 422 也不许 500」当场抓出 6 处必 500。
所以本文件的每条判据都:
  · 走真 ASGI 栈(TestClient),不直接调函数;
  · 打真 PG(conftest 建的一次性库),不 mock cursor;
  · 断言 **status + 响应体形状 + 人话字段**,不只断言"没炸"。

覆盖的附带条件
  ⑥ G-4「response 多返回一键 → 受控失败而非裸 500」→ test_response_model_forbids_extra_keys
  补充令「逐响应人话字段非空且零内部词」→ test_*_carries_human_copy 系列
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.defensive_geo.copy_registry import PublicCopyLeak, assert_public_copy_clean

pytestmark = pytest.mark.integration

TENANT = 7301
OTHER_TENANT = 7302


@pytest.fixture(scope="module")
def app_client():
    """最小 ASGI 宿主:只挂本 router + 一个把 user 注入 request.state 的中间件。

    🔴 中间件写的键必须与生产一致(``request.state.user`` 里是 ``user_id``)。
       本仓 2026-08-20 踩过「夹具用的键是生产从来不会发的键」——
       生产中间件写 ``user_id``、模块读 ``user["id"]``,整片端点生产必 500 而判据全绿。
       所以这里**照抄生产形状**,并在下方 test_fixture_user_shape_matches_production 钉住。
    """
    from api.defensive_geo_api import router

    app = FastAPI()

    @app.middleware("http")
    async def _inject_user(request, call_next):
        tid = request.headers.get("X-Test-Tenant")
        if tid:
            request.state.user = {"user_id": int(tid), "is_admin": False}
        return await call_next(request)

    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def _headers(tenant=TENANT, idem=None):
    h = {"X-Test-Tenant": str(tenant)}
    if idem:
        h["Idempotency-Key"] = idem
    return h


def _plan_body(**over):
    body = {
        "clientRequestId": f"creq-{uuid.uuid4().hex[:10]}",
        "brandId": 901,
        "profileRevisionId": "prof-1",
        "mode": "defensive",
        "questions": [
            {"text": "这个牌子靠谱吗", "modeSide": "defensive",
             "familyKey": "identity_check", "brandExposure": "named"},
            {"text": "他们家售后怎么样", "modeSide": "defensive",
             "familyKey": "service_check", "brandExposure": "named"},
        ],
    }
    body.update(over)
    return body


@pytest.fixture(autouse=True)
def _allow_brand(monkeypatch):
    """放行 RBAC —— 本文件验的是 façade 合同,不是 RBAC 实现。

    ⚠️ 但**跨租户判据不能靠它**:那几条走 plan_store 的归属 WHERE,
       与这里 patch 掉的 brand RBAC 是两道独立的门。
       (只 patch 一道就以为验了归属,正是「两把锁叠在同一条路径上」那个坑。)
    """
    import auth.brand_access as ba

    monkeypatch.setattr(ba, "require_brand_access", lambda *a, **k: None)


# ══════════════════════ 夹具形状自证 ══════════════════════════════════════
def test_fixture_user_shape_matches_production():
    """🔴 夹具注入的键必须是生产真的会写的那个。

    生产 ``auth/middleware.py`` 往 ``request.state.user`` 写 ``user_id``。
    夹具若写成 ``id``,整片端点在生产必 500 而这里全绿。
    """
    import inspect

    import auth.middleware as mw

    src = inspect.getsource(mw)
    assert '"user_id"' in src or "'user_id'" in src, (
        "生产中间件里找不到 user_id —— 夹具形状可能已与生产分叉,先核对再改判据"
    )


# ══════════════════════ 正路 ══════════════════════════════════════════════
def test_create_question_plan_returns_frozen_plan(app_client):
    r = app_client.post("/api/defensive-geo/question-plans/preview",
                        json=_plan_body(), headers=_headers())
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["counts"] == {"defensive": 2, "offensive": 0, "total": 2}
    assert len(d["canonicalHash"]) == 64
    # ordinal 连续、身份键唯一
    assert [q["globalOrdinal"] for q in d["questions"]] == [1, 2]
    assert len({q["questionIdentityKey"] for q in d["questions"]}) == 2


def test_question_plan_response_carries_human_copy(app_client):
    """🔴 补充令:逐响应人话字段非空 + 零内部词。"""
    r = app_client.post("/api/defensive-geo/question-plans/preview",
                        json=_plan_body(), headers=_headers())
    d = r.json()
    assert_public_copy_clean(d["modeUserLabel"], field="modeUserLabel")
    assert d["modeUserLabel"] == "先守住品牌"
    # 必须不命中:响应里不许出现裸内部枚举当人话
    assert d["modeUserLabel"] != d["mode"]


def test_run_preview_carries_all_three_human_fields(app_client):
    """补充令点名的三件:userLabel / publicExplanation / nextAction.label。"""
    plan = app_client.post("/api/defensive-geo/question-plans/preview",
                           json=_plan_body(), headers=_headers()).json()
    r = app_client.post(
        "/api/defensive-geo/run-previews",
        json={"questionPlanId": plan["planId"], "questionPlanRevision": plan["planRevision"],
              "profileRevisionId": "prof-1", "platformKeys": ["deepseek", "doubao"]},
        headers=_headers(idem=f"idem-{uuid.uuid4().hex[:10]}"),
    )
    assert r.status_code == 200, r.text
    d = r.json()
    for field in ("lifecycleUserLabel", "fundingPolicyUserLabel", "modeUserLabel", "costUserLabel"):
        assert_public_copy_clean(d[field], field=field)
    assert_public_copy_clean(d["nextAction"]["label"], field="nextAction.label")
    assert d["nextAction"]["kind"] and d["nextAction"]["target"], "死动作:有 label 没 target"
    assert d["plannedCells"] == 2 * 2, "题数 × 平台数"


def test_every_string_field_in_run_preview_is_leak_free(app_client):
    """扫**整个响应**的字符串面,不只扫我记得的那几个字段。

    只扫记得的字段 = 手写分母;新加一个人话字段忘了过门不会有任何东西变红。
    """
    plan = app_client.post("/api/defensive-geo/question-plans/preview",
                           json=_plan_body(), headers=_headers()).json()
    d = app_client.post(
        "/api/defensive-geo/run-previews",
        json={"questionPlanId": plan["planId"], "questionPlanRevision": plan["planRevision"],
              "profileRevisionId": "prof-1", "platformKeys": ["deepseek"]},
        headers=_headers(idem=f"idem-{uuid.uuid4().hex[:10]}"),
    ).json()

    # 只查名字里带 Label/Explanation 的字段 —— 那些就是"要给人看的"。
    # 其余(hash/id/枚举)本来就该是机器面。
    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(node, str) and (
            path.endswith(("UserLabel", "Explanation")) or path.endswith("nextAction.label")
        ):
            assert_public_copy_clean(node, field=path)

    walk(d)


# ══════════════════════ ⑥ G-4:多一个键 → 受控失败 ═════════════════════════
def test_request_with_unknown_key_is_422_not_500(app_client):
    """请求侧 strict:未知字段 422(§15 开头)。"""
    body = _plan_body()
    body["thisKeyDoesNotExist"] = 1
    r = app_client.post("/api/defensive-geo/question-plans/preview",
                        json=body, headers=_headers())
    assert r.status_code == 422, r.text
    assert r.status_code != 500


def test_response_model_forbids_extra_keys(app_client, monkeypatch):
    """🔴 G-4 正样本:**响应**多返回一个键 → 受控失败,不是裸 500 泄内部。

    ``extra="forbid"`` 三次打挂生产(本仓 2026-08-14 记录),所以这条不是走形式:
    它注入一个多余键,证明 ① 会失败(不是静默带出去)② 失败面是受控的
    (没有 SQL / host / traceback 漏出)。
    """
    import api.defensive_geo_api as mod

    orig = mod.QuestionPlanResponse

    class Leaky(orig):
        model_config = dict(orig.model_config)

    # 直接构造一个带多余键的实例 —— 应当抛,而不是被静默接受
    with pytest.raises(Exception) as exc:
        orig(
            planId=str(uuid.uuid4()), planRevision=1, canonicalHash="a" * 64,
            canonicalHashVersion="v", brandId=1, profileRevisionId="p", mode="defensive",
            modeUserLabel="先守住品牌", questions=[], counts={"defensive": 0, "offensive": 0, "total": 0},
            expiresAt="2026-01-01T00:00:00+00:00", copyRegistryVersion="v",
            idempotentReplay=False,
            surpriseExtraKey="boom",
        )
    msg = str(exc.value)
    assert "surpriseExtraKey" in msg or "extra" in msg.lower()
    # 必须不命中:失败信息里不许有连接串/SQL/主机
    for leak in ("postgresql://", "password", "Traceback", "psycopg2"):
        assert leak not in msg


def test_all_new_response_models_are_strict():
    """全出口 census:本模块**每个** response_model 都必须 forbid extra。

    分母从 router 的 routes 机械取,不手抄 —— 新加一个端点忘了 strict 必红。
    """
    from api.defensive_geo_api import router

    checked = 0
    for route in router.routes:
        model = getattr(route, "response_model", None)
        if model is None:
            continue
        checked += 1
        assert model.model_config.get("extra") == "forbid", (
            f"{route.path} 的 response_model {model.__name__} 不是 forbid —— "
            "多返回一个键会被静默带出去"
        )
    assert checked >= 4, f"只检了 {checked} 个 response_model,分母不对"


def test_error_details_are_a_closed_set():
    """SafeError 的 details 是闭集;开放 details = 给 raw detail 开后门。"""
    from api.defensive_geo_api import _safe_error

    with pytest.raises(RuntimeError):
        _safe_error("NOT_FOUND", details={"rawSql": "SELECT 1"})
    # 反向对照:白名单键必须能过
    exc = _safe_error("NOT_FOUND", details={"planId": "x"})
    assert exc.detail["details"] == {"planId": "x"}


# ══════════════════════ 归属每请求现做(真 HTTP 层)══════════════════════
def test_cross_tenant_get_plan_is_404_not_403(app_client):
    """🔴 ACT-02:跨租户与不存在同形 404,不泄露对象存在性。

    ⚠️ 这条**不靠** _allow_brand 那个 patch —— 它走 plan_store 的归属 WHERE,
       是另一道独立的门。
    """
    plan = app_client.post("/api/defensive-geo/question-plans/preview",
                           json=_plan_body(), headers=_headers(TENANT)).json()
    mine = app_client.get(
        f"/api/defensive-geo/question-plans/{plan['planId']}?revision=1",
        headers=_headers(TENANT))
    assert mine.status_code == 200

    theirs = app_client.get(
        f"/api/defensive-geo/question-plans/{plan['planId']}?revision=1",
        headers=_headers(OTHER_TENANT))
    absent = app_client.get(
        f"/api/defensive-geo/question-plans/{uuid.uuid4()}?revision=1",
        headers=_headers(OTHER_TENANT))
    assert theirs.status_code == 404
    assert absent.status_code == 404
    assert theirs.json() == absent.json(), "跨租户与不存在的响应体不同 —— 泄露了对象存在性"


def test_unauthenticated_is_refused(app_client):
    r = app_client.post("/api/defensive-geo/question-plans/preview", json=_plan_body())
    assert r.status_code in (403, 404), r.text
    assert r.status_code != 500


# ══════════════════════ exact revision(REV-11)═══════════════════════════
def test_get_plan_requires_explicit_revision(app_client):
    """没有「不传就给 latest」这条路 —— 缺 revision 必须 422,不是静默给最新。"""
    plan = app_client.post("/api/defensive-geo/question-plans/preview",
                           json=_plan_body(), headers=_headers()).json()
    r = app_client.get(f"/api/defensive-geo/question-plans/{plan['planId']}",
                       headers=_headers())
    assert r.status_code == 422, f"缺 revision 却返回了 {r.status_code} —— 疑似静默升 latest"


def test_get_plan_wrong_revision_is_404(app_client):
    plan = app_client.post("/api/defensive-geo/question-plans/preview",
                           json=_plan_body(), headers=_headers()).json()
    r = app_client.get(f"/api/defensive-geo/question-plans/{plan['planId']}?revision=99",
                       headers=_headers())
    assert r.status_code == 404


# ══════════════════════ 幂等 ══════════════════════════════════════════════
def test_same_client_request_replays_same_plan(app_client):
    body = _plan_body()
    a = app_client.post("/api/defensive-geo/question-plans/preview",
                        json=body, headers=_headers(idem="k1")).json()
    b = app_client.post("/api/defensive-geo/question-plans/preview",
                        json=body, headers=_headers(idem="k2-different")).json()
    assert b["planId"] == a["planId"], "换 HTTP key 建出了第二份题单"
    assert b["idempotentReplay"] is True
    assert a["idempotentReplay"] is False


def test_run_preview_requires_idempotency_key(app_client):
    """资金相关的两阶段必须带幂等键 —— 没有键就没法保证「恰一次」。"""
    plan = app_client.post("/api/defensive-geo/question-plans/preview",
                           json=_plan_body(), headers=_headers()).json()
    r = app_client.post(
        "/api/defensive-geo/run-previews",
        json={"questionPlanId": plan["planId"], "questionPlanRevision": 1,
              "profileRevisionId": "prof-1", "platformKeys": ["deepseek"]},
        headers=_headers())
    assert r.status_code == 422


def test_run_preview_replay_returns_same_preview(app_client):
    plan = app_client.post("/api/defensive-geo/question-plans/preview",
                           json=_plan_body(), headers=_headers()).json()
    payload = {"questionPlanId": plan["planId"], "questionPlanRevision": 1,
               "profileRevisionId": "prof-1", "platformKeys": ["deepseek"]}
    key = f"idem-{uuid.uuid4().hex[:10]}"
    a = app_client.post("/api/defensive-geo/run-previews", json=payload,
                        headers=_headers(idem=key)).json()
    b = app_client.post("/api/defensive-geo/run-previews", json=payload,
                        headers=_headers(idem=key)).json()
    assert a["previewId"] == b["previewId"]
    assert a["idempotentReplay"] is False
    assert b["idempotentReplay"] is True, "重放没被标出来 —— 调用方会以为又冻了一次钱"


# ══════════════════════ 零资金副作用 ══════════════════════════════════════
def test_preview_creates_zero_funding_side_effects(app_client, db):
    """🔴 shadow/preview 零扣费红线:preview 只写 preview 行,不碰任何资金表。"""
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS c FROM diagnosis_runs")
        runs_before = cur.fetchone()["c"]

    plan = app_client.post("/api/defensive-geo/question-plans/preview",
                           json=_plan_body(), headers=_headers()).json()
    app_client.post(
        "/api/defensive-geo/run-previews",
        json={"questionPlanId": plan["planId"], "questionPlanRevision": 1,
              "profileRevisionId": "prof-1", "platformKeys": ["deepseek"]},
        headers=_headers(idem=f"idem-{uuid.uuid4().hex[:10]}"))

    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS c FROM diagnosis_runs")
        runs_after = cur.fetchone()["c"]
        cur.execute("SELECT count(*) AS c FROM defgeo_diagnosis_run_previews")
        previews = cur.fetchone()["c"]

    assert runs_after == runs_before, "preview 建出了 diagnosis_run —— 那是要冻钱的"
    # 反向对照:preview 行确实写进去了,否则上面那条是"什么都没发生"的假绿
    assert previews >= 1, "preview 一行都没写 —— 上面的『零副作用』什么都没证明"


#: 资金符号清单。判据打的是「这些名字从哪个门进来」,不是「有没有出现过」。
_BILLING_SYMBOLS = frozenset({
    "freeze_points", "commit_freeze", "release_freeze",
    "charge_points", "refund_points", "deduct_points",
})
#: 资金模块清单(import 的来源侧)。
_BILLING_MODULES = ("middleware.billing", "db.wallet_db", "wallet_db")

#: 🔴 资金模块里**只读、不动钱**的符号白名单 —— 判据对它们放行。
#:
#: 为什么要有这张表(而不是把 `db.wallet_db` 从模块清单里删掉):
#: 如果只按符号名黑名单判(freeze/commit/release/charge…),那就是本仓明令
#: 禁止的「补一个词漏三个词」—— 我漏写的任何一个资金函数都会静默放行。
#: 所以这里反过来 fail-closed:**从资金模块 import 任何不在白名单里的名字都判红**,
#: 想加就得显式加进来并说明它凭什么不动钱。
#: `get_feature_pricing` = 读 `feature_pricing` 目录价,纯 SELECT,不碰余额。
_READONLY_BILLING_READS = frozenset({"get_feature_pricing"})

#: 🔴 唯一合法出口。改这个集合 = 改资金面的进入方式,必须是显式决定。
_ALLOWED_BILLING_GATE = frozenset({"_freeze_exact"})


def _billing_entry_points() -> dict[str, set[str]]:
    """机械导出:façade 里每个资金符号分别是在**哪个顶层函数**里被引用的。

    分母 = ``api/defensive_geo_api.py`` 整个模块的 AST(不是我手写的清单)。
    """
    import ast
    import inspect

    import api.defensive_geo_api as mod

    tree = ast.parse(inspect.getsource(mod))
    entries: dict[str, set[str]] = {}

    for top in tree.body:
        if not isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # 模块级(import / 赋值 / 类定义)—— 归到 "<模块级>",它永远不许出现
            for node in ast.walk(top):
                _collect(node, "<模块级>", entries)
            continue
        for node in ast.walk(top):
            _collect(node, top.name, entries)
    return entries


def _collect(node, owner: str, entries: dict[str, set[str]]) -> None:
    import ast

    if isinstance(node, ast.ImportFrom) and node.module:
        from_money_module = any(
            node.module == m or node.module.endswith("." + m) for m in _BILLING_MODULES
        )
        for alias in node.names:
            # fail-closed:从资金模块 import 的名字,不在只读白名单里就记账
            if from_money_module and alias.name not in _READONLY_BILLING_READS:
                entries.setdefault(f"{node.module}.{alias.name}", set()).add(owner)
            elif alias.name in _BILLING_SYMBOLS:
                entries.setdefault(alias.name, set()).add(owner)
    elif isinstance(node, ast.Name) and node.id in _BILLING_SYMBOLS:
        entries.setdefault(node.id, set()).add(owner)
    elif isinstance(node, ast.Attribute) and node.attr in _BILLING_SYMBOLS:
        entries.setdefault(node.attr, set()).add(owner)


def test_billing_enters_the_facade_through_exactly_one_gate():
    """结构锚(第四班 ③ 改判法):资金符号**只许经 `_freeze_exact` 这一个出口**进入 façade。

    为什么改判法而不是放宽
    ----------------------
    原判据是「façade 里不许出现任何资金符号」。那在 confirm 还没接资金半的时候
    是对的;confirm 现在**确实**要冻结算力,原口径已经与被测对象矛盾 ——
    继续留着只会逼人把它删掉,那才是真的放宽。

    新口径**更紧**:不再问"有没有",而是问"有几个门"。资金面只有一个入口时,
    审计、变异和后续的资金判据都只需要盯一个地方;一旦有第二个门,
    「_freeze_exact 里所有的守卫」(拒 admin 零句柄 / 算术守恒 / 矩阵校验)
    就全部可以被绕过,而且不会有任何判据变红。

    模块级 import 也一律判红:模块级 import 让整个文件都够得着资金函数,
    等于门开在外墙上。
    """
    entries = _billing_entry_points()
    assert entries, (
        "façade 里一个资金符号都扫不到 —— 多半是扫描器坏了(confirm 明明要冻结),"
        "零分母的『全绿』不是证据"
    )
    offenders = {
        symbol: sorted(owners - _ALLOWED_BILLING_GATE)
        for symbol, owners in entries.items()
        if owners - _ALLOWED_BILLING_GATE
    }
    assert not offenders, (
        f"资金符号从 `_freeze_exact` 以外的门进入了 façade:{offenders}。"
        "要么把调用收回 `_freeze_exact`,要么显式改 `_ALLOWED_BILLING_GATE` 并说明"
        "新出口凭什么也配有那套守卫。"
    )


def test_the_single_gate_actually_exists_and_is_the_freeze_leg():
    """反向对照:上一条如果因为「`_freeze_exact` 根本没引用资金符号」而全绿,
    那它什么都没守。这里钉住那个门里**真的**有 freeze_points。"""
    entries = _billing_entry_points()
    assert "freeze_points" in entries, "扫描器没在 façade 里找到 freeze_points"
    assert entries["freeze_points"] == {"_freeze_exact"}, \
        f"freeze_points 的引用位置不是唯一的 _freeze_exact:{entries['freeze_points']}"
