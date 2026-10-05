"""服务商分销链路 · 判别锁(工单 WO_INVENTORY_POINTS_DEADLOCK_2026-08-12 §4 验收判据)。

每条判据都配**反向对照** —— 只验"正面能过"是零判别力的:
一个恒返回成功的实现也能让正面全绿。
"""

from __future__ import annotations

import uuid

import pytest

from conftest import (  # type: ignore[import-not-found]
    binding_needs_attention,
    give_inventory,
    governance_version,
    make_user,
)

# 测试用户 id 段 —— conftest 的清理按 >= 900000 走,别用小 id。
AGENT = 900101
CUSTOMER = 900102
ADMIN = 900103
OTHER_AGENT = 900104
DOWNSTREAM_PROVIDER = 900105


def _req_id(tag: str) -> str:
    return f"test-{tag}-{uuid.uuid4().hex[:12]}"


def _wallet(cur, user_id: int):
    cur.execute(
        "SELECT paid_points, bonus_points FROM user_wallets WHERE user_id=%s", (user_id,)
    )
    return dict(cur.fetchone())


def _inventory(cur, agent_user_id: int):
    cur.execute(
        """SELECT paid_inventory_points, bonus_inventory_points
           FROM agent_inventory_wallets WHERE agent_user_id=%s""",
        (agent_user_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else {"paid_inventory_points": 0, "bonus_inventory_points": 0}


def _lot_remaining(cur, agent_user_id: int) -> int:
    cur.execute(
        """SELECT COALESCE(SUM(remaining_points + reserved_points),0) AS s
           FROM dealer_inventory_lots
           WHERE owner_agent_user_id=%s AND status <> 'consumed'""",
        (agent_user_id,),
    )
    return int(cur.fetchone()["s"])


# ============================================================
# 验收判据 1 · admin 能调整库存,留痕带操作人与原因
# ============================================================

def test_admin_adjust_increase_records_operator_and_reason(db):
    from services.admin_agent_inventory import admin_adjust_inventory

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1)
    make_user(cur, ADMIN, is_admin=True)
    give_inventory(cur, AGENT, paid=0)
    # 铸造护栏 3 要求订单号真实存在于 recharge_orders(不是本包新加的门槛)
    cur.execute(
        # 🔴 列名照 schema 抄,别照印象写:是 `amount_cents` 不是 `amount`
        #    (工单 §7「真实列名(避免踩空)」点名过这一条,我第一版还是写错了)
        """INSERT INTO recharge_orders(id, user_id, amount_cents, base_points, bonus_points,
                                       payment_status)
           VALUES ('TESTORD900101', %s, 10000, 5000, 0, 'paid')
           ON CONFLICT (id) DO NOTHING""",
        (AGENT,),
    )
    db.commit()

    result = admin_adjust_inventory(
        AGENT, direction="increase", paid_points=5000, bonus_points=0,
        related_order_id="TESTORD900101", reason="对公转账入账 · 测试",
        operator_user_id=ADMIN, operator_username="admin", request_id=_req_id("adj"),
        ip_address="127.0.0.1",
    )
    assert result["success"] is True

    cur.execute(
        """SELECT action, operator_user_id, reason, paid_points
           FROM agent_inventory_admin_actions WHERE agent_user_id=%s""",
        (AGENT,),
    )
    row = cur.fetchone()
    assert row["action"] == "adjust_increase"
    assert row["operator_user_id"] == ADMIN          # ← 操作人
    assert "对公转账入账" in row["reason"]            # ← 原因
    assert row["paid_points"] == 5000
    assert _inventory(cur, AGENT)["paid_inventory_points"] == 5000
    # lot 对侧必须同步建立,否则就是又造一条 u133 式漂移
    assert _lot_remaining(cur, AGENT) == 5000


def test_admin_adjust_rejects_blank_reason(db):
    """反向对照 1a:不填原因 → 拒绝。"""
    from services.admin_agent_inventory import InventoryAdminError, admin_adjust_inventory

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1)
    make_user(cur, ADMIN, is_admin=True)
    give_inventory(cur, AGENT, paid=0)
    db.commit()

    with pytest.raises(InventoryAdminError) as exc:
        admin_adjust_inventory(
            AGENT, direction="increase", paid_points=100, bonus_points=0,
            related_order_id="TESTORD900101", reason="  ",
            operator_user_id=ADMIN, operator_username="admin",
            request_id=_req_id("adj"), ip_address=None,
        )
    assert exc.value.code == "REASON_REQUIRED"


def test_admin_adjust_rejects_non_provider_target(db):
    """反向对照 1b:目标不是服务商 → 拒绝(库存只属于服务商)。"""
    from services.admin_agent_inventory import InventoryAdminError, admin_adjust_inventory

    cur = db.cursor()
    make_user(cur, CUSTOMER, agent_level=0)
    make_user(cur, ADMIN, is_admin=True)
    db.commit()

    with pytest.raises(InventoryAdminError) as exc:
        admin_adjust_inventory(
            CUSTOMER, direction="increase", paid_points=100, bonus_points=0,
            related_order_id="TESTORD900101", reason="测试",
            operator_user_id=ADMIN, operator_username="admin",
            request_id=_req_id("adj"), ip_address=None,
        )
    assert exc.value.code == "TARGET_NOT_SERVICE_PROVIDER"


