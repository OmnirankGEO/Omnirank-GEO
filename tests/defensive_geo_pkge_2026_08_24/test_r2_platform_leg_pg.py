"""包E · R2:平台成本腿启用(Owner 已批)+ ④ 同构姊妹洞 + 全类收口。

═══════════════════════════════════════════════════════════════════════
🔴 这一轮真正的发现:洞不止 Owner 点名的那一个,而且比它贵
═══════════════════════════════════════════════════════════════════════
Owner 点名的是 ④ ``_outcome_without_settlement`` 的候选集不含 ``exempt_recorded``
(与 ⑦ 同形,后果是平台单 ``command_state`` 永卡非终态,零资金风险)。

补判据时按"修点也修类"往外扫了一圈,量到的是**另一类**:
迁移 044 的 ``chk_defgeo_pcmd_platform_state`` 逐字规定

    principal_kind <> 'platform_cost_center' OR funding_state = 'exempt_recorded'

而全包有 **12 处** ``bump_status(funding_state=...)`` 调用点会写这一列:
``reconciler`` **6** + ``publish_outbox`` **3** + ``settlement_review`` **3**
(R2 返修实测口径 —— 第一版数成"6 处",漏了 ``publish_outbox`` 那个一行式
``bump_status(..., funding_state="released")`` 与 ``settlement_review`` 里
两个三元式;判据 ``test_r2_41`` 机械枚举,数字不再靠人数)。
另有 3 处在**建单 INSERT** 时写这一列(``publish_funding`` → ``insert_command``),
走的不是 ``bump_status``,按 ``_CONFIRM_FUNDING_STATE`` 天然合规。

平台腿一启用,那 12 处里任何一处碰到平台单都会**违反 CHECK ⇒ 整个事务当场炸**。
我在真库上实测过这一发(不是推理):

    ERROR: new row for relation "defgeo_publish_commands"
           violates check constraint "chk_defgeo_pcmd_platform_state"

也就是说:**一条卡住的平台发布会把整轮收敛(或整轮派发)打死**,
波及的是所有租户的所有命令。这比 ④ 那个"永卡非终态"贵一个量级。

修法不是在 12 个调用点各写一遍守卫(「同一谓词写两处 ⇒ 必有一处没人验」,
12 处就是 12 次机会写错),而是把规则写进**写这一列的唯一那条语句**里
(``store.bump_status`` 的 CASE)。**单点修法的正确性前提**是"确实只有那一条" ——
``test_r2_41`` 正是钉这一条:全包只有 ``store.py`` 的 SQL 会 ``SET funding_state``。
本文件 §E 逐条驱动这一类。
"""

from __future__ import annotations

import ast
import os
import uuid
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from services.defensive_geo import payer_classification as _payer
from services.defensive_geo.publish import execution_budget_policy as _policy
from services.defensive_geo.publish import publish_outbox as _dispatch
from services.defensive_geo.publish import publish_worker as _worker
from services.defensive_geo.publish import store as _store

from tests.defensive_geo_pkge_2026_08_24 import _seed
from tests.defensive_geo_pkge_2026_08_24.conftest import connect
from tests.defensive_geo_pkge_2026_08_24.test_real_chain_pg import _run

ROOT = Path(__file__).resolve().parents[2]
RECON_SRC = ROOT / "services" / "defensive_geo" / "publish" / "reconciler.py"


@pytest.fixture(scope="module", autouse=True)
def _base(_schema) -> Iterator[None]:                     # noqa: ANN001
    """🔴 平台账号走**环境变量**(``commercial_service_routing._configured_user_id``
    读的就是它)。设完必须还原 —— 进程内其它模块共享同一个环境。
    """
    import auth.brand_access  # noqa: F401,PLC0415

    _seed.install_base_rows()
    before = os.environ.get("PLATFORM_DIRECT_SERVICE_USER_ID")
    os.environ["PLATFORM_DIRECT_SERVICE_USER_ID"] = str(_seed.PLATFORM_ACCOUNT)
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("PLATFORM_DIRECT_SERVICE_USER_ID", None)
        else:
            os.environ["PLATFORM_DIRECT_SERVICE_USER_ID"] = before


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    from api import defensive_publish_api

    app = FastAPI()
    app.include_router(defensive_publish_api.router)

    @app.middleware("http")
    async def _inject_user(request: Request, call_next):  # noqa: ANN001
        who = request.headers.get("X-Test-Identity", "a")
        request.state.user = dict(_seed.IDENTITIES[who])
        return await call_next(request)

    with TestClient(app) as c:
        yield c


# ══════════════════════════════════════════════════════════════════════════
# A. 件二① —— 判别位搬家 = 行为零变化
# ══════════════════════════════════════════════════════════════════════════
def _legacy_classify(user: dict[str, Any] | None,
                     platform_uid: int | None) -> tuple[str, str, None]:
    """搬家**之前** ``api/defensive_geo_api._classify_payer`` 的函数体,逐字复刻。

    🔴 这是等价判据的 oracle。抽取类改动唯一守得住的方式就是留一份旧实现
       当对照 —— 拿新实现自己跟自己比,等于没比。
    """
    u = user or {}
    if bool(u.get("is_admin")):
        return ("admin_platform_ledger", "platform_cost_center", None)
    actor_id = u.get("user_id")
    if platform_uid is not None and actor_id and int(actor_id) == platform_uid:
        return ("admin_platform_ledger", "platform_cost_center", None)
    return ("personal_wallet", "personal", None)


class _Req:
    """最小 Request 替身 —— 只有 ``state.user`` 一件事。"""

    class _S:
        pass

    def __init__(self, user: dict[str, Any] | None) -> None:
        self.state = _Req._S()
        self.state.user = user


#: 身份轴 × 平台账号轴的**笛卡尔积**。举例式判据在这种"两个轴互相影响"的
#: 判别上必漏(本仓 2026-08-14 记过)。
_USER_AXIS: tuple[tuple[str, dict[str, Any] | None], ...] = (
    ("admin", {"user_id": 9703, "is_admin": True}),
    ("ordinary", {"user_id": _seed.TENANT_A, "is_admin": False}),
    ("platform_account_self", {"user_id": _seed.PLATFORM_ACCOUNT, "is_admin": False}),
    ("admin_and_platform", {"user_id": _seed.PLATFORM_ACCOUNT, "is_admin": True}),
    ("no_user_id", {"is_admin": False}),
    ("empty", {}),
    ("none", None),
)
_PLATFORM_UID_AXIS: tuple[int | None, ...] = (None, _seed.PLATFORM_ACCOUNT, 424242)


@pytest.mark.parametrize("label,user", _USER_AXIS, ids=[u[0] for u in _USER_AXIS])
@pytest.mark.parametrize("platform_uid", _PLATFORM_UID_AXIS)
def test_r2_01_extracted_classifier_is_equivalent_to_the_old_body(
    label: str, user: dict[str, Any] | None, platform_uid: int | None,
) -> None:
    """纯判别层:新实现 ≡ 旧函数体,逐格。"""
    got = _payer.classify_payer(
        is_admin=bool((user or {}).get("is_admin")),
        user_id=(user or {}).get("user_id"),
        platform_direct_user_id=platform_uid,
    )
    assert tuple(got) == _legacy_classify(user, platform_uid), label


@pytest.mark.parametrize("label,user", _USER_AXIS, ids=[u[0] for u in _USER_AXIS])
def test_r2_02_endpoint_adapter_is_equivalent_to_the_old_body(
    label: str, user: dict[str, Any] | None,
) -> None:
    """端点侧适配器:``_classify_payer(request)`` 的返回值逐位同形、逐格同值。

    平台账号取当前环境里那一个(本模块 fixture 设成了 ``PLATFORM_ACCOUNT``)——
    与旧实现读的是同一个来源。
    """
    from api import defensive_geo_api as _api

    got = _api._classify_payer(_Req(user))                # noqa: SLF001
    assert isinstance(got, tuple) and len(got) == 3, f"返回形状变了:{got!r}"
    assert got == _legacy_classify(user, _seed.PLATFORM_ACCOUNT), label


def test_r2_03_classifier_denominator_and_discriminating_power() -> None:
    """分母 + 判别力:只有两格,且两格**确实都可达**。"""
    census = _payer.census()
    reachable = {
        tuple(_payer.classify_payer(is_admin=bool((u or {}).get("is_admin")),
                                    user_id=(u or {}).get("user_id"),
                                    platform_direct_user_id=p))[0]
        for _, u in _USER_AXIS for p in _PLATFORM_UID_AXIS
    }
    assert reachable == set(census["classifiablePolicies"]) == {
        "admin_platform_ledger", "personal_wallet"}, (
        f"判别结果塌成了一格:{reachable} —— 那样上面所有等价断言都没有判别力")


