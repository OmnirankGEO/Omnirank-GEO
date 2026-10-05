"""C-3 · queued 格在形成 attempt 之前被 abandon/refund ⇒ **账本必须闭合**。

Codex 终审 P1-10 的那一格:``reclaim_orphans`` 只动 ``terminal_state IS NULL``
的**在飞** attempt 行,而 queued 格在账本里**一行都没有**
(``open_for_claim`` 只在 claim 成 running 时才写)。于是那条 UPDATE 命中零行,
它以为自己干完了 —— 那一格从此停在:

    计划了、没尝试、也没有任何终态记录

资金侧看:既没被扣(没交付)、也没被退(整任务 release 只看
``fulfillment_state``)、更没有人管(不在任何 worker 的候选集里)。
**既没扣也没退也没人管** = 第三态。
"""

from __future__ import annotations

import pytest

from tests.defgeo_woc_closure_2026_08_25 import _seed
from tests.defgeo_woc_closure_2026_08_25.conftest import connect


@pytest.fixture(scope="module", autouse=True)
def _identities():
    _seed.install_identities()


def _task_with_cells(owner: int = _seed.TENANT_A):
    brand = _seed.new_brand(owner=owner)
    task = _seed.new_monitoring_task(brand_id=brand)
    return brand, task


# ══════════════════════════════════════════════════════════════════════════
# A. abandon 那一半
# ══════════════════════════════════════════════════════════════════════════
def test_c3_01_abandoned_queued_cell_closes_the_ledger() -> None:
    """🔴 本体判据:整任务被判废之后,那个 queued 格在账本里有终态。

    拆红:把 ``recover_abandoned_monitoring_task_execution`` 里那一跳
    ``close_unattempted_for_cells`` 摘掉 ⇒ 账本零行 ⇒ 本条红。
    """
    from db.monitoring_db import recover_abandoned_monitoring_task_execution

    brand, task = _task_with_cells()
    cell_id, plan_hash = _seed.new_run_cell(
        task_id=task, brand_id=brand, state="queued", provider_dispatched=False)

    assert _seed.attempts_for(plan_hash) == [], "夹具阶段账本就不该有行"

    changed = recover_abandoned_monitoring_task_execution(task)
    assert changed >= 1, changed

    rows = _seed.attempts_for(plan_hash)
    assert len(rows) == 1, f"账本没闭合:{rows}"
    assert rows[0]["terminal_state"] == "policy_skipped", rows[0]
    assert rows[0]["provider_called"] is False, rows[0]
    assert rows[0]["error_code"] == "worker_lost_before_dispatch", rows[0]


def test_c3_02_conservation_holds_after_closure() -> None:
    """闭合之后 §6.3 的守恒式必须成立:terminal 追上 planned,attempted 不动。

    这一条解释了**为什么落 policy_skipped 而不是 engine_error**:
    后者会让 ``attempted == outcome + ambiguous + engine_error`` 右边多一、
    左边不动 —— 等式当场破,而破的原因与真实缺陷毫无关系。
    """
    from db.monitoring_db import recover_abandoned_monitoring_task_execution
    from services.defensive_geo.monitoring import attempt_ledger as _ledger
    from services.defensive_geo.monitoring import denominators as _den

    brand, task = _task_with_cells()
    _, plan_hash = _seed.new_run_cell(
        task_id=task, brand_id=brand, state="queued", provider_dispatched=False)
    recover_abandoned_monitoring_task_execution(task)

    conn = connect()
    try:
        cur = conn.cursor()
        attempts = _ledger.attempts_for_cell(cur, plan_cell_id=plan_hash)
        conn.rollback()
    finally:
        conn.close()

    facts = _ledger.cell_denominator_facts(attempts)
    assert facts["isTerminal"] is True, facts
    assert facts["isAttempted"] is False, facts
    # attempted=0 ⇒ 守恒式右边也必须是 0(policy_skipped 不进这三档任何一档)
    _den.assert_attempted_conservation(
        attempted_cells=0, canonical_outcome_cells=0,
        ambiguous_cells=0, final_engine_error_cells=0)