def test_admin_adjust_orphan_mint_rejected(db):
    """反向对照 1c:没有真实订单号的增加 → 铸造护栏拒绝(不是本包放宽的地方)。"""
    from services.admin_agent_inventory import InventoryAdminError, admin_adjust_inventory

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1)
    make_user(cur, ADMIN, is_admin=True)
    give_inventory(cur, AGENT, paid=0)
    db.commit()

    with pytest.raises(InventoryAdminError) as exc:
        admin_adjust_inventory(
            AGENT, direction="increase", paid_points=100, bonus_points=0,
            related_order_id=None, reason="没有订单号的入账",
            operator_user_id=ADMIN, operator_username="admin",
            request_id=_req_id("adj"), ip_address=None,
        )
    assert exc.value.code == "MINT_GUARD_REJECTED"


def test_admin_adjust_duplicate_request_id_rejected(db):
    """反向对照 1d:同一 request_id 重复提交 → 拒绝(资金动作 fail-closed)。"""
    from services.admin_agent_inventory import InventoryAdminError, admin_adjust_inventory

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1)
    make_user(cur, ADMIN, is_admin=True)
    give_inventory(cur, AGENT, paid=10000, lot_suffix="dup")
    db.commit()

    request_id = _req_id("dup")
    kwargs = dict(
        direction="decrease", paid_points=100, bonus_points=0, related_order_id=None,
        reason="重复提交测试", operator_user_id=ADMIN, operator_username="admin",
        request_id=request_id, ip_address=None,
    )
    admin_adjust_inventory(AGENT, **kwargs)
    with pytest.raises(InventoryAdminError) as exc:
        admin_adjust_inventory(AGENT, **kwargs)
    assert exc.value.code == "DUPLICATE_REQUEST"
    # 只扣了一次
    assert _inventory(cur, AGENT)["paid_inventory_points"] == 9900


# ============================================================
# 验收判据 2 · admin 代划拨;绑定缺失时行为明确,不得静默成功
# ============================================================

def test_admin_allocate_creates_binding_in_same_transaction(db):
    from services.admin_agent_inventory import admin_allocate_to_customer

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1)
    make_user(cur, CUSTOMER, agent_level=0)
    make_user(cur, ADMIN, is_admin=True)
    give_inventory(cur, AGENT, paid=8000, lot_suffix="alloc")
    db.commit()

    result = admin_allocate_to_customer(
        AGENT, CUSTOMER, paid_points=5000, bonus_points=0,
        binding_expected_version=governance_version(cur, CUSTOMER, "commercial_binding"),
        reason="代服务商划拨 · 测试", operator_user_id=ADMIN, operator_username="admin",
        request_id=_req_id("alloc"), ip_address=None,
    )
    assert result["binding"]["binding_action"] == "created"
    assert _inventory(cur, AGENT)["paid_inventory_points"] == 3000
    assert _wallet(cur, CUSTOMER)["paid_points"] == 5000
    # lot 同步消耗 —— 不再制造 u133 那种"钱包扣了 lot 没动"的漂移
    assert _lot_remaining(cur, AGENT) == 3000


def test_admin_allocate_requires_binding_version_when_missing(db):
    """反向对照 2a:绑定缺失又没带 CAS 版本 → 明确报错,**不静默建绑定**。"""
    from services.admin_agent_inventory import InventoryAdminError, admin_allocate_to_customer

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1)
    make_user(cur, CUSTOMER, agent_level=0)
    make_user(cur, ADMIN, is_admin=True)
    give_inventory(cur, AGENT, paid=8000, lot_suffix="nover")
    db.commit()

    with pytest.raises(InventoryAdminError) as exc:
        admin_allocate_to_customer(
            AGENT, CUSTOMER, paid_points=100, bonus_points=0,
            binding_expected_version=None, reason="没带版本",
            operator_user_id=ADMIN, operator_username="admin",
            request_id=_req_id("alloc"), ip_address=None,
        )
    assert exc.value.code == "BINDING_VERSION_REQUIRED"
    cur.execute("SELECT count(*) AS c FROM customer_agent_bindings WHERE customer_user_id=%s",
                (CUSTOMER,))
    assert cur.fetchone()["c"] == 0        # 一条都没建
    assert _inventory(cur, AGENT)["paid_inventory_points"] == 8000   # 一分没扣


def test_admin_allocate_rejects_customer_bound_to_other_provider(db):
    """反向对照 2b:客户归属他人 → 拒绝并指向换绑流程(工单三选一取 (1))。"""
    from services.admin_agent_inventory import InventoryAdminError, admin_allocate_to_customer

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1)
    make_user(cur, OTHER_AGENT, agent_level=1)
    make_user(cur, CUSTOMER, agent_level=0)
    make_user(cur, ADMIN, is_admin=True)
    give_inventory(cur, AGENT, paid=8000, lot_suffix="other")
    cur.execute(
        """INSERT INTO customer_agent_bindings(customer_user_id, agent_user_id,
                                               binding_source, bound_at)
           VALUES (%s,%s,'admin_manual',NOW())""",
        (CUSTOMER, OTHER_AGENT),
    )
    db.commit()

    with pytest.raises(InventoryAdminError) as exc:
        admin_allocate_to_customer(
            AGENT, CUSTOMER, paid_points=100, bonus_points=0,
            binding_expected_version=1, reason="跨绑划拨尝试",
            operator_user_id=ADMIN, operator_username="admin",
            request_id=_req_id("alloc"), ip_address=None,
        )
    assert exc.value.code == "CUSTOMER_BOUND_TO_OTHER_PROVIDER"
    assert exc.value.details["next_step"] == "commercial_service_binding"
    assert _inventory(cur, AGENT)["paid_inventory_points"] == 8000


