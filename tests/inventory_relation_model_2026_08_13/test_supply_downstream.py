"""P0 热修判据 · WO_INVREL_P0_HOTFIX §6。

编号与工单 §6 一一对应。核心是**端到端差分守恒** + lot 双侧同事务 +
R5 红线在新增写动作后仍然成立。
"""

from __future__ import annotations

import pytest

from services.agent_inventory import supply_downstream_inventory
from services.channel_partner_requests import resolve_relationship
from services.dealer_inventory_resale import ResaleError
from services.relationship_privacy import find_private_relationship_fields

from .conftest import TEST_ID_BASE, make_binding, make_channel, make_user

# 与生产 u46 → u18 同构:上游服务商 → 下线服务商,已绑定系数 13000bps
UP = TEST_ID_BASE + 41
DOWN = TEST_ID_BASE + 42
STRANGER = TEST_ID_BASE + 43
GHOST = TEST_ID_BASE + 199
BOUND_BPS = 13000


def _wallet(cur, uid):
    cur.execute(
        "SELECT paid_inventory_points AS paid, bonus_inventory_points AS bonus "
        "FROM agent_inventory_wallets WHERE agent_user_id=%s", (uid,),
    )
    row = cur.fetchone()
    return (int(row["paid"]), int(row["bonus"])) if row else (0, 0)


def _lot_side(cur, uid):
    cur.execute(
        "SELECT COALESCE(SUM(remaining_points),0) AS n FROM dealer_inventory_lots "
        "WHERE owner_agent_user_id=%s AND status='active'", (uid,),
    )
    return int(cur.fetchone()["n"])


def _seed_lot(cur, uid, points, cost_cents):
    cur.execute(
        """INSERT INTO dealer_inventory_lots
           (lot_id, owner_agent_user_id, original_points, remaining_points, reserved_points,
            acquisition_cost_cents, remaining_cost_cents, reserved_cost_cents,
            status, source_kind, pricing_version, evidence_jsonb)
           VALUES (%s,%s,%s,%s,0,%s,%s,0,'active','platform_purchase','seed-v1','{"seed":true}'::jsonb)""",
        (f"SEED{uid}-{points}", uid, points, points, cost_cents, cost_cents),
    )


@pytest.fixture
def chain(db):
    cur = db.cursor()
    make_user(cur, UP, phone="13910000001", name="上游服务商", agent_level=2)
    make_user(cur, DOWN, phone="13910000002", name="下线服务商", agent_level=1)
    make_user(cur, STRANGER, phone="13910000003", name="陌生服务商", agent_level=1)
    make_channel(cur, buyer_dealer_id=DOWN, upstream_user_id=UP, bps=BOUND_BPS)
    # 上游有 100000 算力库存,成本 100000 分;lot 与钱包一致(无既有漂移)
    cur.execute(
        """INSERT INTO agent_inventory_wallets (agent_user_id, paid_inventory_points, bonus_inventory_points)
           VALUES (%s,100000,5000) ON CONFLICT (agent_user_id) DO UPDATE
           SET paid_inventory_points=100000, bonus_inventory_points=5000""",
        (UP,),
    )
    _seed_lot(cur, UP, 100000, 100000)
    db.commit()
    return cur


# ============================================================
# §6-1 🔴 端到端:划拨成功 + 双侧差分守恒
# ============================================================

def test_61_end_to_end_supply_conserves_both_sides(chain, db):
    before_up_w, before_up_lot = _wallet(chain, UP), _lot_side(chain, UP)
    before_down_w, before_down_lot = _wallet(chain, DOWN), _lot_side(chain, DOWN)

    result = supply_downstream_inventory(
        chain, UP, DOWN, paid_points=10000, bonus_points=1000, idempotency_ref="t61",
    )
    db.commit()

    after_up_w, after_up_lot = _wallet(chain, UP), _lot_side(chain, UP)
    after_down_w, after_down_lot = _wallet(chain, DOWN), _lot_side(chain, DOWN)

    # 上游钱包减少、lot 同额消耗
    assert before_up_w[0] - after_up_w[0] == 10000
    assert before_up_w[1] - after_up_w[1] == 1000
    assert before_up_lot - after_up_lot == 10000
    # 下线钱包增加、lot 同额建立(🔴 不建 lot 就是新漂移)
    assert after_down_w[0] - before_down_w[0] == 10000
    assert after_down_w[1] - before_down_w[1] == 1000
    assert after_down_lot - before_down_lot == 10000
    # 🔴 差分守恒(不是"必须为 0" —— 那在有存量时恒真或恒红)
    assert (after_up_w[0] + after_down_w[0]) == (before_up_w[0] + before_down_w[0])
    assert (after_up_lot + after_down_lot) == (before_up_lot + before_down_lot)
    assert result["supplied_paid"] == 10000 and result["supplied_bonus"] == 1000


