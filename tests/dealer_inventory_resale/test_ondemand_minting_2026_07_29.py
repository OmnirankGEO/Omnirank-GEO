"""按需铸造第一步(T1 + T2 + 四道护栏)· 工单 §6 九条判别锁。

工单:docs/AI-CONTEXT/WORKORDER_ONDEMAND_MINTING_2026-07-29.md

锁 1 真实下单 → 平台跳铸出流水**带订单号** → 逐跳履约成功
锁 2 平台账号余额为 0 时下单仍然成功(→ test_resale_funds
     ::test_platform_seller_with_zero_balance_mints_on_demand,那里从零批次零余额起跑)
锁 3 bonus 按等级铸入 bonus 池,paid/bonus 两条独立流水、pool 正确(不许混池)
锁 4 铸造量 ≠ 订单声明量 → 拒绝
锁 5 无 related_order_id 的铸造 → 拒绝**并告警**(两条路都拒)
锁 6 单笔超上限 → 拒绝
锁 7 日累计超阈值 → 告警触发(不拦)
锁 8 守恒等式:铸造前后 SUM(钱包三池) == SUM(流水),diff 恒 0
锁 9 分布级:铸造笔数与平台跳数 1:1;造「有订单但零铸造」样本必须转红
"""

from __future__ import annotations

import json

import pytest

from services import dealer_inventory_resale as resale
from services import inventory_minting_guard as guard

from .test_resale_funds import (
    _issue_manufacturer,
    _pay_order,
    _prepare_order,
    _seed_users,
    _wallet,
)


UNIT_COST = '{"mint_cost_numerator_cents":50,"mint_cost_denominator_points":1}'


def _set_guard_config(cur, payload: str) -> None:
    cur.execute(
        """INSERT INTO system_settings(key,value,value_type)
           VALUES ('inventory_minting_guard_config',%s,'json')
           ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,value_type='json'""",
        (payload,),
    )


def _conservation_diff(cur) -> int:
    cur.execute(
        """SELECT (SELECT COALESCE(SUM(paid_inventory_points+bonus_inventory_points
                                       +frozen_inventory_points),0)
                     FROM agent_inventory_wallets)
                - (SELECT COALESCE(SUM(points),0) FROM agent_inventory_transactions)
                AS diff"""
    )
    return int(cur.fetchone()["diff"])


def _seed_platform_chain(cur) -> None:
    """平台 1 → 服务商 10 的最小链路 · 平台**不预铸任何批次**。"""
    _seed_users(cur, 1, 10)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    _set_guard_config(cur, UNIT_COST)


def _order(cur, *, order_id: str, base_points: int, bonus_points: int = 0) -> None:
    cur.execute(
        """INSERT INTO recharge_orders
           (id,user_id,amount_cents,base_points,bonus_points,payment_status,order_type)
           VALUES (%s,10,%s,%s,%s,'paid','agent_inventory_purchase')""",
        # amount_cents 是 int4;本套件只关心 points 口径,金额钳到安全范围即可
        (order_id, min(50 * (base_points + bonus_points), 2_000_000_000),
         base_points, bonus_points),
    )


class _AlertRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, fingerprint, *, severity, title, detail, payload=None):
        self.calls.append((str(fingerprint), str(severity), str(title)))

    def fingerprints(self) -> set[str]:
        return {item[0] for item in self.calls}


@pytest.fixture
def alerts(monkeypatch):
    """把告警出口换成可观测的记录器。

    真实出口 ai_ops_db.upsert_alert 自开连接并 commit(所以生产里"拒绝+告警"
    在调用方回滚后仍留痕);测试库没有 ai_ops_alerts 表,只能在出口处观测。
    """
    recorder = _AlertRecorder()
    monkeypatch.setattr(guard, "emit_guard_alert", recorder)
    return recorder


# ============================================================
# 锁 1 · 真实下单 → 铸出带订单号的流水 → 逐跳履约成功
# ============================================================

