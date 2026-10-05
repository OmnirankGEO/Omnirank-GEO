"""WP4 判据 · 激活 outbox + 付费命令准入(ACT-04/05/06/07/10/11/14/15)。

并发判据打**真 PG16 多连接**,不打线程 mock:ACT-06 的承重是数据库唯一约束,
用 mock 只能证明"我写的 Python 会捕 IntegrityError",证明不了
"20 个真事务里恰好一个赢"。
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg2
import pytest

from services.defensive_geo import funding_projection
from services.defensive_geo.activation_outbox import (
    ActivationEnqueueError,
    OUTBOX_TABLE,
    STATUSES,
    enqueue_activation,
    has_external_start,
    mark_external_start,
    mark_materialized,
    orphaned_activation_roots,
    unmaterialized_roots,
)
from services.defensive_geo.work_admission import (
    ADMITTING_MILESTONES,
    WORK_KINDS,
    ZERO_EFFECT_MILESTONES,
    admit,
)

ROOT = Path(__file__).resolve().parents[2]
MIGRATION_043 = ROOT / "db" / "migration_043_defgeo_activation_outbox_2026_08_21.sql"

HASH_A = "a" * 64


def _one(row):
    """取单列结果,兼容 RealDictCursor(生产)与 tuple 游标。

    夹具改成与生产同构(RealDictCursor)之后,判据自己若还按下标取值就会
    KeyError —— 判据代码同样要按**生产的行形态**写,
    否则"把夹具修对了"反而会把判据打红,让人误以为是回归。
    """
    if row is None:
        return None
    return next(iter(row.values())) if isinstance(row, dict) else row[0]


# ------------------------------------------------------------ ACT-06 并发恰一

def test_enqueue_is_idempotent_on_replay(conn, probe_rows):
    """活性自证 + §3.2「重放返回原对象」。"""
    with conn.cursor() as cur:
        first = enqueue_activation(
            cur, accepted_snapshot_id=probe_rows["snap_a"], quote_id=9001,
            brand_id=9001, accepted_snapshot_hash=HASH_A,
        )
        second = enqueue_activation(
            cur, accepted_snapshot_id=probe_rows["snap_a"], quote_id=9001,
            brand_id=9001, accepted_snapshot_hash=HASH_A,
        )
    assert first.created is True
    assert second.created is False
    assert first.outbox_id == second.outbox_id


def test_act06_twenty_concurrent_activations_create_exactly_one(schema_loaded, probe_rows_committed):
    """🔴 ACT-06:20 并发只产生一个 activation root。

    承重的是 ``defgeo_activation_outbox_root_unique``,**不是应用层**。
    变异「删掉那条 UNIQUE」必须让本条转红 —— 见 §4 撕锁记录。
    """
    snap_id, quote_id, brand_id = probe_rows_committed

    def attempt(_i: int) -> bool:
        c = psycopg2.connect(schema_loaded)
        try:
            with c, c.cursor() as cur:
                root = enqueue_activation(
                    cur, accepted_snapshot_id=snap_id, quote_id=quote_id,
                    brand_id=brand_id, accepted_snapshot_hash=HASH_A,
                )
            return root.created
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=20) as pool:
        created_flags = list(pool.map(attempt, range(20)))

    assert sum(1 for f in created_flags if f) == 1, created_flags

    c = psycopg2.connect(schema_loaded)
    try:
        with c.cursor() as cur:
            cur.execute(
                f"SELECT count(*) FROM {OUTBOX_TABLE}"
                f" WHERE accepted_snapshot_id=%s AND quote_id=%s",
                (snap_id, quote_id),
            )
            assert cur.fetchone()[0] == 1
    finally:
        c.close()


# ------------------------------------------------------- ACT-07 fail-closed

def test_enqueue_raises_instead_of_returning_a_success_shape(conn):
    """🔴 fail-closed 的全部意义:失败**不许**长成正常返回值。

    这里用一个不存在的快照 id 触发外键失败。若日后有人把异常吞掉改成
    ``return {"enqueued": False}``,本条立刻转红。
    """
    with conn.cursor() as cur:
        with pytest.raises(ActivationEnqueueError):
            enqueue_activation(
                cur, accepted_snapshot_id=987654321, quote_id=9001,
                brand_id=9001, accepted_snapshot_hash=HASH_A,
            )


def test_enqueue_rejects_malformed_hash_before_touching_the_db(conn):
    with conn.cursor() as cur:
        with pytest.raises(ActivationEnqueueError):
            enqueue_activation(
                cur, accepted_snapshot_id=9001, quote_id=9001,
                brand_id=9001, accepted_snapshot_hash="nope",
            )


def test_activation_module_imports_no_money(conn):
    """ACT-06/13「activation 本身 execution freeze=0」的结构性保证。

    最稳的"零资金副作用"实现 = 这一层根本没有能力碰钱。
    """
    src = (ROOT / "services" / "defensive_geo" / "activation_outbox.py").read_text(
        encoding="utf-8"
    )
    code = re.sub(r'""".*?"""', "", src, flags=re.S)      # 剥 docstring,只看真代码
    for forbidden in ("wallet", "billing", "freeze_points", "charge", "deduct"):
        assert forbidden not in code, forbidden


#: 🔴 [工单C 2026-08-25] 分母从**动这张表的全部迁移**机械枚举,不再只钉 043 一份。
#:
#: ACT-06/07/13 的「activation 零资金列」是**这张表**的不变量,不是 043 那一个
#: 文件的不变量。只钉 043 的话,任何后来的迁移(052 已经是第二个)都可以
#: 往里塞一列 points,而**没有任何判据会红** ——
#: 本仓记过「手写分母漏掉的那一项不会让任何判据变红」。
#: 这里按目录现扫:凡是文件里出现 `defgeo_activation_outbox` 的迁移都进分母。
def _migrations_touching_the_outbox() -> list:
    hits = []
    for path in sorted((ROOT / "db").glob("migration_*.sql")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "defgeo_activation_outbox" in text:
            hits.append(path)
    return hits


def test_no_migration_puts_a_funding_column_on_the_activation_outbox():
    """ACT-06/07/13:activation 本身 execution freeze = 0 ⇒ 这张表不出现金额列。

    🔴 分母是**算出来的**(现扫 db/migration_*.sql),不是手抄的一个文件名。
       043 那一版只锁住了自己;052 加列时这条判据完全没有意见 ——
       如果 052 塞的是 points 而不是身份,一样不会红。
    """
    migrations = _migrations_touching_the_outbox()
    # 分母自证:至少要扫到 043 与 052 两份,否则 glob 写错了(空分母恒绿)
    names = {p.name for p in migrations}
    assert MIGRATION_043.name in names, f"分母里没有 043:{sorted(names)}"
    assert len(migrations) >= 2, f"只扫到 {len(migrations)} 份迁移 —— 分母可疑"

    for path in migrations:
        body = re.sub(r"--.*$", "", path.read_text(encoding="utf-8"), flags=re.M)
        for forbidden in ("points", "wallet", "amount", "balance", "freeze"):
            assert forbidden.lower() not in body.lower(), f"{path.name}: {forbidden}"


# --------------------------------------------- §12.3 kill window 3:外调 marker

def test_external_start_marker_is_write_once(conn, probe_rows):
    with conn.cursor() as cur:
        root = enqueue_activation(
            cur, accepted_snapshot_id=probe_rows["snap_a"], quote_id=9001,
            brand_id=9001, accepted_snapshot_hash=HASH_A,
        )
        assert has_external_start(cur, root.outbox_id) is False
        mark_external_start(cur, root.outbox_id, "provider-req-1")
        assert has_external_start(cur, root.outbox_id) is True
        # 二次写入不得覆盖 —— 覆盖等于抹掉"我可能已经外调过"
        mark_external_start(cur, root.outbox_id, "provider-req-2")
        cur.execute(
            f"SELECT external_start_marker FROM {OUTBOX_TABLE} WHERE id=%s",
            (root.outbox_id,),
        )
        assert _one(cur.fetchone()) == "provider-req-1"


def test_materialize_is_terminal_and_replay_safe(conn, probe_rows):
    with conn.cursor() as cur:
        root = enqueue_activation(
            cur, accepted_snapshot_id=probe_rows["snap_a"], quote_id=9001,
            brand_id=9001, accepted_snapshot_hash=HASH_A,
        )
        assert mark_materialized(cur, root.outbox_id) is True
        assert mark_materialized(cur, root.outbox_id) is False   # 重放不重复物化


def test_half_state_is_unrepresentable(conn, probe_rows):
    """materialized 必须带时间戳;裸改 status 会被 CHECK 拒(ACT-07 库层那一半)。"""
    with conn.cursor() as cur:
        root = enqueue_activation(
            cur, accepted_snapshot_id=probe_rows["snap_a"], quote_id=9001,
            brand_id=9001, accepted_snapshot_hash=HASH_A,
        )
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                f"UPDATE {OUTBOX_TABLE} SET status='materialized' WHERE id=%s",
                (root.outbox_id,),
            )


# ------------------------------- 裁定判据:事实丢了 reconciler 能否从别处重建

def test_lost_outbox_fact_is_rebuildable_from_the_accepted_pointer(conn, probe_rows):
    """🔴 这条正面兑现裁定给的判别标准。

    先把会话标成"客户已确认",但**不**入队(模拟事实丢失),
    reconciler 必须能从 accepted 指针把它找回来。
    找不回来 = fail-open 在本链上永远不可接受;找得回来 =
    fail-closed 的理由收窄为"失败不许伪装成成功"(见模块 docstring)。
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE keyword_selection_sessions SET customer_confirmed_snapshot_id=%s,"
            " customer_confirmed_snapshot_hash=%s, customer_confirmed_at=now()"
            " WHERE id=%s",
            (probe_rows["snap_a"], HASH_A, probe_rows["session_a"]),
        )
        orphans = orphaned_activation_roots(cur)
        assert any(o["quote_id"] == 9001 for o in orphans), orphans

        # 入队之后就不再是孤儿 —— 反向对照,证明这条查询不是恒真。
        enqueue_activation(
            cur, accepted_snapshot_id=probe_rows["snap_a"], quote_id=9001,
            brand_id=9001, accepted_snapshot_hash=HASH_A,
        )
        assert not any(o["quote_id"] == 9001 for o in orphaned_activation_roots(cur))