def test_r2_04_worker_side_admin_resolution_reads_roles_not_a_column() -> None:
    """worker 侧取身份:**现查角色**。``users`` 表没有 ``is_admin`` 列。"""
    conn = connect()
    try:
        cur = conn.cursor()
        assert _payer.is_admin_user(cur, _seed.TENANT_ADMIN) is True
        assert _payer.is_admin_user(cur, _seed.TENANT_A) is False
        assert _payer.is_admin_user(cur, _seed.PLATFORM_ACCOUNT) is False, (
            "平台直营账号被判成了 admin —— 它一旦是 admin 就撞 billing 免单旁路")
        assert _payer.is_admin_user(cur, None) is False
        assert _payer.classify_user_id(cur, _seed.TENANT_ADMIN).funding_policy == (
            "admin_platform_ledger")
        assert _payer.classify_user_id(cur, _seed.TENANT_A).funding_policy == "personal_wallet"
        conn.rollback()
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
# B. 件二② —— 普通身份路径**字节级**不变
# ══════════════════════════════════════════════════════════════════════════
def test_r2_10_ordinary_path_is_byte_identical_when_policy_is_passed_explicitly() -> None:
    """显式传 ``personal_wallet`` 与吃默认值**逐字段相同**,含确定性 id 与 hash。

    🔴 「默认参数 = 旧行为不变」必须落到字节,不能只说"应该一样"。
       ``derive`` 的 id 是整份推导的 sha256 前缀,所以两个 draft 的 id 相等
       就等于"推导里一个字节都没动"。
    """
    ctx = _seed.seed_accepted_snapshot(activate=False)
    accepted = int(ctx["acceptedSnapshotId"])
    conn = connect()
    try:
        cur = conn.cursor()
        old = _policy.derive(cur, tenant_owner_id=_seed.TENANT_A,
                             accepted_snapshot_id=accepted, payer_user_id=_seed.TENANT_A)
        new = _policy.derive(cur, tenant_owner_id=_seed.TENANT_A,
                             accepted_snapshot_id=accepted,
                             funding_policy="personal_wallet", payer_user_id=_seed.TENANT_A)
        plat = _policy.derive(cur, tenant_owner_id=_seed.TENANT_A,
                              accepted_snapshot_id=accepted,
                              funding_policy="admin_platform_ledger",
                              payer_user_id=_seed.TENANT_A)
        conn.rollback()
    finally:
        conn.close()

    assert tuple(new) == tuple(old), (
        f"显式传默认值改变了推导:\n old={old}\n new={new}")
    assert new.execution_budget_snapshot_id == old.execution_budget_snapshot_id
    assert new.budget_hash == old.budget_hash

    # 判别力:换成平台腿必须**不同** —— 否则"两者相同"可能只是因为
    # ``fundingPolicy`` 根本没进 digest,那样上面那条就是恒真的。
    assert plat.execution_budget_snapshot_id != old.execution_budget_snapshot_id
    assert plat.budget_hash != old.budget_hash
    assert plat.funding_policy == "admin_platform_ledger"


def test_r2_11_materializer_signs_the_same_row_for_an_ordinary_tenant() -> None:
    """真链字节级:物化器落库那一行的 id **等于**"吃默认值"推出来的 id。"""
    ctx = _seed.seed_accepted_snapshot(activate=True)
    accepted = int(ctx["acceptedSnapshotId"])

    conn = connect()
    try:
        cur = conn.cursor()
        expected = _policy.derive(cur, tenant_owner_id=_seed.TENANT_A,
                                  accepted_snapshot_id=accepted, payer_user_id=_seed.TENANT_A)
        conn.rollback()
    finally:
        conn.close()

    from services.defensive_geo import activation_materializer as _mat

    out = _mat.materialize_pending()
    assert out["materialized"] >= 1, out

    row = _budget_row(accepted)
    assert row is not None, "物化器没落库"
    assert str(row["funding_policy"]) == "personal_wallet"
    assert str(row["execution_budget_snapshot_id"]) == expected.execution_budget_snapshot_id, (
        "普通身份这一行不再与旧默认逐字节相同 —— 「默认参数=旧行为不变」被打破")


def _budget_row(accepted_snapshot_id: int) -> dict[str, Any] | None:
    conn = connect()
    try:
        cur = conn.cursor()
        # 🔴 表名从 store 的常量取,不手抄(第一版手抄错了,当场 UndefinedTable ——
        #    SQL 四维度核验第 1 条:列名/表名肉眼核对真库,别照记忆写)。
        cur.execute(
            f"SELECT * FROM {_store.BUDGET_TABLE} WHERE accepted_snapshot_id = %s",
            (int(accepted_snapshot_id),))
        row = cur.fetchone()
        conn.rollback()
    finally:
        conn.close()
    return dict(row) if row else None


# ══════════════════════════════════════════════════════════════════════════
# C. 件二③ —— 真链两臂
# ══════════════════════════════════════════════════════════════════════════
def _ready(name: str, *, who: str, tenant: int, brand: int,
           publications: int = 2) -> dict[str, Any]:
    from services.defensive_geo import activation_materializer as _mat

    ctx = _seed.seed_accepted_snapshot(tenant=tenant, brand=brand,
                                       publications=publications, activate=True)
    out = _mat.materialize_pending()
    assert out["materialized"] >= 1, f"物化器没物化:{out}"
    revision_id, article_hash = _seed.seed_article(
        brand=ctx["brandId"], body=f"包E R2 正文 {name} · 这是一段可以发布的稿子。")
    ctx.update({
        "planItemKey": f"r2_{name}",
        "articleRevisionId": revision_id,
        "expectedArticleHash": article_hash,
        "serviceProjectionId": _policy.service_projection_id_for(ctx["acceptedSnapshotId"]),
        "who": who,
    })
    return ctx


def _confirmed(client: TestClient, ctx: dict[str, Any]) -> dict[str, Any]:
    head = {"X-Test-Identity": ctx["who"]}
    resp = client.post(
        "/api/defensive-geo/publish/decision-snapshots/preview",
        headers={**head, "Idempotency-Key": "r2-p-" + uuid.uuid4().hex},
        json={
            "planItemKey": ctx["planItemKey"],
            "articleRevisionId": ctx["articleRevisionId"],
            "expectedArticleHash": ctx["expectedArticleHash"],
            "acceptedSnapshotId": ctx["acceptedSnapshotId"],
            "serviceProjectionId": ctx["serviceProjectionId"],
            "brandId": ctx["brandId"],
        },
    )
    assert resp.status_code == 200, f"preview 失败:{resp.text[:900]}"
    frozen = resp.json()["snapshotResponse"]["snapshot"]
    got = client.post(
        f"/api/defensive-geo/publish/decision-snapshots/{frozen['decisionSnapshotId']}/confirm",
        headers={**head, "Idempotency-Key": "r2-c-" + uuid.uuid4().hex},
        json={"expectedHash": frozen["canonicalHash"],
              "expectedVersion": frozen["snapshotVersion"]},
    )
    assert got.status_code == 200, f"confirm 失败:{got.text[:900]}"
    ctx["publishCommandId"] = got.json()["publishCommandId"]
    return ctx


def test_r2_20_admin_identity_runs_the_whole_chain_on_the_platform_leg(
        client: TestClient) -> None:
    """🔴 平台腿全链:确认 → 冻结(落平台账)→ 派发 → 回执 → 收敛终态。

    Owner 口径逐条对上:
      · ``fundingState = exempt_recorded``;
      · **客户钱包零冻结**(她的余额一个数都不动);
      · 经 R2 修好的候选集**收敛到终态**(``command_state = completed``)。
    """
    ctx = _ready("adm", who="adminowner",
                 tenant=_seed.TENANT_ADMIN, brand=_seed.BRAND_ADMIN)

    budget = _budget_row(int(ctx["acceptedSnapshotId"]))
    assert budget is not None and str(budget["funding_policy"]) == "admin_platform_ledger", (
        f"物化器没给 admin 品牌主签平台腿预算:{budget}")

    tenant_before = _seed.wallet(_seed.TENANT_ADMIN)
    platform_before = _seed.wallet(_seed.PLATFORM_ACCOUNT)

    _confirmed(client, ctx)
    cmd = _seed.command_row(ctx["publishCommandId"])
    exact = int(cmd["exact_settlement_points"])
    assert exact > 0, "这一单是 0 价,平台腿那条冻结路径根本没被驱动"

    assert str(cmd["funding_policy"]) == "admin_platform_ledger"
    assert str(cmd["principal_kind"]) == "platform_cost_center"
    assert str(cmd["funding_state"]) == "exempt_recorded"
    assert int(cmd["payer_user_id"]) == _seed.PLATFORM_ACCOUNT, (
        f"付款方不是平台直营账号:{cmd['payer_user_id']}")
    assert cmd["freeze_id"] is not None, (
        "平台腿没有拿到 freeze_id —— FIN-06「平台账真实记账」没有发生")

    # 客户钱包**一个数都不动**;平台账真冻。
    assert _seed.wallet(_seed.TENANT_ADMIN) == tenant_before, (
        f"exempt_recorded 却动了客户钱包:{tenant_before} → {_seed.wallet(_seed.TENANT_ADMIN)}")
    platform_after = _seed.wallet(_seed.PLATFORM_ACCOUNT)
    assert platform_after["frozen_points"] == platform_before["frozen_points"] + exact, (
        f"平台账没有真冻:{platform_before} → {platform_after}(exact={exact})")

    # 派发 → 回执 → 核实 → 收敛
    _run(_worker.dispatch_pending(limit=10, provider_call=_accepted("SN-R2-ADM")))
    _mark_verified(ctx["publishCommandId"])
    actions = _run(_worker.reconcile_tick(limit=200))

    mine = [a for a in actions["items"] if a["commandId"] == ctx["publishCommandId"]]
    assert any(a["kind"] == "commit" for a in mine), (
        f"平台腿没有走 ④ 的 commit:{mine} —— 候选集或守卫有一边把它漏了")
    final = _seed.command_row(ctx["publishCommandId"])
    assert str(final["command_state"]) == "completed", (
        f"平台单没收敛到终态:{final['command_state']} —— 客户面会永远停在「处理中」")
    assert str(final["funding_state"]) == "exempt_recorded", final["funding_state"]
    assert final["settled_at"] is not None, "结算了却没落 settled_at"
    assert _seed.wallet(_seed.TENANT_ADMIN) == tenant_before, "收敛阶段动了客户钱包"

    # ═══════════════════════════════════════════════════════════════════
    # 🔴 [R3 · Owner 批口径①] 平台账那笔冻结**真的被 commit 了**
    # ═══════════════════════════════════════════════════════════════════
    # 上一轮这里是一条「现状钉住」:平台账 frozen_points 只增不减,并写明
    # 「Owner 拍板 commit 之后这一行会红,那正是它该做的」。口径拍下来了,
    # 它按自己的设计翻了红,现在改向 —— 这就是现状锁存在的意义。
    #
    # commit 的钱包语义(``middleware/billing`` 逐字):frozen -= exact,
    # paid **不动**(paid 在 freeze 那一刻就已经扣掉了)。所以:
    #   · frozen 回到冻结前 = 这笔占用释放了;
    #   · paid 比冻结前少 exact = 平台**真花掉了**这笔钱。
    platform_final = _seed.wallet(_seed.PLATFORM_ACCOUNT)
    assert platform_final["frozen_points"] == platform_before["frozen_points"], (
        f"平台账的冻结没有被 commit 掉:{platform_before} → {platform_final}(exact={exact})")
    assert platform_final["paid_points"] == platform_before["paid_points"] - exact, (
        f"commit 之后平台账没有真花掉这笔钱:{platform_before} → {platform_final}")