def test_lock1_real_order_mints_with_order_id_and_settles(db_conn):
    cur = db_conn.cursor()
    _seed_platform_chain(cur)
    before = _conservation_diff(cur)
    terms = resale.build_quote_terms(
        cur, buyer_user_id=10, points=2, platform_reference_amount_cents=100,
        pricing_version="proc-test-v1",
    )
    _prepare_order(cur, order_id="O-LOCK1", quote_id="Q-LOCK1", terms=terms)
    result = _pay_order(cur, order_id="O-LOCK1", buyer_id=10, points=2)
    assert result["jit_fulfillment_settled"] is True

    cur.execute(
        """SELECT agent_user_id,type,pool,points,related_order_id,lot_id,cost_basis_cents
             FROM agent_inventory_transactions
            WHERE type='manufacturer_origin_in' ORDER BY id"""
    )
    minted = [dict(row) for row in cur.fetchall()]
    assert len(minted) == 1
    assert int(minted[0]["agent_user_id"]) == 1
    assert minted[0]["pool"] == "paid"
    assert int(minted[0]["points"]) == 2
    assert minted[0]["related_order_id"] == "O-LOCK1", "铸造流水必须带订单号"
    assert minted[0]["lot_id"], "铸造必须落到可审计批次"
    assert int(minted[0]["cost_basis_cents"]) == 100

    # 逐跳履约真的成功:买方拿到货,平台净变化 0
    assert _wallet(cur, 10)["paid_inventory_points"] == 2
    assert _wallet(cur, 1)["paid_inventory_points"] == 0
    cur.execute("SELECT state FROM dealer_resale_fulfillment_plans WHERE root_order_id='O-LOCK1'")
    assert cur.fetchone()["state"] == "settled"
    # 锁 8 同批核验
    assert _conservation_diff(cur) == before == 0


# ============================================================
# 锁 1b · 同一订单在平台跳**不可能双铸**(复审要求指认的那条)
# ============================================================

def test_lock1b_same_order_settled_twice_does_not_double_mint(db_conn):
    """一致性锁被拆成"单笔硬拒 + 累计告警"后,"平台跳不会双铸"就不能只靠论述。

    这条锁把它钉死:同一 order_id 重复触发结算 → 第二次必须短路,
    铸造流水笔数与铸造总量都不增,平台钱包不变。
    """
    cur = db_conn.cursor()
    _seed_platform_chain(cur)
    terms = resale.build_quote_terms(
        cur, buyer_user_id=10, points=2, platform_reference_amount_cents=100,
        pricing_version="proc-test-v1",
    )
    _prepare_order(cur, order_id="O-DOUBLE", quote_id="Q-DOUBLE", terms=terms)
    _pay_order(cur, order_id="O-DOUBLE", buyer_id=10, points=2)

    def minted_rows():
        cur.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(points),0) AS n "
            "FROM agent_inventory_transactions "
            "WHERE type='manufacturer_origin_in' AND related_order_id='O-DOUBLE'"
        )
        row = cur.fetchone()
        return int(row["c"]), int(row["n"])

    assert minted_rows() == (1, 2)
    wallet_before = dict(_wallet(cur, 1))
    buyer_before = dict(_wallet(cur, 10))

    # 重复回调 —— 支付网关重投是常态
    again = _pay_order(cur, order_id="O-DOUBLE", buyer_id=10, points=2)
    assert again is not None
    assert minted_rows() == (1, 2), "重复结算不得产生第二笔铸造"
    assert _wallet(cur, 1)["paid_inventory_points"] == wallet_before["paid_inventory_points"]
    assert _wallet(cur, 10)["paid_inventory_points"] == buyer_before["paid_inventory_points"]
    cur.execute(
        "SELECT COUNT(*) AS c FROM dealer_inventory_lots "
        "WHERE owner_agent_user_id=1 AND source_kind='manufacturer_origin'"
    )
    assert int(cur.fetchone()["c"]) == 1, "重复结算不得产生第二个铸造批次"
    assert _conservation_diff(cur) == 0


# ============================================================
# 锁 1c · 平台余额 > 0 时**先回收再铸缺口**(复审补充的那条)
# ============================================================