# ══════════════════════════════════════════════════════════════════════════
# B. 退款那一半
# ══════════════════════════════════════════════════════════════════════════
def test_c3_10_refund_release_closes_the_ledger_too() -> None:
    """退款释放那条路同样要闭合,理由码是 ``fulfillment_refunded``。

    退款路径比 abandon 更需要这一跳:钱**确实退了**,而账本里这几个 queued 格
    连"被取消"这件事都没记下 —— 对账时它们看起来像"还欠客户一次测量"。
    """
    from services.defensive_geo.monitoring.run_ledger_bridge import (
        UNATTEMPTED_REFUNDED_REASON, close_unattempted_for_cells,
    )

    brand, task = _task_with_cells()
    cell_id, plan_hash = _seed.new_run_cell(
        task_id=task, brand_id=brand, state="failed", provider_dispatched=False,
        fulfillment_state="released")

    conn = connect()
    try:
        cur = conn.cursor()
        n = close_unattempted_for_cells(
            cur, monitoring_cell_ids=[cell_id],
            reason_code=UNATTEMPTED_REFUNDED_REASON)
        conn.commit()
    finally:
        conn.close()

    assert n == 1, n
    rows = _seed.attempts_for(plan_hash)
    assert len(rows) == 1 and rows[0]["error_code"] == "fulfillment_refunded", rows


# ══════════════════════════════════════════════════════════════════════════
# B2. [外选 EXTC-06] 闭合行的**归属** —— 只验状态不验归属会漏掉这一格
# ══════════════════════════════════════════════════════════════════════════
def test_c3_11_closure_rows_carry_the_real_tenant_and_brand() -> None:
    """🔴 [外选 EXTC-06] 每条闭合行必须挂在**它自己那个租户/品牌**名下。

    改动前 C-3 那几条判据只断言 ``terminal_state`` / ``provider_called`` /
    ``error_code`` / 守恒式 —— **一条都没读**账本行上的 ``tenant_owner_user_id``。
    于是"格闭合了"与"格闭合了、但钱的归属故事没闭合"分不开:
    把 ``COALESCE(b.owner_user_id, 0)`` 写成常量 ``0``,每条 policy_skipped
    都会归到 tenant 0 名下 —— 按租户对账/审计时,这些"已闭合"的格
    在真租户名下**根本不存在**,第三态只是换了件马甲。

    判别力靠**两个不同租户同一批闭合**:一个常量(不论 0 还是别的什么)
    没法同时等于两个不同的 owner。单租户样本对"写死常量"零区分力。

    拆红:把那条 SELECT 里的 ``COALESCE(b.owner_user_id, 0)`` 换成任何常量
    ⇒ 本条红。
    """
    from services.defensive_geo.monitoring.run_ledger_bridge import (
        UNATTEMPTED_ABANDONED_REASON, close_unattempted_for_cells,
    )

    brand_a, task_a = _task_with_cells(_seed.TENANT_A)
    brand_b, task_b = _task_with_cells(_seed.TENANT_B)
    cell_a, hash_a = _seed.new_run_cell(
        task_id=task_a, brand_id=brand_a, state="failed", provider_dispatched=False)
    cell_b, hash_b = _seed.new_run_cell(
        task_id=task_b, brand_id=brand_b, state="failed", provider_dispatched=False)
    assert hash_a != hash_b, "两个格的 plan_hash 撞了 —— 样本区分不开"

    conn = connect()
    try:
        cur = conn.cursor()
        n = close_unattempted_for_cells(
            cur, monitoring_cell_ids=[cell_a, cell_b],
            reason_code=UNATTEMPTED_ABANDONED_REASON)
        conn.commit()
    finally:
        conn.close()
    assert n == 2, f"两个格没都闭合(实得 {n})"

    row_a = _seed.attempts_for(hash_a)
    row_b = _seed.attempts_for(hash_b)
    assert len(row_a) == 1 and len(row_b) == 1, (row_a, row_b)

    assert int(row_a[0]["tenant_owner_user_id"]) == _seed.TENANT_A, (
        f"A 的闭合行挂错了租户:{row_a[0]}")
    assert int(row_b[0]["tenant_owner_user_id"]) == _seed.TENANT_B, (
        f"B 的闭合行挂错了租户:{row_b[0]}")
    assert int(row_a[0]["brand_id"]) == brand_a, row_a[0]
    assert int(row_b[0]["brand_id"]) == brand_b, row_b[0]
    # run_authority_id = 任务号:对账时"这条闭合属于哪一次运行"也要答得上来
    assert str(row_a[0]["run_authority_id"]) == str(task_a), row_a[0]
    assert str(row_b[0]["run_authority_id"]) == str(task_b), row_b[0]
    # 探针活性:两条行的归属**确实不同**(否则上面两条可能同时被一个常量满足)
    assert row_a[0]["tenant_owner_user_id"] != row_b[0]["tenant_owner_user_id"]