def test_r2_23_platform_leg_release_arm_refunds_the_platform_account(
        client: TestClient) -> None:
    """🔴 [R3] 平台腿的 **release 臂**:canonical 权威零接单 ⇒ 平台账全额退回。

    与上一条(commit 臂)成对 —— 只有 commit 臂的话,「平台腿会结算」
    可能只在一个方向上成立。release 的钱包语义是 frozen -= exact **且** paid += exact。
    """
    ctx = _ready("rel", who="adminowner",
                 tenant=_seed.TENANT_ADMIN, brand=_seed.BRAND_ADMIN)
    _confirmed(client, ctx)
    cid = ctx["publishCommandId"]
    exact = int(_seed.command_row(cid)["exact_settlement_points"])
    platform_before = _seed.wallet(_seed.PLATFORM_ACCOUNT)
    tenant_before = _seed.wallet(_seed.TENANT_ADMIN)

    conn = connect()
    try:
        cur = conn.cursor()
        _store.bump_status(cur, publish_command_id=cid,
                           canonical_publication_state="rejected_no_effect")
        conn.commit()
    finally:
        conn.close()

    actions = _run(_worker.reconcile_tick(limit=200))
    mine = [a for a in actions["items"] if a["commandId"] == cid]
    assert any(a["kind"] == "release" for a in mine), f"平台腿没有走 release:{mine}"

    cmd = _seed.command_row(cid)
    assert str(cmd["funding_state"]) == "exempt_recorded", cmd["funding_state"]
    assert cmd["settled_at"] is not None
    platform_after = _seed.wallet(_seed.PLATFORM_ACCOUNT)
    assert platform_after["frozen_points"] == platform_before["frozen_points"] - exact
    assert platform_after["paid_points"] == platform_before["paid_points"] + exact, (
        f"权威零接单却没退回平台账原池:{platform_before} → {platform_after}")
    assert _seed.wallet(_seed.TENANT_ADMIN) == tenant_before, "release 动了客户钱包"


def test_r2_24_a_settled_platform_leg_is_never_settled_twice(
        client: TestClient) -> None:
    """🔴 [R3] 平台腿的 ``fundingState`` 是常量 ⇒ **掉不出候选集**,必须靠 ``settled_at``。

    没有终态标记的话,同构之后每一轮收敛都会把同一条平台单再结算一遍
    (``commit_freeze`` 幂等,钱不会重复动,但动作/告警会无限重复)。
    判据 = **连跑两轮**,第二轮对这条命令零动作。
    """
    ctx = _ready("twice", who="adminowner",
                 tenant=_seed.TENANT_ADMIN, brand=_seed.BRAND_ADMIN)
    _confirmed(client, ctx)
    cid = ctx["publishCommandId"]
    _run(_worker.dispatch_pending(limit=10, provider_call=_accepted("SN-R2-TWICE")))
    _mark_verified(cid)

    first = [a for a in _run(_worker.reconcile_tick(limit=200))["items"]
             if a["commandId"] == cid]
    assert first, "第一轮就没结算 —— 这条判据的前提不成立"
    after_first = _seed.wallet(_seed.PLATFORM_ACCOUNT)

    second = [a for a in _run(_worker.reconcile_tick(limit=200))["items"]
              if a["commandId"] == cid]
    assert not second, (
        f"结算过的平台单在第二轮又被处置了一次:{second} —— "
        "它的 fundingState 是常量,掉不出候选集,只能靠 settled_at 收口")
    assert _seed.wallet(_seed.PLATFORM_ACCOUNT) == after_first, "第二轮又动了平台账"


def _review_arm(client: TestClient, name: str, decision: str,
                canonical: str) -> tuple[str, int, dict[str, int]]:
    ctx = _ready(name, who="adminowner",
                 tenant=_seed.TENANT_ADMIN, brand=_seed.BRAND_ADMIN)
    _confirmed(client, ctx)
    cid = ctx["publishCommandId"]
    exact = int(_seed.command_row(cid)["exact_settlement_points"])
    conn = connect()
    try:
        cur = conn.cursor()
        # 🔴 造的是**收敛器③ 真会留下的那一格**:平台腿的"待人工核验"
        #    落在 ``command_state`` 上(``funding_state`` 对它是常量,
        #    写了也会被 ``bump_status`` 的 CASE 挡掉 —— 那正是设计)。
        _store.bump_status(cur, publish_command_id=cid,
                           canonical_publication_state=canonical,
                           command_state="settlement_pending",
                           funding_state="pending_reconciliation")
        conn.commit()
    finally:
        conn.close()
    before = _seed.wallet(_seed.PLATFORM_ACCOUNT)

    from services.defensive_geo.publish import settlement_review as _rv

    conn = connect()
    try:
        cur = conn.cursor()
        _run(_rv.apply_admin_action(
            cur, publish_command_id=cid, action=decision,
            admin_user_id=9703, reason=f"包E R3 判据:{decision}"))
        conn.commit()
    finally:
        conn.close()
    return cid, exact, before


def test_r2_25_admin_review_commit_settles_the_platform_freeze(
        client: TestClient) -> None:
    """🔴 [R3] Z-1 人工处置**确认已执行**:平台账那笔冻结同样 commit。

    人工处置与收敛器是**同一个决定,换了个决定人**。R2 时这里挡着平台腿,
    留着它就等于口径①只落实了一半 —— 走人工出口的那些单子,冻结照样悬着。
    """
    cid, exact, before = _review_arm(client, "rvc", "admin_commit",
                                     "verified_published")
    cmd = _seed.command_row(cid)
    assert str(cmd["funding_state"]) == "exempt_recorded", cmd["funding_state"]
    assert str(cmd["command_state"]) == "completed"
    after = _seed.wallet(_seed.PLATFORM_ACCOUNT)
    assert after["frozen_points"] == before["frozen_points"] - exact
    assert after["paid_points"] == before["paid_points"], (
        f"人工 commit 却把钱退了回去:{before} → {after}")


def test_r2_26_admin_review_release_refunds_the_platform_freeze(
        client: TestClient) -> None:
    """🔴 [R3] Z-1 人工处置**确认未执行**:平台账全额退回(与上一条成对)。"""
    cid, exact, before = _review_arm(client, "rvr", "admin_release",
                                     "failed_no_effect")
    cmd = _seed.command_row(cid)
    assert str(cmd["funding_state"]) == "exempt_recorded", cmd["funding_state"]
    after = _seed.wallet(_seed.PLATFORM_ACCOUNT)
    assert after["frozen_points"] == before["frozen_points"] - exact
    assert after["paid_points"] == before["paid_points"] + exact, (
        f"人工确认未执行却没退回平台账:{before} → {after}")


def test_r2_43_no_settlement_call_site_still_excludes_the_platform_leg() -> None:
    """🔴 [R3 · Owner 点名③] 结算原语调用点 census —— **7 处一个不许再挡平台单**。

    此前 7 处**全部**用 ``if not is_platform_cost(...)`` 或平台早退分支把平台腿挡在外面。
    口径① 之后一处都不该再挡。这条判据机械枚举 ``commit_exact`` / ``release_exact``
    的每一个调用点,断言它**外层没有** ``is_platform_cost`` 守卫。

    行为臂**七处逐处点名**:④ commit(``test_r2_20``)· ④ release(``test_r2_23``)·
    ④ preserve(与 ④ 同一条路径)· ⑦ release(``test_24``)·
    Z-1 人工两臂(``test_r2_25/26``)· 广告法门 release(``test_r4_01a/01b`` 两腿各一条)。

    🔴 [R4 订正] 上一版这里写着「广告法门驱动不到:规则未签发 ⇒ 门恒 advisory」——
       **那句是假的**,而且一行就能验:

           python -c "from services.defensive_geo.publish import legal_gate as l; print(l.gate_mode())"
           → blocking          (defgeo.publish.legal_catalog 1.0.0,Owner 2026-07-23 签发,早于本包的底)

       我连着三轮把一个**没跑过的推断**当成了取证结论写进交付单。
       教训写死在这里:**「不可达」是一句强断言,必须附可执行取证** ——
       它和"我没找到调用点"完全不是一回事。
    """
    pkg = ROOT / "services" / "defensive_geo"
    sites: dict[str, list[str]] = {}
    guarded: list[str] = []
    for path in sorted(pkg.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("commit_exact", "release_exact")):
                    continue
                sites.setdefault(rel, []).append(fn.name)
                # 外层是否套着 ``if (not) is_platform_cost(...)``
                for parent in ast.walk(fn):
                    if not isinstance(parent, ast.If):
                        continue
                    test_src = ast.dump(parent.test)
                    if "is_platform_cost" not in test_src:
                        continue
                    if any(node is n for stmt in parent.body for n in ast.walk(stmt)):
                        guarded.append(f"{rel}::{fn.name}")

    total = sum(len(v) for v in sites.values())
    assert total >= 7, f"只扫到 {total} 个结算调用点 —— 探针写废了({sites})"
    assert guarded == [], (
        f"这些结算调用点仍然把平台腿挡在外面:{sorted(set(guarded))} —— "
        "口径① 之后平台账的冻结必须与钱包腿同构地结算,挡住 = 冻结永远悬着")