def test_lock1c_platform_balance_is_consumed_before_minting(db_conn):
    """退款会让平台余额 > 0;若总是铸新的,退回来的算力永远没人用 →
    平台账上长期挂一堆"退回但永不使用"的算力,又长出一个新的 1.3 亿。

    取货顺序必须是:先吃可回收余额 → 不足才铸缺口。
    """
    cur = db_conn.cursor()
    _seed_platform_chain(cur)

    # 造一笔"退款回流"形态的可回收批次:平台账上 3 点 platform_purchase
    cur.execute(
        """INSERT INTO dealer_inventory_lots
           (lot_id,owner_agent_user_id,original_points,remaining_points,reserved_points,
            acquisition_cost_cents,remaining_cost_cents,reserved_cost_cents,
            status,source_kind,pricing_version,evidence_jsonb)
           VALUES ('RECYCLED-1',1,3,3,0,150,150,0,'active','platform_purchase',
                   'proc-test-v1','{"refund_of_order_id":"O-OLD"}'::jsonb)"""
    )
    cur.execute(
        """INSERT INTO agent_inventory_wallets (agent_user_id,paid_inventory_points)
           VALUES (1,3)
           ON CONFLICT (agent_user_id) DO UPDATE SET paid_inventory_points=3"""
    )
    cur.execute(
        """INSERT INTO agent_inventory_transactions
           (agent_user_id,type,pool,points,balance_paid_after,balance_bonus_after,
            related_order_id) VALUES (1,'resale_refund_in','paid',3,3,0,'O-OLD')"""
    )
    assert _conservation_diff(cur) == 0

    # 要 5 点:3 点走回收、2 点才铸
    terms = resale.build_quote_terms(
        cur, buyer_user_id=10, points=5, platform_reference_amount_cents=250,
        pricing_version="proc-test-v1",
    )
    hop = terms["fulfillment_plan"]["hops"][0]
    assert (hop["points"], hop["existing_inventory_points"], hop["mint_points"]) == (5, 3, 2)
    kinds = sorted(item["allocation_kind"] for item in hop["allocations"])
    assert kinds == ["existing_lot", "manufacturer_mint"]

    _prepare_order(cur, order_id="O-RECYCLE", quote_id="Q-RECYCLE", terms=terms)
    _pay_order(cur, order_id="O-RECYCLE", buyer_id=10, points=5)

    cur.execute(
        "SELECT COALESCE(SUM(points),0) AS n FROM agent_inventory_transactions "
        "WHERE type='manufacturer_origin_in' AND related_order_id='O-RECYCLE'"
    )
    assert int(cur.fetchone()["n"]) == 2, "只铸缺口,不是全额"
    # 回收批次被吃干净,平台余额回到 0(不再单调堆积)
    cur.execute("SELECT remaining_points,status FROM dealer_inventory_lots WHERE lot_id='RECYCLED-1'")
    recycled = cur.fetchone()
    assert (int(recycled["remaining_points"]), recycled["status"]) == (0, "consumed")
    assert _wallet(cur, 1)["paid_inventory_points"] == 0
    assert _wallet(cur, 10)["paid_inventory_points"] == 5
    assert _conservation_diff(cur) == 0


def test_lock1c_dormant_preissued_stock_is_not_treated_as_recyclable(db_conn):
    """可回收 ≠ 预铸休眠。admin 显式发行的 manufacturer_origin(生产那 1.3 亿)
    必须留给第二步 T3 销毁,不能被平台跳当成余额慢慢卖掉 —— 否则铸造路径
    要等存量耗尽才第一次触发,「观察真实订单走通铸造路径」当场落空。"""
    cur = db_conn.cursor()
    _seed_users(cur, 1, 10)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    _issue_manufacturer(cur, points=100, acquisition_cost_cents=5000, key="dormant-not-recyclable")
    terms = resale.build_quote_terms(
        cur, buyer_user_id=10, points=2, platform_reference_amount_cents=100,
        pricing_version="proc-test-v1",
    )
    hop = terms["fulfillment_plan"]["hops"][0]
    assert (hop["existing_inventory_points"], hop["mint_points"]) == (0, 2)
    assert resale.PLATFORM_DORMANT_SOURCE_KINDS == ("manufacturer_origin", "opening_balance")


# ============================================================
# 锁 3 · bonus 铸入 bonus 池 · paid/bonus 两条独立流水 · pool 正确
# ============================================================

