"""B-6 · **外选变异逼出来的补洞**(判据作者没参与选题的那一批)。

═══════════════════════════════════════════════════════════════════════
🔴 这一族存在的理由:自己写判据又自己挑变异 = 自己出题自己批改
═══════════════════════════════════════════════════════════════════════
本单第一轮 26 发**自选**变异跑完 20/26 精确之后,一个只读代码与判据、
没写过判据的独立审计方另挑了一批,并**预判全部存活**。它挑中的面几乎
都是同一类:「注释/docstring 声称在守某件事,而没有任何一条判据站在那件事上」。

本文件按那份清单逐条补,补的都是**行为**判据(不是再加一把结构锁):

  · 上限的**边界**(>= 还是 >)与**取值**(5 还是 3);
  · 「无物理腿」那条早退分支 —— AST 探针为它写了特例,行为面却零判据;
  · ``mark_settled`` 的「至多一次」那一半(另一半在 ``_settle_exact`` 顶部,
    重复调用根本走不到这里);
  · 预算分母覆盖自证的**取值域**那一半(只验过「缺一格」);
  · 钱向 → commandState 闭表的**方向**(所有 provider 替身都返 accepted,
    ``rejected`` 那条钱向从没被驱动过);
  · 上游单号绑定谓词里 ``kind == "accepted"`` 那一半;
  · strict 反查的**假阳性方向**(收紧过头:合法订单被误判撞号);
  · Z-1 三条动作里完全没被测过的那一条(``admin_hold``);
  · ``_assert_direction`` 的「未起步态不许放行」。
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from services.defensive_geo.publish import publish_funding as _pf
from services.defensive_geo.publish import publish_outbox as _dispatch
from services.defensive_geo.publish import settlement_review as _rv
from services.defensive_geo.publish import store as _store
from services.meijiehezi import client as _mhz

from tests.defgeo_wob_publish_funding_2026_08_25 import _seed
from tests.defgeo_wob_publish_funding_2026_08_25._chain import (   # noqa: F401
    base, client, confirmed_command, drain, drain_outbox, must_not_be_called,
    rejected, run,
)
from tests.defgeo_wob_publish_funding_2026_08_25.conftest import connect


def _row(cid: str) -> dict:
    conn = connect()
    try:
        row = _store.get_command_any_tenant(conn.cursor(), publish_command_id=cid)
        assert row is not None
        return dict(row)
    finally:
        conn.close()


def _bump(cid: str, **updates) -> None:
    conn = connect()
    try:
        _store.bump_status(conn.cursor(), publish_command_id=cid, **updates)
        conn.commit()
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
# ① 上限的边界与取值(MUT-AUD-01 / 02)
# ══════════════════════════════════════════════════════════════════════════
def test_b6_01_commit_ceiling_fires_on_exactly_the_nth_attempt(
        client, monkeypatch) -> None:
    """转人工发生在**第 N 次**,不是第 N+1 次。

    🔴 `` >= `` 改成 `` > `` 时,``test_b1_30`` 那条(循环 N+1 轮)照样绿 ——
       它只问"最后有没有转人工",不问"第几次"。多挂一轮 = 多冻一轮客户的钱。
    """
    from tests.defgeo_wob_publish_funding_2026_08_25._chain import fake_billing
    from services.defensive_geo.publish import publish_worker as _worker

    drain()
    drain_outbox()
    ctx = confirmed_command(client, "b6_ceiling_edge")
    cid = ctx["publishCommandId"]
    _bump(cid, canonical_publication_state="verified_published")

    import middleware.billing as _billing
    monkeypatch.setattr(_billing, "commit_freeze", fake_billing("not_found"))

    fired_at = None
    for n in range(1, _pf.SETTLE_MAX_ATTEMPTS + 3):
        run(_worker.reconcile_tick(limit=200))
        if str(_row(cid)["command_state"]) == "quarantined":
            fired_at = n
            break
    assert fired_at == _pf.SETTLE_MAX_ATTEMPTS, (
        f"转人工发生在第 {fired_at} 轮,应当是第 {_pf.SETTLE_MAX_ATTEMPTS} 轮 —— "
        "上限的边界错一格 = 多冻客户一轮的钱")
    # 🔴 转人工那一手**只记原因、不算一次重试**(``increment=False``)——
    #    算进去会让「第几次转人工」这个数字比真实多一。
    assert int(_row(cid)["settlement_attempts"]) == _pf.SETTLE_MAX_ATTEMPTS, (
        f"计数 {_row(cid)['settlement_attempts']} != 上限 "
        f"{_pf.SETTLE_MAX_ATTEMPTS} —— 转人工那一手把自己也算成了一次重试")


def test_b6_02_settle_max_attempts_is_the_same_number_as_the_diagnosis_chain() -> None:
    """上限**取值**与诊断链同源。

    🔴 ``publish_funding`` 的注释逐字写「诊断链 ``_SETTLE_MAX_ATTEMPTS`` 同值」,
       而 ``test_b1_90`` 只对账了四条谓词字符串,**没对账这个数**。
       同一谓词写两处 ⇒ 必有一处没人验,这一处就是它。
    """
    import services.diagnosis_runs as _dr

    assert _pf.SETTLE_MAX_ATTEMPTS == _dr._SETTLE_MAX_ATTEMPTS, (    # noqa: SLF001
        f"发布链 {_pf.SETTLE_MAX_ATTEMPTS} vs 诊断链 "
        f"{_dr._SETTLE_MAX_ATTEMPTS} —— 「照抄那套语义」里包含这个数")   # noqa: SLF001


# ══════════════════════════════════════════════════════════════════════════
# ② 「无物理腿」那条早退分支(MUT-AUD-04)
# ══════════════════════════════════════════════════════════════════════════
def _no_physical_leg_command(name: str) -> str:
    """造一条**库层合法**的「无物理腿」命令:0 算力 + 无冻结句柄。

    ``chk_defgeo_pcmd_freeze_handle`` 允许两种:``exact_settlement_points = 0``,
    或非钱包/组织腿。这里用 0 算力那一格 —— 直接把一条 50 算力的钱包腿
    ``freeze_id`` 抹成 NULL 是**生产不可能**出现的形状,
    夹具供了生产不会供的东西 = 判据在测一个不存在的场景。
    """
    import json
    import uuid

    key = f"{name}-{uuid.uuid4().hex[:8]}"
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO defgeo_publish_slots (publish_slot_id, tenant_owner_id, "
            " service_projection_id, accepted_snapshot_id, plan_item_key, brand_id, "
            " publish_item_request_id) "
            "VALUES (%s, 9701, 'sp-wob-noleg', 424243, %s, 9801, %s)",
            (f"slot_{key}", f"plan_{key}", f"pir_{key}"))
        cur.execute(
            "INSERT INTO defgeo_publish_decision_snapshots "
            "(decision_snapshot_id, publish_slot_id, tenant_owner_id, snapshot_version, "
            " canonical_hash, frozen_payload, lifecycle, expires_at, idempotency_key, "
            " request_canonical_hash) "
            "VALUES (%s,%s,9701,1,%s,%s::jsonb,'open', NOW() + INTERVAL '1 day', %s, %s)",
            (f"ds_{key}", f"slot_{key}", f"h_{key}",
             json.dumps({"executionBudgetSnapshotId": f"ebs_{key}"}),
             f"sidem_{key}", f"srch_{key}"))
        cur.execute(
            "INSERT INTO defgeo_publish_commands "
            "(publish_command_id, publish_slot_id, decision_snapshot_id, "
            " decision_snapshot_hash, command_canonical_hash, command_generation, "
            " lineage_kind, tenant_owner_id, actor_user_id, brand_id, "
            " publish_item_request_id, article_revision_id, article_hash, "
            " public_media_key, canonical_root_domain_key, funding_policy, "
            " principal_kind, exact_settlement_points, freeze_task_ref, "
            " funding_state, command_state, canonical_publication_state, "
            " idempotency_key, request_canonical_hash) "
            "VALUES (%s,%s,%s,%s,%s,1,'root',9701,9701,9801,%s,'article:1','ah',%s,'crdk',"
            " 'personal_wallet','personal',0,%s,'frozen','queued','verified_published',%s,%s)",
            (f"pcmd_{key}", f"slot_{key}", f"ds_{key}", f"h_{key}", f"cch_{key}",
             f"pir_{key}", f"pmk_{key}", f"defgeo_publish_{key}",
             f"idem_{key}", f"rch_{key}"))
        conn.commit()
    finally:
        conn.close()
    return f"pcmd_{key}"


def test_b6_10_zero_amount_command_settles_without_touching_billing(
        monkeypatch) -> None:
    """0 元 / 无冻结句柄的命令:**不调 billing**,但业务终态照样收敛。

    🔴 这条路整包原来零判据 —— 讽刺的是 ``test_b1_92`` 的 AST 探针
       专门为它写了 ``no_leg`` 特例(结构上被承认存在,行为上没人验)。
    """
    import middleware.billing as _billing
    from tests.defgeo_wob_publish_funding_2026_08_25._chain import raising_billing

    cid = _no_physical_leg_command("b6_zero_leg")
    row = _row(cid)
    assert row["freeze_id"] is None and int(row["exact_settlement_points"]) == 0

    monkeypatch.setattr(_billing, "commit_freeze",
                        raising_billing(AssertionError("无物理腿却去动了钱")))
    conn = connect()
    try:
        cur = conn.cursor()
        outcome = run(_pf.commit_exact(cur, row, reason="无物理腿探针",
                                       terminal_command_state="completed"))
        conn.commit()
    finally:
        conn.close()

    assert outcome.settled and outcome.reason == "no_physical_leg", outcome
    after = _row(cid)
    assert after["settled_at"] is not None, "无物理腿也要收敛业务终态"
    assert str(after["funding_state"]) == "committed", after["funding_state"]
    assert str(after["command_state"]) == "completed", after["command_state"]


def test_b6_11_zero_amount_command_with_a_stale_version_is_retry_not_settled() -> None:
    """无物理腿 + CAS 落空 ⇒ **retry**,不是"已结算"。

    这一格是外选变异 MUT-AUD-04 打的那一枪:把它报成 settled,
    ``settled_at`` 不写而调用方以为收了口 —— 从此没有任何一轮自动收敛会回来看它。
    """
    cid = _no_physical_leg_command("b6_zero_cas")
    stale = _row(cid)
    _bump(cid, status_reason="别人先动了一手")
    assert int(_row(cid)["status_version"]) > int(stale["status_version"])

    conn = connect()
    try:
        cur = conn.cursor()
        outcome = run(_pf.commit_exact(cur, stale, reason="无物理腿 + 过期版本",
                                       terminal_command_state="completed"))
        conn.commit()
    finally:
        conn.close()
    assert outcome.verdict == "retry", outcome
    assert outcome.reason == "terminal_cas_missed", outcome
    assert _row(cid)["settled_at"] is None, "CAS 落空却报成已结算"


# ══════════════════════════════════════════════════════════════════════════
# ③ mark_settled 的「至多一次」那一半(MUT-AUD-05)
# ══════════════════════════════════════════════════════════════════════════
def test_b6_20_mark_settled_is_at_most_once(client) -> None:
    """直接驱动 ``store.mark_settled``:第二次必须 **0 行**、时间戳不变。

    🔴 ``test_b1_13`` 打的是 ``_settle_exact`` 顶部那个早退(另一半),
       重复调用根本走不到 ``mark_settled``。这道 ``WHERE settled_at IS NULL``
       自上线起没有任何判据 —— 而"已结算"正是 ④⑦ 候选集与 Z-1 队列
       **共同**依赖的那个事实。
    """
    drain()
    drain_outbox()
    cid = confirmed_command(client, "b6_at_most_once")["publishCommandId"]
    conn = connect()
    try:
        cur = conn.cursor()
        first = _store.mark_settled(cur, publish_command_id=cid)
        conn.commit()
    finally:
        conn.close()
    assert first is True, "第一次没写进去 —— 场景没搭对"
    stamped = _row(cid)["settled_at"]

    conn = connect()
    try:
        cur = conn.cursor()
        second = _store.mark_settled(cur, publish_command_id=cid)
        conn.commit()
    finally:
        conn.close()
    assert second is False, "第二次仍然改到了行 —— 「至多一次」那一半没了"
    assert _row(cid)["settled_at"] == stamped, "已结算的时间戳被后到的一手改写了"


# ══════════════════════════════════════════════════════════════════════════
# ④ 预算分母覆盖自证的**取值域**那一半(MUT-AUD-06)
# ══════════════════════════════════════════════════════════════════════════
def test_b6_30_budget_contribution_rejects_an_illegal_value(monkeypatch) -> None:
    """分母表里出现一个**拼错的取值** ⇒ 当场抛,不静默按 0。

    🔴 ``test_b4_04`` 只喂过「缺一格」(missing/extra 那一半)。
       另一半的现实后果很具体:把 ``reserved`` 敲成 ``reserverd``,
       ``budget_usage`` 的 ``reserved_states`` 会静默变成空集合 ⇒
       那一格对 cap 的贡献恒 0 = P1-3 换个入口原样复活。
    """
    typo = dict(_store._BUDGET_CONTRIBUTION)               # noqa: SLF001
    typo["frozen"] = "reserverd"
    monkeypatch.setattr(_store, "_BUDGET_CONTRIBUTION", typo)
    with pytest.raises(_store.StoreError) as err:
        _store.budget_contribution_map()
    assert "reserverd" in str(err.value), str(err.value)


# ══════════════════════════════════════════════════════════════════════════
# ⑤ 钱向 → commandState 闭表的**方向**(MUT-AUD-03)
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("direction,expect", [
    ("commit", "completed"),
    ("preserve_historical_commit", "completed"),
    ("release", "failed"),
    ("none", "queued"),
    ("hold_frozen", "running"),
    ("hold_or_quarantine", "settlement_pending"),
])
def test_b6_40_command_state_table_is_direction_correct(
        direction: str, expect: str) -> None:
    """闭表逐格。**对调 commit / release 两格必须红**。"""
    assert _dispatch._command_state_for(direction) == expect      # noqa: SLF001


def test_b6_41_authoritative_rejection_lands_on_failed_not_completed(client) -> None:
    """真链驱动那条从没被驱动过的钱向:上游**权威拒稿** ⇒ commandState=failed。

    🔴 判据包里所有 provider 替身原来都返 ``accepted``(``rejected()`` 定义了
       却一次都没用)—— 死夹具会让人误以为 ``rejected_no_effect`` 那条钱向
       被测过。它没有。
    """
    from services.defensive_geo.publish import publish_worker as _worker

    drain()
    drain_outbox()
    cid = confirmed_command(client, "b6_rejected")["publishCommandId"]
    run(_worker.dispatch_pending(limit=50, provider_call=rejected()))
    row = _row(cid)
    assert str(row["canonical_publication_state"]) == "rejected_no_effect", row
    assert str(row["command_state"]) == "failed", (
        f"权威拒稿落成了 {row['command_state']} —— 钱向与命令态对调了")


# ══════════════════════════════════════════════════════════════════════════
# ⑥ 上游单号绑定谓词的 ``kind == "accepted"`` 那一半(MUT-AUD-11)
# ══════════════════════════════════════════════════════════════════════════
def test_b6_50_non_accepted_detail_never_becomes_an_order_ref(client) -> None:
    """``unknown`` 那一格的 ``detail`` 是**人话/字段名**,不是单号 —— 不许写进那一列。

    🔴 现实形态:``provider_transport._mhz_publish`` 在「上游接单但未返回单号」时
       返 ``detail='上游接单但未返回单号'``(**常量**),
       ``ConfirmationRequiredError`` 时返字段名。这些一旦写进 051 唯一索引所在的
       那一列,第二条命令直接撞唯一,而第一条把一个非订单号当成了结算身份。
    """
    from services.defensive_geo.publish import publish_worker as _worker

    def _unknown_with_detail(_command):                   # noqa: ANN001
        return _dispatch.ProviderResult(
            kind="unknown", raw_table="mhz_publish_order_items",
            raw_column="status", raw_value="awaiting_sync",
            detail="上游接单但未返回单号")

    drain()
    drain_outbox()
    cid = confirmed_command(client, "b6_unknown_detail")["publishCommandId"]
    run(_worker.dispatch_pending(limit=50, provider_call=_unknown_with_detail))
    row = _row(cid)
    assert row["provider_order_ref"] is None, (
        f"非 accepted 的 detail 被当成单号写进去了:{row['provider_order_ref']!r}")
    assert "未返回单号" in str(row["status_reason"] or ""), (
        "那句人话也没进 status_reason —— 两边都丢了")


# ══════════════════════════════════════════════════════════════════════════
# ⑦ strict 反查的**假阳性**方向(MUT-AUD-14)
# ══════════════════════════════════════════════════════════════════════════
def test_b6_60_strict_batch_still_binds_when_upstream_lists_one_order_twice() -> None:
    """同一条订单在上游列表里出现两行 ⇒ 那是**同一单**,仍然要绑。

    🔴 B-5 全族原来只验了「命中多条不绑」这一个方向。收紧过头的后果
       与 P1-5 相反但同样贵:每一篇都落 AmbiguousResponseError → awaiting_sync,
       钱冻着、货发出去了、单号绑不上。
    """
    from tests.defgeo_wob_publish_funding_2026_08_25.test_b5_provider_ref_uniqueness_pg import (  # noqa: E501
        _TITLE, _client_with,
    )

    items = [
        {"title": _TITLE, "resource_id": 991001, "ordernum": "11SAME"},
        {"title": _TITLE, "resource_id": 991001, "ordernum": "11SAME"},
    ]
    c = _client_with(items)
    got = asyncio.run(c._lookup_order_sns_for_batch(          # noqa: SLF001
        list_endpoint="/x", title=_TITLE, target_resource_ids=[991001],
        retries=1, delay_seconds=0, strict_unique=True))
    assert got.get(991001) == "11SAME", (
        f"同一单被列了两次就不绑了:{got} —— 收紧过头,合法订单永远绑不上")


# ══════════════════════════════════════════════════════════════════════════
# ⑧ Z-1 三条动作里从没被测过的那一条(MUT-AUD-13)
# ══════════════════════════════════════════════════════════════════════════
def test_b6_70_admin_hold_never_moves_money(client) -> None:
    """「维持隔离」= 留痕 + 计时,**一分钱都不动**。

    🔴 ``admin_hold`` 在整包判据里原来一次都没出现:既没验它不动资金态,
       也没验它必填理由。而它恰好是老板拍板「必填理由」的那一条 ——
       把它的 target state 改成 ``committed``,admin 点「先放着」= 静默扣款。
    """
    drain()
    drain_outbox()
    cid = confirmed_command(client, "b6_hold")["publishCommandId"]
    _bump(cid, funding_state="pending_reconciliation",
          command_state="settlement_pending",
          canonical_publication_state="unknown")
    before_wallet = _seed.wallet(_seed.TENANT_A)

    conn = connect()
    try:
        cur = conn.cursor()
        outcome = run(_rv.apply_admin_action(
            cur, publish_command_id=cid, action="admin_hold",
            admin_user_id=9703, reason="等上游回执,先不动钱"))
        conn.commit()
    finally:
        conn.close()

    row = _row(cid)
    assert str(row["funding_state"]) == "quarantined", row["funding_state"]
    assert row["settled_at"] is None, "「维持隔离」把账结了 —— 那是扣款,不是隔离"
    assert _seed.wallet(_seed.TENANT_A) == before_wallet, "「先放着」动了钱"
    assert outcome.action == "admin_hold"
    assert _rv._ACTION_TARGET_STATE["admin_hold"] is None, (      # noqa: SLF001
        "「维持隔离」被配了一个目标资金态 —— 它的定义就是**不改资金态**")


def test_b6_71_admin_hold_requires_a_reason(client) -> None:
    """没有理由的「先放着」= 把死路写进账本 —— 应用层这道闸必须挡住。"""
    drain()
    drain_outbox()
    cid = confirmed_command(client, "b6_hold_noreason")["publishCommandId"]
    _bump(cid, funding_state="pending_reconciliation",
          command_state="settlement_pending")
    conn = connect()
    try:
        cur = conn.cursor()
        with pytest.raises(_rv.ReviewError):
            run(_rv.apply_admin_action(
                cur, publish_command_id=cid, action="admin_hold",
                admin_user_id=9703, reason="   "))
        conn.rollback()
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
# ⑨ ``_assert_direction`` 的「未起步态不许放行」(审计 §7)
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("state", ["not_started", "queued", "submitting",
                                   "reported_success_unverified"])
def test_b6_80_direction_guard_refuses_the_none_and_hold_states(state: str) -> None:
    """钱向是 ``none`` / ``hold_frozen`` 的态,commit 与 release **都**不许放行。

    🔴 这道断言存在的理由逐字是「调用方传错一个字符串就能退款」;
       把它放宽成 ``direction not in (expected, "none")`` 之前,
       没有一条判据用未起步态试探过它 —— 那条理由从没被证明成立。
    """
    cmd = {"canonical_publication_state": state}
    for intent in ("commit", "release"):
        with pytest.raises(_pf.FundingError) as err:
            _pf._assert_direction(cmd, expected=intent)   # noqa: SLF001
        assert state in str(err.value), str(err.value)


def test_b6_81_settle_exact_refuses_them_too() -> None:
    """出口那一层同样挡住(守卫写在里面,不是只写在私有函数上)。"""
    for intent, fn in (("commit", _pf.commit_exact), ("release", _pf.release_exact)):
        with pytest.raises(_pf.FundingError):
            run(fn(None, {"canonical_publication_state": "queued"}, reason="探针"))