def test_r2_21_ordinary_identity_still_runs_the_personal_wallet_leg(
        client: TestClient) -> None:
    """反向对照臂:普通身份**一点没变** —— 个人钱包腿、真冻结、平台账零动作。"""
    platform_before = _seed.wallet(_seed.PLATFORM_ACCOUNT)
    ctx = _ready("ord", who="a", tenant=_seed.TENANT_A, brand=_seed.BRAND_A)

    budget = _budget_row(int(ctx["acceptedSnapshotId"]))
    assert budget is not None and str(budget["funding_policy"]) == "personal_wallet"

    before = _seed.wallet(_seed.TENANT_A)
    _confirmed(client, ctx)
    cmd = _seed.command_row(ctx["publishCommandId"])
    exact = int(cmd["exact_settlement_points"])

    assert str(cmd["funding_policy"]) == "personal_wallet"
    assert str(cmd["principal_kind"]) == "personal"
    assert str(cmd["funding_state"]) == "frozen"
    assert int(cmd["payer_user_id"]) == _seed.TENANT_A
    after = _seed.wallet(_seed.TENANT_A)
    assert after["frozen_points"] == before["frozen_points"] + exact
    assert after["paid_points"] == before["paid_points"] - exact
    assert _seed.wallet(_seed.PLATFORM_ACCOUNT) == platform_before, (
        "普通身份的单把钱冻到平台账上了 —— 判别位串格")


# ══════════════════════════════════════════════════════════════════════════
# D. 件一 —— ④ 同构姊妹洞:正反两条
# ══════════════════════════════════════════════════════════════════════════
def test_r2_12_non_platform_leg_outside_the_settlement_states_is_left_alone(
        client: TestClient) -> None:
    """④ 加宽候选集的**反向对照**:非平台腿却停在 ``exempt_recorded`` ⇒ 不结算。

    (顺手多修一处 = 顺手多欠一条判据。分岔点就是"非平台腿 + 非结算态"。)
    """
    ctx = _ready("sib", who="a", tenant=_seed.TENANT_A, brand=_seed.BRAND_A)
    _confirmed(client, ctx)
    cid = ctx["publishCommandId"]
    before = _seed.wallet(_seed.TENANT_A)

    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE {_store.COMMAND_TABLE} SET funding_state='exempt_recorded', "
            f"canonical_publication_state='verified_published' "
            f"WHERE publish_command_id = %s", (cid,))
        conn.commit()
    finally:
        conn.close()

    actions = _run(_worker.reconcile_tick(limit=200))
    mine = [a for a in actions["items"] if a["commandId"] == cid]
    assert not [a for a in mine if a["kind"] in ("commit", "release", "platform_converge")], (
        f"非平台腿的异常形状被自动结算了:{mine}")
    assert _seed.wallet(_seed.TENANT_A) == before


def test_r2_13_platform_leg_missing_outbox_is_requeued(client: TestClient) -> None:
    """② 的候选集也补了 ``exempt_recorded``:平台单丢了 outbox 必须补得回来。"""
    ctx = _ready("rq", who="adminowner",
                 tenant=_seed.TENANT_ADMIN, brand=_seed.BRAND_ADMIN)
    _confirmed(client, ctx)
    cid = ctx["publishCommandId"]

    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(f"DELETE FROM {_store.OUTBOX_TABLE} WHERE publish_command_id = %s", (cid,))
        cur.execute(
            f"UPDATE {_store.COMMAND_TABLE} SET created_at = NOW() - INTERVAL '2 hours' "
            f"WHERE publish_command_id = %s", (cid,))
        conn.commit()
    finally:
        conn.close()

    actions = _run(_worker.reconcile_tick(limit=200))
    assert any(a["kind"] == "requeue_outbox" and a["commandId"] == cid
               for a in actions["items"]), (
        f"平台单丢了 outbox 补不回来:{[a for a in actions['items'] if a['commandId'] == cid]}")
    assert len(_seed.outbox_rows(cid)) == 1
    assert str(_seed.command_row(cid)["funding_state"]) == "exempt_recorded"


# ══════════════════════════════════════════════════════════════════════════
# E. 全类 —— 每一条会写 funding_state 的路径,用平台单驱动一遍
# ══════════════════════════════════════════════════════════════════════════
def _platformize(command_id: str) -> None:
    """把一条真确认出来的命令翻成平台腿形状(只翻判别用的那两列)。"""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE {_store.COMMAND_TABLE} SET funding_policy='admin_platform_ledger', "
            f"principal_kind='platform_cost_center', funding_state='exempt_recorded' "
            f"WHERE publish_command_id = %s", (command_id,))
        conn.commit()
    finally:
        conn.close()


def test_r2_30_bump_status_never_moves_a_platform_legs_funding_state(
        client: TestClient) -> None:
    """🔴 收口点本身:``bump_status`` 对平台腿**不写** ``funding_state``,其余列照写。

    这一条打的是"那条 UPDATE 语句"。没有它,下面 §E 的每一条都只能证明
    "没炸",证明不了"是被这道闸挡住的"。
    """
    ctx = _ready("bs", who="a", tenant=_seed.TENANT_A, brand=_seed.BRAND_A)
    _confirmed(client, ctx)
    cid = ctx["publishCommandId"]
    _platformize(cid)

    conn = connect()
    try:
        cur = conn.cursor()
        fresh = _store.bump_status(
            cur, publish_command_id=cid,
            funding_state="pending_reconciliation",     # ← 会违反 CHECK 的那一发
            command_state="settlement_pending",
            status_reason="R2:全类收口",
        )
        conn.commit()
    finally:
        conn.close()

    assert fresh is not None
    assert str(fresh["funding_state"]) == "exempt_recorded", (
        "平台腿的 fundingState 被改动了 —— 迁移 044 的 CHECK 会当场炸整个事务")
    # 其余列必须照常落地 —— 挡的是"这条腿上没有定义的资金方向",不是调用方的意图。
    assert str(fresh["command_state"]) == "settlement_pending"
    assert "R2" in str(fresh["status_reason"])


def test_r2_31_non_platform_leg_funding_state_still_moves(client: TestClient) -> None:
    """反向对照:非平台腿照常改 —— 否则上面那条可能只是因为**谁都改不动**。"""
    ctx = _ready("bs2", who="a", tenant=_seed.TENANT_A, brand=_seed.BRAND_A)
    _confirmed(client, ctx)
    cid = ctx["publishCommandId"]

    conn = connect()
    try:
        cur = conn.cursor()
        fresh = _store.bump_status(
            cur, publish_command_id=cid, funding_state="pending_reconciliation")
        conn.commit()
    finally:
        conn.close()
    assert fresh is not None and str(fresh["funding_state"]) == "pending_reconciliation"


def test_r2_32_stuck_platform_leg_does_not_kill_the_whole_reconcile_tick(
        client: TestClient) -> None:
    """③ 外调后久无回执:平台单**不能**把整轮收敛打死。

    这是本轮最贵的那一格。修之前它是:
      ``ERROR: violates check constraint "chk_defgeo_pcmd_platform_state"``
    ⇒ 整个 tick 抛出 ⇒ **所有租户**的收敛当轮全废。
    """
    victim = _ready("v", who="a", tenant=_seed.TENANT_A, brand=_seed.BRAND_A)
    _confirmed(client, victim)

    ctx = _ready("stuck", who="adminowner",
                 tenant=_seed.TENANT_ADMIN, brand=_seed.BRAND_ADMIN)
    _confirmed(client, ctx)
    cid = ctx["publishCommandId"]
    _run(_worker.dispatch_pending(limit=10, provider_call=_accepted("SN-R2-STUCK")))

    conn = connect()
    try:
        cur = conn.cursor()
        _store.bump_status(cur, publish_command_id=cid,
                           canonical_publication_state="queued")
        cur.execute(
            f"UPDATE {_store.COMMAND_TABLE} SET external_start_at = NOW() - INTERVAL '3 hours' "
            f"WHERE publish_command_id = %s", (cid,))
        conn.commit()
    finally:
        conn.close()

    actions = _run(_worker.reconcile_tick(limit=200))     # ← 不许抛
    assert any(a["kind"] == "hold_unknown" and a["commandId"] == cid
               for a in actions["items"]), actions
    cmd = _seed.command_row(cid)
    assert str(cmd["canonical_publication_state"]) == "unknown"
    assert str(cmd["funding_state"]) == "exempt_recorded", cmd["funding_state"]
    assert str(cmd["command_state"]) == "settlement_pending", (
        "「需要人工核验」这件事没落到 commandState 上 —— 那才是平台腿表达它的地方")
    # 同一轮里的普通单照常被处理 —— 证明这一轮真的跑完了,不是提前抛掉了。
    assert _seed.command_row(victim["publishCommandId"]) is not None


def test_r2_33_mirror_conflict_on_a_platform_leg_does_not_abort(
        client: TestClient) -> None:
    """⑤ 镜像冲突隔离:平台单同样不能炸(它也会写 ``funding_state='quarantined'``)。"""
    ctx = _ready("mc", who="adminowner",
                 tenant=_seed.TENANT_ADMIN, brand=_seed.BRAND_ADMIN)
    _confirmed(client, ctx)
    cid = ctx["publishCommandId"]

    conn = connect()
    try:
        cur = conn.cursor()
        _store.bump_status(cur, publish_command_id=cid,
                           canonical_publication_state="conflict")
        conn.commit()
    finally:
        conn.close()

    actions = _run(_worker.reconcile_tick(limit=200))     # ← 不许抛
    cmd = _seed.command_row(cid)
    assert str(cmd["funding_state"]) == "exempt_recorded", cmd["funding_state"]
    assert str(cmd["command_state"]) == "quarantined", (
        f"平台单的镜像冲突没有进 Z-1 队列:{cmd['command_state']} · {actions}")