def test_c3_12_a_cell_without_a_frozen_tenant_is_not_closed_as_zero() -> None:
    """🔴 [工单 E3-1 · 2026-08-26 · 本条**语义反转**] 租户取不到 ⇒ **不闭合**。

    ═══════════════════════════════════════════════════════════════════
    这一条原来叫 ``..._closes_as_zero_not_as_a_crash``,断言的是
    ``COALESCE(b.owner_user_id, 0)`` 那条兜底:owner 为空时**照样闭合**,
    账本里落一个 ``tenant_owner_user_id = 0``。
    Codex 二审 P1-F6 打的正是那个 0:**0 号用户不存在**,落 0 等于把整条运行
    挂在一个编出来的租户名下,而那比"缺一行账"贵得多 ——
    缺一行是看得见的(五卡守恒当场判数据坏了),假租户是看不见的。
    ═══════════════════════════════════════════════════════════════════

    所以本条现在断言相反的事:格上没有冻结租户 ⇒ 这一跳**零闭合**、零落账,
    并且**不抛**(现役的恢复/退款动作不许被一条观测账本挡住)。

    与 c3_11 的关系:那一条证明"拿得到就落对",本条证明"拿不到就不落"。
    两条一起才排除"永远落一个 0"和"永远不落"两种退化实现。
    """
    from services.defensive_geo.monitoring.run_ledger_bridge import (
        UNATTEMPTED_ABANDONED_REASON, close_unattempted_for_cells,
    )

    brand, task = _task_with_cells(_seed.TENANT_A)
    # 🔴 造的是**格上**没有冻结租户,不是"品牌没有 owner":桥现在只读格上
    #    那一列。顺手把品牌 owner 也清掉,是为了证明它**不会**被当兜底读回来
    #    —— 品牌有 owner 而格上没有时仍然必须零闭合(否则"现读可变列"就回来了)。
    cell, plan_hash = _seed.new_run_cell(
        task_id=task, brand_id=brand, state="failed", provider_dispatched=False,
        tenant_owner_user_id=None)

    conn = connect()
    try:
        cur = conn.cursor()
        n = close_unattempted_for_cells(
            cur, monitoring_cell_ids=[cell], reason_code=UNATTEMPTED_ABANDONED_REASON)
        conn.commit()
    finally:
        conn.close()
    assert n == 0, f"格上没有冻结租户却闭合了 {n} 行 —— 那一行的租户只能是编的"
    assert _seed.attempts_for(plan_hash) == [], (
        "落了账 —— 账本里多了一行租户来路不明的记录")