def test_admin_allocate_rejects_service_provider_target_with_channel_guidance(db):
    """验收判据 4 反向对照:把服务商当"客户" → 明确提示走渠道关系,不是无差别报错。"""
    from services.admin_agent_inventory import InventoryAdminError, admin_allocate_to_customer

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1)
    make_user(cur, DOWNSTREAM_PROVIDER, agent_level=1)
    make_user(cur, ADMIN, is_admin=True)
    give_inventory(cur, AGENT, paid=8000, lot_suffix="prov")
    db.commit()

    with pytest.raises(InventoryAdminError) as exc:
        admin_allocate_to_customer(
            AGENT, DOWNSTREAM_PROVIDER, paid_points=100, bonus_points=0,
            binding_expected_version=1, reason="把服务商当客户",
            operator_user_id=ADMIN, operator_username="admin",
            request_id=_req_id("alloc"), ip_address=None,
        )
    assert exc.value.code == "TARGET_IS_SERVICE_PROVIDER"
    assert exc.value.details["next_step"] == "channel_relationship"
    assert "渠道关系" in str(exc.value)


# ============================================================
# 验收判据 3 · 走 commercial-service-binding 端点 → 灯不亮
#             裸插 admin_manual → 灯亮
# ============================================================

def test_binding_via_governance_service_does_not_light_attention(db):
    from services.admin_user_governance import change_commercial_binding

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1)
    make_user(cur, CUSTOMER, agent_level=0)
    make_user(cur, ADMIN, is_admin=True)
    db.commit()

    change_commercial_binding(
        CUSTOMER, AGENT,
        expected_version=governance_version(cur, CUSTOMER, "commercial_binding"),
        reason="走治理端点建归属", operator_user_id=ADMIN, operator_username="admin",
        request_id=_req_id("bind"), ip_address=None,
    )
    assert binding_needs_attention(cur, CUSTOMER) is False


def test_raw_insert_binding_lights_attention(db):
    """反向对照 3:裸 INSERT 一条 admin_manual → 判据为真(灯亮)。

    🔴 这条是 P1-5 改判据的**红线锁**:改完之后"没有审计凭证的绑定"必须仍然亮灯。
    """
    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1)
    make_user(cur, CUSTOMER, agent_level=0)
    cur.execute(
        """INSERT INTO customer_agent_bindings(customer_user_id, agent_user_id,
                                               binding_source, bound_at)
           VALUES (%s,%s,'admin_manual',NOW())""",
        (CUSTOMER, AGENT),
    )
    db.commit()
    assert binding_needs_attention(cur, CUSTOMER) is True


def test_unmarked_audit_does_not_exempt_binding(db):
    """反向对照 3b:**存在**一条对得上这条绑定的审计,但没有补录标记、也不同期 → 仍然亮灯。

    🔴 这条锁是变异验证逼出来的:原本只有"裸插无审计 → 亮灯"一条,
       而把补录分支的 `backfill='true'` / `original_bound_at` 两个条件整个换成 `1=1`
       **杀不死** —— 因为裸插的场景根本没有审计行,两种写法都亮灯。
       真正危险的形态是"有审计行但不是补录",那时放宽的判据会静默豁免。
       没有这条用例,P1-5 改判据这件事等于没有判据。
    """
    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1)
    make_user(cur, CUSTOMER, agent_level=0)
    make_user(cur, ADMIN, is_admin=True)
    cur.execute(
        """INSERT INTO customer_agent_bindings(customer_user_id, agent_user_id,
                                               binding_source, bound_at)
           VALUES (%s,%s,'admin_manual', NOW() - INTERVAL '40 days')
           RETURNING id""",
        (CUSTOMER, AGENT),
    )
    binding_id = cur.fetchone()["id"]
    # 一条**对得上 binding_id 与承接方**、但既不同期、也没有补录标记的审计。
    cur.execute(
        """INSERT INTO admin_user_governance_audits(
               subject_user_id, scope, operator_user_id, operator_username, request_id,
               reason, before_snapshot, after_snapshot, evidence_jsonb,
               version_before, version_after)
           VALUES (%s,'commercial_binding',%s,'admin','req-unmarked','手写的审计',
                   '{}'::jsonb,
                   jsonb_build_object('commercial_provider_user_id', %s::text),
                   jsonb_build_object('binding_id', %s::text),
                   1, 2)""",
        (CUSTOMER, ADMIN, str(AGENT), str(binding_id)),
    )
    db.commit()
    assert binding_needs_attention(cur, CUSTOMER) is True


def test_assert_drift_unchanged_is_a_real_guard(db):
    """反向对照:差额被改变时守恒断言必须抛错(否则它只是装饰)。"""
    from services.dealer_inventory_resale import ResaleError
    from services.inventory_lot_ledger import assert_drift_unchanged, drift_points_of

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1)
    give_inventory(cur, AGENT, paid=4000, lot_suffix="guard")
    db.commit()
    assert drift_points_of(cur, AGENT) == 0
    # 差额没变 → 放行
    assert assert_drift_unchanged(cur, AGENT, before=0, action="不变") == 0
    # 谎报一个 before → 必须抛(等价于"这次操作把差额改了")
    with pytest.raises(ResaleError):
        assert_drift_unchanged(cur, AGENT, before=123, action="被改变")