def test_61b_downstream_lot_cost_uses_bound_multiplier(chain, db):
    """计价锁:下线 lot 的成本 = 上游成本 × **已绑定**系数,不回落默认。"""
    supply_downstream_inventory(chain, UP, DOWN, paid_points=10000, idempotency_ref="t61b")
    db.commit()
    chain.execute(
        "SELECT acquisition_cost_cents AS c FROM dealer_inventory_lots "
        "WHERE owner_agent_user_id=%s AND source_kind='direct_resale'", (DOWN,),
    )
    cost = int(chain.fetchone()["c"])
    # 上游 10000 点成本 10000 分 × 13000/10000 = 13000 分。
    # 回落默认 10000bps 会得 10000 → 本断言转红。
    assert cost == 13000, cost
    assert cost != 10000, "回落默认系数 = 计价锁失效"


def test_61c_lot_drift_unchanged_for_both_sides(chain, db):
    """对账维度:供货前后两侧 drift **差分**不变(既有漂移仍在也不误报)。"""
    from services.inventory_lot_ledger import drift_points_of

    before = (drift_points_of(chain, UP), drift_points_of(chain, DOWN))
    supply_downstream_inventory(chain, UP, DOWN, paid_points=7000, idempotency_ref="t61c")
    db.commit()
    assert (drift_points_of(chain, UP), drift_points_of(chain, DOWN)) == before


# ============================================================
# §6-2 lot 不足 → fail-closed 且钱包零变化
# ============================================================

def test_62_lot_shortfall_is_fail_closed_and_wallet_untouched(chain, db):
    # 钱包有 100000,但把 lot 削到 500 → lot 侧不足
    chain.execute(
        "UPDATE dealer_inventory_lots SET remaining_points=500, remaining_cost_cents=500 "
        "WHERE owner_agent_user_id=%s", (UP,),
    )
    db.commit()
    before_up, before_down = _wallet(chain, UP), _wallet(chain, DOWN)

    with pytest.raises(ResaleError):
        supply_downstream_inventory(chain, UP, DOWN, paid_points=10000, idempotency_ref="t62")
    db.rollback()

    # 🔴 反向对照:钱包动了必须转红 —— "钱包扣了 lot 没消"正是要防的半边账
    assert _wallet(chain, UP) == before_up
    assert _wallet(chain, DOWN) == before_down


# ============================================================
# §6-3 bonus 永不消 lot / 永不建 lot
# ============================================================

def test_63_bonus_never_touches_lots(chain, db):
    before_up_lot, before_down_lot = _lot_side(chain, UP), _lot_side(chain, DOWN)
    supply_downstream_inventory(chain, UP, DOWN, bonus_points=3000, idempotency_ref="t63")
    db.commit()
    # 只走 bonus:两侧 lot 一点都不该动(消了或建了都转红)
    assert _lot_side(chain, UP) == before_up_lot
    assert _lot_side(chain, DOWN) == before_down_lot
    # 反向对照:钱包确实动了 —— 否则这条测试可能只是"什么都没发生"
    assert _wallet(chain, DOWN)[1] == 3000


# ============================================================
# §6-4 🔴 R5 红线:新增写动作后仍然不可区分、零私有字段
# ============================================================