def test_unmaterialized_roots_feeds_the_reconciler(conn, probe_rows):
    with conn.cursor() as cur:
        root = enqueue_activation(
            cur, accepted_snapshot_id=probe_rows["snap_a"], quote_id=9001,
            brand_id=9001, accepted_snapshot_hash=HASH_A,
        )
        assert any(r["id"] == root.outbox_id for r in unmaterialized_roots(cur))
        mark_materialized(cur, root.outbox_id)
        assert not any(r["id"] == root.outbox_id for r in unmaterialized_roots(cur))


def test_status_set_matches_the_migration_check(conn):
    """闭集分母:模块常量与库 CHECK 必须一致,漏一个值会静默放行。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conname='defgeo_activation_outbox_status_closed'"
        )
        definition = _one(cur.fetchone())
    for status in STATUSES:
        assert f"'{status}'" in definition, status
    assert len(re.findall(r"'(\w+)'", definition)) == len(STATUSES)


# --------------------------------------------------- ACT-11 / 15:里程碑闸

def test_act11_only_service_activated_admits_paid_work():
    assert ADMITTING_MILESTONES == frozenset({"service_activated"})
    for kind in WORK_KINDS:
        ok = admit(work_kind=kind, milestone="service_activated",
                   funding_policy="personal_wallet",
                   balance_points=1000, required_points=10)
        assert ok.admitted, kind


@pytest.mark.parametrize("milestone", sorted(ZERO_EFFECT_MILESTONES))
def test_act11_pre_activation_milestones_have_zero_side_effects(milestone):
    for kind in WORK_KINDS:
        v = admit(work_kind=kind, milestone=milestone,
                  funding_policy="personal_wallet",
                  balance_points=10 ** 9, required_points=1)
        assert not v.admitted
        assert v.reason == "service_not_activated"
        assert (v.freeze_count, v.outbox_count, v.provider_calls) == (0, 0, 0)


def test_act11_rich_balance_cannot_buy_past_the_milestone_gate():
    """余额再多也不能越过里程碑闸 —— 顺序错了会让"没开工"被报成"余额不足"。"""
    v = admit(work_kind="content_generation", milestone="customer_accepted",
              funding_policy="personal_wallet",
              balance_points=10 ** 9, required_points=1)
    assert v.reason == "service_not_activated"
    assert v.reason != "execution_funding_pending"


# ------------------------------------------------- ACT-04 / 05:钱与审批的顺序

def test_act05_approval_is_checked_before_balance():
    """🔴「普通成员无审批时……**不扣个人钱包**」。

    审批必须早于余额判断:反过来的话,个人余额够的组织成员会被静默走个人钱包。
    """
    v = admit(work_kind="content_generation", milestone="service_activated",
              funding_policy="organization_budget",
              balance_points=0, required_points=100,
              approval_state="required")
    assert v.reason == "approval_required"
    assert v.reason != "execution_funding_pending"
    assert any(a.kind == "request_approval" for a in v.actions)


def test_act04_insufficient_balance_yields_typed_actions_per_funding_cell():
    """出口按 payer 裁剪 —— 复用窗A 四格矩阵,不复述(U-7「服务端按 payer 裁剪」)。"""
    personal = admit(work_kind="content_generation", milestone="service_activated",
                     funding_policy="personal_wallet",
                     balance_points=5, required_points=105)
    assert personal.reason == "execution_funding_pending"
    kinds = {a.kind for a in personal.actions}
    assert kinds == set(funding_projection.allowed_insufficient_actions("personal_wallet"))
    # 个人钱包不得出现"找组织审批" —— 那是串格
    assert "request_budget_approval" not in kinds

    org = admit(work_kind="content_generation", milestone="service_activated",
                funding_policy="organization_budget",
                balance_points=5, required_points=105,
                approval_state="approved")
    assert "request_budget_approval" in {a.kind for a in org.actions}
    assert "top_up" not in {a.kind for a in org.actions}


def test_act04_shortfall_message_is_the_signed_u4_sentence():
    """§0.5.5 U-4 逐字常驻文案 —— 全旅程最反直觉的一格,必须先解释后出现。"""
    v = admit(work_kind="content_generation", milestone="service_activated",
              funding_policy="personal_wallet",
              balance_points=5, required_points=105)
    assert v.message == "客户货款已收妥;开始交付需消耗你的算力,还差 100,充值后自动继续"


def test_scope_cap_cannot_be_borrowed_across_scopes():
    v = admit(work_kind="media_publication", milestone="service_activated",
              funding_policy="personal_wallet",
              balance_points=10 ** 9, required_points=500,
              scope_remaining_points=100)
    assert v.reason == "scope_cap_exhausted"


# ---------------------------------------------------------- ACT-10(O1)

def test_act10_unavailable_capability_does_not_block_other_work_kinds():
    """百科/官网/单平台不可用**不阻塞其它**套餐能力。"""
    blocked = admit(work_kind="media_publication", milestone="service_activated",
                    funding_policy="personal_wallet",
                    balance_points=1000, required_points=10,
                    capability_available=False)
    assert not blocked.admitted and blocked.reason == "capability_unavailable"

    still_ok = admit(work_kind="content_generation", milestone="service_activated",
                     funding_policy="personal_wallet",
                     balance_points=1000, required_points=10,
                     capability_available=True)
    assert still_ok.admitted


# ------------------------------------------------------- §0.5.6:无死路

def test_every_rejection_carries_at_least_one_exit():
    """没有出口的拒绝不许存在。这条覆盖全部 BlockReason。"""
    cases = [
        dict(milestone="customer_accepted", funding_policy="personal_wallet",
             balance_points=1, required_points=1),
        dict(milestone="service_activated", funding_policy="personal_wallet",
             balance_points=0, required_points=99),
        dict(milestone="service_activated", funding_policy="organization_budget",
             balance_points=0, required_points=99, approval_state="required"),
        dict(milestone="service_activated", funding_policy="personal_wallet",
             balance_points=10 ** 9, required_points=99, scope_remaining_points=1),
        dict(milestone="service_activated", funding_policy="personal_wallet",
             balance_points=10 ** 9, required_points=1, capability_available=False),
    ]
    seen = set()
    for case in cases:
        v = admit(work_kind="content_generation", **case)
        assert not v.admitted
        assert v.actions, v.reason
        for action in v.actions:
            assert action.label and not re.search(r"[a-z]+_[a-z_]+", action.label)
        seen.add(v.reason)
    assert len(seen) == 5, seen


def test_admission_never_reports_side_effects():
    """ACT-15「command/freeze/outbox/provider 均为 0」。"""
    for case in (
        dict(milestone="customer_accepted", balance_points=1, required_points=1),
        dict(milestone="service_activated", balance_points=0, required_points=9),
    ):
        v = admit(work_kind="content_generation",
                  funding_policy="personal_wallet", **case)
        assert (v.freeze_count, v.outbox_count, v.provider_calls) == (0, 0, 0)