# ============================================================
# 验收判据 8 · 补录后灯灭,且补录与原生机械可分
# ============================================================

def test_backfill_turns_attention_off(db):
    from services.admin_user_governance import backfill_commercial_binding_audit

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1)
    make_user(cur, CUSTOMER, agent_level=0)
    make_user(cur, ADMIN, is_admin=True)
    # 历史绑定:bound_at 在很久以前 —— 补录记录**永远**落在 ±5 秒窗外
    cur.execute(
        """INSERT INTO customer_agent_bindings(customer_user_id, agent_user_id,
                                               binding_source, bound_at)
           VALUES (%s,%s,'admin_manual', NOW() - INTERVAL '40 days')""",
        (CUSTOMER, AGENT),
    )
    db.commit()
    assert binding_needs_attention(cur, CUSTOMER) is True

    backfill_commercial_binding_audit(
        CUSTOMER, expected_version=governance_version(cur, CUSTOMER, "commercial_binding"),
        reason="历史归属补录凭证 · 工单 P1-5", operator_user_id=ADMIN,
        operator_username="admin", request_id=_req_id("bf"), ip_address=None,
    )
    assert binding_needs_attention(cur, CUSTOMER) is False


def test_backfill_is_mechanically_distinguishable(db):
    """补录必须能被机械识别 —— 不是"看起来像当时写的"。"""
    from services.admin_user_governance import backfill_commercial_binding_audit

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1)
    make_user(cur, CUSTOMER, agent_level=0)
    make_user(cur, ADMIN, is_admin=True)
    cur.execute(
        """INSERT INTO customer_agent_bindings(customer_user_id, agent_user_id,
                                               binding_source, bound_at)
           VALUES (%s,%s,'admin_manual', NOW() - INTERVAL '40 days')""",
        (CUSTOMER, AGENT),
    )
    db.commit()
    backfill_commercial_binding_audit(
        CUSTOMER, expected_version=governance_version(cur, CUSTOMER, "commercial_binding"),
        reason="历史归属补录凭证", operator_user_id=ADMIN, operator_username="admin",
        request_id=_req_id("bf"), ip_address=None,
    )
    cur.execute(
        """SELECT evidence_jsonb, created_at FROM admin_user_governance_audits
           WHERE subject_user_id=%s AND scope='commercial_binding'""",
        (CUSTOMER,),
    )
    row = cur.fetchone()
    evidence = row["evidence_jsonb"]
    assert evidence["backfill"] == "true"
    assert evidence["original_bound_at"]          # 锚死当时的绑定时间
    assert evidence["backfilled_at"]              # 什么时候补的
    # created_at 照实落今天 —— 与 original_bound_at 差 40 天,一眼可辨
    cur.execute(
        "SELECT (%s::timestamp - %s::timestamp) > INTERVAL '30 days' AS far_apart",
        (row["created_at"].replace(tzinfo=None), evidence["original_bound_at"]),
    )
    assert cur.fetchone()["far_apart"] is True


def test_backfill_never_writes_binding(db):
    """补录对 customer_agent_bindings **只读** —— 一个字段都不许改。"""
    from services.admin_user_governance import backfill_commercial_binding_audit

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1)
    make_user(cur, CUSTOMER, agent_level=0)
    make_user(cur, ADMIN, is_admin=True)
    cur.execute(
        """INSERT INTO customer_agent_bindings(customer_user_id, agent_user_id,
                                               binding_source, bound_at)
           VALUES (%s,%s,'admin_manual', NOW() - INTERVAL '40 days')
           RETURNING id, agent_user_id, binding_source, bound_at""",
        (CUSTOMER, AGENT),
    )
    before = dict(cur.fetchone())
    db.commit()

    backfill_commercial_binding_audit(
        CUSTOMER, expected_version=governance_version(cur, CUSTOMER, "commercial_binding"),
        reason="补录", operator_user_id=ADMIN, operator_username="admin",
        request_id=_req_id("bf"), ip_address=None,
    )
    cur.execute(
        "SELECT id, agent_user_id, binding_source, bound_at "
        "FROM customer_agent_bindings WHERE customer_user_id=%s",
        (CUSTOMER,),
    )
    assert dict(cur.fetchone()) == before


def test_backfill_refuses_when_not_lit(db):
    """反向对照 8:已有凭证的归属不许重复补录(否则就是刷审计)。"""
    from services.admin_user_governance import (
        GovernanceValidationError,
        backfill_commercial_binding_audit,
        change_commercial_binding,
    )

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1)
    make_user(cur, CUSTOMER, agent_level=0)
    make_user(cur, ADMIN, is_admin=True)
    db.commit()
    change_commercial_binding(
        CUSTOMER, AGENT,
        expected_version=governance_version(cur, CUSTOMER, "commercial_binding"),
        reason="正规建归属", operator_user_id=ADMIN, operator_username="admin",
        request_id=_req_id("bind"), ip_address=None,
    )
    with pytest.raises(GovernanceValidationError) as exc:
        backfill_commercial_binding_audit(
            CUSTOMER, expected_version=governance_version(cur, CUSTOMER, "commercial_binding"),
            reason="重复补录", operator_user_id=ADMIN, operator_username="admin",
            request_id=_req_id("bf"), ip_address=None,
        )
    assert exc.value.code == "NO_CHANGE"


