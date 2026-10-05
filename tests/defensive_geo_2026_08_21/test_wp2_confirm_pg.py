"""confirm 资金半:接现役 admission(§15.3 / §3.5 / DIA-FIN-02,03,09,11,13)。

判据形态 —— 全部**真 HTTP × 真 PG**
-----------------------------------
工单点名「判据打真 HTTP 不打纯函数」。理由在上一班已经付过学费:
幂等键挂错哈希那个缺陷在纯函数层完全看不见(两个 hash 各自都算得对),
只有真 HTTP 才照得出来。

**核心不变式**(下面每条判据都在打它):
    preview 被消费**恰一次** ⟺ 恰一个 command + 恰一次 freeze + 恰一条 outbox
    任何一步失败 ⟹ 三者**全为 0**

所以每条"必须失败"的判据都配一组**三面计数**(command / freeze / outbox),
而不是只断言 HTTP 码。只验状态码会漏掉「返回了 409 但钱已经冻了」——
本仓 2026-08-18 记过「只验 !=422 漏整层库合同」,同一个道理。
"""

from __future__ import annotations

import concurrent.futures
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.defensive_geo.copy_registry import assert_public_copy_clean, user_label

pytestmark = pytest.mark.integration

TENANT = 8401
OTHER = 8402


@pytest.fixture(scope="module")
def client():
    from api.defensive_geo_api import router

    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):
        tid = request.headers.get("X-Test-Tenant")
        if tid:
            request.state.user = {"user_id": int(tid), "is_admin": False}
        return await call_next(request)

    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _allow_brand(monkeypatch):
    import auth.brand_access as ba

    monkeypatch.setattr(ba, "require_brand_access", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _clean_denominator(db):
    """每条判据开跑前清掉本租户的活跃 run。

    🔴 这不是"打扫卫生",是**分母纪律**。`uq_diag_active_per_brand` 是生产
    真约束:一个品牌同时只能有一个活跃诊断。上一条判据成功 confirm 留下的
    活跃 run,会让下一条判据的 confirm **合法地** 409 —— 于是判据红得莫名
    其妙,而被测代码毫无问题。分母不干净时,判据测的是"上一条判据留了什么",
    不是被测行为。

    (这条 fixture 本身是被真事故逼出来的:全套判据曾经集体 409,
     真因就是一行残留的 `pending_freeze`。)
    """
    with db.cursor() as cur:
        cur.execute("DELETE FROM diagnosis_runs WHERE owner_user_id = ANY(%s)", ([TENANT, OTHER],))
    db.commit()
    yield


def _h(tenant=TENANT, idem=None):
    h = {"X-Test-Tenant": str(tenant)}
    if idem:
        h["Idempotency-Key"] = idem
    return h


def _make_preview(client, tenant=TENANT):
    plan = client.post(
        "/api/defensive-geo/question-plans/preview",
        json={"clientRequestId": "creq-" + uuid.uuid4().hex[:10], "brandId": 901,
              "profileRevisionId": "prof-1", "mode": "defensive",
              "questions": [{"text": "这个牌子靠谱吗", "modeSide": "defensive",
                             "familyKey": "identity_check", "brandExposure": "named"}]},
        headers=_h(tenant)).json()
    prev = client.post(
        "/api/defensive-geo/run-previews",
        json={"questionPlanId": plan["planId"], "questionPlanRevision": 1,
              "profileRevisionId": "prof-1", "platformKeys": ["deepseek"]},
        headers=_h(tenant, idem="idem-" + uuid.uuid4().hex[:10])).json()
    return prev


class Counts:
    """三面计数快照。**一次取三个** —— 分开取会在并发下拍到不一致的瞬间。"""

    def __init__(self, db):
        with db.cursor() as cur:
            cur.execute("SELECT count(*) AS c FROM diagnosis_runs")
            self.commands = cur.fetchone()["c"]
            cur.execute("SELECT count(*) AS c FROM point_freezes")
            self.freezes = cur.fetchone()["c"]
            cur.execute("SELECT count(*) AS c FROM notification_outbox")
            self.outbox = cur.fetchone()["c"]

        # 🔴 [#62 2026-09-05] 三个读完**立刻收事务**。
        #    上面「一次取三个」保的是快照一致(三个数来自同一事务),
        #    读完再 rollback 不破坏它 —— 但不收就会一直握着这三张表的读锁。
        #    后果是**自锁死**:测试握着 diagnosis_runs 的读锁,
        #    然后同步等一个 HTTP 请求,而应用每次启动都重放全部迁移
        #    (无 applied 账本),那笔 `migration_diagnosis_runs_2026_07_13.sql`
        #    要 AccessExclusiveLock —— 它等的是测试自己。
        #    伪装极好:进程活着、有 active 查询、表数在涨,全都指向「就是慢」;
        #    只有 pg_blocking_pids 配 xact_start **时长**能把「在等」和「等不到」分开。
        db.rollback()

    def delta(self, other):
        return (other.commands - self.commands,
                other.freezes - self.freezes,
                other.outbox - self.outbox)


@pytest.fixture
def counts(db):
    def _snap():
        return Counts(db)
    return _snap


def test_counting_surfaces_exist(db):
    """🔴 判据可用性关:三张计数表必须真的在。

    表不在时 `Counts` 会抛,但如果哪天有人给它加了 try/except,
    「零副作用」会变成「零查询」—— 那时所有 delta 都是 (0,0,0),全绿。
    """
    with db.cursor() as cur:
        for t in ("diagnosis_runs", "point_freezes", "notification_outbox"):
            cur.execute("SELECT to_regclass(%s) AS r", (t,))
            assert cur.fetchone()["r"] is not None, f"计数表 {t} 不存在 —— 零副作用判据会恒绿"


# ══════════════════════ 409 全家:零 command 零 freeze 零 outbox ═══════════
def _assert_zero_side_effects(before, after, label):
    d = before.delta(after)
    assert d == (0, 0, 0), (
        f"{label}:期望零副作用,实得 (command,freeze,outbox) 增量 = {d}。"
        "返回 409 但钱已经冻了 —— 只验状态码会完全漏掉这种"
    )


def test_wrong_hash_is_409_with_zero_side_effects(client, counts):
    """SNAPSHOT_CHANGED(所见即所签)。"""
    prev = _make_preview(client)
    before = counts()
    r = client.post(f"/api/defensive-geo/run-previews/{prev['previewId']}/confirm",
                    json={"expectedHash": "f" * 64},
                    headers=_h(idem="idem-" + uuid.uuid4().hex[:10]))
    after = counts()
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "SNAPSHOT_CHANGED"
    _assert_zero_side_effects(before, after, "hash 不符")


def test_price_change_is_409_with_zero_side_effects(client, counts, db):
    """DIA-FIN-09:preview→confirm 之间 catalog 变了 → 409,零 command 零 freeze。

    [2026-08-27 · 门9 重锚 · 三臂取证]
    ------------------------------------
    原来这条靠 monkeypatch ``_live_pricing_catalog_version`` 造「改价」那一幕。
    **E2-2(P1-F2)之后 confirm 不再调它** —— 版本改由
    ``_pricing_catalog_version_from_row(_lock_pricing_row_for_confirm(cur, …))``
    从**本事务锁住的那一行**派生(修的是「版本读与 freeze 读之间的窗口」)。
    桩还在,路已经不走那儿:改价这一幕**一次都没发生过**,confirm 正常 200。

    三臂:父提交 4b3cdd886(E2 之前)单跑 **1 passed**;合流尖 f5417c561
    **1 failed**;而同尖上真改一次价的判据(funding_p0::
    ``test_e2_a_flip_before_the_version_read_is_still_rejected_by_the_version_gate``)
    **绿**。⇒ 坏的是尺子的模拟手段,不是那道闸。

    ⚠️ 这次红的方向是**幸运**的。同一个脱靶若长在「断言不出现某某」那类判据上,
       会**恒真变绿**、永远没人知道。

    重锚成**真的改一次价**:不依赖 confirm 内部走哪个函数取版本,
    只依赖「版本由价目内容派生」这条约定 —— E2-2 之后这条约定比以前更硬。
    改完必须**改回去**:本包的库是 session 级共用的,留着会污染后面的判据。

    🔴 为什么动 ``requires_paid_points`` 而不是 ``cost_points``
    ---------------------------------------------------------
    重锚第一版改的是 ``cost_points``。它确实 409 —— 但**亲手注毒**(把版本闸
    短路)之后**仍然 409**:那个 409 是 L1186 的**金额闸**给的
    (改了单价,冻结额自然对不上确认额),版本闸一直是**被另一道闸挡在身后**的。
    改 ``cost_points`` 同时动了**两条轴**,判据因此分不清谁在守。

    ``requires_paid_points`` 在版本 canonical 里
    (``_pricing_catalog_version_from_row``:feature_code|cost_points|requires_paid_points),
    却**不改冻结额** ⇒ 金额闸不响 ⇒ 409 只能来自版本闸,且抛在 freeze **之前**,
    这才是本条 docstring 说的「零 command 零 freeze」。
    注毒复核:短路版本闸 ⇒ 本条**当场红**(见交付文的注毒回执)。
    """
    prev = _make_preview(client)
    before = counts()
    with db.cursor() as cur:
        cur.execute("SELECT requires_paid_points FROM feature_pricing "
                    "WHERE feature_code=%s", ("geo_diagnosis",))
        row = cur.fetchone()
        assert row is not None, "价目里没有 geo_diagnosis —— 判据没有被测对象"
        old_flag = bool(row["requires_paid_points"])
        cur.execute("UPDATE feature_pricing SET requires_paid_points=%s "
                    "WHERE feature_code=%s", (not old_flag, "geo_diagnosis"))
    db.commit()
    try:
        r = client.post(f"/api/defensive-geo/run-previews/{prev['previewId']}/confirm",
                        json={"expectedHash": prev["canonicalHash"]},
                        headers=_h(idem="idem-" + uuid.uuid4().hex[:10]))
        after = counts()
    finally:
        with db.cursor() as cur:
            cur.execute("UPDATE feature_pricing SET requires_paid_points=%s "
                        "WHERE feature_code=%s", (old_flag, "geo_diagnosis"))
        db.commit()
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "SNAPSHOT_CHANGED"
    _assert_zero_side_effects(before, after, "catalog 改价")


def test_expired_preview_is_409_with_zero_side_effects(client, counts, db):
    """DIA-FIN-11:expired → `PREVIEW_EXPIRED`,零 command/freeze/outbox。

    过期用**改库里的 expires_at** 造,不是等 30 分钟,也不是 mock 掉时钟 ——
    判据里不许有「今天」,而 expiry 是服务端用 DB 的 NOW() 判的,
    所以造反例的正确姿势是把那一行的 expires_at 推到过去。
    """
    prev = _make_preview(client)
    with db.cursor() as cur:
        cur.execute(
            "UPDATE defgeo_diagnosis_run_previews SET lifecycle='expired' WHERE preview_id=%s",
            (prev["previewId"],))
    db.commit()
    before = counts()
    r = client.post(f"/api/defensive-geo/run-previews/{prev['previewId']}/confirm",
                    json={"expectedHash": prev["canonicalHash"]},
                    headers=_h(idem="idem-" + uuid.uuid4().hex[:10]))
    after = counts()
    assert r.status_code == 409, r.text
    d = r.json()["detail"]
    assert d["code"] == "PREVIEW_EXPIRED"
    # U-2 逐字:必须告诉她「没有扣除任何算力」
    assert "没有扣除任何算力" in d["publicExplanation"], d
    assert_public_copy_clean(d["publicExplanation"], field="publicExplanation")
    assert_public_copy_clean(d["nextAction"]["label"], field="nextAction.label")
    _assert_zero_side_effects(before, after, "preview 过期")


def _live_catalog_version():
    from api.defensive_geo_api import _live_pricing_catalog_version

    return _live_pricing_catalog_version("geo_diagnosis")


def test_approval_required_is_403_with_zero_side_effects(client, counts, db):
    """DIA-FIN-13:`approvalRequirement=required` 且未批 → 403,零副作用。"""
    prev = _make_preview(client)
    with db.cursor() as cur:
        # 🔴 approval_requirement 是冻结列,trigger 会拒 UPDATE ——
        #    所以直接插一行 required 的 preview,而不是改现有的。
        #    这也顺带证明了那道不可变锁真的在管这一列。
        cur.execute(
            "INSERT INTO defgeo_diagnosis_run_previews "
            "(preview_id,tenant_owner_user_id,brand_id,created_by_user_id,question_plan_id,"
            " question_plan_revision,question_plan_hash,profile_revision_id,campaign_mode,"
            " frozen_payload,canonical_hash,feature_code,pricing_catalog_version,"
            " base_points,extra_points,exact_total_points,funding_policy,principal_kind,"
            " approval_requirement,planned_cells,lifecycle,idempotency_key,"
            " canonical_request_hash,expires_at) "
            "VALUES (gen_random_uuid(),%s,901,%s,gen_random_uuid(),1,%s,'prof-1','defensive',"
            " '{}'::jsonb,%s,'geo_diagnosis',%s,"
            " 0,0,0,'personal_wallet','personal','required',1,'open',%s,%s,NOW()+INTERVAL '1 hour') "
            "RETURNING preview_id, canonical_hash",
            # [A-2 · 2026-08-25] pricing_catalog_version 不再手抄常量:它现在由
            #   **真实价目内容** hash 出来(改价必然改版本)。写死一个旧串会让
            #   confirm 先在版本比对上 409,这条判据就再也验不到 APPROVAL_REQUIRED
            #   —— 红因与被测行为无关。取现值 = 让这条判据继续测它本来要测的那件事。
            # 顺序 = SQL 里 %s 出现的顺序:tenant / created_by / question_plan_hash /
            #   canonical_hash / pricing_catalog_version / idempotency_key / canonical_request_hash。
            (TENANT, TENANT, "a" * 64, "d" * 64, _live_catalog_version(),
             uuid.uuid4().hex, "c" * 64))
        row = cur.fetchone()
    db.commit()
    before = counts()
    r = client.post(f"/api/defensive-geo/run-previews/{row['preview_id']}/confirm",
                    json={"expectedHash": row["canonical_hash"].strip()},
                    headers=_h(idem="idem-" + uuid.uuid4().hex[:10]))
    after = counts()
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "APPROVAL_REQUIRED"
    _assert_zero_side_effects(before, after, "待审批")


def test_cross_tenant_confirm_is_404_with_zero_side_effects(client, counts):
    """归属每请求现做:别人的 preview 一律 404(不泄露存在性),且零副作用。"""
    prev = _make_preview(client, tenant=TENANT)
    before = counts()
    r = client.post(f"/api/defensive-geo/run-previews/{prev['previewId']}/confirm",
                    json={"expectedHash": prev["canonicalHash"]},
                    headers=_h(OTHER, idem="idem-" + uuid.uuid4().hex[:10]))
    after = counts()
    assert r.status_code == 404
    _assert_zero_side_effects(before, after, "跨租户 confirm")


def test_confirm_requires_idempotency_key(client, counts):
    prev = _make_preview(client)
    before = counts()
    r = client.post(f"/api/defensive-geo/run-previews/{prev['previewId']}/confirm",
                    json={"expectedHash": prev["canonicalHash"]}, headers=_h())
    after = counts()
    assert r.status_code == 422
    _assert_zero_side_effects(before, after, "缺幂等键")


def test_unknown_response_key_is_controlled_failure_not_raw_500(client):
    """G-4 在 confirm 上同样成立:请求多一个键 → 422,不是 500。"""
    prev = _make_preview(client)
    r = client.post(f"/api/defensive-geo/run-previews/{prev['previewId']}/confirm",
                    json={"expectedHash": prev["canonicalHash"], "surprise": 1},
                    headers=_h(idem="k"))
    assert r.status_code == 422
    assert r.status_code != 500


# ══════════════════════ 成功路径 + DIA-FIN-02 ═════════════════════════════
def test_confirm_creates_exactly_one_of_each(client, counts):
    """🔴 主锁:成功一次 ⇒ command / freeze / outbox 各恰一。

    ⚠️ 本包目前签发的 preview 是 **0 算力**(shadow 定价),
       ``freeze_points`` 对 0 额度不产生 point_freezes 行 —— 所以 freeze 增量为 0
       是**正确的**,不是"没冻上"。判据据实断言并写明理由,不假装冻了。
       真实计价接通后这一格会变,那时判据必须同步改 —— 见交付单「未完成」。
    """
    prev = _make_preview(client)
    before = counts()
    r = client.post(f"/api/defensive-geo/run-previews/{prev['previewId']}/confirm",
                    json={"expectedHash": prev["canonicalHash"]},
                    headers=_h(idem="idem-" + uuid.uuid4().hex[:10]))
    after = counts()
    assert r.status_code == 200, r.text
    dc, df, do = before.delta(after)
    assert dc == 1, f"command 增量 {dc},期望恰 1"
    assert do == 1, f"outbox 增量 {do},期望恰 1"
    # 🔴 这条曾经写的是 `df == 0`,理由是"当前 preview 为 0 算力"——
    #    那是 preview 还在**硬编码 0 算力**时代的残留。preview 改读真实
    #    `feature_pricing` 之后,0 算力不再成立,而这条判据却还在断言
    #    「不许有冻结行」—— 正好断言掉了 DIA-FIN-02 要求"恰一个"的那个东西。
    #    判据跟着被测代码走,不跟着记忆走。
    assert df == 1, f"freeze 增量 {df};真实计价下必须恰一条冻结行"


def test_confirm_response_carries_matrix_and_human_copy(client):
    """四格投影 + 人话三件(补充令)。"""
    prev = _make_preview(client)
    d = client.post(f"/api/defensive-geo/run-previews/{prev['previewId']}/confirm",
                    json={"expectedHash": prev["canonicalHash"]},
                    headers=_h(idem="idem-" + uuid.uuid4().hex[:10])).json()
    # 四格逐值(personal_wallet 那一格)
    assert d["fundingPolicy"] == "personal_wallet"
    assert d["principalKind"] == "personal"
    assert d["billingModeProjection"] == "paid"
    assert d["fundingState"] == "frozen"
    assert d["fundingHandle"]["kind"] == "wallet_freeze"
    assert d["sponsorPolicyRef"] is None
    # billing_mode 不扩宽
    assert d["billingModeProjection"] in ("paid", "exempt")
    for f in ("runStateUserLabel", "fundingStateUserLabel", "costUserLabel"):
        assert_public_copy_clean(d[f], field=f)
    assert_public_copy_clean(d["nextAction"]["label"], field="nextAction.label")


def test_consumed_replay_returns_original_command_not_a_second(client, counts):
    """DIA-FIN-11 后半:consumed 同 hash 重放 → 原 command,**零新增**。"""
    prev = _make_preview(client)
    first = client.post(f"/api/defensive-geo/run-previews/{prev['previewId']}/confirm",
                        json={"expectedHash": prev["canonicalHash"]},
                        headers=_h(idem="k1-" + uuid.uuid4().hex[:8])).json()
    before = counts()
    second = client.post(f"/api/defensive-geo/run-previews/{prev['previewId']}/confirm",
                         json={"expectedHash": prev["canonicalHash"]},
                         headers=_h(idem="k2-DIFFERENT-" + uuid.uuid4().hex[:8]))
    after = counts()
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["diagnosisCommandId"] == first["diagnosisCommandId"], "重放建出了第二个 command"
    assert body["idempotentReplay"] is True
    assert first["idempotentReplay"] is False
    _assert_zero_side_effects(before, after, "consumed 重放")


def test_consumed_replay_with_different_hash_is_409(client, counts):
    """consumed 但 hash 不同 = 想拿旧 preview 换新内容 → 409,零副作用。"""
    prev = _make_preview(client)
    client.post(f"/api/defensive-geo/run-previews/{prev['previewId']}/confirm",
                json={"expectedHash": prev["canonicalHash"]},
                headers=_h(idem="k-" + uuid.uuid4().hex[:8]))
    before = counts()
    r = client.post(f"/api/defensive-geo/run-previews/{prev['previewId']}/confirm",
                    json={"expectedHash": "e" * 64},
                    headers=_h(idem="k-" + uuid.uuid4().hex[:8]))
    after = counts()
    assert r.status_code == 409
    _assert_zero_side_effects(before, after, "consumed 异 hash")


def test_dia_fin_02_twenty_concurrent_confirms_yield_exactly_one(client, counts):
    """🔴 DIA-FIN-02:同 previewId + 同 expectedHash,20 并发 confirm。

    规格原文:「同一 previewId + expectedHash + **Idempotency-Key** 20 并发 confirm,
    恰一个 command/run/freeze/outbox,**其余返回同一对象**」。

    🔴 第一版这条判据是错的,而且错得很隐蔽 —— 它给 20 个线程发了 **20 把不同的
       Idempotency-Key**。那根本不是 DIA-FIN-02 的场景,而且后果是判据失去区分力:
       变异 C6(摘掉 `FOR UPDATE`)和 C7(摘掉 `lifecycle='open'` CAS)**双双存活**。

       真因是三把锁叠在同一条路径上:`FOR UPDATE` → lifecycle CAS →
       `uq_diag_active_per_brand`。拆掉前两把,第三把仍然把"恰一个"兜住了,
       于是判据照绿。**叠锁路径上"相关判据全绿"证明不了任何一把锁被验过,
       只有变异存活是信号。**

       改成同一把 key 之后,判据要求的不再只是"恰一个",而是
       「其余**也必须 200 并拿到同一个 command**」—— 那条路只有走 `FOR UPDATE`
       串行化 + 命中 consumed 重放分支才能满足,第三把锁替不了它。
    """
    prev = _make_preview(client)
    before = counts()
    shared_key = "dia-fin-02-" + uuid.uuid4().hex[:10]

    def _one(_i):
        return client.post(
            f"/api/defensive-geo/run-previews/{prev['previewId']}/confirm",
            json={"expectedHash": prev["canonicalHash"]},
            headers=_h(idem=shared_key))

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        results = list(ex.map(_one, range(20)))
    after = counts()

    codes = sorted({r.status_code for r in results})
    assert codes == [200], (
        f"同一把 Idempotency-Key 的 20 并发出现了非 200:{codes}。"
        "规格要求「其余返回同一对象」—— 让重放者吃 409 就是让前端把一次成功的"
        "确认显示成失败,她会再点一次。"
    )

    command_ids = {r.json()["diagnosisCommandId"] for r in results}
    assert len(command_ids) == 1, f"产生了 {len(command_ids)} 个不同 command:{command_ids}"
    assert len({r.json()["runId"] for r in results}) == 1

    dc, df, do = before.delta(after)
    assert (dc, df, do) == (1, 1, 1), (
        f"20 并发的 (command, freeze, outbox) 增量 = {(dc, df, do)},要求恰 (1,1,1)"
    )


def test_dia_fin_03_failure_inside_transaction_rolls_back_everything(client, counts, monkeypatch):
    """DIA-FIN-03 的原子面:第 ④ 步(outbox)炸 ⇒ ②③ 全部回滚。

    这一条打的正是「同一事务」到底成没成立:
    如果 command 与 outbox 不在一个事务里,这里会留下一个孤儿 command。
    """
    prev = _make_preview(client)
    import api.defensive_geo_api as mod

    def _boom(*a, **k):
        raise RuntimeError("注入:outbox 写失败")

    monkeypatch.setattr(mod, "_enqueue_confirm_outbox", _boom)
    before = counts()
    r = client.post(f"/api/defensive-geo/run-previews/{prev['previewId']}/confirm",
                    json={"expectedHash": prev["canonicalHash"]},
                    headers=_h(idem="idem-" + uuid.uuid4().hex[:10]))
    after = counts()
    assert r.status_code == 500
    # 受控失败:不许泄 raw
    body = r.json()["detail"]
    assert body["code"] == "INTERNAL_ERROR"
    for leak in ("Traceback", "psycopg2", "postgresql://", "注入"):
        assert leak not in str(body), f"错误信封泄了 {leak}"
    _assert_zero_side_effects(before, after, "outbox 失败应整体回滚")


def test_preview_stays_open_after_rollback(client, db, monkeypatch):
    """回滚后 preview 必须还是 open —— 否则她永远 confirm 不了,而钱也没冻。

    「preview 被消费了但 command 不存在」是最坏的半状态:
    对用户表现为「点了没反应,再点说已用过」。
    """
    prev = _make_preview(client)
    import api.defensive_geo_api as mod

    monkeypatch.setattr(mod, "_enqueue_confirm_outbox",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    client.post(f"/api/defensive-geo/run-previews/{prev['previewId']}/confirm",
                json={"expectedHash": prev["canonicalHash"]},
                headers=_h(idem="idem-" + uuid.uuid4().hex[:10]))
    with db.cursor() as cur:
        cur.execute("SELECT lifecycle, consumed_command_id FROM defgeo_diagnosis_run_previews "
                    "WHERE preview_id=%s", (prev["previewId"],))
        row = cur.fetchone()
    assert row["lifecycle"] == "open", f"回滚后 preview 是 {row['lifecycle']} —— 半状态"
    assert row["consumed_command_id"] is None


# ══════════════════════ 结构不变式 ════════════════════════════════════════
def test_admit_run_default_path_is_byte_identical():
    """🔴 我给 `admit_run` 加了可选 `_cursor`,必须证明**默认路径没被改动**。

    判法:默认路径仍走 `get_db()`,且 `_cursor` 是 keyword-only
    (位置参数调用点 60+ 个,多一个位置参数就全错位)。
    """
    import inspect

    from services.diagnosis_runs import admit_run

    sig = inspect.signature(admit_run)
    p = sig.parameters["_cursor"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY, "_cursor 必须 keyword-only"
    assert p.default is None, "_cursor 默认必须是 None(默认 = 旧行为)"
    src = inspect.getsource(admit_run)
    assert "get_db() if _cursor is None else" in src, "默认路径不再走 get_db —— 旧行为被改了"
    assert "if _cursor is None and not res.admitted" in src, \
        "重试条件没排除借用模式 —— 借来的事务里重试是假重试"


def test_confirm_never_widens_billing_mode():
    """§3.4 红线:billing_mode 只投影 paid|exempt。

    结构锚:confirm 把 `spec.billing_mode_projection` 原样传给 admit_run,
    而那个值由资金矩阵单点裁决(矩阵判据已逐格锁死 paid|exempt)。
    """
    import inspect

    import api.defensive_geo_api as mod

    src = inspect.getsource(mod.confirm_run_preview)
    assert "spec.billing_mode_projection" in src, "billing_mode 不再来自矩阵 —— 可能被就地拼了"
    for forbidden in ('"shadow"', "'shadow'", '"qa"', "'qa'", '"formal"'):
        assert forbidden not in src, f"confirm 里出现 {forbidden} —— 疑似扩宽 billing_mode"


def test_confirm_uses_borrowed_cursor_for_all_four_legs():
    """结构锚:四条腿必须都吃**同一个** cur。

    值层面「都成功了」证明不了它们在一个事务里 —— 只有全都拿到同一个游标才是。

    🔴 用 AST 而不是 grep 源码文本。第一版写的是
       ``assert "_freeze_exact(\\n                cur" in src`` ——
       把**缩进宽度**焊进了判据。真实代码是 ``await _freeze_exact(`` 且缩进多四格,
       于是这条判据一直红,红因与被测行为毫无关系。反过来更危险:
       同一把锚在别人重排代码后会静默变**恒绿**。
       结构判据要打**语法结构**,不打字符位置。
    """
    import ast
    import inspect
    import textwrap

    import api.defensive_geo_api as mod

    src = inspect.getsource(mod.confirm_run_preview)
    assert "FOR UPDATE" in src, "没锁行 —— 并发下 CAS 之外还会有读改写竞态"

    tree = ast.parse(textwrap.dedent(src))

    def _first_arg_names(fn_name: str) -> list[str]:
        """收集对 fn_name 的每次调用的第一个位置实参(展开 await)。"""
        out = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name != fn_name:
                continue
            if node.args and isinstance(node.args[0], ast.Name):
                out.append(node.args[0].id)
            else:
                out.append("<非裸变量>")
        return out

    def _kwarg_names(fn_name: str, kw: str) -> list[str]:
        out = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name != fn_name:
                continue
            for k in node.keywords:
                if k.arg == kw:
                    out.append(k.value.id if isinstance(k.value, ast.Name) else "<非裸变量>")
        return out

    # ② command:admit 必须借同一个 cur
    assert _kwarg_names("_admit_run", "_cursor") == ["cur"], \
        f"admit_run 的 _cursor 不是 cur:{_kwarg_names('_admit_run', '_cursor')}"
    # ③ freeze
    assert _first_arg_names("_freeze_exact") == ["cur"], \
        f"freeze 没拿到同一个 cur:{_first_arg_names('_freeze_exact')}"
    # ④ outbox
    assert _first_arg_names("_enqueue_confirm_outbox") == ["cur"], \
        f"outbox 没拿到同一个 cur:{_first_arg_names('_enqueue_confirm_outbox')}"
    # ⑤ 推进 running(少了这一腿 → run 卡 pending_freeze → 10 分钟后被收尸退款)
    assert _first_arg_names("start_run_in_caller_txn") == ["cur"], \
        f"start_run 没拿到同一个 cur:{_first_arg_names('start_run_in_caller_txn')}"


# ══════════════════════════════════════════════════════════════════════════
# 第四班 ②:confirm 之后 run 必须真的**在跑**,不是卡在 pending_freeze
# ══════════════════════════════════════════════════════════════════════════
def test_confirm_leaves_the_run_running_with_a_freeze_handle(client, db):
    """🔴 这条判据是被一个真缺陷逼出来的,不是补充说明。

    confirm 第一版做完 admit + freeze + outbox 就 commit 了,**从来没人把 run
    推进 running**。当时所有判据都绿 —— 因为没有一条去看那一行的状态。后果:

      · `uq_diag_active_per_brand` 把品牌锁死,该品牌后续诊断一律 409;
      · 10 分钟后 sweeper 的 `pending_freeze` 收尸双表定位到那笔冻结,
        CAS 进 `release_pending` → **算力退回、诊断静默死掉**,
        而用户界面上写着"已开始"。

    所以这条不看 HTTP 码,只看**库里那一行**。
    """
    prev = _make_preview(client)
    r = client.post(
        f"/api/defensive-geo/run-previews/{prev['previewId']}/confirm",
        json={"expectedHash": prev["canonicalHash"]},
        headers=_h(idem="run-state-" + uuid.uuid4().hex[:8]))
    assert r.status_code == 200, r.text
    run_token = r.json()["diagnosisCommandId"]

    with db.cursor() as cur:
        cur.execute(
            "SELECT run_status, billing_mode, freeze_id, freeze_backend "
            "FROM diagnosis_runs WHERE run_token=%s", (run_token,))
        row = cur.fetchone()

    assert row is not None, f"confirm 返回了 command {run_token} 但库里没有这一行"
    assert row["run_status"] == "running", (
        f"confirm 之后 run 停在 {row['run_status']!r}。pending_freeze 会被 sweeper "
        "收尸退款 —— 这是「钱冻了、诊断死了、界面说在跑」的三方不一致。"
    )
    if row["billing_mode"] == "paid":
        # chk_freeze_handle 的库级不变式:paid 行进 running 必须持有句柄
        assert row["freeze_id"] is not None and row["freeze_backend"] is not None, \
            "paid 行进了 running 却没有 freeze 句柄 —— 违反 chk_freeze_handle 的语义"


def test_confirm_outbox_event_is_not_a_terminal_lie(client, db):
    """confirm 落的 outbox 事件不许是终态事件。

    第一版用的是 `DIAGNOSIS_COMPLETED`,渲染标题「品牌体检已完成」—— 在体检
    一秒都还没跑的时候。仓里已有同款前车之鉴(R6 2026-08-17 的
    `MONITORING_ENABLED_BY_OTHER`)。判据打**渲染出来的标题**,不打枚举名:
    换个枚举名但标题照样说"已完成"仍然是骗人。
    """
    from services.notification_events import (
        TERMINAL_SUPERSEDE_GROUPS, NotificationEventType, RecipientKind, render_notification,
    )

    prev = _make_preview(client)
    r = client.post(
        f"/api/defensive-geo/run-previews/{prev['previewId']}/confirm",
        json={"expectedHash": prev["canonicalHash"]},
        headers=_h(idem="evt-" + uuid.uuid4().hex[:8]))
    assert r.status_code == 200, r.text
    run_token = r.json()["diagnosisCommandId"]

    with db.cursor() as cur:
        cur.execute(
            "SELECT event_type FROM notification_outbox WHERE business_id=%s", (run_token,))
        rows = cur.fetchall()
    assert len(rows) == 1, f"confirm 落了 {len(rows)} 条 outbox,要求恰一条"
    event_value = rows[0]["event_type"]

    event = NotificationEventType(event_value)
    assert event not in TERMINAL_SUPERSEDE_GROUPS["diagnosis"], (
        f"{event_value} 属于诊断终态组 —— 它会和真正的终态互相覆盖,"
        "用户最后只看得见一条"
    )

    rendered = render_notification(event, RecipientKind.USER, {
        "business_no": "诊断-x", "status": "已开始，正在体检",
        "occurred_at": "2026-08-21T00:00:00+00:00", "summary": "",
    })
    title = rendered["title"]
    for lie in ("已完成", "已结束", "已取消", "已退回", "未完成"):
        assert lie not in title, f"confirm 时刻的通知标题写着 {lie!r}:{title!r}"
    assert_public_copy_clean(title, field="outbox.title")


def test_confirmed_event_is_not_in_the_terminal_supersede_group():
    """纯结构:非终态事件不许混进终态覆盖组。

    混进去的伤害是**反向**的:后到的"已开始"会把真正的"已完成/已退款"盖掉,
    用户最后只看得见一条"开始了"。
    """
    from services.notification_events import TERMINAL_SUPERSEDE_GROUPS, NotificationEventType

    group = TERMINAL_SUPERSEDE_GROUPS["diagnosis"]
    assert NotificationEventType.DIAGNOSIS_CONFIRMED not in group
    # 反向对照:这个组不是空的(否则上面那条什么都没证明)
    assert NotificationEventType.DIAGNOSIS_COMPLETED in group


# ---------------------------------------------------------------------------
# [#62 2026-09-05] confirm 时「题单已被改版」这道闸
#
# 🔴 创建 preview 时(defensive_geo_api.py:732)已经有同名的一道闸,
#    但那道闸只看**创建那一刻**。这个 bug 长在两者之间:
#    前端重建了题单(新 revision),confirm 仍拿着旧 preview ——
#    她**以为**在跑新题单,实际跑的是旧的,钱也按旧的冻。
# ---------------------------------------------------------------------------

def _preview_plan_ref(db, preview_id):
    """从库里取 preview 绑的 (plan_id, revision) —— 不依赖响应体字段名。"""
    with db.cursor() as cur:
        cur.execute("SELECT question_plan_id, question_plan_revision "
                    "  FROM defgeo_diagnosis_run_previews WHERE preview_id=%s", (preview_id,))
        r = cur.fetchone()
    # 🔴 读完立刻收事务:留着不提交的读事务会挡住应用侧惰性重放的迁移 DDL,
    #    而「被挡住」与「跑得慢」在进程活/有 active/表数在涨上全部同形,
    #    只有 pg_blocking_pids 能分 —— 全新库首跑实测踩过一次。
    db.rollback()
    return r["question_plan_id"], r["question_plan_revision"]


def _supersede(db, plan_id, revision):
    """把该 revision 标成「已被取代」。

    只能 NULL → 非 NULL(migration_040:147 的触发器只放行这一个方向),
    所以这是造这个反例的**唯一**合法姿势。
    """
    with db.cursor() as cur:
        cur.execute("UPDATE defgeo_question_plans SET superseded_by_revision=%s "
                    " WHERE plan_id=%s AND plan_revision=%s",
                    (revision + 1, plan_id, revision))
        assert cur.rowcount == 1, f"没改到行(plan={plan_id} rev={revision})"
    db.commit()


def test_superseded_plan_is_409_with_zero_side_effects(client, counts, db):
    """题单在确认前被改版 ⇒ 409 + 新 reason 文案 + 刷新出口,且**零副作用**。

    🔴 终态断言是**零副作用**,不是「弹窗对不对」:
       只要 CAS 把 preview 推到 consumed,这次确认就「恰好发生过一次」了,
       再拒也收不回来。所以这道闸必须排在任何写之前,而证明它排在前面的
       唯一办法是量三面计数,不是读代码顺序。
    """
    prev = _make_preview(client)
    plan_id, rev = _preview_plan_ref(db, prev["previewId"])
    _supersede(db, plan_id, rev)

    before = counts()
    r = client.post(f"/api/defensive-geo/run-previews/{prev['previewId']}/confirm",
                    json={"expectedHash": prev["canonicalHash"]},
                    headers=_h(idem="idem-" + uuid.uuid4().hex[:10]))
    after = counts()

    assert r.status_code == 409, r.text
    d = r.json()["detail"]
    assert d["code"] == "QUESTION_PLAN_NOT_RUNNABLE", d
    # 🔴 文案从登记表读,不手写在夹具里 —— 手写的对客文案是判据在跟自己对话,
    #    改了登记表也不会红。
    assert d["publicExplanation"] == user_label("reason", "question_plan_superseded"), d
    assert d["nextAction"]["label"] == user_label("action", "refresh_and_retry"), d
    # 她刚点了确认,第一反应是钱有没有被划走 —— 这句必须在。
    assert "没有扣除任何算力" in d["publicExplanation"], d
    assert_public_copy_clean(d["publicExplanation"], field="publicExplanation")
    assert_public_copy_clean(d["nextAction"]["label"], field="nextAction.label")
    _assert_zero_side_effects(before, after, "题单已被改版")


def test_live_plan_still_confirms(client, counts, db):
    """🔁 正样本臂:题单**没有**被改版时,这道新闸不许拦。

    **红了说明什么**:新闸过严,把正常确认也拒了 ——
    这比漏拦更糟(她付了钱却下不了单)。反臂绿而这条红时,
    读数看起来"闸很有效",实际是**全拒**,零区分力。
    """
    prev = _make_preview(client)
    plan_id, rev = _preview_plan_ref(db, prev["previewId"])
    with db.cursor() as cur:   # 自证:这一版确实没被标取代
        cur.execute("SELECT superseded_by_revision AS s FROM defgeo_question_plans "
                    " WHERE plan_id=%s AND plan_revision=%s", (plan_id, rev))
        assert cur.fetchone()["s"] is None
    db.rollback()   # 同上:别把只读事务挂着跨过 HTTP 调用
    before = counts()
    r = client.post(f"/api/defensive-geo/run-previews/{prev['previewId']}/confirm",
                    json={"expectedHash": prev["canonicalHash"]},
                    headers=_h(idem="idem-" + uuid.uuid4().hex[:10]))
    after = counts()
    assert r.status_code == 200, r.text
    assert after.commands > before.commands, "正样本臂没真的下单,后面的比较没有意义"


def test_three_confirm_rejections_each_keep_their_own_copy(client, db):
    """🔁 方向锁:confirm 的三个拒绝原因**各自钉死是哪一句**,不只是「互不相同」。

    🔴 「三者两两不等」对**方向对调**结构性失明:把过期与改版的文案换个个儿,
       不等式仍然成立。所以这里逐档钉死到登记表里那一条,
       而期望值按**驱动的那个状态**取键,不读响应里的 code 反查。
    """
    seen = {}

    # ① 题单被改版
    p1 = _make_preview(client)
    pid, rev = _preview_plan_ref(db, p1["previewId"])
    _supersede(db, pid, rev)
    r1 = client.post(f"/api/defensive-geo/run-previews/{p1['previewId']}/confirm",
                     json={"expectedHash": p1["canonicalHash"]},
                     headers=_h(idem="idem-" + uuid.uuid4().hex[:10]))
    seen["superseded"] = r1.json()["detail"]["publicExplanation"]

    # ② preview 过期
    p2 = _make_preview(client)
    with db.cursor() as cur:
        cur.execute("UPDATE defgeo_diagnosis_run_previews SET lifecycle='expired' "
                    " WHERE preview_id=%s", (p2["previewId"],))
    db.commit()
    r2 = client.post(f"/api/defensive-geo/run-previews/{p2['previewId']}/confirm",
                     json={"expectedHash": p2["canonicalHash"]},
                     headers=_h(idem="idem-" + uuid.uuid4().hex[:10]))
    seen["expired"] = r2.json()["detail"]["publicExplanation"]

    # ③ hash 对不上
    p3 = _make_preview(client)
    r3 = client.post(f"/api/defensive-geo/run-previews/{p3['previewId']}/confirm",
                     json={"expectedHash": "0" * 64},
                     headers=_h(idem="idem-" + uuid.uuid4().hex[:10]))
    seen["hash"] = r3.json()["detail"]["publicExplanation"]

    assert seen["superseded"] == user_label("reason", "question_plan_superseded")
    assert seen["expired"] == user_label("reason", "preview_expired")
    assert seen["hash"] == user_label("reason", "snapshot_changed")
    assert len(set(seen.values())) == 3, f"三档压成了 {len(set(seen.values()))} 句:{seen}"