def test_lock3_bonus_mint_lands_in_bonus_pool_with_separate_ledger(db_conn):
    from services.agent_inventory import purchase_inventory_prepay

    cur = db_conn.cursor()
    _seed_platform_chain(cur)
    _order(cur, order_id="O-LOCK3", base_points=100, bonus_points=70)

    purchase_inventory_prepay(
        cur, agent_user_id=10, paid_points=100, bonus_points=70,
        related_order_id="O-LOCK3", description="囤货达门槛 · 渠道等级奖励",
    )

    wallet = _wallet(cur, 10)
    assert (
        int(wallet["paid_inventory_points"]), int(wallet["bonus_inventory_points"])
    ) == (100, 70), "赠送必须进 bonus 池,不许混入 paid"

    cur.execute(
        """SELECT type,pool,points,related_order_id FROM agent_inventory_transactions
            WHERE agent_user_id=10 ORDER BY pool,id"""
    )
    rows = [(r["type"], r["pool"], int(r["points"]), r["related_order_id"])
            for r in cur.fetchall()]
    assert rows == [
        ("purchase_prepay", "bonus", 70, "O-LOCK3"),
        ("purchase_prepay", "paid", 100, "O-LOCK3"),
    ], "paid/bonus 必须两条独立流水且 pool 字段正确"
    assert _conservation_diff(cur) == 0


# ============================================================
# 锁 4 · 铸造量 ≠ 订单声明量 → 拒绝
# ============================================================

def test_lock4_mint_amount_over_declaration_is_rejected(db_conn):
    cur = db_conn.cursor()
    _seed_platform_chain(cur)
    _order(cur, order_id="O-LOCK4", base_points=100, bonus_points=0)

    with pytest.raises(guard.MintingGuardError) as denied:
        guard.assert_mint_allowed(
            cur, source=guard.SOURCE_INVENTORY_PREPAY, agent_user_id=10,
            pool=guard.POOL_PAID, points=101, related_order_id="O-LOCK4",
        )
    assert denied.value.code == "MINT_AMOUNT_MISMATCH"

    # 履约计划口径:铸造量必须与 hops.points 精确相等
    with pytest.raises(guard.MintingGuardError) as mismatch:
        guard.assert_mint_allowed(
            cur, source=guard.SOURCE_RESALE_PLATFORM_ROOT, agent_user_id=1,
            pool=guard.POOL_PAID, points=50, related_order_id="O-LOCK4",
            plan_declared_points=100,
        )
    assert mismatch.value.code == "MINT_AMOUNT_MISMATCH"

    # 等于声明量则放行(证明上面红的是"量不对"而不是"什么都过不去")
    allowed = guard.assert_mint_allowed(
        cur, source=guard.SOURCE_INVENTORY_PREPAY, agent_user_id=10,
        pool=guard.POOL_PAID, points=100, related_order_id="O-LOCK4",
    )
    assert allowed["declared_capacity"] == 100


def test_lock4b_repeat_same_amount_mint_is_allowed_but_alerted(db_conn, alerts):
    """[执行方订正 3] 一致性锁被拆成两层后,这一层的行为必须也有锁。

    同一订单同额再铸一次(= dispute 裁决重结算的形态)**必须放行**
    —— 做成"累计必须相等"就会把合法重结算打断;
    但累计越过声明量**必须告警**,慢性重复铸造不能没人知道。

    真实的 dispute 重结算路径(services/dispute_settlement.py)依赖全 schema 库,
    本机 test_nogo_v6_dispute_escrow 在**基线上**就因 user_wallets 形状不符而 ERROR,
    故此处以护栏层的行为锁替代,把"为什么敢降成告警"钉在可执行断言上。
    """
    cur = db_conn.cursor()
    _seed_platform_chain(cur)
    _order(cur, order_id="O-REDO", base_points=100, bonus_points=0)

    first = guard.assert_mint_allowed(
        cur, source=guard.SOURCE_INVENTORY_PURCHASE_AUTO, agent_user_id=10,
        pool=guard.POOL_PAID, points=100, related_order_id="O-REDO",
    )
    assert first["overminted"] is False
    assert guard.ALERT_FP_OVERMINT not in alerts.fingerprints()

    cur.execute(
        """INSERT INTO agent_inventory_transactions
           (agent_user_id,type,pool,points,balance_paid_after,balance_bonus_after,
            related_order_id) VALUES (10,'purchase_auto','paid',100,100,0,'O-REDO')"""
    )
    second = guard.assert_mint_allowed(
        cur, source=guard.SOURCE_INVENTORY_PURCHASE_AUTO, agent_user_id=10,
        pool=guard.POOL_PAID, points=100, related_order_id="O-REDO",
    )
    assert second["overminted"] is True, "累计越线必须被识别"
    assert guard.ALERT_FP_OVERMINT in alerts.fingerprints(), "越线必须告警"
    assert second["points"] == 100, "合法重结算不得被拦"