# ============================================================
# 验收判据 6 · 库存自用守恒
# ============================================================

def test_self_use_conversion_is_conserved(db):
    from services.admin_agent_inventory import convert_inventory_for_self_use

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1, paid=0, bonus=0)
    give_inventory(cur, AGENT, paid=6000, bonus=2000, lot_suffix="self")
    db.commit()

    result = convert_inventory_for_self_use(
        AGENT, paid_points=4000, bonus_points=1000, reason="自己跑诊断",
        operator_user_id=AGENT, operator_username="agent",
        request_id=_req_id("self"), ip_address=None,
    )
    assert result["success"] is True
    inv = _inventory(cur, AGENT)
    wallet = _wallet(cur, AGENT)
    # 库存等额减少
    assert inv["paid_inventory_points"] == 2000
    assert inv["bonus_inventory_points"] == 1000
    # 钱包等额增加(1:1)
    assert wallet["paid_points"] == 4000
    assert wallet["bonus_points"] == 1000
    # 不凭空产生算力:出 5000 = 入 5000
    assert (6000 - inv["paid_inventory_points"]) + (2000 - inv["bonus_inventory_points"]) == 5000
    assert wallet["paid_points"] + wallet["bonus_points"] == 5000
    # lot 侧同步消耗 paid 部分
    assert _lot_remaining(cur, AGENT) == 2000


def test_self_use_rejects_more_than_available(db):
    """反向对照 6:超过库存 → 拒绝,不透支。"""
    from services.admin_agent_inventory import InventoryAdminError, convert_inventory_for_self_use

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1)
    give_inventory(cur, AGENT, paid=100, lot_suffix="over")
    db.commit()

    with pytest.raises(InventoryAdminError) as exc:
        convert_inventory_for_self_use(
            AGENT, paid_points=999999, bonus_points=0, reason="超额转换",
            operator_user_id=AGENT, operator_username="agent",
            request_id=_req_id("self"), ip_address=None,
        )
    assert exc.value.code in {"INVENTORY_REJECTED", "LOT_REJECTED"}
    assert _inventory(cur, AGENT)["paid_inventory_points"] == 100


# ============================================================
# 验收判据 7 · 「名下零客户 + 有库存」的服务商能把钱用掉
#             —— 这就是 2026-08-12 的事故场景
# ============================================================

def test_provider_with_zero_customers_can_spend_inventory(db):
    from services.admin_agent_inventory import convert_inventory_for_self_use

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1, paid=0, bonus=0)
    give_inventory(cur, AGENT, paid=5416, bonus=2708, lot_suffix="stuck")
    db.commit()

    # 前置条件:名下零客户(事故当时 u133 就是这个状态)
    cur.execute(
        "SELECT count(*) AS c FROM customer_agent_bindings WHERE agent_user_id=%s", (AGENT,)
    )
    assert cur.fetchone()["c"] == 0
    # 而且钱包是 0 —— 「付了钱账户看不到」
    assert _wallet(cur, AGENT) == {"paid_points": 0, "bonus_points": 0}

    convert_inventory_for_self_use(
        AGENT, paid_points=5416, bonus_points=2708, reason="没有客户,自己先用",
        operator_user_id=AGENT, operator_username="agent",
        request_id=_req_id("stuck"), ip_address=None,
    )
    # 钱到了能扣费的那个钱包里 —— 锁死解除
    assert _wallet(cur, AGENT) == {"paid_points": 5416, "bonus_points": 2708}
    assert _inventory(cur, AGENT)["paid_inventory_points"] == 0


# ============================================================
# lot 漂移:判据是【前后差分】,不是【必须为 0】
# ============================================================

def test_new_path_does_not_change_existing_drift(db):
    """存量漂移的账号照样能操作,但操作**不许改变**差额。

    🔴 这条锁的是"焊死为 0 会把要解救的人继续锁死"那个坑:
       u133 身上本来就有 -5416 的漂移,若判据写成"必须对得上",
       给他做任何库存动作都会被自己的守卫拒绝。
    """
    from services.admin_agent_inventory import convert_inventory_for_self_use
    from services.inventory_lot_ledger import drift_points_of

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1, paid=0, bonus=0)
    # 复现生产形态:钱包有 8000、lot 只有 3000 → 存量漂移 +5000
    give_inventory(cur, AGENT, paid=8000, with_lot=False)
    cur.execute(
        """INSERT INTO dealer_inventory_lots(
               lot_id, owner_agent_user_id, original_points, remaining_points, reserved_points,
               acquisition_cost_cents, remaining_cost_cents, reserved_cost_cents,
               status, source_kind, pricing_version, evidence_jsonb)
           VALUES ('TESTLOTDRIFT',%s,3000,3000,0,3000,3000,0,'active',
                   'platform_purchase','test-v1','{}'::jsonb)""",
        (AGENT,),
    )
    db.commit()
    drift_before = drift_points_of(cur, AGENT)
    assert drift_before == 5000            # 存量漂移确实存在(判据不是空的)

    convert_inventory_for_self_use(
        AGENT, paid_points=1000, bonus_points=0, reason="带存量漂移也要能操作",
        operator_user_id=AGENT, operator_username="agent",
        request_id=_req_id("drift"), ip_address=None,
    )
    assert drift_points_of(cur, AGENT) == drift_before   # 差额没被改变


