"""真发布端到端 + 四个 kill window + 收敛三窗口(工单①②⑤)。

═══════════════════════════════════════════════════════════════════════
🔴 这一份是本包的主链:确认 → 冻结 → 派发 → 回执 → 结算
═══════════════════════════════════════════════════════════════════════
夹具里**没有**被测对象的替身:

  · app  = 真 ``api.defensive_publish_api.router``;
  · DB   = conftest 装好的「生产 pg_dump + 迁移」一次性库;
  · 钱   = 真 ``middleware.billing.freeze_points`` 写真 ``point_freezes``;
  · 派发 = 真 ``publish_worker.dispatch_pending`` → 真 ``dispatch_once``;
  · 收敛 = 真 ``reconciler.reconcile_once``。

**唯一注入的是 provider 边界那一跳**(``provider_call``)。这正是工单⑤ 允许的
那一格:真实媒体 sandbox 口径 Owner 未定,先验到 provider 边界。
注入走**参数**不走分支 —— 生产的 cron 一个参数都不传,
所以链上文件里 ``is_test`` / ``dry_run`` / ``sandbox`` 零命中
(``test_90_no_test_shortcuts_on_the_production_path`` 机械钉住这一条)。
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from services.defensive_geo.publish import execution_budget_policy as _policy
from services.defensive_geo.publish import publish_outbox as _dispatch
from services.defensive_geo.publish import publish_worker as _worker
from services.defensive_geo.publish import store as _store

from tests.defensive_geo_pkge_2026_08_24 import _seed
from tests.defensive_geo_pkge_2026_08_24.conftest import connect

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def _base(_schema) -> Iterator[None]:                     # noqa: ANN001
    import auth.brand_access  # noqa: F401,PLC0415

    _seed.install_base_rows()
    yield


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
# fake provider —— **三态**,与真适配器同形
# ══════════════════════════════════════════════════════════════════════════
def order_ref_for(order_sn: str, publish_command_id: str) -> str:
    """夹具与断言**共用**的一条派生规则(写两处必有一处漂)。"""
    return f"{order_sn}-{str(publish_command_id)[-8:]}"


def _accepted(order_sn: str = "PKGE-SN-1") -> _dispatch.ProviderCall:
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
    def _call(command):                                   # noqa: ANN001
        return _dispatch.ProviderResult(
            kind="accepted", raw_table="mhz_publish_order_items",
            raw_column="status", raw_value="submitted",
            detail=order_ref_for(order_sn, command.get("publish_command_id") or ""))
    return _call


def _rejected() -> _dispatch.ProviderCall:
    def _call(_command):                                  # noqa: ANN001
        return _dispatch.ProviderResult(
            kind="rejected_no_effect", raw_table="mhz_publish_order_items",
            raw_column="status", raw_value="rejected", detail="上游权威拒稿")
    return _call


def _explodes() -> _dispatch.ProviderCall:
    def _call(_command):                                  # noqa: ANN001
        raise TimeoutError("注入:外调途中超时(结果未知)")
    return _call


# ══════════════════════════════════════════════════════════════════════════
# 走到 confirm 的那一段
# ══════════════════════════════════════════════════════════════════════════
def _ready_context(name: str, *, publications: int = 2) -> dict[str, Any]:
    """一把新格:真造 accepted snapshot → 真跑物化器签预算 → 真造文章。"""
    from services.defensive_geo import activation_materializer as _mat

    ctx = _seed.seed_accepted_snapshot(publications=publications, activate=True)
    out = _mat.materialize_pending()
    assert out["materialized"] >= 1, f"物化器没物化:{out}"
    revision_id, article_hash = _seed.seed_article(
        brand=ctx["brandId"], body=f"包E 主链正文 {name} · 这是一段可以发布的稿子。")
    ctx.update({
        "planItemKey": f"plan_{name}",
        "articleRevisionId": revision_id,
        "expectedArticleHash": article_hash,
        "serviceProjectionId": _policy.service_projection_id_for(ctx["acceptedSnapshotId"]),
    })
    return ctx


def _preview(client: TestClient, ctx: dict[str, Any]) -> dict[str, Any]:
    resp = client.post(
        "/api/defensive-geo/publish/decision-snapshots/preview",
        headers={"Idempotency-Key": "pkge-p-" + uuid.uuid4().hex},
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
    return resp.json()


def _confirm(client: TestClient, snapshot_response: dict[str, Any]) -> Any:
    """🔴 三个值全部从**冻结面**里取,不是判据自己拼。

    自己拼 hash/version 等于让判据构造被测代码的输入中间值 ——
    那样 ``expectedHash`` 对不对就再也测不出来了(本仓记过:
    「判据必须驱动那一行,不能自己构造它的输出」)。
    """
    frozen = snapshot_response["snapshot"]
    return client.post(
        f"/api/defensive-geo/publish/decision-snapshots/{frozen['decisionSnapshotId']}/confirm",
        headers={"Idempotency-Key": "pkge-c-" + uuid.uuid4().hex},
        json={"expectedHash": frozen["canonicalHash"],
              "expectedVersion": frozen["snapshotVersion"]},
    )


def _confirmed_command(client: TestClient, name: str) -> dict[str, Any]:
    ctx = _ready_context(name)
    preview = _preview(client, ctx)
    snap = preview["snapshotResponse"]
    resp = _confirm(client, snap)
    assert resp.status_code == 200, f"confirm 失败:{resp.text[:900]}"
    body = resp.json()
    ctx["publishCommandId"] = body["publishCommandId"]
    ctx["preview"] = preview
    return ctx


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _assert_only_this_command_moved(actions: dict[str, Any], command_id: str) -> None:
    """本轮收敛里**只有这一条**命令动了钱。

    共享队列下,「钱包差额 == 本条 exact」这句话只有在别人没动的时候才成立。
    与其把别人的动作估进去,不如断言这一轮里根本没有别人 ——
    ``_drain()`` 已经把队列排空,这里是那件事的自证。
    """
    money_kinds = {"commit", "release", "release_never_dispatched", "preserve"}
    others = sorted({a["commandId"] for a in actions["items"]
                     if a["kind"] in money_kinds and a["commandId"] != command_id})
    assert not others, (
        f"这一轮还有别的命令在动钱:{others} —— 钱包差额里混进了别人家的账")


def _drain(rounds: int = 6) -> None:
    """🔴 **共享队列的分母清理**(本仓记过:共享队列判据先清分母)。

    ``reconcile_once`` 是全局的:它会顺手把**别的判据**留下的命令也收敛掉。
    于是「本条判据前后钱包差多少」里混进了别人家的 release/commit ——
    我第一版就是这么红的(实测 paid 差了 50,来自另一条命令的退款)。

    所以每条要量钱的判据在**布置好自己的场景之后、量钱之前**先把队列排空。
    排空是幂等的:跑到一轮零 action 为止。
    """
    for _ in range(rounds):
        out = _run(_worker.reconcile_tick(limit=200))
        if not out["actions"]:
            return


# ══════════════════════════════════════════════════════════════════════════
# 01 · 主链:确认 → 冻结 → 派发 → 回执 → 结算 commit
# ══════════════════════════════════════════════════════════════════════════
def test_01_confirm_freezes_exact_points(client: TestClient) -> None:
    """确认这一步**真的冻钱**(不是"应该会冻")。"""
    before = _seed.wallet(_seed.TENANT_A)
    ctx = _confirmed_command(client, "chain1")
    after = _seed.wallet(_seed.TENANT_A)

    cmd = _seed.command_row(ctx["publishCommandId"])
    exact = int(cmd["exact_settlement_points"])
    assert exact > 0, "冻结了 0 算力 —— 这条链没有被测对象"
    assert str(cmd["funding_state"]) == "frozen", cmd["funding_state"]
    assert after["frozen_points"] == before["frozen_points"] + exact, (
        f"钱包冻结量对不上:{before} → {after},exact={exact}")
    assert cmd["freeze_id"] is not None, "没有可恢复的资金句柄"

    # confirm 必须同事务落一条 outbox —— 否则没人会去发它(P0-1 的病根)
    rows = _seed.outbox_rows(ctx["publishCommandId"])
    assert len(rows) == 1 and str(rows[0]["status"]) == "pending", rows


def test_02_dispatch_claims_and_calls_provider_once(client: TestClient) -> None:
    """派发:claim → 法律门 → marker → 外调 → 落 canonical outcome。"""
    ctx = _confirmed_command(client, "chain2")
    result = _run(_worker.dispatch_pending(limit=10, provider_call=_accepted("SN-CHAIN2")))
    assert result["dispatched"] >= 1, f"一条都没派发:{result}"

    cmd = _seed.command_row(ctx["publishCommandId"])
    assert cmd["external_start_at"] is not None, "没有写 external-start marker"
    assert int(cmd["provider_call_count"]) == 1, cmd["provider_call_count"]
    assert str(cmd["canonical_publication_state"]) == "submitting", (
        f"provider 接单应投影成 submitting(hold_frozen),实得 "
        f"{cmd['canonical_publication_state']}")
    assert str(cmd["funding_state"]) == "frozen", (
        "上游只是接单,还没核实发布 —— 这时 commit 就是把'自报'当结算真值")
    assert str(cmd["provider_order_ref"]) == order_ref_for(
        "SN-CHAIN2", ctx["publishCommandId"]), (
        "上游单号没落到 047 那一列(或落的是**别的命令**那一个)—— "
        "回执核对将无键可依 / 或一个上游结果去结算两笔冻结")

    rows = _seed.outbox_rows(ctx["publishCommandId"])
    assert str(rows[0]["status"]) == "dispatched", rows


def test_03_verified_publication_commits_once(client: TestClient) -> None:
    """核实过的发布 ⇒ reconciler **commit 一次**,钱从冻结变成已扣。"""
    ctx = _confirmed_command(client, "chain3")
    _run(_worker.dispatch_pending(limit=10, provider_call=_accepted("SN-CHAIN3")))
    # 🔴 排空必须在**把本条推成终态之前** —— 推成终态之后再排空,
    #    排空自己就会把它 commit 掉,下面那一轮就什么都不剩(实测过一次)。
    _drain()

    # 上游同步表给出终态 + 核实轴通过(两轴判定,不是裸 status)
    conn = connect()
    try:
        cur = conn.cursor()
        _store.bump_status(
            cur, publish_command_id=ctx["publishCommandId"],
            canonical_publication_state="verified_published",
            url_verification_state="verified",
            url_availability_state="available",
        )
        conn.commit()
    finally:
        conn.close()

    before = _seed.wallet(_seed.TENANT_A)
    exact = int(_seed.command_row(ctx["publishCommandId"])["exact_settlement_points"])
    actions = _run(_worker.reconcile_tick(limit=200))
    assert any(a["kind"] == "commit" and a["commandId"] == ctx["publishCommandId"]
               for a in actions["items"]), actions
    _assert_only_this_command_moved(actions, ctx["publishCommandId"])

    cmd = _seed.command_row(ctx["publishCommandId"])
    assert str(cmd["funding_state"]) == "committed", cmd["funding_state"]
    after = _seed.wallet(_seed.TENANT_A)
    # 🔴 钱包语义以 ``middleware/billing.py`` 为准(那里逐字写着
    #    「commit 只动 frozen_points,三池不动」):
    #      freeze  : paid -= exact, frozen += exact   ← 钱在**冻结那一刻**就离开可用池
    #      commit  : frozen -= exact,paid 不动         ← 这一步是"确认花掉",不是再扣一次
    #      release : frozen -= exact,paid += exact     ← 退回原池
    #    我第一版按"commit 时再扣一次"写,真跑当场红 —— 记在这里免得下一个人重犯。
    assert after["frozen_points"] == before["frozen_points"] - exact, (
        f"commit 之后冻结没出池:{before} → {after}")
    assert after["paid_points"] == before["paid_points"], (
        f"commit 又动了一次可用池(重复扣费):{before} → {after}(exact={exact})")

    # 重放:再跑一轮不许再 commit 一次(FIN-01 只 commit 一次)
    again = _run(_worker.reconcile_tick(limit=50))
    assert not any(a["commandId"] == ctx["publishCommandId"] and a["kind"] == "commit"
                   for a in again["items"]), again
    assert _seed.wallet(_seed.TENANT_A) == after, "第二轮收敛又动了钱"


def test_04_authoritative_rejection_releases_once(client: TestClient) -> None:
    """上游**权威拒稿** = 零接单证据 ⇒ release 一次,算力全额退回。"""
    ctx = _confirmed_command(client, "chain4")
    exact = int(_seed.command_row(ctx["publishCommandId"])["exact_settlement_points"])

    _run(_worker.dispatch_pending(limit=10, provider_call=_rejected()))
    cmd = _seed.command_row(ctx["publishCommandId"])
    assert str(cmd["canonical_publication_state"]) == "rejected_no_effect", cmd

    # 🔴 排空必须发生在**本条已经摆好、还没收敛**之前 —— 但本条此刻已经
    #    是 rejected_no_effect,排空会顺手把它也收了。所以这里改为:
    #    量钱窗口内只允许本条动(下面 _assert_only_this_command_moved 自证)。
    before = _seed.wallet(_seed.TENANT_A)
    actions = _run(_worker.reconcile_tick(limit=200))
    _assert_only_this_command_moved(actions, ctx["publishCommandId"])
    cmd = _seed.command_row(ctx["publishCommandId"])
    assert str(cmd["funding_state"]) == "released", cmd["funding_state"]
    after = _seed.wallet(_seed.TENANT_A)
    assert after["frozen_points"] == before["frozen_points"] - exact
    assert after["paid_points"] == before["paid_points"] + exact, (
        f"确认零接单,算力必须全额退回原池:{before} → {after}(exact={exact})")


def test_05_unknown_outcome_never_releases(client: TestClient) -> None:
    """🔴 外调抛异常 = **结果未知**,绝不 release(§12.1 逐字)。

    这是最贵的一格:把接线故障伪装成正常退款,等于替供应商决定"他没收到"。
    """
    ctx = _confirmed_command(client, "chain5")
    before = _seed.wallet(_seed.TENANT_A)

    _run(_worker.dispatch_pending(limit=10, provider_call=_explodes()))
    cmd = _seed.command_row(ctx["publishCommandId"])
    assert str(cmd["canonical_publication_state"]) == "unknown", cmd
    assert str(cmd["funding_state"]) == "pending_reconciliation", cmd["funding_state"]

    _run(_worker.reconcile_tick(limit=50))
    cmd = _seed.command_row(ctx["publishCommandId"])
    assert str(cmd["funding_state"]) != "released", "未知态被自动退款了"
    after = _seed.wallet(_seed.TENANT_A)
    assert after["frozen_points"] == before["frozen_points"], "未知态动了冻结量"

    # 未知态必须能被运维看见(Z-1 队列 = commands 上的投影)
    conn = connect()
    try:
        cur = conn.cursor()
        queue = {str(r["publish_command_id"]) for r in _store.review_queue(cur, limit=200)}
        conn.rollback()
    finally:
        conn.close()
    assert ctx["publishCommandId"] in queue, "未知态没有出现在人工核验队列里 = 没有处置入口"


# ══════════════════════════════════════════════════════════════════════════
# 10-13 · 四个 kill window
# ══════════════════════════════════════════════════════════════════════════
def test_10_window1_replay_after_claim_returns_same_root(client: TestClient) -> None:
    """窗口①:幂等 claim 成功后、业务事务开始前被杀 ⇒ 同一 root,不建第二 command。"""
    ctx = _ready_context("kw1")
    preview = _preview(client, ctx)
    snap = preview["snapshotResponse"]
    before = _seed.counts()

    first = _confirm(client, snap)
    assert first.status_code == 200, first.text[:600]
    mid = _seed.counts()
    assert mid[_store.COMMAND_TABLE] == before[_store.COMMAND_TABLE] + 1

    # 同 snapshot 再 confirm(换 Idempotency-Key)⇒ 重放原对象,零新增
    second = _confirm(client, snap)
    assert second.status_code == 200, second.text[:600]
    assert second.json()["publishCommandId"] == first.json()["publishCommandId"], (
        "重放建出了第二条 command —— 窗口① 没收敛")
    after = _seed.counts()
    assert after[_store.COMMAND_TABLE] == mid[_store.COMMAND_TABLE], "多了一条 command"
    assert after["point_freezes"] == mid["point_freezes"], "重放又冻了一次钱"


def test_11_window2_business_object_freeze_outbox_are_one_transaction(
        client: TestClient) -> None:
    """窗口②:业务对象 + exact freeze + outbox **同事务**。

    判法:让 outbox 入队当场炸,断言 command 与冻结**一起**没有留下 ——
    三者要么全在要么全不在。只看"正常时三者都在"证明不了它们同事务。
    """
    ctx = _ready_context("kw2")
    preview = _preview(client, ctx)
    snap = preview["snapshotResponse"]
    before = _seed.counts()

    original = _store.enqueue_outbox
    _store.enqueue_outbox = lambda *a, **k: (_ for _ in ()).throw(  # type: ignore[assignment]
        RuntimeError("注入:outbox 入队失败(判据)"))
    try:
        resp = _confirm(client, snap)
    finally:
        _store.enqueue_outbox = original                  # type: ignore[assignment]

    assert resp.status_code >= 400, f"入队都失败了却返回成功:{resp.status_code}"
    after = _seed.counts()
    assert after[_store.COMMAND_TABLE] == before[_store.COMMAND_TABLE], (
        "outbox 失败但 command 留下了 —— 这就是「钱冻了但没人会去发」")
    assert after["point_freezes"] == before["point_freezes"], (
        "outbox 失败但冻结留下了 —— 三者不在同一事务里")
    assert after[_store.OUTBOX_TABLE] == before[_store.OUTBOX_TABLE]


def test_12_window3_recovery_never_retransmits(client: TestClient) -> None:
    """窗口③:已有 external-start marker ⇒ **不盲重传**,external start ≤ 1。"""
    ctx = _confirmed_command(client, "kw3")
    _run(_worker.dispatch_pending(limit=10, provider_call=_accepted("SN-KW3")))
    first = _seed.command_row(ctx["publishCommandId"])
    assert int(first["provider_call_count"]) == 1

    # 模拟"外调后被杀、队列超时被重新领走":把 outbox 放回 pending
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE {_store.OUTBOX_TABLE} SET status='pending', available_at=NOW(), "
            f"claim_token=NULL WHERE publish_command_id=%s",
            (ctx["publishCommandId"],))
        conn.commit()
    finally:
        conn.close()

    calls = {"n": 0}

    def _counting(_command):                              # noqa: ANN001
        calls["n"] += 1
        return _dispatch.ProviderResult(
            kind="accepted", raw_table="mhz_publish_order_items",
            raw_column="status", raw_value="submitted", detail="SN-KW3-SECOND")

    _run(_worker.dispatch_pending(limit=10, provider_call=_counting))
    assert calls["n"] == 0, (
        f"恢复路径又外调了 {calls['n']} 次 —— 那是在赌对方幂等(§12.3 窗口③ 逐字禁止)")

    second = _seed.command_row(ctx["publishCommandId"])
    assert int(second["provider_call_count"]) == 1, "external start 超过一次"
    assert str(second["funding_state"]) == "pending_reconciliation", second["funding_state"]
    assert str(second["provider_order_ref"]) == order_ref_for(
        "SN-KW3", ctx["publishCommandId"]), "重放把原单号覆盖了"


def test_13_window4_outcome_without_settlement_converges(client: TestClient) -> None:
    """窗口④:canonical outcome 已落、settlement 未完成 ⇒ reconciler 收敛到终点。"""
    ctx = _confirmed_command(client, "kw4")
    _run(_worker.dispatch_pending(limit=10, provider_call=_accepted("SN-KW4")))

    # worker 写完 canonical state 就被杀:这里直接把 outcome 落到终态,钱仍在 frozen
    conn = connect()
    try:
        cur = conn.cursor()
        _store.bump_status(
            cur, publish_command_id=ctx["publishCommandId"],
            canonical_publication_state="verified_published",
            url_verification_state="verified", url_availability_state="available")
        conn.commit()
    finally:
        conn.close()
    assert str(_seed.command_row(ctx["publishCommandId"])["funding_state"]) == "frozen"

    _run(_worker.reconcile_tick(limit=50))
    cmd = _seed.command_row(ctx["publishCommandId"])
    assert str(cmd["funding_state"]) == "committed", cmd["funding_state"]
    assert str(cmd["command_state"]) == "completed", cmd["command_state"]


def test_14_marker_is_durable_before_the_provider_call(client: TestClient) -> None:
    """🔴 窗口③ 的**前提**:marker 必须**已经落盘**再外调。

    marker 与外调同处一个未提交事务时,进程在外调途中被杀 ⇒ 回滚 ⇒ marker 消失
    ⇒ 下一次盲重传。判法:在 provider 回调里用**另一条连接**去读那一行 ——
    读得到才说明它已经提交了。
    """
    ctx = _confirmed_command(client, "kw5")
    seen: dict[str, Any] = {}

    def _peek(_command):                                  # noqa: ANN001
        probe = connect()
        try:
            cur = probe.cursor()
            cur.execute(
                f"SELECT external_start_at FROM {_store.COMMAND_TABLE} "
                f"WHERE publish_command_id = %s", (ctx["publishCommandId"],))
            row = cur.fetchone()
            seen["external_start_at"] = row["external_start_at"] if row else None
            probe.rollback()
        finally:
            probe.close()
        return _dispatch.ProviderResult(
            kind="accepted", raw_table="mhz_publish_order_items",
            raw_column="status", raw_value="submitted", detail="SN-KW5")

    _run(_worker.dispatch_pending(limit=10, provider_call=_peek))
    assert "external_start_at" in seen, "provider 回调没被调到 —— 本条没有被测对象"
    assert seen["external_start_at"] is not None, (
        "外调发生时,另一条连接看不到 external-start marker —— "
        "它还在未提交事务里,崩溃就会丢,下一次必然盲重传"
    )


def test_15_article_edited_after_confirm_is_not_published(client: TestClient) -> None:
    """🔴 DEL-08:发的必须是**冻结的那一版**正文。

    confirm 之后有人改了稿 ⇒ worker 不许把新版发出去,也不许"发个大概"。
    判法:确认后直接改库里的正文,断言 provider **一次都没被调**。
    """
    ctx = _confirmed_command(client, "editafter")
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE articles SET content = %s WHERE id = %s",
            ("有人在确认之后改了稿 —— 这一版没有任何人签过。",
             int(ctx["articleRevisionId"].split(":", 1)[1])))
        conn.commit()
    finally:
        conn.close()

    calls = {"n": 0}

    def _counting(_command):                              # noqa: ANN001
        calls["n"] += 1
        return _dispatch.ProviderResult(
            kind="accepted", raw_table="mhz_publish_order_items",
            raw_column="status", raw_value="submitted", detail="SN-EDIT")

    _run(_worker.dispatch_pending(limit=10, provider_call=_counting))
    assert calls["n"] == 0, (
        f"正文与冻结指纹对不上,provider 仍被调了 {calls['n']} 次 —— "
        "发出去的是一版没人签过的稿")
    cmd = _seed.command_row(ctx["publishCommandId"])
    assert cmd["external_start_at"] is None, "没发却写了 external-start marker"
    assert str(cmd["funding_state"]) == "frozen", "没发却改了钱向"


# ══════════════════════════════════════════════════════════════════════════
# 20-23 · 收敛器三窗口(工单②)
# ══════════════════════════════════════════════════════════════════════════
def _age_command(command_id: str, *, created_seconds: int = 0,
                 external_seconds: int = 0) -> None:
    """把某条命令的时间往前推。**只推被判据读的那一列**。

    (本仓记过:钉时间列要钉投影真正读的那一列,只钉 created_at 等于没钉。)
    """
    conn = connect()
    try:
        cur = conn.cursor()
        if created_seconds:
            cur.execute(
                f"UPDATE {_store.COMMAND_TABLE} SET created_at = NOW() - "
                f"(%s || ' seconds')::INTERVAL WHERE publish_command_id = %s",
                (created_seconds, command_id))
        if external_seconds:
            cur.execute(
                f"UPDATE {_store.COMMAND_TABLE} SET external_start_at = NOW() - "
                f"(%s || ' seconds')::INTERVAL WHERE publish_command_id = %s",
                (external_seconds, command_id))
        conn.commit()
    finally:
        conn.close()


def test_20_reconcile_requeues_frozen_but_never_dispatched(client: TestClient) -> None:
    """收敛窗口 A:已冻结但没有 outbox ⇒ 补入队,**钱不动**。"""
    ctx = _confirmed_command(client, "rc1")
    exact = int(_seed.command_row(ctx["publishCommandId"])["exact_settlement_points"])
    before = _seed.wallet(_seed.TENANT_A)

    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(f"DELETE FROM {_store.OUTBOX_TABLE} WHERE publish_command_id = %s",
                    (ctx["publishCommandId"],))
        conn.commit()
    finally:
        conn.close()
    _age_command(ctx["publishCommandId"], created_seconds=3600)

    actions = _run(_worker.reconcile_tick(limit=50))
    assert any(a["kind"] == "requeue_outbox" and a["commandId"] == ctx["publishCommandId"]
               for a in actions["items"]), actions
    assert len(_seed.outbox_rows(ctx["publishCommandId"])) == 1, "没补上 outbox"
    assert _seed.wallet(_seed.TENANT_A) == before, "补入队却动了钱"
    # 🔴 [撕锁返修 · MUT-18] 只量钱包是**不够的**:一发变异往这一项里塞
    #    ``bump_status(funding_state='released')`` —— 它只翻 DB 那一列、
    #    没走 ``release_exact``,于是钱包一分不动,上面那条照样绿。变异存活了。
    #    资金状态是**两处**:钱包的池 + command 上那一列。两处都要量,
    #    否则"钱没动"只证明了其中一半。
    cmd = _seed.command_row(ctx["publishCommandId"])
    assert str(cmd["funding_state"]) == "frozen", (
        f"补入队顺手改了 command 的钱向:{cmd['funding_state']} —— "
        "这一项逐字是「补入队,钱不动」")
    assert str(cmd["command_state"]) not in ("completed", "failed", "cancelled"), (
        f"补入队把命令收成了终态:{cmd['command_state']}")
    assert int(cmd["exact_settlement_points"]) == exact


def test_21_reconcile_holds_external_started_without_outcome(client: TestClient) -> None:
    """收敛窗口 B:外调后久无回执 ⇒ ``unknown`` + ``pending_reconciliation``,零重传零退款。"""
    ctx = _confirmed_command(client, "rc2")
    _run(_worker.dispatch_pending(limit=10, provider_call=_accepted("SN-RC2")))

    # 回到"上游还没给终态"的那一格,并把 marker 推老
    conn = connect()
    try:
        cur = conn.cursor()
        _store.bump_status(cur, publish_command_id=ctx["publishCommandId"],
                           canonical_publication_state="queued")
        conn.commit()
    finally:
        conn.close()
    _age_command(ctx["publishCommandId"], external_seconds=7200)

    before = _seed.wallet(_seed.TENANT_A)
    actions = _run(_worker.reconcile_tick(limit=50))
    assert any(a["kind"] == "hold_unknown" and a["commandId"] == ctx["publishCommandId"]
               for a in actions["items"]), actions
    cmd = _seed.command_row(ctx["publishCommandId"])
    assert str(cmd["canonical_publication_state"]) == "unknown"
    assert str(cmd["funding_state"]) == "pending_reconciliation"
    assert int(cmd["provider_call_count"]) == 1, "收敛时又外调了一次"
    assert _seed.wallet(_seed.TENANT_A) == before, "未知态动了钱"


def test_22_reconcile_releases_when_queue_gave_up_and_never_dispatched(
        client: TestClient) -> None:
    """收敛窗口 C(包E 补的第 7 项):队列判定跑不通 + **零 external-start** ⇒ release 一次。

    这一格的证据是 ``external_start_at IS NULL`` —— 权威的零接单证据,
    不是"超时了就退"。反向对照见 :func:`test_05_unknown_outcome_never_releases`:
    有 marker 的那一格永远不会走到这里。
    """
    ctx = _confirmed_command(client, "rc3")
    exact = int(_seed.command_row(ctx["publishCommandId"])["exact_settlement_points"])
    _drain()                                              # 共享队列:先清别人的账
    before = _seed.wallet(_seed.TENANT_A)

    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE {_store.OUTBOX_TABLE} SET status='needs_review', "
            f"last_error='注入:通道一直不可用' WHERE publish_command_id = %s",
            (ctx["publishCommandId"],))
        conn.commit()
    finally:
        conn.close()

    actions = _run(_worker.reconcile_tick(limit=200))
    assert any(a["kind"] == "release_never_dispatched"
               and a["commandId"] == ctx["publishCommandId"]
               for a in actions["items"]), actions
    _assert_only_this_command_moved(actions, ctx["publishCommandId"])
    cmd = _seed.command_row(ctx["publishCommandId"])
    assert str(cmd["funding_state"]) == "released"
    assert str(cmd["command_state"]) == "needs_action", (
        "退了钱却没有下一步 —— §0.5.6 铁律:任何阻塞必须自带解决方案")
    assert cmd["external_start_at"] is None
    after = _seed.wallet(_seed.TENANT_A)
    assert after["frozen_points"] == before["frozen_points"] - exact
    assert after["paid_points"] == before["paid_points"] + exact, (
        f"零外调,算力必须全额退回原池:{before} → {after}(exact={exact})")


def test_22b_external_started_is_never_released_even_when_the_queue_gave_up(
        client: TestClient) -> None:
    """🔴 [撕锁返修 · MUT-17] 第 7 项的**证据条件**必须真的在守。

    ═══════════════════════════════════════════════════════════════════
    这条判据是变异存活逼出来的
    ═══════════════════════════════════════════════════════════════════
    MUT-17 把 ``_release_never_dispatched`` 的
    ``external_start_at IS NULL AND provider_call_count = 0`` 两行删掉,
    **一条判据都没红**。原因是我原来的两条判据都够不到那一格:
      · ``test_05`` 的命令是 ``pending_reconciliation``(不是 ``frozen``);
      · ``test_22`` 的命令本来就没有 marker。
    于是「已经外调过、队列又放弃了」这一格没人守 —— 而那正是最贵的一格:
    盲退款 = 替供应商决定"他没收到"。

    这里把那一格造出来:**有 marker** + ``frozen`` + outbox ``needs_review``。
    正确行为 = 一分钱都不许退(它得走人工核验,不是自动退款)。
    """
    ctx = _confirmed_command(client, "rc5")
    _run(_worker.dispatch_pending(limit=10, provider_call=_accepted("SN-RC5")))
    assert _seed.command_row(ctx["publishCommandId"])["external_start_at"] is not None

    # 回到 frozen + 队列放弃:这一格与 test_22 的差别**只有 marker 在不在**。
    conn = connect()
    try:
        cur = conn.cursor()
        _store.bump_status(cur, publish_command_id=ctx["publishCommandId"],
                           funding_state="frozen", command_state="running",
                           canonical_publication_state="queued")
        cur.execute(
            f"UPDATE {_store.OUTBOX_TABLE} SET status='needs_review', "
            f"last_error='注入:队列放弃' WHERE publish_command_id = %s",
            (ctx["publishCommandId"],))
        conn.commit()
    finally:
        conn.close()

    exact = int(_seed.command_row(ctx["publishCommandId"])["exact_settlement_points"])
    before = _seed.wallet(_seed.TENANT_A)
    actions = _run(_worker.reconcile_tick(limit=200))

    released = [a for a in actions["items"]
                if a["commandId"] == ctx["publishCommandId"]
                and a["kind"] in ("release", "release_never_dispatched")]
    assert not released, (
        f"已经外调过的命令被自动退款了:{released} —— "
        "零 external-start 才是权威的零接单证据,有 marker 时退款是在赌对方没收到")
    cmd = _seed.command_row(ctx["publishCommandId"])
    assert str(cmd["funding_state"]) != "released", cmd["funding_state"]
    after = _seed.wallet(_seed.TENANT_A)
    assert after["frozen_points"] == before["frozen_points"], (
        f"有 marker 的命令冻结量动了:{before} → {after}(exact={exact})")


def test_23_transport_not_configured_defers_instead_of_burning_the_attempt(
        client: TestClient) -> None:
    """「通道没配好」≠「外调失败」:放回队列、不动状态、不动钱。"""
    from services.defensive_geo.publish import provider_transport as _tp

    ctx = _confirmed_command(client, "rc4")
    before = _seed.wallet(_seed.TENANT_A)

    original = _tp.resolve
    _tp.resolve = lambda *a, **k: (_ for _ in ()).throw(  # type: ignore[assignment]
        _tp.TransportNotConfigured("注入:外发通道未配置"))
    try:
        result = _run(_worker.dispatch_pending(limit=10))   # 不传 provider_call ⇒ 走生产解析
    finally:
        _tp.resolve = original                            # type: ignore[assignment]

    assert result["deferred"] >= 1, f"没有被延后:{result}"
    cmd = _seed.command_row(ctx["publishCommandId"])
    assert cmd["external_start_at"] is None, "通道没配好却写了 external-start marker"
    assert str(cmd["funding_state"]) == "frozen", "通道没配好就改了钱向"
    assert _seed.wallet(_seed.TENANT_A) == before

    rows = _seed.outbox_rows(ctx["publishCommandId"])
    assert str(rows[0]["status"]) == "pending", rows
    assert rows[0]["last_error"], "延后没有留下原因 —— 运维查不到为什么一直不发"


def _platform_freeze(points: int, brand_id: int) -> tuple[int, str]:
    """在**平台直营账号**上开一笔真冻结,返回 ``(freeze_id, freeze_backend)``。

    🔴 [R3] 为什么夹具要多做这一步:R3 之后平台腿是**真结算**的。
       如果还沿用真确认留下的那把钱包句柄(payer = 客户),
       那条 release 会退到**客户**钱包上 —— 那是夹具形状不对,不是被测代码不对。
       生产里平台腿的 ``payer_user_id`` 就是平台账号,夹具照着做。
    """
    from middleware.billing import freeze_points

    conn = connect()
    try:
        cur = conn.cursor()
        res = _run(freeze_points(
            # [合流 2026-08-24] 跟包G 段二③统一码走(publish_funding.PUBLISH_FEATURE_CODE)
            _seed.PLATFORM_ACCOUNT, "media_proxy_publish",
            # task_ref 前缀与 publish_funding 同族 —— conftest 的 session 清理
            # 按这个前缀删冻结行,不留跨 session 残留。
            task_ref="defgeo_publish_r3_" + uuid.uuid4().hex[:12],
            brand_id=int(brand_id), extra_cost=int(points),
            reason="包E R3 判据:平台成本腿冻结", _cursor=cur))
        conn.commit()
    finally:
        conn.close()
    assert res and res.get("freeze_id"), f"平台账冻结没拿到句柄:{res}"
    return int(res["freeze_id"]), str(res.get("freeze_table") or "legacy")


def _make_platform_leg(command_id: str, *, policy: str = "admin_platform_ledger",
                       principal: str = "platform_cost_center",
                       funding_state: str = "exempt_recorded",
                       repoint_freeze: bool = True) -> None:
    """把一条**真确认出来的**命令翻成平台成本腿。

    翻三列身份(``funding_policy`` / ``principal_kind`` / ``funding_state``),
    并把冻结句柄**改挂到平台账号自己那笔真冻结上**(见 :func:`_platform_freeze`)。
    ``repoint_freeze=False`` 用于反向对照臂(非平台腿的异常形状)。
    """
    conn = connect()
    try:
        cur = conn.cursor()
        if repoint_freeze:
            cur.execute(
                f"SELECT exact_settlement_points, brand_id FROM {_store.COMMAND_TABLE} "
                f"WHERE publish_command_id = %s", (command_id,))
            row = cur.fetchone()
            points, brand_id = int(row["exact_settlement_points"]), int(row["brand_id"])
        conn.commit()
    finally:
        conn.close()

    fid, backend = _platform_freeze(points, brand_id) if repoint_freeze else (None, None)

    conn = connect()
    try:
        cur = conn.cursor()
        if repoint_freeze:
            cur.execute(
                f"UPDATE {_store.COMMAND_TABLE} SET funding_policy=%s, principal_kind=%s, "
                f"funding_state=%s, payer_user_id=%s, freeze_id=%s, freeze_backend=%s "
                f"WHERE publish_command_id = %s",
                (policy, principal, funding_state, _seed.PLATFORM_ACCOUNT, fid, backend,
                 command_id))
        else:
            cur.execute(
                f"UPDATE {_store.COMMAND_TABLE} SET funding_policy=%s, principal_kind=%s, "
                f"funding_state=%s WHERE publish_command_id = %s",
                (policy, principal, funding_state, command_id))
        cur.execute(
            f"UPDATE {_store.OUTBOX_TABLE} SET status='needs_review', "
            f"last_error='注入:通道一直不可用' WHERE publish_command_id = %s",
            (command_id,))
        conn.commit()
    finally:
        conn.close()


def test_24_platform_cost_leg_settles_on_the_platform_account(
        client: TestClient) -> None:
    """🔴 [R3 · Owner 批口径①] 第 7 项的**平台成本腿**:与钱包腿**同构**地真结算。

    ═══════════════════════════════════════════════════════════════════
    这条判据改过一次向 —— 是被口径变更改的,不是被实现改的
    ═══════════════════════════════════════════════════════════════════
    R1/R2 时它断言的是「收敛状态、**零钱包动作**」,并附一条"现状钉住":
    平台账的 ``frozen_points`` 只增不减。那条现状钉住是**故意留给这一刻的** ——
    Owner 拍板口径① 之后它按自己的设计翻红,提醒我来改向,而不是让口径变更悄悄溜过。

    现在的正确行为:零 external-start = 权威零接单 ⇒ 对**平台账那笔冻结**
    走 ``release_exact``(同一个真值表、同一句 ``_assert_direction``,平台腿不豁免)。
    客户钱包仍然一个数都不动 —— 那是 ``exempt_recorded`` 的含义。

    ═══════════════════════════════════════════════════════════════════
    [R1 起] 补这条判据时先证伪过一件事:这条腿原本**执行不到**
    ═══════════════════════════════════════════════════════════════════
    迁移 044 的 ``chk_defgeo_pcmd_platform_state`` 逐字规定平台成本中心的
    ``funding_state`` 恒为 ``exempt_recorded``,而第 7 项的查询原来只收
    ``funding_state = 'frozen'`` —— 守卫说"我管平台腿",候选集把平台腿全滤掉了。
    先把候选集补上 ``exempt_recorded``(产品代码一行),这条判据才够得到被测行。
    (本仓记过:夹具做窄不会变红,只会让判据够不到被测代码。)

    正确行为 = **平台账真退、客户钱包零动作**。

    🟡 现役签发链只签 ``personal_wallet``,所以这条腿在生产的分母是 0 ——
       **纵深防御,非承重**,交付单里如实标注。
    """
    ctx = _confirmed_command(client, "rc6")
    cid = ctx["publishCommandId"]
    exact = int(_seed.command_row(cid)["exact_settlement_points"])
    _drain()                                              # 共享队列:先清别人的账
    _make_platform_leg(cid)
    before = _seed.wallet(_seed.TENANT_A)
    platform_before = _seed.wallet(_seed.PLATFORM_ACCOUNT)

    actions = _run(_worker.reconcile_tick(limit=200))

    mine = [a for a in actions["items"] if a["commandId"] == cid]
    assert any(a["kind"] == "release_never_dispatched" for a in mine), (
        f"平台成本腿没有被结算:{mine} —— 候选集或守卫有一边把它漏了")

    cmd = _seed.command_row(cid)
    assert str(cmd["canonical_publication_state"]) == "rejected_no_effect", cmd
    assert str(cmd["command_state"]) == "needs_action", (
        "收敛了却没有下一步 —— §0.5.6:任何阻塞必须自带解决方案")
    # 🔴 资金状态是**两处**(钱包的池 + command 上那一列),两处都要量。
    assert str(cmd["funding_state"]) == "exempt_recorded", (
        f"平台腿的钱向被改成了 {cmd['funding_state']!r} —— 平台成本恒为 exempt_recorded"
        "(迁移 044 chk_defgeo_pcmd_platform_state 也是这么写的)")
    assert cmd["settled_at"] is not None, (
        "结算了却没落 settled_at —— 平台腿的 fundingState 是常量,"
        "没有这个标记它永远掉不出候选集,每一轮都会被再结算一次")

    # 平台账**真退**:冻结回落、算力回原池。
    platform_after = _seed.wallet(_seed.PLATFORM_ACCOUNT)
    assert platform_after["frozen_points"] == platform_before["frozen_points"] - exact, (
        f"平台账的冻结没有回落:{platform_before} → {platform_after}(exact={exact})")
    assert platform_after["paid_points"] == platform_before["paid_points"] + exact, (
        f"零外调却没把算力退回平台账原池:{platform_before} → {platform_after}")
    # 客户钱包**一个数都不动** —— 那才是 exempt_recorded 的含义。
    after = _seed.wallet(_seed.TENANT_A)
    assert after == before, (
        f"平台腿结算动了客户钱包:{before} → {after}(exact={exact})")


def test_24b_non_platform_leg_outside_frozen_is_never_released(
        client: TestClient) -> None:
    """🔴 [R1 返修] 上一条把候选集加宽了 —— 这条是那次加宽的**反向对照**。

    加宽候选集(``funding_state IN ('frozen','exempt_recorded')``)不许顺手
    加宽退款面。造一个本不该存在的形状:**非**平台腿却停在 ``exempt_recorded``。
    正确行为 = 一分钱不退、一列不改,留给人工队列。

    (本仓记过:顺手多修一处 = 顺手多欠一条判据。判据要打在新旧行为
     真正分岔的那一格 —— 分岔点就是"非平台腿 + 非 frozen"。)
    """
    ctx = _confirmed_command(client, "rc7")
    cid = ctx["publishCommandId"]
    _drain()
    _make_platform_leg(cid, policy="personal_wallet", principal="personal",
                       funding_state="exempt_recorded", repoint_freeze=False)
    before = _seed.wallet(_seed.TENANT_A)

    actions = _run(_worker.reconcile_tick(limit=200))

    mine = [a for a in actions["items"] if a["commandId"] == cid]
    assert not [a for a in mine
                if a["kind"] in ("release", "release_never_dispatched",
                                 "platform_never_dispatched")], (
        f"非平台腿的异常形状被自动处置了:{mine} —— 不猜就是不猜")
    cmd = _seed.command_row(cid)
    assert str(cmd["funding_state"]) == "exempt_recorded", cmd["funding_state"]
    assert str(cmd["canonical_publication_state"]) != "rejected_no_effect", cmd
    assert _seed.wallet(_seed.TENANT_A) == before


# ══════════════════════════════════════════════════════════════════════════
# 90 · 生产路径零测试短路
# ══════════════════════════════════════════════════════════════════════════
_CHAIN_FILES = (
    "services/defensive_geo/publish/publish_worker.py",
    "services/defensive_geo/publish/provider_transport.py",
    "services/defensive_geo/publish/publish_outbox.py",
    "services/defensive_geo/publish/reconciler.py",
    "services/defensive_geo/activation_materializer.py",
    "services/defensive_geo/publish/execution_budget_policy.py",
)


def test_90_no_test_shortcuts_on_the_production_path() -> None:
    """本仓铁律:链上文件 ``is_test`` / ``dry_run`` / ``sandbox`` 零命中。

    🔴 判**代码**不判散文:docstring / 注释里引用这三个词是病历,
       删证据不是删缺陷(本仓记过:引用裁决原文会让裸串结构锚判红)。
       所以先把注释与字符串剥掉,再看剩下的代码里有没有。
    """
    import io
    import tokenize

    banned = ("is_test", "dry_run", "sandbox")
    offenders: list[str] = []
    for rel in _CHAIN_FILES:
        src = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
        code_only: list[str] = []
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            code_only.append(tok.string)
        joined = " ".join(code_only)
        for word in banned:
            if word in joined:
                offenders.append(f"{rel}:{word}")
    assert not offenders, (
        f"生产链上出现测试短路词:{offenders} —— 注入必须走参数,不走分支")


def test_91_shortcut_detector_is_alive(tmp_path) -> None:                # noqa: ANN001
    """探测器活性自证:真代码抓得到、注释抓不到。两向都验。"""
    import io
    import tokenize

    def _hits(text: str) -> bool:
        code = [t.string for t in tokenize.generate_tokens(io.StringIO(text).readline)
                if t.type not in (tokenize.COMMENT, tokenize.STRING)]
        return "dry_run" in " ".join(code)

    assert _hits("if dry_run:\n    pass\n"), "真代码里的 dry_run 漏了 —— 上面那条是恒绿的"
    assert not _hits("# 历史:曾经有过 dry_run 分支,已删\nx = 1\n"), (
        "把注释里的引用当成缺陷 —— 那会逼人删证据")


def test_92_worker_default_resolves_the_production_transport() -> None:
    """不传参数时,worker 走的必须是**生产登记表**,不是某个默认假 transport。"""
    from services.defensive_geo.publish import provider_transport as _tp

    assert _tp.registered_keys() == (_tp.PRODUCTION_TRANSPORT_KEY,), (
        f"登记表里不止生产那一个:{_tp.registered_keys()} —— "
        "多出来的那个就是「分支式测试短路」的另一种长相")
    import inspect

    sig = inspect.signature(_worker.dispatch_pending)
    assert sig.parameters["provider_call"].default is None, (
        "provider_call 有了非 None 默认值 —— 生产就不再走 resolve() 了")