# ============================================================
# 锁 5 · 无订单号 → 拒绝 + 告警(paid 与 bonus 两条路都拒)
# ============================================================

@pytest.mark.parametrize("pool", ["paid", "bonus"])
@pytest.mark.parametrize("order_id", [None, "", "   ", "O-DOES-NOT-EXIST"])
def test_lock5_orphan_mint_is_rejected_and_alerted(db_conn, alerts, pool, order_id):
    cur = db_conn.cursor()
    _seed_platform_chain(cur)

    with pytest.raises(guard.MintingGuardError) as denied:
        guard.assert_mint_allowed(
            cur, source=guard.SOURCE_INVENTORY_PREPAY, agent_user_id=10,
            pool=pool, points=10, related_order_id=order_id,
        )
    assert denied.value.code == "MINT_ORPHAN_REJECTED"
    assert guard.ALERT_FP_ORPHAN in alerts.fingerprints(), "拒绝必须留痕,静默拒绝等于没有护栏"
    assert any(item[1] == "critical" for item in alerts.calls)


def test_lock5_bonus_path_shares_the_same_guard(db_conn, alerts):
    """mutation ⑤:bonus 若不复用同一份护栏实现,这条必红。"""
    from services.agent_inventory import purchase_inventory_prepay

    cur = db_conn.cursor()
    _seed_platform_chain(cur)
    with pytest.raises(guard.MintingGuardError) as denied:
        purchase_inventory_prepay(
            cur, agent_user_id=10, paid_points=0, bonus_points=70,
            related_order_id=None,
        )
    assert denied.value.code == "MINT_ORPHAN_REJECTED"
    assert guard.ALERT_FP_ORPHAN in alerts.fingerprints()
    cur.execute("SELECT COUNT(*) AS c FROM agent_inventory_transactions")
    assert int(cur.fetchone()["c"]) == 0, "被拒的铸造不得留下任何流水"


# ============================================================
# 锁 6 · 单笔超上限 → 拒绝
# ============================================================

def test_lock6_single_mint_over_cap_is_rejected(db_conn):
    cur = db_conn.cursor()
    _seed_platform_chain(cur)
    cap = guard.DEFAULTS["max_single_mint_points"]
    # 订单把量声明得足够大 → 一致性锁放行,唯一能拦住它的就是单笔上限
    _order(cur, order_id="O-LOCK6", base_points=cap + 1, bonus_points=0)

    with pytest.raises(guard.MintingGuardError) as denied:
        guard.assert_mint_allowed(
            cur, source=guard.SOURCE_INVENTORY_PREPAY, agent_user_id=10,
            pool=guard.POOL_PAID, points=cap + 1, related_order_id="O-LOCK6",
        )
    assert denied.value.code == "MINT_SINGLE_CAP_EXCEEDED"

    ok = guard.assert_mint_allowed(
        cur, source=guard.SOURCE_INVENTORY_PREPAY, agent_user_id=10,
        pool=guard.POOL_PAID, points=cap, related_order_id="O-LOCK6",
    )
    assert ok["max_single_mint_points"] == cap


def test_lock6_cap_covers_the_largest_real_business_order(db_conn):
    """[执行方订正] ¥100,000 战略囤货 ≈ 13,684,210 算力(生产实测 ¥10,000→1,368,421),
    工单原定的 500 万上限会当场挡死它;订正后的上限必须放行它、同时挡住多打一个零。"""
    strategic = 13_684_210
    assert guard.DEFAULTS["max_single_mint_points"] > strategic
    assert guard.DEFAULTS["max_single_mint_points"] < strategic * 10