def test_lot_drift_rows_is_a_real_criterion(db):
    """反向对照:对得上的账号必须返回空 —— 否则这个"判据"恒真,等于没有。"""
    from services.inventory_lot_ledger import lot_drift_rows

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1)
    give_inventory(cur, AGENT, paid=4000, lot_suffix="clean")
    db.commit()
    assert lot_drift_rows(cur, agent_user_id=AGENT) == []

    cur.execute(
        "UPDATE agent_inventory_wallets SET paid_inventory_points=4001 WHERE agent_user_id=%s",
        (AGENT,),
    )
    db.commit()
    rows = lot_drift_rows(cur, agent_user_id=AGENT)
    assert len(rows) == 1 and rows[0]["drift_points"] == 1


# ============================================================
# [R1 返修 §2] 消 lot 下沉到 allocate_offline —— 锁打在**函数**上,不打在端点上
# ============================================================

def test_allocate_offline_consumes_lot_and_keeps_drift(db):
    """服务商侧线下划拨后差额不变。

    🔴 锁打在 `services.agent_inventory.allocate_offline` 本身,不打在
       `/api/agent/inventory/allocate-offline` 端点上 —— 该函数有三个调用方,
       只锁端点的话下一个调用方照样漏账(这正是 R1 要求下沉的理由)。
    """
    from services.agent_inventory import allocate_offline
    from services.inventory_lot_ledger import drift_points_of

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1)
    make_user(cur, CUSTOMER, agent_level=0)
    give_inventory(cur, AGENT, paid=8000, bonus=1000, lot_suffix="off")
    db.commit()
    drift_before = drift_points_of(cur, AGENT)
    assert drift_before == 0

    allocate_offline(
        cur, agent_user_id=AGENT, customer_user_id=CUSTOMER,
        paid_points=3000, bonus_points=500, description="线下划拨",
    )
    db.commit()
    assert _inventory(cur, AGENT)["paid_inventory_points"] == 5000
    assert _lot_remaining(cur, AGENT) == 5000            # lot 同步消耗
    assert drift_points_of(cur, AGENT) == drift_before   # 差额不变


def test_allocate_offline_consumes_only_paid_not_bonus(db):
    """反向对照:`bonus` 永不消 lot(§5.3 bonus 不作有价库存)。

    纯 bonus 划拨后 lot 一点不动 —— 把 bonus 算进消耗会凭空多消一份有价库存。
    """
    from services.agent_inventory import allocate_offline
    from services.inventory_lot_ledger import drift_points_of

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1)
    make_user(cur, CUSTOMER, agent_level=0)
    give_inventory(cur, AGENT, paid=4000, bonus=2000, lot_suffix="bonly")
    db.commit()
    lot_before = _lot_remaining(cur, AGENT)
    drift_before = drift_points_of(cur, AGENT)

    allocate_offline(
        cur, agent_user_id=AGENT, customer_user_id=CUSTOMER,
        paid_points=0, bonus_points=1500, description="纯赠送划拨",
    )
    db.commit()
    assert _lot_remaining(cur, AGENT) == lot_before       # lot 一点没动
    assert _inventory(cur, AGENT)["bonus_inventory_points"] == 500
    assert drift_points_of(cur, AGENT) == drift_before


def test_agent_rebate_bonus_only_is_lot_noop(db):
    """`services/agent_rebate.py` 走的是 `paid_points=0, bonus_points=rebate`
    → 天然不消任何 lot、不改差额。这条锁固定住"返利链不受本次下沉影响"。
    """
    from services.agent_inventory import allocate_offline
    from services.inventory_lot_ledger import drift_points_of

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1)
    make_user(cur, CUSTOMER, agent_level=0)
    give_inventory(cur, AGENT, paid=1000, bonus=5000, lot_suffix="rebate")
    db.commit()
    lot_before = _lot_remaining(cur, AGENT)
    drift_before = drift_points_of(cur, AGENT)

    # 与 agent_rebate.py:112 完全同形的调用
    allocate_offline(
        cur, agent_user_id=AGENT, customer_user_id=CUSTOMER,
        paid_points=0, bonus_points=800, description="自定返利 8.0% · order=TESTORD",
    )
    db.commit()
    assert _lot_remaining(cur, AGENT) == lot_before
    assert drift_points_of(cur, AGENT) == drift_before