def test_64_supply_to_non_downstream_is_refused(chain, db):
    """陌生服务商 / 不存在账号都不能被供货,且不透露对方是否存在。"""
    from services.channel_partner_requests import ChannelPartnerRequestError

    for target in (STRANGER, GHOST):
        with pytest.raises(ChannelPartnerRequestError) as exc:
            supply_downstream_inventory(chain, UP, target, paid_points=100)
        assert exc.value.code == "NO_BOUND_CHANNEL_RELATIONSHIP"
    db.rollback()


def test_64b_downstream_cannot_supply_upstream(chain, db):
    """🔴 有向性:反过来供货必须被拒(下级不能拿上级当下线)。"""
    from services.channel_partner_requests import ChannelPartnerRequestError

    with pytest.raises(ChannelPartnerRequestError):
        supply_downstream_inventory(chain, DOWN, UP, paid_points=100)
    db.rollback()


def test_64c_supply_result_has_zero_private_fields(chain, db):
    """🔴 加了写动作最容易顺手把 `cost_multiplier` 带进返回体 —— 机械扫描。"""
    result = supply_downstream_inventory(chain, UP, DOWN, paid_points=1000, idempotency_ref="t64c")
    db.commit()
    assert find_private_relationship_fields(result) == []
    # 关系解析本身仍然干净
    assert find_private_relationship_fields(resolve_relationship(chain, UP, DOWN)) == []


def test_64d_downstream_still_cannot_see_upstream(chain, db):
    """🔴 R5 回归:下线查上游仍与"不存在"逐字节同构。"""
    from services.channel_partner_requests import ChannelPartnerRequestError

    def payload(actor, target):
        try:
            resolve_relationship(chain, actor, target)
        except ChannelPartnerRequestError as exc:
            return {"code": exc.code, "message": str(exc), "details": exc.details}
        raise AssertionError("不该有关系")

    upward = payload(DOWN, UP)
    ghost = payload(DOWN, GHOST)
    stranger = payload(UP, STRANGER)
    assert upward == ghost == stranger
    assert upward == {"code": "TARGET_NOT_FOUND", "message": "未找到该账号", "details": {}}
    # 反向对照:上游看下游仍然看得见 —— 否则上面的"同构"可能是整条链坏了
    assert resolve_relationship(chain, UP, DOWN)["relation"] == "downstream_partner"


# ============================================================
# §6-5 关系表零变化(供货不得写关系表)
# ============================================================

def test_65_supply_does_not_write_relationship_tables(chain, db):
    def snapshot():
        chain.execute(
            "SELECT id, buyer_dealer_id, upstream_channel_account_id, cost_multiplier_bps, "
            "status FROM channel_pricing_relationships ORDER BY id"
        )
        ch = [dict(r) for r in chain.fetchall()]
        chain.execute(
            "SELECT id, customer_user_id, agent_user_id, binding_source "
            "FROM customer_agent_bindings ORDER BY id"
        )
        return ch, [dict(r) for r in chain.fetchall()]

    before = snapshot()
    supply_downstream_inventory(chain, UP, DOWN, paid_points=2000, bonus_points=500, idempotency_ref="t65")
    db.commit()
    assert snapshot() == before


# ============================================================
# §1 解除阻断:下线的主动作必须是**能执行**的供货
# ============================================================

def test_10_downstream_primary_action_is_executable_supply(chain):
    rel = resolve_relationship(chain, UP, DOWN)
    assert rel["primary_action"]["action"] == "supply_downstream"
    assert "supply_downstream" in rel["allowed_actions"]
    # 🔴 说教文案必须整句消失 —— Owner 点名要删的那句
    assert "现在还不能由你直接发起供货" not in rel["effect_note"]
    assert "请提醒 TA 到自己的库存中心进货" not in rel["effect_note"]


def test_10b_both_relation_also_offers_supply_as_primary(chain, db):
    make_binding(chain, customer_user_id=DOWN, agent_user_id=UP)
    db.commit()
    rel = resolve_relationship(chain, UP, DOWN)
    assert rel["relation"] == "both"
    assert rel["primary_action"]["action"] == "supply_downstream"
    # 按客户划拨仍可选,但降为次动作
    assert "allocate_customer" in rel["allowed_actions"]