# ============================================================
# 锁 7 · 日累计超阈值 → 告警(不拦)
# ============================================================

def _relative_config(**over) -> str:
    payload = {
        "mint_cost_numerator_cents": 50, "mint_cost_denominator_points": 1,
        "daily_baseline_days": 7, "daily_relative_multiple": 10,
        "daily_alert_floor_points": 100,
    }
    payload.update(over)
    return json.dumps(payload)


def _seed_past_days(cur, daily_points: list[int]) -> None:
    """给过去 N 个自然日各造一笔铸造流水,用来喂中位数基线。"""
    for offset, amount in enumerate(daily_points, start=1):
        if amount <= 0:
            continue
        cur.execute(
            """INSERT INTO agent_inventory_transactions
               (agent_user_id,type,pool,points,balance_paid_after,balance_bonus_after,
                related_order_id,created_at)
               VALUES (1,'manufacturer_origin_in','paid',%s,0,0,'O-HIST',
                       date_trunc('day', LOCALTIMESTAMP) - (%s || ' days')::interval
                       + INTERVAL '1 hour')""",
            (int(amount), str(offset)),
        )


def test_lock7_daily_volume_alerts_relative_to_baseline(db_conn, alerts):
    """越过『过去 7 日中位数 × 10』才告警,且只告警不拦。"""
    cur = db_conn.cursor()
    _seed_platform_chain(cur)
    _set_guard_config(cur, _relative_config())
    _seed_past_days(cur, [100, 100, 100, 100, 100, 100, 100])  # 中位数 100 → 阈值 1000
    assert guard.daily_mint_baseline_points(cur, baseline_days=7) == 100
    _order(cur, order_id="O-LOCK7", base_points=1500, bonus_points=0)

    result = guard.assert_mint_allowed(
        cur, source=guard.SOURCE_INVENTORY_PREPAY, agent_user_id=10,
        pool=guard.POOL_PAID, points=1500, related_order_id="O-LOCK7",
    )
    assert result["daily_baseline_median"] == 100
    assert result["daily_alerted"] is True, "越阈必须告警"
    assert guard.ALERT_FP_DAILY in alerts.fingerprints()
    assert result["points"] == 1500, "第 4 道只告警不拦"


def test_lock7_normal_day_within_baseline_stays_quiet(db_conn, alerts):
    """业务量长上去后,同样的绝对量不再报警 —— 相对阈值的意义就在这。"""
    cur = db_conn.cursor()
    _seed_platform_chain(cur)
    _set_guard_config(cur, _relative_config())
    _seed_past_days(cur, [1000, 1000, 1000, 1000, 1000, 1000, 1000])  # 阈值 10000
    _order(cur, order_id="O-LOCK7B", base_points=1500, bonus_points=0)
    result = guard.assert_mint_allowed(
        cur, source=guard.SOURCE_INVENTORY_PREPAY, agent_user_id=10,
        pool=guard.POOL_PAID, points=1500, related_order_id="O-LOCK7B",
    )
    assert result["daily_baseline_median"] == 1000
    assert result["daily_alerted"] is False
    assert guard.ALERT_FP_DAILY not in alerts.fingerprints()


def test_lock7_floor_suppresses_cold_start_noise(db_conn, alerts):
    """基线为 0(冷启动/稀疏)时,地板以下不报警 —— 否则第一笔就响。"""
    cur = db_conn.cursor()
    _seed_platform_chain(cur)
    _set_guard_config(cur, _relative_config())
    assert guard.daily_mint_baseline_points(cur, baseline_days=7) == 0
    _order(cur, order_id="O-LOCK7C", base_points=99, bonus_points=0)
    result = guard.assert_mint_allowed(
        cur, source=guard.SOURCE_INVENTORY_PREPAY, agent_user_id=10,
        pool=guard.POOL_PAID, points=99, related_order_id="O-LOCK7C",
    )
    assert result["daily_baseline_median"] == 0
    assert result["daily_alerted"] is False, "地板以下不报警"
    assert guard.ALERT_FP_DAILY not in alerts.fingerprints()