# ══════════════════════════════════════════════════════════════════════════
# C. 作用域三条 —— 少一条就会写出假记录
# ══════════════════════════════════════════════════════════════════════════
def test_c3_20_running_cell_is_never_closed_early() -> None:
    """还在跑的格**不许**被提前收口。"""
    from services.defensive_geo.monitoring.run_ledger_bridge import (
        UNATTEMPTED_ABANDONED_REASON, close_unattempted_for_cells,
    )

    brand, task = _task_with_cells()
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO monitoring_run_cells
                 (task_id, brand_id, keyword_id, keyword_source, keyword_snapshot,
                  question_snapshot, target_brand_snapshot, platform, is_planned,
                  state, entitlement_snapshot, order_snapshot, fulfillment_credential,
                  fulfillment_state, plan_hash, claim_token, claim_until)
               VALUES (%s,%s,1,'confirmed','kw','q','b','dashscope',TRUE,'running',
                       '{}'::jsonb,'{}'::jsonb, gen_random_uuid(), 'reserved',
                       repeat('a',64), gen_random_uuid(), NOW() + interval '10 min')
               RETURNING id, plan_hash""",
            (task, brand))
        row = cur.fetchone()
        conn.commit()
        cell_id, plan_hash = int(row["id"]), str(row["plan_hash"])
    finally:
        conn.close()

    conn = connect()
    try:
        cur = conn.cursor()
        n = close_unattempted_for_cells(
            cur, monitoring_cell_ids=[cell_id],
            reason_code=UNATTEMPTED_ABANDONED_REASON)
        conn.commit()
    finally:
        conn.close()
    assert n == 0, "还在 running 的格被提前收口了"
    assert _seed.attempts_for(plan_hash) == []


def test_c3_21_dispatched_cell_is_left_to_reclaim_orphans() -> None:
    """**派发过**的格不许说"零 provider 调用"。

    那一类由 ``reclaim_orphans`` 按 ``provider_outcome_unknown`` 处置。
    两个函数作用域互补,所以这条同时证明它们**不重叠**
    (两把锁叠同一路径时,其中一把的变异会被另一把吞掉)。
    """
    from services.defensive_geo.monitoring.run_ledger_bridge import (
        UNATTEMPTED_ABANDONED_REASON, close_unattempted_for_cells,
    )

    brand, task = _task_with_cells()
    cell_id, plan_hash = _seed.new_run_cell(
        task_id=task, brand_id=brand, state="pending_provider_confirmation",
        provider_dispatched=True)

    conn = connect()
    try:
        cur = conn.cursor()
        n = close_unattempted_for_cells(
            cur, monitoring_cell_ids=[cell_id],
            reason_code=UNATTEMPTED_ABANDONED_REASON)
        conn.commit()
    finally:
        conn.close()
    assert n == 0, "派发过的格被记成了『零 provider 调用』—— 那是说谎"
    assert _seed.attempts_for(plan_hash) == []


def test_c3_22_cell_that_already_has_a_ledger_row_is_untouched(monkeypatch) -> None:
    """账本已有行的格归 ``reclaim_orphans``,本函数**连试都不许试**。

    ═══════════════════════════════════════════════════════════════════
    🔴 [撕锁订正] 只断言"没多出一行"证明不了作用域在守
    ═══════════════════════════════════════════════════════════════════
    第一版只看 `n == 0` 与行数没变。MUT-C3-04(把 `NOT EXISTS` 那一格从
    候选查询里摘掉)**活了下来** —— 因为摘掉之后这一格确实进了候选、
    确实调了 `record_policy_skip`,但那条 INSERT 撞上 046 的
    `uq_defgeo_attempt_cell_ordinal`(同一格第 N 次尝试只能有一行),
    被 `guarded` 的 SAVEPOINT 回滚吞掉,函数照样返 0、行数照样没变。

    也就是说:**应用层的作用域和库层的唯一约束叠在同一条路径上**,
    其中一把的变异被另一把吞掉。本仓记过这一条 ——
    「两把锁叠同一路径:存活时别以为是自己那把在守」。

    所以补一条**库层给不了**的断言:被测函数**根本不该调**
    `record_policy_skip`。作用域的职责是"连候选都不选它",
    不是"选了但写失败"。两者在库里长得一样,在这条 spy 上分得开。
    """
    from services.defensive_geo.monitoring import attempt_ledger as _ledger
    from services.defensive_geo.monitoring import run_ledger_bridge as _bridge
    from services.defensive_geo.monitoring.run_ledger_bridge import (
        UNATTEMPTED_ABANDONED_REASON, close_unattempted_for_cells,
    )

    brand, task = _task_with_cells()
    cell_id, plan_hash = _seed.new_run_cell(
        task_id=task, brand_id=brand, state="failed", provider_dispatched=False)

    conn = connect()
    try:
        cur = conn.cursor()
        _ledger.record_policy_skip(
            cur, plan_cell_id=plan_hash, run_authority_id=str(task),
            tenant_owner_user_id=_seed.TENANT_A, brand_id=brand,
            reason_code="not_in_purchased_run_plan")
        conn.commit()
    finally:
        conn.close()
    before = _seed.attempts_for(plan_hash)
    assert len(before) == 1

    # 🔴 spy 打在**被测模块引用到的那个名字**上(``_bridge._ledger.record_policy_skip``),
    #    不是打在 attempt_ledger 模块的原始属性上 —— 桥是 ``import ... as _ledger``,
    #    改错地方会让 spy 恒零次,而"没被调"与"spy 没装上"长得一样。
    calls: list = []
    real = _bridge._ledger.record_policy_skip
    monkeypatch.setattr(
        _bridge._ledger, "record_policy_skip",
        lambda *a, **k: (calls.append(k.get("reason_code")), real(*a, **k))[1])

    conn = connect()
    try:
        cur = conn.cursor()
        n = close_unattempted_for_cells(
            cur, monitoring_cell_ids=[cell_id],
            reason_code=UNATTEMPTED_ABANDONED_REASON)
        conn.commit()
    finally:
        conn.close()
    assert n == 0, n
    assert _seed.attempts_for(plan_hash) == before, "重复写了一行"
    # 🔴 这一条才是作用域自己的判据:**连试都没试**。
    #    只看行数的话,046 的 uq_defgeo_attempt_cell_ordinal 会替应用层背锅,
    #    作用域被摘掉也照样绿(MUT-C3-04 实录)。
    assert calls == [], (
        f"作用域没拦住:被测函数对一个账本已有行的格调了 record_policy_skip {calls} —— "
        "它只是被库层唯一约束吞掉了,不是被作用域挡住的")


def test_c3_23_unregistered_reason_code_is_refused() -> None:
    """现场造一个理由码 ⇒ 拒写。

    账本与现役 cell 必须是同一个故事;两套词会让同一件事在两张表里叫两个名字。
    """
    from services.defensive_geo.monitoring.run_ledger_bridge import (
        close_unattempted_for_cells,
    )

    brand, task = _task_with_cells()
    cell_id, plan_hash = _seed.new_run_cell(
        task_id=task, brand_id=brand, state="failed", provider_dispatched=False)

    conn = connect()
    try:
        cur = conn.cursor()
        n = close_unattempted_for_cells(
            cur, monitoring_cell_ids=[cell_id], reason_code="随手编一个")
        conn.commit()
    finally:
        conn.close()
    assert n == 0, n
    assert _seed.attempts_for(plan_hash) == []


def test_c3_30_reason_codes_match_what_the_live_chain_writes_into_the_cell() -> None:
    """理由码**逐字**等于现役写进 cell 的那两个,不另起名字。

    分母取自 ``UNATTEMPTED_REASONS``(不手抄),值与
    ``db/monitoring_db.py`` 的 CASE 分支逐字比对。
    """
    import io as _io

    from services.defensive_geo.monitoring import run_ledger_bridge as _bridge
    from tests.defgeo_woc_closure_2026_08_25.conftest import ROOT

    # 🔴 路径从 conftest 的 ROOT 取,不从被测模块的 __file__ 往上数层数 ——
    #    数层数的写法在包结构变一层时会静默指到仓库外(我第一版就指到了
    #    C:\AI-Test\db\monitoring_db.py,那是**另一棵树**)。
    #    本仓记过:finding 坐标必须带「长在哪棵树上」。
    live = ROOT / "db" / "monitoring_db.py"
    assert live.exists(), f"取不到现役 monitoring_db.py:{live}"
    src = _io.open(live, encoding="utf-8", newline="", errors="replace").read()
    for code in _bridge.UNATTEMPTED_REASONS:
        assert f"'{code}'" in src, (
            f"理由码 {code!r} 在现役 monitoring_db.py 里找不到 —— 账本另起了名字")
    assert set(_bridge.UNATTEMPTED_REASONS) == {
        "worker_lost_before_dispatch", "fulfillment_refunded"}, _bridge.UNATTEMPTED_REASONS
    assert _bridge.census()["unattemptedReasons"] == list(_bridge.UNATTEMPTED_REASONS)