def test_r2_34_unknown_provider_outcome_on_a_platform_leg_does_not_abort(
        client: TestClient) -> None:
    """派发器那两处:provider 结果未知时也会写 ``pending_reconciliation``。

    平台单走到这里同样不许炸 —— 炸的话**整轮派发**都废,波及所有租户。
    """
    ctx = _ready("unk", who="adminowner",
                 tenant=_seed.TENANT_ADMIN, brand=_seed.BRAND_ADMIN)
    _confirmed(client, ctx)
    cid = ctx["publishCommandId"]

    _run(_worker.dispatch_pending(limit=10, provider_call=_explodes()))   # ← 不许抛
    cmd = _seed.command_row(cid)
    assert str(cmd["funding_state"]) == "exempt_recorded", cmd["funding_state"]
    assert str(cmd["canonical_publication_state"]) in ("unknown", "queued", "submitting"), cmd
    assert _seed.wallet(_seed.TENANT_ADMIN)["frozen_points"] >= 0


def _platform_branch_funcs(tree: "ast.AST") -> list[Any]:
    """真正**调用** ``is_platform_cost`` 的函数(不算 docstring / 注释里提到的)。"""
    import ast

    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "is_platform_cost" for n in ast.walk(fn)):
            out.append(fn)
    return out


def _sql_literals(fn: "ast.AST") -> list[str]:
    """函数体里**真正的字符串字面量**(SQL 就长在这里)。

    🔴 [R2 返修 · Review 点名] 上一版是 ``"exempt_recorded" in 函数源码`` ——
       **裸子串**,一句注释("这里不含 exempt_recorded")就能让它恒绿。
       结构锚必须打在承重的那个东西上:候选集在 SQL 字符串里,不在注释里。
       docstring 是函数体的第一条 ``Expr``,这里一并剔掉。
    """
    import ast

    lits: list[str] = []
    body = list(getattr(fn, "body", []))
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]                                   # 去 docstring
    for stmt in body:
        for n in ast.walk(stmt):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                lits.append(n.value)
            elif isinstance(n, ast.JoinedStr):            # f-string 的字面片段
                lits.extend(p.value for p in n.values
                            if isinstance(p, ast.Constant) and isinstance(p.value, str))
    return lits


def _candidate_set_admits_platform(fn: "ast.AST") -> bool:
    """该函数的 SQL 候选集是否收了 ``exempt_recorded``。

    只认**同一条字符串字面量里同时出现** ``funding_state`` 与 ``exempt_recorded``
    的那一段 —— 那才是"候选集收了它",而不是别处随便提了一嘴。
    """
    return any("funding_state" in s and "exempt_recorded" in s for s in _sql_literals(fn))


def test_r2_40_every_platform_branch_has_exempt_recorded_in_its_candidate_set() -> None:
    """🔴 全类锁(Owner 点名的那次 grep,做成判据)。

    ``reconciler.py`` 里每一个 ``is_platform_cost`` 分支,所在函数的 **SQL**
    必须把 ``exempt_recorded`` 收进候选集 —— 否则那个分支永远够不到行,
    守卫就是装饰(⑦ 与 ④ 各栽过一次)。

    分母**机械枚举源码**,不手抄:新增第三个平台分支却忘了补候选集,这里当场红。
    """
    tree = ast.parse(RECON_SRC.read_text(encoding="utf-8"))
    with_branch = _platform_branch_funcs(tree)
    names = sorted(f.name for f in with_branch)

    assert len(with_branch) >= 2, (
        f"扫不到平台分支({names})—— 探针写废了,下面的断言就是恒真的")
    for fn in with_branch:
        assert _candidate_set_admits_platform(fn), (
            f"{fn.name} 有平台腿分支,**SQL 候选集**却不含 exempt_recorded —— "
            "迁移 044 的 CHECK 保证平台腿恒 exempt_recorded,这个分支一行都执行不到")

    # 反向对照:一个**没有**平台分支的函数不该被算进来。
    assert "_external_started_without_outcome" not in names


