"""B-4(= Codex P1-3)· 平台发布腿必须进预算上限统计。

═══════════════════════════════════════════════════════════════════════
🔴 这一族钉的那句话:**分母是机械枚举出来的,不是手抄的**
═══════════════════════════════════════════════════════════════════════
``budget_usage`` 原来的两个 IN 列表是手抄的:

    reserved  = frozen / pending_reconciliation / quarantined
    committed = committed

而迁移 044 的 ``chk_defgeo_pcmd_platform_state`` 保证平台腿恒
``exempt_recorded`` ⇒ 它**一格都不在分母里**:每一笔平台发布对
global / scope 两道 cap 的贡献都是 0,cap 说"还剩很多",
而平台钱包已经真冻出去了(``freeze_points(platform_uid, ...)``)。

本仓记过:**手写分母漏掉的那一项不会让任何判据变红**。
所以修法不是"再手抄一格进去",而是让覆盖性本身成为一条会红的断言。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from services.defensive_geo.publish import publish_settlement as _settle
from services.defensive_geo.publish import store as _store

from tests.defgeo_wob_publish_funding_2026_08_25._chain import (   # noqa: F401
    base, client, drain, drain_outbox,
)
from tests.defgeo_wob_publish_funding_2026_08_25.conftest import connect

ROOT = Path(__file__).resolve().parents[2]
MIGRATION_044 = ROOT / "db" / "migration_044_defgeo_publish_decision_2026_08_21.sql"


# ══════════════════════════════════════════════════════════════════════════
# ① 分母的机械来源:与迁移 044 的 CHECK 逐字对账
# ══════════════════════════════════════════════════════════════════════════
def test_b4_01_funding_states_match_the_migration_check() -> None:
    """``store.FUNDING_STATES`` ≡ 044 的 ``chk_defgeo_pcmd_funding_state``。

    🔴 从**迁移文件**里正则抽,不手抄 —— 手抄的清单漏一格不会有任何判据变红。
    """
    sql = MIGRATION_044.read_text(encoding="utf-8")
    m = re.search(
        r"ADD CONSTRAINT chk_defgeo_pcmd_funding_state\s*\n\s*CHECK \(funding_state IN \(([^)]*)\)\)",
        sql)
    assert m, "044 里那条 CHECK 的形态变了 —— 探针写废了(不是'没有那条约束')"
    declared = {v.strip().strip("'") for v in m.group(1).replace("\n", " ").split(",")}
    assert declared == set(_store.FUNDING_STATES), (
        f"迁移声明 {sorted(declared)} vs store.FUNDING_STATES "
        f"{sorted(_store.FUNDING_STATES)} —— 分母与真表漂了")


def test_b4_02_migration_check_is_also_live_in_the_database() -> None:
    """再从**真库**读一次(文件对了不等于库里装的是那一条)。"""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint "
            "WHERE conname = 'chk_defgeo_pcmd_funding_state' "
            "  AND conrelid = 'public.defgeo_publish_commands'::regclass")
        row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None, "库里没有这条 CHECK —— 夹具没装齐"
    for state in _store.FUNDING_STATES:
        assert f"'{state}'" in row["d"], f"库里的 CHECK 缺 {state}: {row['d']}"


def test_b4_03_budget_denominator_covers_every_funding_state() -> None:
    """覆盖性自证:少一格 ⇒ ``budget_contribution_map`` **抛**,不静默按 0。"""
    mapping = _store.budget_contribution_map()
    assert set(mapping) == set(_store.FUNDING_STATES)
    assert set(mapping.values()) <= set(_store.BUDGET_CONTRIBUTIONS)
    assert mapping["exempt_recorded"] == "by_settlement", (
        "平台腿又被排除在预算分母之外了 —— 那正是 P1-3")


def test_b4_04_coverage_assert_is_alive(monkeypatch) -> None:
    """反向对照:分母**真的**漏一格时会红(不是"我以为它会红")。"""
    trimmed = {k: v for k, v in _store._BUDGET_CONTRIBUTION.items()   # noqa: SLF001
               if k != "quarantined"}
    monkeypatch.setattr(_store, "_BUDGET_CONTRIBUTION", trimmed)
    with pytest.raises(_store.StoreError) as err:
        _store.budget_contribution_map()
    assert "quarantined" in str(err.value), str(err.value)


def test_b4_05_commit_direction_states_are_derived_not_hardcoded() -> None:
    """"哪些 canonical 态算已扣" 由 §15.7 真值表现算,不是手抄的两个字符串。"""
    derived = set(_store._commit_direction_states())              # noqa: SLF001
    expect = {s for s in _settle.CANONICAL_STATES
              if _settle.settlement_direction(s) in ("commit", "preserve_historical_commit")}
    assert derived == expect and derived, (derived, expect)


# ══════════════════════════════════════════════════════════════════════════
# ② 真库行为:平台腿真的进了 reserved / committed
# ══════════════════════════════════════════════════════════════════════════
_SNAP_ID = "wob-budget-snap"


def _mk_snapshot_and_commands(rows: list[tuple[str, int, str | None, str]]) -> str:
    """造一批 command 挂在同一个 executionBudgetSnapshotId 下。

    ``rows`` = [(funding_state, points, settled_at_sql, canonical_state), ...]
    只写状态列 —— 本判据测的是 ``budget_usage`` 的**分母**,不是签发链。
    """
    import json
    import uuid

    snap_key = f"{_SNAP_ID}-{uuid.uuid4().hex[:8]}"
    conn = connect()
    try:
        cur = conn.cursor()
        for i, (state, points, settled, canonical) in enumerate(rows):
            # 🔴 一格一 slot:``defgeo_pds_one_open_per_slot`` 只允许每个 slot
            #    有一份 open 快照 —— 一个 slot 塞多份是生产不会有的形状。
            cur.execute(
                "INSERT INTO defgeo_publish_slots (publish_slot_id, tenant_owner_id, "
                " service_projection_id, accepted_snapshot_id, plan_item_key, brand_id, "
                " publish_item_request_id) "
                "VALUES (%s, 9701, 'sp-wob-budget', 424242, %s, 9801, %s) "
                "RETURNING publish_slot_id",
                (f"slot_{snap_key}_{i}", f"plan_{snap_key}_{i}", f"pir_{snap_key}_{i}"))
            slot = cur.fetchone()["publish_slot_id"]
            ds_id = f"ds_{snap_key}_{i}"
            cur.execute(
                "INSERT INTO defgeo_publish_decision_snapshots "
                "(decision_snapshot_id, publish_slot_id, tenant_owner_id, "
                " snapshot_version, canonical_hash, frozen_payload, lifecycle, "
                " expires_at, idempotency_key, request_canonical_hash) "
                "VALUES (%s,%s,9701,1,%s,%s::jsonb,'open', NOW() + INTERVAL '1 day', %s, %s)",
                (ds_id, slot, f"h_{snap_key}_{i}",
                 json.dumps({"executionBudgetSnapshotId": snap_key}),
                 f"sidem_{snap_key}_{i}", f"srch_{snap_key}_{i}"))
            principal = ("platform_cost_center" if state == "exempt_recorded"
                         else "tenant_owner")
            policy = ("admin_platform_ledger" if state == "exempt_recorded"
                      else "personal_wallet")
            cur.execute(
                "INSERT INTO defgeo_publish_commands "
                "(publish_command_id, publish_slot_id, decision_snapshot_id, "
                " decision_snapshot_hash, command_canonical_hash, command_generation, "
                " lineage_kind, tenant_owner_id, actor_user_id, brand_id, "
                " publish_item_request_id, article_revision_id, article_hash, "
                " public_media_key, canonical_root_domain_key, funding_policy, "
                " principal_kind, exact_settlement_points, freeze_task_ref, "
                # 🔴 044 的 chk_defgeo_pcmd_freeze_handle:钱包/组织腿且金额 > 0 时
                #    freeze 三元组必须齐全。夹具照真 CHECK 供,不供生产不会供的形状。
                " freeze_id, freeze_backend, payer_user_id, "
                " funding_state, command_state, canonical_publication_state, "
                " idempotency_key, request_canonical_hash, settled_at) "
                "VALUES (%s,%s,%s,%s,%s,1,'root',9701,9701,9801,%s,'article:1','ah',"
                f" %s,'crdk',%s,%s,%s,%s,%s,'legacy',9701,%s,'completed',%s,%s,%s,"
                f" {settled or 'NULL'})",
                (f"pcmd_{snap_key}_{i}", slot, ds_id, f"h_{snap_key}_{i}",
                 f"cch_{snap_key}_{i}", f"pir_{snap_key}_{i}",
                 f"pmk_{snap_key}_{i}", policy, principal, points,
                 f"defgeo_publish_{snap_key}_{i}", 900000 + i,
                 state, canonical, f"idem_{snap_key}_{i}", f"rch_{snap_key}_{i}"))
        conn.commit()
    finally:
        conn.close()
    return snap_key


def _usage(snap_key: str) -> dict[str, int]:
    conn = connect()
    try:
        return _store.budget_usage(conn.cursor(), execution_budget_snapshot_id=snap_key)
    finally:
        conn.close()


def test_b4_10_unsettled_platform_leg_counts_as_reserved() -> None:
    """平台腿**没结算** ⇒ 算 reserved(钱正冻在平台钱包上)。

    🔴 「删掉修复即红」:把 ``exempt_recorded`` 的贡献改回 ``none``,
       这条从 130 变回 0。
    """
    snap = _mk_snapshot_and_commands([
        ("frozen", 100, None, "queued"),
        ("exempt_recorded", 130, None, "queued"),
    ])
    usage = _usage(snap)
    assert usage["reservedPoints"] == 230, (
        f"平台腿没进 reserved:{usage} —— 每一笔平台发布对 cap 的贡献是 0,"
        "cap 说还剩很多而平台钱包已经真冻出去了")
    assert usage["committedPoints"] == 0, usage


def test_b4_11_committed_platform_leg_counts_as_committed() -> None:
    """平台腿**按 commit 向收尾** ⇒ 算 committed(成本已落地)。"""
    snap = _mk_snapshot_and_commands([
        ("committed", 90, "NOW()", "verified_published"),
        ("exempt_recorded", 130, "NOW()", "verified_published"),
    ])
    usage = _usage(snap)
    assert usage["committedPoints"] == 220, usage
    assert usage["reservedPoints"] == 0, usage


def test_b4_11b_retracted_platform_leg_still_counts_as_committed() -> None:
    """``retracted``(preserve_historical_commit)那一格也算已扣。

    🔴 这条是撕锁 MUT-B4-03 逼出来的:把 ``_commit_direction_states`` 从
       "按真值表现算" 改回手抄 ``("verified_published",)``,行为判据一条都没红 ——
       因为当时没有任何一条真库判据用到 ``retracted``。
       手抄清单漏掉的那一格,只有真的有人踩上去才会红。
    """
    snap = _mk_snapshot_and_commands([
        ("exempt_recorded", 70, "NOW()", "retracted"),
    ])
    assert _usage(snap) == {"reservedPoints": 0, "committedPoints": 70}, (
        "下架保留历史 commit 的那一笔没算进已扣 —— 预算 cap 会凭空多出一格")


def test_b4_12_released_platform_leg_counts_nowhere() -> None:
    """平台腿**按 release 向收尾** ⇒ 两边都不算(钱退回去了,预算可再占用)。"""
    snap = _mk_snapshot_and_commands([
        ("released", 50, "NOW()", "rejected_no_effect"),
        ("exempt_recorded", 130, "NOW()", "rejected_no_effect"),
    ])
    usage = _usage(snap)
    assert usage == {"reservedPoints": 0, "committedPoints": 0}, (
        f"退回去的钱仍占着 cap:{usage} —— §3.4「明确 release 后才可重新占用」")


def test_b4_13_wallet_leg_buckets_are_unchanged() -> None:
    """反向对照:钱包腿四格的归属**一格没变**(改分母不许顺手改别人)。"""
    snap = _mk_snapshot_and_commands([
        ("frozen", 1, None, "queued"),
        ("pending_reconciliation", 2, None, "unknown"),
        ("quarantined", 4, None, "unknown"),
        ("committed", 8, "NOW()", "verified_published"),
        ("released", 16, "NOW()", "rejected_no_effect"),
    ])
    assert _usage(snap) == {"reservedPoints": 7, "committedPoints": 8}


def test_b4_14_cap_gate_sees_the_platform_leg(client) -> None:
    """端到端:平台腿占掉的额度**真的**让 ``check_and_lock_budget`` 报 blocker。

    分母对了但没人用它,等于没修 —— 这一条把分母接到那道闸上。
    """
    from services.defensive_geo.publish import publish_funding as _pf

    snap = _mk_snapshot_and_commands([("exempt_recorded", 300, None, "queued")])
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO defgeo_provider_execution_budgets "
            "(execution_budget_snapshot_id, budget_version, tenant_owner_id, "
            " accepted_snapshot_id, service_projection_id, global_cap_points, "
            " scope_key, scope_cap_points, funding_policy, payer_user_id, budget_hash) "
            "VALUES (%s, 1, 9701, 424242, 'sp-wob', 400, 'media_publication', 400, "
            "        'personal_wallet', 9701, %s)",
            (snap, f"bh_{snap}"))
        conn.commit()
        _budget, blockers = _pf.check_and_lock_budget(
            cur, tenant_owner_id=9701, accepted_snapshot_id=424242,
            service_projection_id="sp-wob", required_points=200)
        conn.rollback()
    finally:
        conn.close()
    assert blockers, (
        "cap=400、平台腿已占 300、再要 200 —— 却没有 blocker:"
        "平台腿对 cap 的贡献仍是 0")
    assert blockers[0].reserved == 300, blockers[0]
    assert blockers[0].remaining == 100, blockers[0]