def test_lock7_baseline_median_fills_zero_days(db_conn):
    """🔴 基线按自然日**补零**取中位数:只有 1 天铸过时中位数必须是 0,
    否则泄漏自己会把基线抬高、把自己藏起来。"""
    cur = db_conn.cursor()
    _seed_platform_chain(cur)
    _seed_past_days(cur, [1_000_000])  # 只有昨天有量
    assert guard.daily_mint_baseline_points(cur, baseline_days=7) == 0


# ============================================================
# 锁 8 · 守恒等式 diff 恒 0
# ============================================================

def test_lock8_conservation_holds_before_and_after_mint(db_conn):
    cur = db_conn.cursor()
    _seed_platform_chain(cur)
    assert _conservation_diff(cur) == 0
    terms = resale.build_quote_terms(
        cur, buyer_user_id=10, points=4, platform_reference_amount_cents=200,
        pricing_version="proc-test-v1",
    )
    _prepare_order(cur, order_id="O-LOCK8", quote_id="Q-LOCK8", terms=terms)
    assert _conservation_diff(cur) == 0
    _pay_order(cur, order_id="O-LOCK8", buyer_id=10, points=4)
    assert _conservation_diff(cur) == 0

    # 铸造既加了钱包也加了流水 —— 缺任一边等式立刻破(mutation ④ 钉这条)
    cur.execute(
        "SELECT COALESCE(SUM(points),0) AS n FROM agent_inventory_transactions "
        "WHERE type='manufacturer_origin_in'"
    )
    assert int(cur.fetchone()["n"]) == 4


# ============================================================
# 锁 9 · 分布级:铸造笔数与平台跳 1:1 · 数据层注入必须转红
# ============================================================

def test_lock9_mint_distribution_tracks_orders(db_conn, alerts):
    cur = db_conn.cursor()
    _seed_platform_chain(cur)

    healthy_empty = guard.mint_distribution_status(cur)
    assert healthy_empty == {
        "lookback_days": 30, "applicable": True, "expected_mints": 0,
        "actual_mints": 0, "healthy": True, "anomaly_form": None,
    }

    terms = resale.build_quote_terms(
        cur, buyer_user_id=10, points=2, platform_reference_amount_cents=100,
        pricing_version="proc-test-v1",
    )
    _prepare_order(cur, order_id="O-LOCK9", quote_id="Q-LOCK9", terms=terms)
    _pay_order(cur, order_id="O-LOCK9", buyer_id=10, points=2)

    status = guard.mint_distribution_status(cur)
    assert status["expected_mints"] == 1 and status["actual_mints"] == 1
    assert status["healthy"] is True
    assert guard.ALERT_FP_DISTRIBUTION not in alerts.fingerprints()

    # 🔴 数据层注入「有订单但零铸造」—— 恒真断言在这里必然放过,真判据必须转红
    cur.execute(
        "DELETE FROM agent_inventory_transactions WHERE type='manufacturer_origin_in'"
    )
    injected = guard.mint_distribution_status(cur)
    assert injected["healthy"] is False
    assert injected["anomaly_form"] == "silent_zero_mint"
    assert guard.ALERT_FP_DISTRIBUTION in alerts.fingerprints()


def test_lock9_detects_mint_exceeding_orders(db_conn, alerts):
    cur = db_conn.cursor()
    _seed_platform_chain(cur)
    _order(cur, order_id="O-LOCK9B", base_points=10, bonus_points=0)
    # 没有任何平台跳,却出现带订单号的铸造 → 另一种单边形态
    cur.execute(
        """INSERT INTO agent_inventory_transactions
           (agent_user_id,type,pool,points,balance_paid_after,balance_bonus_after,
            related_order_id)
           VALUES (1,'manufacturer_origin_in','paid',10,10,0,'O-LOCK9B')"""
    )
    status = guard.mint_distribution_status(cur)
    assert status["healthy"] is False
    assert status["anomaly_form"] == "mint_exceeds_orders"
    assert guard.ALERT_FP_DISTRIBUTION in alerts.fingerprints()


# ============================================================
# 配置面:非法配置 fail-closed,不静默回落默认值
# ============================================================