def test_r2_44_queue_membership_admits_the_platform_leg_on_both_sides() -> None:
    """🔴 [R3] 类锁**扫窄了**的实证:第三个姊妹长在 ``settlement_review``,不在 reconciler。

    ═══════════════════════════════════════════════════════════════════
    这条判据是被一次真红逼出来的
    ═══════════════════════════════════════════════════════════════════
    补 Z-1 人工处置那两条判据时当场红:``该 command 的资金态是 'exempt_recorded',
    不在核验队列 ['pending_reconciliation','quarantined'] 里``。
    也就是说,**需要人工裁的平台单既不出现在队列里,也会被处置接口当场拒绝** ——
    与 ④⑦ 候选集完全同一类(按一列圈范围,而那一列对某条腿是常量),
    只是长在另一个文件。我上一版的类锁只扫 ``reconciler.py``,**扫窄了**。

    这条判据两边对账:``store.review_queue`` 的 SQL 与
    ``settlement_review.is_queue_member`` 必须是同一个意思(同一谓词两处 ⇒ 必须有判据对账)。
    """
    from services.defensive_geo.publish import settlement_review as _rv

    src = (ROOT / "services" / "defensive_geo" / "publish" / "store.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    queue_fn = next(n for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == "review_queue")
    blob = "\n".join(_sql_literals(queue_fn))

    # ① SQL 侧:平台腿那一支存在,且用的是 commandState + 未结算,不是 fundingState。
    assert "principal_kind" in blob and "settled_at IS NULL" in blob, (
        "review_queue 的 SQL 里没有平台腿那一支 —— 平台单永远进不了 Z-1 队列")
    for state in _rv.QUEUE_PLATFORM_COMMAND_STATES:
        assert state in blob, (
            f"平台腿的队列态 {state!r} 没出现在 review_queue 的 SQL 里 —— 两边飘了")
    for state in _rv.QUEUE_FUNDING_STATES:
        assert state in blob, f"钱包腿的队列态 {state!r} 从 SQL 里掉了"

    # ② 谓词侧:逐格核对(平台腿看 commandState,钱包腿看 fundingState)。
    plat = {"funding_policy": "admin_platform_ledger", "funding_state": "exempt_recorded"}
    for state in _rv.QUEUE_PLATFORM_COMMAND_STATES:
        assert _rv.is_queue_member({**plat, "command_state": state, "settled_at": None}), state
        assert not _rv.is_queue_member(
            {**plat, "command_state": state, "settled_at": "2026-08-24"}), (
            f"已结算的平台单还留在队列里({state})—— 重复处置 = 重复扣/重复退")
    assert not _rv.is_queue_member({**plat, "command_state": "completed", "settled_at": None})

    wallet = {"funding_policy": "personal_wallet"}
    for state in _rv.QUEUE_FUNDING_STATES:
        assert _rv.is_queue_member({**wallet, "funding_state": state}), state
    assert not _rv.is_queue_member({**wallet, "funding_state": "frozen"}), (
        "钱包腿的队列判别被平台腿那一支带宽了 —— 加宽不许顺手加宽别人")


def test_r2_40b_the_candidate_set_probe_is_not_satisfied_by_a_comment() -> None:
    """🔴 探针自证:一句**注释**(或 docstring)不许让上面那条变绿。

    这就是 Review 点名的那个边界。用两段合成源码正反各打一发 ——
    不亲手注一次毒,「收紧了」只是一句声明。
    """
    import ast

    poisoned = (
        "def f(cur):\n"
        '    """候选集这里**不含** exempt_recorded,只是 docstring 提了一嘴。"""\n'
        "    # funding_state / exempt_recorded 也可以写在注释里\n"
        "    cur.execute(\"SELECT 1 WHERE funding_state IN ('frozen')\")\n"
        "    if _funding.is_platform_cost(row['funding_policy']):\n"
        "        pass\n"
    )
    good = (
        "def f(cur):\n"
        "    cur.execute(\"SELECT 1 WHERE funding_state IN ('frozen','exempt_recorded')\")\n"
        "    if _funding.is_platform_cost(row['funding_policy']):\n"
        "        pass\n"
    )
    bad_fn = _platform_branch_funcs(ast.parse(poisoned))
    good_fn = _platform_branch_funcs(ast.parse(good))
    assert len(bad_fn) == 1 and len(good_fn) == 1, "分支探针在合成样本上就没打中"
    assert _candidate_set_admits_platform(bad_fn[0]) is False, (
        "注释/docstring 里提一句 exempt_recorded 就让判据变绿了 —— 裸子串锚")
    assert _candidate_set_admits_platform(good_fn[0]) is True, (
        "真的写在 SQL 里也判不出来 —— 收紧过头,判据够不到正样本")


def _sets_funding_state(blob: str) -> bool:
    """这段 SQL 里有没有**写** ``funding_state``(赋值位),而不是**读**它(谓词位)。

    🔴 [E1-1 顺手修 · 2026-08-26] 原探针是
    ``UPDATE`` and ``SET`` and ``funding_state =`` **在整坨字面量里任意位置**。
    它分不清「赋值」和「谓词」:E1-1 给 ``mark_external_start`` 的 WHERE 加了
    ``AND c.funding_state = ANY(%s)``(**读**),整条判据当场红 —— 那是误伤。

    加豁免名单是错的解法(手工削弱判据)。也不能按「SET…WHERE 区间」取 ——
    本仓的 SQL 是**拼**出来的,``"UPDATE "`` / ``" SET "`` /
    ``"funding_state = CASE …"`` 是各自独立的字面量,串起来的顺序与最终 SQL
    不一致,区间法在真 blob 上扫到 0 个。

    所以按**位置语义**判:
      · 赋值位 —— 片段开头 / 逗号后 / ``SET`` 后紧跟 ``funding_state =``;
      · 谓词位 —— ``AND`` / ``OR`` / ``WHERE`` 后跟(可带表别名的)
        ``funding_state =``。谓词位一律不算写点。
    """
    import re

    if not (re.search(r"\bUPDATE\b", blob, re.I) and re.search(r"\bSET\b", blob, re.I)):
        return False
    predicate = re.compile(r"\b(?:AND|OR|WHERE)\s+[\w]*\.?funding_state\s*=", re.I)
    assign = re.compile(r"(?:^|,|\bSET\b)\s*[\w]*\.?funding_state\s*=", re.I)
    for line in blob.splitlines():
        if predicate.search(line):
            continue
        if assign.search(line):
            return True
    return False


def test_r2_41_funding_state_has_exactly_one_update_writer() -> None:
    """🔴 [R2 返修 · Review 点名②] 单点收口的**正确性论据**,做成判据。

    "把规则写进写这一列的唯一那条语句"只有在**确实只有那一条**时才成立。
    所以这里机械核两件事:

      ① 全包只有 ``store.py`` 的 SQL 会 ``SET funding_state``
         —— 任何别的文件出现第二条 UPDATE,单点收口当场失效;
      ② 传 ``funding_state`` 给 ``bump_status`` 的调用点分布在**哪几个文件**
         (集合式,不是计数式 —— 计数会被正常业务写过期,集合不会)。

    另有 3 处在**建单 INSERT** 时写这一列(``publish_funding`` → ``insert_command``),
    走的不是 ``bump_status`` —— 它们按 ``_CONFIRM_FUNDING_STATE`` 天然合规。

    ═══════════════════════════════════════════════════════════════════════
    🔴 [工单B P0-4 · 2026-08-25 继任版] 旧断言退役,换成更紧的那一条
    ═══════════════════════════════════════════════════════════════════════
    旧版第②条钉的是「``bump_status(funding_state=…)`` 分布在 reconciler /
    publish_outbox / settlement_review 三个文件,且调用点 ≥ 10」。
    那句话钉的是**顺序反转之前**的形状:那时每个调用点在
    ``commit_exact`` / ``release_exact`` 之后都无条件写一句
    ``funding_state="committed"/"released"`` —— 而那正是 P0-4 的病本身
    (billing 返 success=false 也照写)。修法把**终态**那一格收进
    ``publish_funding._write_terminal``,所以旧断言必然红,**它钉的是该退役的行为**。

    继任者比它紧,三条:
      ②′ 全包**没有任何地方**再用字面量把两格终态
          (``store.SETTLEMENT_TERMINAL_FUNDING_STATES``)传给 ``bump_status``;
      ③′ ``store.write_settlement_terminal``(结算终态的唯一入口)只有
          **一个**调用方 —— ``publish_funding._write_terminal``,
          也就是"物理结算成功且方向一致"之后的那一行。多一个 = P0-4 复活;
      ④′ 非终态值(pending_reconciliation / quarantined)仍分布在
          收敛 / 派发 / 人工 / 结算原语四面 —— 集合式,不是计数式。
    """
    import ast

    import re

    # 🔴 探针自证。样本**取自本仓真的会产出的形状**(拼接碎片),不是教科书 SQL:
    #    第一版自证用的是完整单行 SQL,它过了、真 blob 却扫到 0 个 ——
    #    自证样本形状不对,自证本身就是假绿。
    _bump_shape = "\n".join([
        "funding_state = CASE WHEN principal_kind = %s THEN funding_state ELSE %s END",
        "UPDATE ", " SET ", ", status_version = status_version + 1 WHERE ", " RETURNING ",
    ])
    _read_shape = "\n".join([
        "UPDATE ", " SET external_start_at = NOW()", "         WHERE c.publish_command_id = %s",
        "           AND c.funding_state = ANY(%s)",
    ])
    assert _sets_funding_state(_bump_shape), (
        "探针漏掉了真写点(bump_status 那种拼接形状)—— 尺子没在量")
    assert not _sets_funding_state(_read_shape), (
        "探针把 WHERE 里的**读**当成了写 —— 它分不清赋值位与谓词位")

    pkg = ROOT / "services" / "defensive_geo"
    setters: dict[str, int] = {}
    sql_writers: set[str] = set()
    for path in sorted(pkg.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and any(
                    kw.arg == "funding_state" for kw in node.keywords):
                fname = (node.func.attr if isinstance(node.func, ast.Attribute)
                         else getattr(node.func, "id", ""))
                if fname == "bump_status":
                    setters[rel] = setters.get(rel, 0) + 1
            # 🔴 SQL 是**拼**出来的:``UPDATE``/``SET``/``funding_state = CASE …``
            #    分别落在同一函数的不同字面量里(第一版按"同一条字面量里同时出现"
            #    去找,扫到 0 个 —— 那不是"没有第二个写点",是探针没打中)。
            #    所以按**函数**聚合字面量再看,并且先剥掉 docstring(散文里的
            #    ``funding_state='quarantined'`` 不是写点)。
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                blob = "\n".join(_sql_literals(node))
                if _sets_funding_state(blob):
                    sql_writers.add(f"{rel}::{node.name}")

    assert sql_writers == {"services/defensive_geo/publish/store.py::bump_status"}, (
        f"``SET funding_state`` 的写点集合 = {sorted(sql_writers)} —— "
        "单点收口的前提是**只有一条** UPDATE 写这一列;多一条,那条 CASE 就守不住了")

    # 分母机械枚举:终态值集合取自 ``store``,并与 ``publish_funding`` 那份对账
    #（同一谓词写两处必有一处没人验）。
    from services.defensive_geo.publish import publish_funding as _pf

    terminal_values = set(_store.SETTLEMENT_TERMINAL_FUNDING_STATES)
    assert terminal_values == set(_pf._TERMINAL_FUNDING_STATE.values()), (   # noqa: SLF001
        "store 与 publish_funding 两份终态清单漂了 —— 谁改了一边没改另一边")

    literal_terminal: set[str] = set()
    other_writers: dict[str, int] = {}
    terminal_callers: set[str] = set()
    for path in sorted(pkg.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                fname = (node.func.attr if isinstance(node.func, ast.Attribute)
                         else getattr(node.func, "id", ""))
                if fname == "write_settlement_terminal":
                    terminal_callers.add(f"{rel}::{fn.name}")
                    continue
                if fname != "bump_status":
                    continue
                for kw in node.keywords:
                    if kw.arg != "funding_state":
                        continue
                    val = kw.value.value if isinstance(kw.value, ast.Constant) else None
                    if val in terminal_values:
                        literal_terminal.add(f"{rel}::{fn.name}")
                    else:
                        other_writers[rel] = other_writers.get(rel, 0) + 1

    # ②′ 没有任何地方再用字面量把终态写出去
    assert literal_terminal == set(), (
        f"这些地方仍在用字面量写结算终态:{sorted(literal_terminal)} —— "
        "P0-4:终态只能在物理结算成功且方向一致之后写,而且只能走 "
        "``store.write_settlement_terminal``")

    # ③′ 终态入口只有一个调用方
    assert terminal_callers == {
        "services/defensive_geo/publish/publish_funding.py::_write_terminal",
    }, (
        f"``write_settlement_terminal`` 的调用方 = {sorted(terminal_callers)} —— "
        "结算终态必须只从结算原语里写(多一个调用方 = 多一条绕过真值表的路)")

    # ④′ 非终态资金态的写点分布(集合式)
    assert set(other_writers) == {
        "services/defensive_geo/publish/reconciler.py",
        "services/defensive_geo/publish/publish_outbox.py",
        "services/defensive_geo/publish/settlement_review.py",
        "services/defensive_geo/publish/publish_funding.py",
    }, f"非终态 ``funding_state`` 写点的文件集合变了:{sorted(other_writers)}"
    assert sum(other_writers.values()) >= 6, (
        f"非终态调用点只扫到 {sum(other_writers.values())} 个 —— 探针可能写废了")


def test_r2_42_clean_queue_covers_every_seeded_identity() -> None:
    """身份清理表 ≡ ``install_base_rows`` 真写的那批 uid(同一谓词写两处必须对账)。"""
    import ast
    import inspect

    src = inspect.getsource(_seed.install_base_rows)
    seeded: set[int] = set()
    for node in ast.walk(ast.parse(src.lstrip())):
        if isinstance(node, ast.Tuple):
            for el in node.elts:
                if isinstance(el, ast.Tuple) and el.elts:
                    head = el.elts[0]
                    if isinstance(head, ast.Constant) and isinstance(head.value, int):
                        seeded.add(head.value)
                    elif isinstance(head, ast.Name):
                        val = getattr(_seed, head.id, None)
                        if isinstance(val, int):
                            seeded.add(val)
    users = {u for u in seeded if 9700 <= u <= 9799}
    assert users, "扫不到 install_base_rows 里的 uid —— 探针写废了"
    assert users <= set(_seed.TEST_USER_IDS), (
        f"这些身份被种下却不在清理表里:{sorted(users - set(_seed.TEST_USER_IDS))} —— "
        "它们的角色行会跨 session 攒着,把付款方判别整条改判")


# ══════════════════════════════════════════════════════════════════════════
# F. [R4-①] 广告法门 release —— **活路径**,两条腿各一条行为判据
# ══════════════════════════════════════════════════════════════════════════
#: 🔴 会命中已签发广告法目录的一句。判据**不自己维护词表** ——
#:    词表只来自签发包(``legal_gate._scan`` 逐字),这里只挑一句它确实抓得住的,
#:    并在 ``test_r4_01c`` 里先自证"这句真的会命中、而普通句子不会"。
_LEGAL_HIT_SENTENCE = "本机构是国家级最佳服务商，效果最好。"


def test_r4_01c_the_legal_probe_sentence_really_trips_the_signed_gate() -> None:
    """探针自证:门是 **blocking**,那句话真的命中,普通句子不命中。

    没有这一条,下面两条"命中后退款"可能只是因为**根本没命中**而走了别的分支。
    """
    from services.defensive_geo.publish import legal_gate as _lg

    assert _lg.gate_mode() == "blocking", (
        f"广告法门当前是 {_lg.gate_mode()!r} —— R4 之前我把它当成 advisory 写了三轮,"
        "一行 python -c 就能验。门一旦回到 advisory,下面两条判据就够不到被测行了")
    hit = _lg.evaluate(article_revision_id="rev-probe", frozen_body=_LEGAL_HIT_SENTENCE)
    assert hit.blocked and hit.hits, f"探针句子没命中:{hit}"
    clean = _lg.evaluate(article_revision_id="rev-probe",
                         frozen_body="这是一段普通的、可以正常发布的稿子。")
    assert not clean.blocked, f"普通句子也被拦了 —— 探针没有判别力:{clean}"


def _legal_arm(client: TestClient, name: str, *, who: str,
               tenant: int, brand: int, payer: int) -> tuple[str, int, dict[str, int]]:
    """造一条**正文命中广告法**的已确认命令,然后真派发一次。

    返回 ``(命令 id, exact, 确认之后·派发之前的付款钱包快照)``。

    🔴 中点快照是**判别力**那一半:只比"派发前后没变"证明不了退款发生过 ——
       "从头到尾什么都没发生"长得一模一样。三点取样才说明白:
       ``before →(冻结 +exact)→ mid →(退款 -exact)→ before``。
       (第一版我把基线取在 confirm **之前**,还照抄了 ``- exact``,当场红。)
    """
    from services.defensive_geo import activation_materializer as _mat

    ctx = _seed.seed_accepted_snapshot(tenant=tenant, brand=brand,
                                       publications=2, activate=True)
    assert _mat.materialize_pending()["materialized"] >= 1
    revision_id, article_hash = _seed.seed_article(
        brand=ctx["brandId"], body=_LEGAL_HIT_SENTENCE)
    ctx.update({
        "planItemKey": f"r4_{name}",
        "articleRevisionId": revision_id,
        "expectedArticleHash": article_hash,
        "serviceProjectionId": _policy.service_projection_id_for(ctx["acceptedSnapshotId"]),
        "who": who,
    })
    _confirmed(client, ctx)
    cid = ctx["publishCommandId"]
    exact = int(_seed.command_row(cid)["exact_settlement_points"])
    mid = _seed.wallet(payer)

    def _must_not_be_called(_command: Any) -> _dispatch.ProviderResult:
        raise AssertionError("广告法命中却还是外调了 —— 那笔钱就再也不能安全退了")

    _run(_worker.dispatch_pending(limit=20, provider_call=_must_not_be_called))
    return cid, exact, mid


def test_r4_01a_legal_hit_on_a_wallet_leg_refunds_the_customer(
        client: TestClient) -> None:
    """🔴 [R4-①] 广告法命中 ⇒ **零外调 + 全额退回客户钱包**(钱包腿)。

    全仓此前**没有任何判据**驱动过这条路径(w3 那条只是直接写死
    ``legal_rule_id`` 列再看投影,没走过派发)。所以两条腿都得新写,
    不是"引用既有判据"。
    """
    before = _seed.wallet(_seed.TENANT_A)
    cid, exact, mid = _legal_arm(client, "wal", who="a", tenant=_seed.TENANT_A,
                                 brand=_seed.BRAND_A, payer=_seed.TENANT_A)

    cmd = _seed.command_row(cid)
    assert int(cmd["provider_call_count"]) == 0, "广告法门在外调之前,却记了外调次数"
    assert cmd["external_start_at"] is None
    assert str(cmd["canonical_publication_state"]) == "failed_no_effect", cmd
    assert str(cmd["funding_state"]) == "released", cmd["funding_state"]
    assert cmd["legal_rule_id"], "命中了却没留下规则坐标 —— 她点不到「修好这一句」"
    assert cmd["settled_at"] is not None, "结算了却没落 settled_at"

    # 判别力:确认那一刻**确实冻过**(否则"前后没变"可能是全程什么都没发生)。
    assert mid["frozen_points"] == before["frozen_points"] + exact, (
        f"确认没冻上:{before} → {mid}(exact={exact})")
    after = _seed.wallet(_seed.TENANT_A)
    assert after["frozen_points"] == before["frozen_points"], (
        f"冻结没回落:{mid} → {after}(exact={exact})")
    assert after["paid_points"] == before["paid_points"], (
        f"零外调却没把算力全额退回原池:{mid} → {after}(exact={exact})")


def test_r4_01b_legal_hit_on_a_platform_leg_refunds_the_platform_account(
        client: TestClient) -> None:
    """🔴 [R4-①] 同一条路径的**平台腿**臂 —— 与 ``test_r2_23`` 同形。

    这一处正是我三轮误标「判据够不到」的那一处。它是**活路径**,
    而且 R3 之后平台腿已经同走 —— 也就是说,这条钱腿一直在跑,只是没人验过。
    """
    tenant_before = _seed.wallet(_seed.TENANT_ADMIN)
    platform_before = _seed.wallet(_seed.PLATFORM_ACCOUNT)
    cid, exact, mid = _legal_arm(client, "plat", who="adminowner",
                                 tenant=_seed.TENANT_ADMIN, brand=_seed.BRAND_ADMIN,
                                 payer=_seed.PLATFORM_ACCOUNT)

    cmd = _seed.command_row(cid)
    assert int(cmd["provider_call_count"]) == 0
    assert str(cmd["canonical_publication_state"]) == "failed_no_effect", cmd
    # 平台腿的 fundingState 是常量 —— 钱退了,这一列**不动**(CASE 挡住)。
    assert str(cmd["funding_state"]) == "exempt_recorded", cmd["funding_state"]
    assert cmd["settled_at"] is not None

    assert mid["frozen_points"] == platform_before["frozen_points"] + exact, (
        f"确认没冻到平台账上:{platform_before} → {mid}(exact={exact})")
    platform_after = _seed.wallet(_seed.PLATFORM_ACCOUNT)
    assert platform_after["frozen_points"] == platform_before["frozen_points"], (
        f"平台账的冻结没回落:{mid} → {platform_after}(exact={exact})")
    assert platform_after["paid_points"] == platform_before["paid_points"], (
        f"广告法命中是权威零接单,却没退回平台账原池:{mid} → {platform_after}")
    assert _seed.wallet(_seed.TENANT_ADMIN) == tenant_before, "平台腿退款动了客户钱包"


def test_r4_02_settled_at_has_exactly_one_writer() -> None:
    """🔴 [R4-②] ``settled_at`` 只能由 ``store.mark_settled`` 写。

    R3 引入它当结算终态标记时,``bump_status`` 的 allowed 里还留着通用直写的口子。
    当时零调用 —— 但通用路径**没有"至多一次"保护**(``mark_settled`` 有
    ``WHERE settled_at IS NULL``),日后顺手一写就能改掉已结算的时间戳,
    而"已结算"是 ④⑦ 候选集与 Z-1 队列**共同**依赖的那个事实。**门开着 = 迟早有人走进来。**
    """
    import re

    # ① 通用直写的口子已经关上(反向对照:换一个仍在 allowed 里的列必须能改)。
    ctx = _seed.seed_accepted_snapshot(activate=False)
    assert ctx                                          # 只为让夹具真的建过行
    with pytest.raises(_store.StoreError) as err:
        _store.bump_status(None, publish_command_id="pcmd_nonexistent",
                           settled_at="2026-08-24")
    assert "settled_at" in str(err.value)

    # ② 结构锁:全包只有 mark_settled 的 SQL 会 ``SET settled_at``。
    pkg = ROOT / "services" / "defensive_geo"
    writers: set[str] = set()
    for path in sorted(pkg.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            blob = "\n".join(_sql_literals(fn))
            if (re.search(r"\bUPDATE\b", blob, re.I) and re.search(r"\bSET\b", blob, re.I)
                    and re.search(r"settled_at\s*=", blob)):
                writers.add(f"{rel}::{fn.name}")
    assert writers == {"services/defensive_geo/publish/store.py::mark_settled"}, (
        f"``SET settled_at`` 的写点集合 = {sorted(writers)} —— "
        "「至多一次」只在 mark_settled 里,多一个写点就等于没有这条保护")

    # ③ 「至多一次」本身:mark_settled 的 SQL 必须带 ``settled_at IS NULL``。
    src = (ROOT / "services" / "defensive_geo" / "publish" / "store.py").read_text(
        encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "mark_settled")
    assert "settled_at IS NULL" in "\n".join(_sql_literals(fn)), (
        "mark_settled 丢了「至多一次」的 WHERE —— 重复结算会把时间戳改到最后一次")


# ── fake provider(与主链同形,三态)────────────────────────────────────
def _accepted(order_sn: str) -> _dispatch.ProviderCall:
    """🔴 [工单B B-5 · 2026-08-25 夹具订正] 上游单号**每单唯一**。

    改之前这个 fake 对整批命令返**同一个字面量**。上游不会那样 ——
    ``order_sn`` 是它那边的单据主键,一单一个。而 ``dispatch_pending``
    是**批量**的:一批里第二条命令拿到已被占用的单号时,051 的唯一索引
    与派发侧的"拒绝绑定转核验"都会正确地拦下它。

    也就是说旧夹具**供了生产不会供的东西**,判据因此在测一个不存在的场景
    (本仓记过:夹具供了生产不会供的东西 ⇒ 假绿/假红)。
    每条命令按自己的 id 派生一个稳定后缀,断言侧照同一条规则拼期望值 ——
    这样"这条命令拿到的是**它自己那一个**单号"才是被钉住的那件事。
    """
    def _call(command: Any) -> _dispatch.ProviderResult:
        cid = str(command.get("publish_command_id") or "")
        return _dispatch.ProviderResult(
            kind="accepted", raw_table="mhz_publish_order_items",
            raw_column="status", raw_value="submitted", detail=f"{order_sn}-{cid[-8:]}")
    return _call


def _explodes() -> _dispatch.ProviderCall:
    def _call(_command: Any) -> _dispatch.ProviderResult:
        raise RuntimeError("注入:外调途中炸了,结果未知")
    return _call


def _mark_verified(command_id: str) -> None:
    """把命令推到"上游说发了 + 核实链通过"那一格 —— 钱向 = commit。"""
    conn = connect()
    try:
        cur = conn.cursor()
        _store.bump_status(
            cur, publish_command_id=command_id,
            canonical_publication_state="verified_published",
            url_verification_state="verified",
            public_url="https://pkge-daily.com.cn/r2/verified",
        )
        conn.commit()
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
# §F · [工单B / Review §7.4-4 ②] 两道 ``quarantined`` 收口闸
# ══════════════════════════════════════════════════════════════════════════
# 工单B 在收敛器 ④ 与 ⑦ 的候选集上各补了一道「已隔离的不许再动」:
#
#   ④ ``_outcome_without_settlement``:  AND command_state <> 'quarantined'
#   ⑦ ``_release_never_dispatched``:    AND command_state NOT IN (..., 'quarantined')
#
# 它守的是这件事:⑤ ``_quarantine_mirror_conflicts`` 把一条**结果存疑**的命令
# 转进 Z-1 人工队列(镜像与 canonical 矛盾 ⇒ 钱不许跟着镜像翻)。如果 ④/⑦
# 还能够到它,下一轮收敛就会**替人工把这笔钱结掉** —— 隔离等于没隔离。
#
# 🔴 为什么判据落在包E 而不是工单B 包:平台腿的行为分母在工单B 里是 0
#    (那条腿的真链臂长在本包)。在工单B 里只写得出结构锁,写不出行为判据。
#    本仓记过:分母比结论小,结论就是假的。


def _quarantine_command(command_id: str) -> None:
    """摆成「⑤ 已把它转进 Z-1」的形状 —— 只动 ``command_state``,钱一格不动。"""
    conn = connect()
    try:
        cur = conn.cursor()
        _store.bump_status(cur, publish_command_id=command_id,
                           command_state="quarantined",
                           status_reason="包E §F:注入 —— 已转人工核验")
        conn.commit()
    finally:
        conn.close()


def _gate7_shape(client: TestClient, name: str) -> tuple[str, int]:
    """造出 ⑦ 的候选形状(平台腿 + outbox needs_review),返回 (cid, exact)。"""
    from tests.defensive_geo_pkge_2026_08_24.test_real_chain_pg import (
        _drain, _make_platform_leg,
    )

    ctx = _ready(name, who="adminowner",
                 tenant=_seed.TENANT_ADMIN, brand=_seed.BRAND_ADMIN)
    _confirmed(client, ctx)
    cid = ctx["publishCommandId"]
    exact = int(_seed.command_row(cid)["exact_settlement_points"])
    _drain()                                   # 共享队列:先清别人的账
    _make_platform_leg(cid)                    # 它自己会把 outbox 摆成 needs_review
    return cid, exact


def test_r2_60_gate7_never_touches_a_quarantined_command(
        client: TestClient) -> None:
    """🔴 ⑦ 够不到**已隔离**的命令 —— 隔离之后钱只能由人来动。

    「删修复即红」:把 ⑦ 候选集里 ``NOT IN`` 那串的 ``'quarantined'`` 去掉,
    这条命令会被自动退款,下面的断言当场红。
    活性对照 = ``test_r2_61``(同一形状不隔离 ⇒ 必须真退)。
    """
    cid, _exact = _gate7_shape(client, "q7")
    _quarantine_command(cid)
    before = _seed.command_row(cid)
    assert str(before["command_state"]) == "quarantined", before["command_state"]
    platform_before = _seed.wallet(_seed.PLATFORM_ACCOUNT)

    actions = _run(_worker.reconcile_tick(limit=200))

    mine = [a for a in actions["items"] if a["commandId"] == cid]
    assert not [a for a in mine
                if a["kind"] in ("release", "release_never_dispatched")], (
        f"已转人工的命令又被收敛器自动退款了:{mine} —— 隔离等于没隔离")
    after = _seed.command_row(cid)
    assert after["settled_at"] is None, "已隔离却被写了结算终态"
    assert str(after["funding_state"]) == str(before["funding_state"])
    assert _seed.wallet(_seed.PLATFORM_ACCOUNT) == platform_before, "动了平台账"


def test_r2_61_gate7_liveness_the_same_shape_unquarantined_is_refunded(
        client: TestClient) -> None:
    """🟢 上一条的活性对照:唯一差别是**没隔离** ⇒ ⑦ 必须真退。

    对照不绿 ⇒ 上一条的"没动"可能只是形状根本没进候选集
    (那样它一条东西都没在验)。两条必须成对读。
    """
    cid, exact = _gate7_shape(client, "q7live")
    platform_before = _seed.wallet(_seed.PLATFORM_ACCOUNT)

    actions = _run(_worker.reconcile_tick(limit=200))

    mine = [a for a in actions["items"] if a["commandId"] == cid]
    assert any(a["kind"] == "release_never_dispatched" for a in mine), (
        f"没隔离的平台腿也没被 ⑦ 处置:{mine} —— 对照臂不成立,"
        "上一条的「没动」也就证明不了任何事")
    after = _seed.command_row(cid)
    assert after["settled_at"] is not None
    platform_after = _seed.wallet(_seed.PLATFORM_ACCOUNT)
    assert platform_after["frozen_points"] == platform_before["frozen_points"] - exact
    assert platform_after["paid_points"] == platform_before["paid_points"] + exact


def _gate4_shape(client: TestClient, name: str) -> tuple[str, int]:
    """造出 ④ 的候选形状:canonical 已落终局、``settled_at`` 仍为空。"""
    from tests.defensive_geo_pkge_2026_08_24.test_real_chain_pg import _drain

    ctx = _ready(name, who="adminowner",
                 tenant=_seed.TENANT_ADMIN, brand=_seed.BRAND_ADMIN)
    _confirmed(client, ctx)
    cid = ctx["publishCommandId"]
    exact = int(_seed.command_row(cid)["exact_settlement_points"])
    _drain()
    _run(_worker.dispatch_pending(limit=10, provider_call=_accepted(f"SN-{name}")))
    _mark_verified(cid)
    return cid, exact


def test_r2_62_gate4_never_touches_a_quarantined_command(
        client: TestClient) -> None:
    """🔴 ④ 同样够不到**已隔离**的命令。

    ④ 与 ⑦ 是同一条纪律的两处落点 —— 只堵一处等于没堵
    (本仓记过:同一目标的语法形态要一次枚举全)。
    「删修复即红」:去掉 ④ 候选集里的 ``AND command_state <> 'quarantined'``,
    这条会被自动 commit,钱替人工花掉。
    """
    cid, _exact = _gate4_shape(client, "q4")
    _quarantine_command(cid)
    before = _seed.command_row(cid)
    assert str(before["canonical_publication_state"]) == "verified_published", before
    assert before["settled_at"] is None
    platform_before = _seed.wallet(_seed.PLATFORM_ACCOUNT)

    _run(_worker.reconcile_tick(limit=200))

    after = _seed.command_row(cid)
    assert after["settled_at"] is None, (
        "已转人工的命令被 ④ 自动结算了 —— 人工队列里那条从此消失,钱已经花掉")
    assert _seed.wallet(_seed.PLATFORM_ACCOUNT) == platform_before, "动了平台账"


def test_r2_63_gate4_liveness_the_same_shape_unquarantined_is_settled(
        client: TestClient) -> None:
    """🟢 ④ 的活性对照:不隔离 ⇒ 必须真结算(否则上一条守的是空气)。"""
    cid, exact = _gate4_shape(client, "q4live")
    platform_before = _seed.wallet(_seed.PLATFORM_ACCOUNT)

    _run(_worker.reconcile_tick(limit=200))

    after = _seed.command_row(cid)
    assert after["settled_at"] is not None, (
        "没隔离的命令 ④ 也没结算 —— 对照臂不成立")
    # 🔴 基线是**冻结之后**取的(confirm 时已经 paid -= exact / frozen += exact)。
    #    所以 commit 的钱包语义 = 冻结被消费掉:frozen -= exact,paid **不动**
    #    —— 与 release 臂(frozen -= exact 且 paid += exact)正好差在这一格,
    #    两条对照读才看得出方向。
    platform_after = _seed.wallet(_seed.PLATFORM_ACCOUNT)
    assert platform_after["frozen_points"] == platform_before["frozen_points"] - exact, (
        f"commit 臂没把冻结消费掉:{platform_before} → {platform_after}(exact={exact})")
    assert platform_after["paid_points"] == platform_before["paid_points"], (
        f"commit 却把算力退回了原池 —— 那是 release 的方向:"
        f"{platform_before} → {platform_after}")


def test_r2_64_every_settling_candidate_set_excludes_quarantined() -> None:
    """🔴 **机械枚举**:凡是会动钱的收敛项,候选集都必须排除 ``quarantined``。

    分母不是手抄的两个函数名,而是从源码里**枚举**出来的
    「函数体内出现 ``commit_exact`` / ``release_exact``」——
    以后新增第三个会结算的收敛项却忘了这道闸,这里当场红。
    (本仓记过:手写分母漏掉的那一项不会让任何判据变红。)
    """
    import ast as _ast

    src = RECON_SRC.read_text(encoding="utf-8")
    tree = _ast.parse(src)
    settling: dict[str, str] = {}
    for node in _ast.walk(tree):
        if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        if any(isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute)
               and n.func.attr in ("commit_exact", "release_exact")
               for n in _ast.walk(node)):
            settling[node.name] = _ast.get_source_segment(src, node) or ""

    assert len(settling) >= 2, (
        f"只枚举到 {sorted(settling)} —— 分母塌了,这条锁在守空气")
    # 判**代码**不判散文:注释里写着 quarantined 不等于候选集排除了它。
    missing = [
        name for name, body in settling.items()
        if "quarantined" not in "\n".join(
            ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
    ]
    assert not missing, (
        f"这些会结算的收敛项没有排除已隔离命令:{missing} —— "
        "⑤ 转进 Z-1 的单子会被下一轮收敛替人工结掉")