def test_allocate_offline_fails_closed_when_lots_insufficient(db):
    """反向对照:lot 不足 → 整笔拒绝,**钱包一分不动**。

    当前生产撞不上(26 个库存钱包全部 wallet_side ≤ lot_side),
    但分支必须有锁 —— "现在撞不上"不是不锁的理由。
    """
    from services.agent_inventory import allocate_offline
    from services.dealer_inventory_resale import ResaleError

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1)
    make_user(cur, CUSTOMER, agent_level=0)
    # 造出"钱包比 lot 多"的反向漂移:钱包 8000 / lot 只有 1000
    give_inventory(cur, AGENT, paid=8000, with_lot=False)
    cur.execute(
        """INSERT INTO dealer_inventory_lots(
               lot_id, owner_agent_user_id, original_points, remaining_points, reserved_points,
               acquisition_cost_cents, remaining_cost_cents, reserved_cost_cents,
               status, source_kind, pricing_version, evidence_jsonb)
           VALUES ('TESTLOTSHORT',%s,1000,1000,0,1000,1000,0,'active',
                   'platform_purchase','test-v1','{}'::jsonb)""",
        (AGENT,),
    )
    db.commit()

    with pytest.raises(ResaleError):
        allocate_offline(
            cur, agent_user_id=AGENT, customer_user_id=CUSTOMER,
            paid_points=5000, bonus_points=0, description="lot 不够",
        )
    db.rollback()
    assert _inventory(cur, AGENT)["paid_inventory_points"] == 8000   # 钱包未动
    assert _lot_remaining(cur, AGENT) == 1000                        # lot 也未动


def test_admin_allocate_consumes_lot_exactly_once(db):
    """🔴 防双重消耗:下沉之后上层**不许**再外挂 consume_lots_fifo。

    消耗量必须 == paid_points,不是 2×。若两处都在消,
    lot 会少扣一倍、差额转正 → `assert_drift_unchanged` 抛错,这条路直接不可用。
    """
    from services.admin_agent_inventory import admin_allocate_to_customer

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1)
    make_user(cur, CUSTOMER, agent_level=0)
    make_user(cur, ADMIN, is_admin=True)
    give_inventory(cur, AGENT, paid=8000, lot_suffix="once")
    db.commit()

    admin_allocate_to_customer(
        AGENT, CUSTOMER, paid_points=3000, bonus_points=0,
        binding_expected_version=governance_version(cur, CUSTOMER, "commercial_binding"),
        reason="只消一次", operator_user_id=ADMIN, operator_username="admin",
        request_id=_req_id("once"), ip_address=None,
    )
    # 消耗恰好 3000(不是 6000)
    assert _lot_remaining(cur, AGENT) == 5000
    assert _inventory(cur, AGENT)["paid_inventory_points"] == 5000


def test_self_use_consumes_lot_exactly_once(db):
    """同上,自用这条路也不许双重消耗。"""
    from services.admin_agent_inventory import convert_inventory_for_self_use

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1, paid=0, bonus=0)
    give_inventory(cur, AGENT, paid=6000, lot_suffix="once2")
    db.commit()

    convert_inventory_for_self_use(
        AGENT, paid_points=2000, bonus_points=0, reason="只消一次",
        operator_user_id=AGENT, operator_username="agent",
        request_id=_req_id("once2"), ip_address=None,
    )
    assert _lot_remaining(cur, AGENT) == 4000
    assert _inventory(cur, AGENT)["paid_inventory_points"] == 4000


# ============================================================
# 验收判据 4 · 服务商能发展下级服务商
# ============================================================

def test_provider_can_request_downstream_provider(db):
    """[invrel 2026-08-13 改写] 申请仍能提交,但**不再先回显对方身份**。

    🔴 为什么改而不是删:这条用例原本断言
        `resolve_downstream_path(AGENT, 陌生服务商)` 会回 `path=channel_relationship`
        —— 那正是工单 v3 §1.3b 第 2 条判定的**账号枚举面**:对任意 target_user_id
        都回展示名与身份,随便试 ID 就能问出"此号注册没有 / 是不是服务商"。
        v3 §P0-2 明令禁止对陌生账号做身份提示,所以那两行断言锁的是**已被推翻的 v2 行为**。
        保留用例本体(申请能提交),把身份回显那部分换成"必须查不到"。
    """
    from services import channel_partner_requests as svc

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1)
    make_user(cur, DOWNSTREAM_PROVIDER, agent_level=1)
    db.commit()

    # 无有向关系 → 与"账号不存在"同一份响应,不透露对方是不是服务商。
    with pytest.raises(svc.ChannelPartnerRequestError) as exc:
        svc.resolve_downstream_path(cur, AGENT, DOWNSTREAM_PROVIDER)
    assert exc.value.code == "TARGET_NOT_FOUND"

    # 但申请本身照常可提交 —— 合作是否成立由平台审批判定(R2)。
    result = svc.create_request(
        requester_user_id=AGENT, target_user_id=DOWNSTREAM_PROVIDER,
        proposed_cost_multiplier_bps=11000, reason="线下已签合作",
        request_id=_req_id("cpr"),
    )
    assert result["status"] == "pending"