def test_invalid_guard_config_fails_closed(db_conn):
    cur = db_conn.cursor()
    _seed_platform_chain(cur)
    bad_payloads = (
        '{"max_single_mint_points":0}',              # 非正
        '{"daily_relative_multiple":"1e9"}',          # 非整数
        '{"daily_alert_points":1000}',                # 已废弃的 key(改相对阈值后不再存在)
        'not-json',
    )
    for payload in bad_payloads:
        _set_guard_config(cur, payload)
        with pytest.raises(guard.MintingGuardError) as bad:
            guard.load_guard_config(cur)
        assert bad.value.code == "MINT_GUARD_CONFIG_INVALID"


def test_mint_cost_basis_matches_production_unit_cost():
    """默认成本基准 = 生产 origin lot 单价(100,000,000 分 / 130,000,000 算力),
    与切换前 FIFO 的 floor 口径逐笔相等 → 逐跳毛利不变(工单 §7)。"""
    assert guard.mint_cost_basis_cents(130) == 100
    assert guard.mint_cost_basis_cents(11818) == 9090   # 生产实测 hop cost=9090
    assert guard.mint_cost_basis_cents(108333) == 83333  # 生产实测 hop cost=83333
    assert guard.mint_cost_basis_cents(65000) == 50000   # 生产实测 hop cost=50000
    assert guard.mint_cost_basis_cents(1) == 1, "下界 1 分:批次与分配都有 cost>0 的 CHECK"


def test_admin_adjust_requires_an_order_too(db_conn, alerts):
    """§3-3:把铸造从「管理动作」变成「订单的必然结果」。"""
    from services.agent_inventory import purchase_inventory_admin_adjust

    cur = db_conn.cursor()
    _seed_platform_chain(cur)
    with pytest.raises(guard.MintingGuardError) as denied:
        purchase_inventory_admin_adjust(
            cur, agent_user_id=10, paid_points=1000, admin_user_id=1,
        )
    assert denied.value.code == "MINT_ORPHAN_REJECTED"
    assert guard.ALERT_FP_ORPHAN in alerts.fingerprints()


def test_unpaid_order_mints_nothing(db_conn):
    """预占阶段不铸造 —— 铸造只在支付结算那一刻发生。"""
    cur = db_conn.cursor()
    _seed_platform_chain(cur)
    terms = resale.build_quote_terms(
        cur, buyer_user_id=10, points=2, platform_reference_amount_cents=100,
        pricing_version="proc-test-v1",
    )
    _prepare_order(cur, order_id="O-UNPAID", quote_id="Q-UNPAID", terms=terms)
    cur.execute("SELECT COUNT(*) AS c FROM agent_inventory_transactions")
    assert int(cur.fetchone()["c"]) == 0
    assert resale.cancel_reservation(cur, "O-UNPAID") is True
    cur.execute("SELECT COUNT(*) AS c FROM agent_inventory_transactions")
    assert int(cur.fetchone()["c"]) == 0
    assert _conservation_diff(cur) == 0


def test_preissued_manufacturer_lot_is_never_consumed(db_conn):
    """存量 1.3 亿在第一步里变成休眠资产(第二步 T3 才销毁):
    平台跳恒铸造,绝不吃预铸批次 —— 这正是「观察真实订单走通铸造路径」的前提。"""
    cur = db_conn.cursor()
    _seed_users(cur, 1, 10)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    _issue_manufacturer(cur, points=100, acquisition_cost_cents=50, key="dormant-stock")
    before = _wallet(cur, 1)["paid_inventory_points"]
    terms = resale.build_quote_terms(
        cur, buyer_user_id=10, points=2, platform_reference_amount_cents=1,
        pricing_version="proc-test-v1",
    )
    _prepare_order(cur, order_id="O-DORMANT", quote_id="Q-DORMANT", terms=terms)
    _pay_order(cur, order_id="O-DORMANT", buyer_id=10, points=2)
    assert _wallet(cur, 1)["paid_inventory_points"] == before == 100
    cur.execute(
        "SELECT remaining_points FROM dealer_inventory_lots WHERE lot_id LIKE 'DML%'"
    )
    assert [int(r["remaining_points"]) for r in cur.fetchall()] == [100]
    assert _conservation_diff(cur) == 0
