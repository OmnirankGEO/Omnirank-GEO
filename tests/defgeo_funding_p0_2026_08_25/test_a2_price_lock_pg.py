"""【A-2 = Codex P0-2】确认价必须锁得住实际冻结额。

修之前的链:
  · ``_live_pricing_catalog_version()`` 恒返常量 ``"defgeo-pricing-shadow-v1"``
    ⇒ confirm 里那句版本比对**永不触发**;
  · ``freeze_points`` 读**现价**(billing.py:1364 ``total_cost = pricing["cost_points"] + extra_cost``),
    而 confirm 只把 preview 里的**旧 extra** 带过去 —— base 是现价;
  · confirm 只验「有没有 freeze_id」,**不比金额**。
  ⇒ 可达:确认 7,800、实冻 8,200、响应仍显示 7,800。

两把保险丝,**分别单独证**:
  ① 版本比对(事前)—— 改价必然换版本 ⇒ 409 SNAPSHOT_CHANGED,零副作用;
  ② 金额比对(事后)—— 物理冻结额 ≠ 确认额 ⇒ 整笔回滚。
  🔴 证②的时候必须**先把①关掉**,否则请求在①就返回了,②等于没被执行过
     —— 那正是本仓「两把锁叠同一路径:存活时别以为是自己那把在守」记过的坑。
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from tests.defgeo_funding_p0_2026_08_25 import _world as W

pytestmark = pytest.mark.integration

COST = 650
COST_RAISED = 820


@pytest.fixture()
def world(db, migrated_dsn, monkeypatch):
    tenant, _a, _b = W.fresh_uids(3)
    with db.cursor() as cur:
        W.ensure_user(cur, tenant, "p0fix_payer_%d" % tenant)
        W.ensure_wallet(cur, tenant, paid=1_000_000)
        W.set_pricing(cur, COST)
        brand_id = W.new_brand(cur, tenant)
    monkeypatch.setattr("auth.brand_access.require_brand_access", lambda *a, **k: None)
    yield {"tenant": tenant, "brand_id": brand_id}
    with db.cursor() as cur:            # 价目是全局单行,跑完必须还原,免得污染别的判据
        W.set_pricing(cur, COST)


@pytest.fixture()
def client():
    return TestClient(W.make_app(), raise_server_exceptions=False)


def _raise_price(db, to=COST_RAISED):
    with db.cursor() as cur:
        W.set_pricing(cur, to)


# ═══════════════════════════════════════════════════════════════════════════
# 版本串本身:由内容派生,不是常量
# ═══════════════════════════════════════════════════════════════════════════
def test_catalog_version_moves_when_the_real_price_moves(world, db, live_server):
    """改一次真价 ⇒ 版本串必须变;改回去 ⇒ 必须变回来。

    第二半(改回去要变回来)是**配对的必须不命中**:少了它,一个"每次调用都返
    随机串"的实现也能让上半条绿,而那样会让所有 confirm 恒 409。
    """
    _ = live_server
    from api.defensive_geo_api import _live_pricing_catalog_version as ver

    before = ver("geo_diagnosis")
    _raise_price(db)
    after = ver("geo_diagnosis")
    assert before != after, (
        "改价之后版本串没变(%r)—— 那 confirm 的版本比对就永远不会触发,"
        "这正是 Codex P0-2 的第一段" % before)
    _raise_price(db, COST)
    assert ver("geo_diagnosis") == before, (
        "价格改回原值,版本却没回到 %r —— 版本不是内容的函数(可能是随机/时间),"
        "那样每一次 confirm 都会被误判成改过价" % before)


def test_catalog_version_is_not_a_constant(live_server):
    """结构面的配对判据:实现里不许再出现那个恒定串。

    值层面的判据(上一条)只能证"这次调用变了";这一条钉住"常量实现已经不在了"。

    🔴 谓词走 **AST 只认代码不认字符串**:第一版写的是
       ``"defgeo-pricing-shadow-v1" not in inspect.getsource(...)``,
       而我在 docstring 里**引用了**那个旧常量说明它错在哪 —— 判据当场判红,
       红因与被测行为零关系(本仓 2026-08-25 记过「引用裁决原文会让裸串结构锚判红」)。
       改成只看**字符串字面量节点**,注释与文档说什么都不影响。
    """
    _ = live_server
    import ast
    import inspect
    import textwrap

    import api.defensive_geo_api as mod

    tree = ast.parse(textwrap.dedent(inspect.getsource(mod._live_pricing_catalog_version)))
    fn = tree.body[0]
    literals = [n.value for n in ast.walk(fn)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    # docstring 是 body[0] 的第一条表达式 —— 它是文档不是逻辑,排除掉。
    doc = ast.get_docstring(fn)
    literals = [s for s in literals if s != doc]
    assert "defgeo-pricing-shadow-v1" not in literals, (
        "常量版本又回来了(它作为**代码里的字面量**出现):%r" % (literals,))
    assert any("feature_pricing" in s for s in literals), (
        "函数里没有一条读 feature_pricing 的语句 —— 版本不再由真实价目派生:%r" % (literals,))


def test_unreadable_catalog_is_fail_closed_not_a_fallback(world, live_server, monkeypatch):
    """价目读不出来时**不许**回落到常量 —— 回落等于把 P0-2 原样放回去。"""
    _ = (world, live_server)
    import api.defensive_geo_api as mod

    monkeypatch.setattr(mod, "_live_pricing_catalog_version",
                        mod._live_pricing_catalog_version)  # 保持真实实现
    with pytest.raises(mod.PricingCatalogUnreadable):
        mod._live_pricing_catalog_version("this_feature_does_not_exist_p0fix")


# ═══════════════════════════════════════════════════════════════════════════
# 保险丝①:改价 → 409,零 command 零 freeze 零 outbox
# ═══════════════════════════════════════════════════════════════════════════
def test_price_change_between_preview_and_confirm_is_rejected_with_zero_side_effects(
        client, world, db, live_server):
    _ = live_server
    prev = W.make_preview(client, world["tenant"], world["brand_id"])
    assert prev["exactTotalPoints"] == COST, prev

    _raise_price(db)
    before = W.Counts(db)
    r = W.confirm(client, world["tenant"], prev)
    after = W.Counts(db)

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "SNAPSHOT_CHANGED", r.text
    assert before.delta(after) == (0, 0, 0), (
        "改价被拒了,却留下了 (run, freeze, outbox) 增量 %r —— "
        "「拒绝」必须是零副作用的,否则钱已经冻了" % (before.delta(after),))
    # 她拿到的必须是一条能照做的下一步,不是一个 code。
    assert r.json()["detail"]["nextAction"]["kind"] == "new_preview", r.text


def test_version_gate_alone_rejects_a_stale_snapshot_even_when_the_price_matches(
        client, world, db, live_server):
    """🔴 保险丝①的**独立**判据 —— 不靠②替它变绿。

    上一条(改价 → 409)其实**证不了①还在**:①被拆掉时,②会因为冻结额 820 ≠
    确认额 650 同样返回 409 SNAPSHOT_CHANGED 且零副作用 —— 两条锁叠在同一条
    路径上,前一条死了后一条会把绿撑住(本仓「两把锁叠同一路径:存活时别以为
    是自己那把在守」)。

    所以这条把两者**分开**:preview 上的 ``pricing_catalog_version`` 是坏的,
    而**价格没变**。此时②不会开火(冻结额 == 确认额),409 只可能来自①。
    ①被拆掉 ⇒ 这一单会真的确认成功(200 + 真冻结)⇒ 本条必红。

    preview 行直接 INSERT:那一列被 ``trg_defgeo_preview_immutable`` 锁死,
    UPDATE 会被拒 —— 这也顺带证明了那道不可变锁真的在管这一列。
    """
    _ = live_server
    import uuid as _uuid

    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO defgeo_diagnosis_run_previews "
            "(preview_id,tenant_owner_user_id,brand_id,created_by_user_id,question_plan_id,"
            " question_plan_revision,question_plan_hash,profile_revision_id,campaign_mode,"
            " frozen_payload,canonical_hash,feature_code,pricing_catalog_version,"
            " base_points,extra_points,exact_total_points,funding_policy,principal_kind,"
            " approval_requirement,planned_cells,lifecycle,idempotency_key,"
            " canonical_request_hash,expires_at) "
            "VALUES (gen_random_uuid(),%s,%s,%s,gen_random_uuid(),1,%s,'prof-1','defensive',"
            " '{}'::jsonb,%s,'geo_diagnosis','defgeo-pricing-STALE-v0',"
            " %s,0,%s,'personal_wallet','personal','not_required',1,'open',%s,%s,"
            " NOW()+INTERVAL '1 hour') "
            "RETURNING preview_id, canonical_hash",
            (world["tenant"], world["brand_id"], world["tenant"], "a" * 64, "d" * 64,
             COST, COST, _uuid.uuid4().hex, "c" * 64))
        row = cur.fetchone()

    before = W.Counts(db)
    r = client.post(
        "/api/defensive-geo/run-previews/%s/confirm" % row["preview_id"],
        json={"expectedHash": row["canonical_hash"].strip()},
        headers=W.headers(world["tenant"], idem="ver-" + _uuid.uuid4().hex[:10]))
    after = W.Counts(db)

    assert r.status_code == 409, (
        "价目版本已经对不上了,confirm 却返回 %s —— 版本闸没在守。"
        "(这一单的**金额是对的**,所以金额闸不会替它开火)" % r.status_code)
    assert r.json()["detail"]["code"] == "SNAPSHOT_CHANGED", r.text
    assert before.delta(after) == (0, 0, 0), before.delta(after)


def test_unchanged_price_still_confirms(client, world, db, live_server):
    """配对的必须不命中:没改价就必须能确认。

    少了这条,一个"永远 409"的实现也能让上面那条绿 —— 那种绿最贵。
    """
    _ = live_server
    prev = W.make_preview(client, world["tenant"], world["brand_id"])
    r = W.confirm(client, world["tenant"], prev)
    assert r.status_code == 200, r.text
    run = W.run_row(db, r.json()["diagnosisCommandId"])
    assert int(W.freeze_row(db, run["freeze_id"])["amount_total"]) == COST


# ═══════════════════════════════════════════════════════════════════════════
# 保险丝②:先把①关掉,再证②真的在守
# ═══════════════════════════════════════════════════════════════════════════
def _disable_version_gate(monkeypatch):
    """把版本函数打回「恒定常量」—— 即**修复前**的形态。

    这不是"绕过判据",而是本包唯一能让保险丝②被执行到的办法:
    ①在前面就返回了,②那一段代码根本跑不到。
    锁叠在同一条路径上时,不关掉前面那把,后面那把是死是活都看不出来。
    """
    import api.defensive_geo_api as mod

    # [E2-2] 补丁点从 `_live_pricing_catalog_version` 搬到 canonical 单点。
    #   confirm 现在用「自己事务里锁住的那一行」算版本,不再走前者 ——
    #   补丁还钉在旧接缝上的话,preview 存常量而 confirm 算真串,恒 409。
    #   打在单点上,preview 与 confirm 两边同值,①自然放行。
    monkeypatch.setattr(mod, "_pricing_catalog_version_from_row",
                        lambda *a, **k: "p0fix-frozen-const")


def test_amount_fuse_rolls_the_whole_confirm_back_when_the_freeze_differs(
        client, world, db, live_server, monkeypatch):
    """①被关掉 + 真改价 ⇒ 物理冻结额(820)≠ 确认额(650)⇒ 整笔回滚。

    这一条就是 Codex 那句「确认 7,800、实冻 8,200、响应仍显示 7,800」的直接反面。
    """
    _ = live_server
    _disable_version_gate(monkeypatch)
    prev = W.make_preview(client, world["tenant"], world["brand_id"])
    assert prev["exactTotalPoints"] == COST

    _raise_price(db)
    before = W.Counts(db)
    r = W.confirm(client, world["tenant"], prev)
    after = W.Counts(db)

    assert r.status_code == 409, (
        "版本闸关掉之后 confirm 返回 %s —— 说明**没有**第二把锁在比金额,"
        "她确认 %s 而系统冻了 %s(Codex P0-2 的可达结果)"
        % (r.status_code, COST, COST_RAISED))
    assert r.json()["detail"]["code"] == "SNAPSHOT_CHANGED", r.text
    assert before.delta(after) == (0, 0, 0), (
        "金额不符被拒,却留下了增量 %r —— 那笔多冻的钱现在挂在客户账上"
        % (before.delta(after),))
    # 钱包必须一分没动(不是"冻了又退",而是根本没冻)。
    assert W.wallet(db, world["tenant"])["frozen_points"] == 0


def test_amount_fuse_does_not_fire_when_nothing_changed(
        client, world, db, live_server, monkeypatch):
    """配对的必须不命中:①关掉、价也没改 ⇒ 必须正常确认。

    没有这条,②可能只是一把"恒拒"的锁,而恒拒的锁与"锁得准"长得一样。
    """
    _ = live_server
    _disable_version_gate(monkeypatch)
    prev = W.make_preview(client, world["tenant"], world["brand_id"])
    r = W.confirm(client, world["tenant"], prev)
    assert r.status_code == 200, r.text
    run = W.run_row(db, r.json()["diagnosisCommandId"])
    fz = W.freeze_row(db, run["freeze_id"])
    assert int(fz["amount_total"]) == int(prev["exactTotalPoints"]) == COST


def test_amount_fuse_also_covers_the_platform_leg(db, migrated_dsn, live_server, monkeypatch):
    """平台腿同样受金额约束 —— 平台多冻也是账错,只是错在我们自己身上。"""
    _ = live_server
    admin_uid, platform_uid, _x = W.fresh_uids(3)
    with W.conn(migrated_dsn).cursor() as cur:
        W.ensure_user(cur, admin_uid, "p0fix_a2admin_%d" % admin_uid, admin=True)
        W.ensure_user(cur, platform_uid, "p0fix_a2plat_%d" % platform_uid)
        W.ensure_wallet(cur, admin_uid, paid=1_000_000)
        W.ensure_wallet(cur, platform_uid, paid=1_000_000)
        W.set_pricing(cur, COST)
        brand_id = W.new_brand(cur, admin_uid)
    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", str(platform_uid))
    monkeypatch.setattr("auth.brand_access.require_brand_access", lambda *a, **k: None)
    _disable_version_gate(monkeypatch)

    client = TestClient(W.make_app(), raise_server_exceptions=False)
    prev = W.make_preview(client, admin_uid, brand_id, admin=True)
    before = W.Counts(db)
    with db.cursor() as cur:
        W.set_pricing(cur, COST_RAISED)
    r = W.confirm(client, admin_uid, prev, admin=True)
    after = W.Counts(db)
    with db.cursor() as cur:
        W.set_pricing(cur, COST)

    assert r.status_code == 409, r.text
    assert before.delta(after) == (0, 0, 0), before.delta(after)
    assert W.wallet(db, platform_uid)["frozen_points"] == 0, "平台钱包被多冻了"


def test_zero_cost_feature_is_not_killed_by_the_amount_fuse(client, world, db, live_server):
    """🔴 金额闸新落在了**免费功能**那条路径上,所以它欠这一条。

    0 价时 ``freeze_points`` 走的是提前返回(``total_cost == 0`` → 无冻结行、
    ``amount=0``),而确认额也是 0 —— 两者相等,闸不该开火。
    闸要是写成「必须有冻结额」或者拿 ``freeze_id`` 当判据,免费功能会被**拦死**,
    而那种坏法在只有"改价被拒"判据的情况下一条都不会红。
    (顺手多修一处 = 顺手多欠一条判据。)
    """
    _ = live_server
    with db.cursor() as cur:
        W.set_pricing(cur, 0)

    prev = W.make_preview(client, world["tenant"], world["brand_id"])
    assert prev["exactTotalPoints"] == 0, prev
    r = W.confirm(client, world["tenant"], prev)
    assert r.status_code == 200, (
        "免费功能被金额闸拦死了:%s %s" % (r.status_code, r.text[:300]))

    run = W.run_row(db, r.json()["diagnosisCommandId"])
    assert run["run_status"] == "running", run
    # 0 价 = 没有要收敛的物理冻结 ⇒ 现役口径把它纠正成 exempt(不是"平台承担"那一支)。
    assert run["billing_mode"] == "exempt", (
        "0 价单被标成 %r —— 那会让状态机去找一笔根本不存在的冻结" % run["billing_mode"])
    assert run["freeze_id"] is None, run
    assert W.wallet(db, world["tenant"])["frozen_points"] == 0


def test_idempotency_key_is_still_required(client, world, live_server):
    """顺带钉住一条既有不变式没被本次改动动到(改动面附近的回归面)。"""
    _ = live_server
    prev = W.make_preview(client, world["tenant"], world["brand_id"])
    r = client.post(
        "/api/defensive-geo/run-previews/%s/confirm" % prev["previewId"],
        json={"expectedHash": prev["canonicalHash"]},
        headers={"X-Test-Tenant": str(world["tenant"])})
    assert r.status_code == 422, r.text
    _ = uuid