def test_preflight_does_not_disclose_ordinary_user_identity(db):
    """[invrel 2026-08-13 改写] 原名 `test_preflight_routes_ordinary_user_to_binding_path`。

    🔴 原断言:陌生普通用户 → `path=commercial_binding` 且 headline 含"普通用户",
       并期望 `create_request` 报 `TARGET_IS_ORDINARY_USER`。
       这三处都是**可区分位**:手机号/账号 ID 可枚举,
       "是普通用户"与"是服务商"与"不存在"三种不同回应 = 三种探测结果。
       v3 §P0-2 把它们合并成同一份响应,因此这条用例改为锁**不可区分性**。
    """
    from services import channel_partner_requests as svc

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1)
    make_user(cur, CUSTOMER, agent_level=0)
    db.commit()

    with pytest.raises(svc.ChannelPartnerRequestError) as exc:
        svc.resolve_downstream_path(cur, AGENT, CUSTOMER)
    assert exc.value.code == "TARGET_NOT_FOUND"
    assert exc.value.details == {}

    with pytest.raises(svc.ChannelPartnerRequestError) as exc:
        svc.create_request(
            requester_user_id=AGENT, target_user_id=CUSTOMER,
            proposed_cost_multiplier_bps=11000, reason="想把普通用户当下级服务商",
            request_id=_req_id("cpr"),
        )
    # 统一码:普通用户 / 不存在 / 已有别的上游 —— 三种都是这一个,不再各有各的码。
    assert exc.value.code == "TARGET_NOT_ELIGIBLE"

    # 反向对照:一个**根本不存在**的账号走的是同一条路径、同一个码。
    with pytest.raises(svc.ChannelPartnerRequestError) as ghost:
        svc.create_request(
            requester_user_id=AGENT, target_user_id=CUSTOMER + 777_000,
            proposed_cost_multiplier_bps=11000, reason="不存在的账号",
            request_id=_req_id("cpr"),
        )
    assert ghost.value.code == exc.value.code
    assert str(ghost.value) == str(exc.value)


def test_channel_request_rejects_below_cost_multiplier(db):
    """反向对照:进货系数低于 10000 → 拒绝(下级不得低于上游有效成本)。"""
    from services import channel_partner_requests as svc

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1)
    make_user(cur, DOWNSTREAM_PROVIDER, agent_level=1)
    db.commit()

    with pytest.raises(svc.ChannelPartnerRequestError) as exc:
        svc.create_request(
            requester_user_id=AGENT, target_user_id=DOWNSTREAM_PROVIDER,
            proposed_cost_multiplier_bps=9000, reason="想给下级更低的价",
            request_id=_req_id("cpr"),
        )
    assert exc.value.code == "COST_MULTIPLIER_TOO_LOW"


def test_channel_request_approval_creates_relationship(db):
    """批准 → 同事务落渠道关系,后续进货即可走 JIT 逐跳转售。"""
    from services import channel_partner_requests as svc

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1)
    make_user(cur, DOWNSTREAM_PROVIDER, agent_level=1)
    make_user(cur, ADMIN, is_admin=True)
    db.commit()

    created = svc.create_request(
        requester_user_id=AGENT, target_user_id=DOWNSTREAM_PROVIDER,
        proposed_cost_multiplier_bps=11000, reason="线下已签合作",
        request_id=_req_id("cpr"),
    )
    svc.approve_request(
        request_row_id=created["request_id_row"], cost_multiplier_bps=None,
        channel_expected_version=governance_version(
            cur, DOWNSTREAM_PROVIDER, "channel_relationship"
        ),
        decision_note="资料齐全,批准", operator_user_id=ADMIN,
        operator_username="admin", request_id=_req_id("appr"), ip_address=None,
    )
    cur.execute(
        """SELECT upstream_channel_account_id, cost_multiplier_bps
           FROM channel_pricing_relationships
           WHERE buyer_dealer_id=%s AND status='active' AND effective_to IS NULL""",
        (DOWNSTREAM_PROVIDER,),
    )
    row = cur.fetchone()
    assert int(row["upstream_channel_account_id"]) == AGENT
    assert int(row["cost_multiplier_bps"]) == 11000


def test_channel_request_rejection_leaves_no_relationship(db):
    """反向对照:驳回 → 不落任何渠道关系。"""
    from services import channel_partner_requests as svc

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1)
    make_user(cur, DOWNSTREAM_PROVIDER, agent_level=1)
    make_user(cur, ADMIN, is_admin=True)
    db.commit()

    created = svc.create_request(
        requester_user_id=AGENT, target_user_id=DOWNSTREAM_PROVIDER,
        proposed_cost_multiplier_bps=11000, reason="申请",
        request_id=_req_id("cpr"),
    )
    svc.reject_request(
        request_row_id=created["request_id_row"], decision_note="资料不全",
        operator_user_id=ADMIN,
    )
    cur.execute(
        """SELECT count(*) AS c FROM channel_pricing_relationships
           WHERE buyer_dealer_id=%s AND status='active'""",
        (DOWNSTREAM_PROVIDER,),
    )
    assert cur.fetchone()["c"] == 0


def test_only_one_pending_request_per_target(db):
    """反向对照:同一目标两条 pending → 拒绝(否则审批顺序决定分润链归谁 = 竞态)。"""
    from services import channel_partner_requests as svc

    cur = db.cursor()
    make_user(cur, AGENT, agent_level=1)
    make_user(cur, OTHER_AGENT, agent_level=1)
    make_user(cur, DOWNSTREAM_PROVIDER, agent_level=1)
    db.commit()

    svc.create_request(
        requester_user_id=AGENT, target_user_id=DOWNSTREAM_PROVIDER,
        proposed_cost_multiplier_bps=11000, reason="先到", request_id=_req_id("cpr"),
    )
    with pytest.raises(svc.ChannelPartnerRequestError) as exc:
        svc.create_request(
            requester_user_id=OTHER_AGENT, target_user_id=DOWNSTREAM_PROVIDER,
            proposed_cost_multiplier_bps=12000, reason="后到", request_id=_req_id("cpr"),
        )
    assert exc.value.code == "PENDING_REQUEST_EXISTS"
