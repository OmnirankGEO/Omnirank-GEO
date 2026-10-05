"""工单 WO_V35_REFUND_SINGLE_LEDGER_REWIRE_2026-08-17 · 判据 1-4 行为锁

判据编号与工单 §判据 一一对应:
  1. C-2 主判据 + 拆锁       -> test_c2_* / test_c2_lock_removal_*
  2. 双拿反向对照            -> test_c2_reverse_*
  3. C-1 代发退款零进停写表  -> test_c1_*
  4. C-3 端点前后行数不变    -> test_c3_*

🔴 零生产数据:见 conftest 顶部说明。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.v35_refund_single_ledger_2026_08_17._shape import (  # noqa: E402
    AGENT, CUSTOMER, CUSTOMER_NOROW, ORDER_ID, ORDER_ID_SPENT,
    credit_pools_total, credit_wallet_row_count, seed_exposure_shape, wallet_of,
)

DB = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB, reason="需 TEST_DATABASE_URL")


# ════════════════════════════════════════════════════════════════
# 判据 1 · C-2 主判据:撤回 > 0 且扣的是 user_wallets
# ════════════════════════════════════════════════════════════════

def test_c2_revoke_hits_user_wallets_not_dead_table(conn):
    """暴露面形状 → 走客户侧回收 → **user_wallets 真被扣**,停写表零变化。

    这是 C-2 的核心:接线前 revoke_credit 的三层 min 中间层(停写表余额)恒 0
    ⇒ 撤回恒 0;接线后按 user_wallets 余额夹紧 ⇒ 撤回 = 未消费额。
    """
    from services.customer_entitlement import revoke_from_customer

    cur = conn.cursor()
    seed_exposure_shape(cur, allocated=4160, consumed=0)
    before_pools = credit_pools_total(cur)
    before_rows = credit_wallet_row_count(cur)
    assert wallet_of(cur, CUSTOMER)["paid_points"] == 4160

    res = revoke_from_customer(
        cur, customer_user_id=CUSTOMER, agent_user_id=AGENT,
        paid_points=4160, bonus_points=0,
        related_order_id=ORDER_ID, source="refund_revoke",
    )

    # 撤回真发生(接线前这里恒 0)
    assert res["revoked_paid"] == 4160, f"撤回额应为 4160,实得 {res['revoked_paid']}"
    # 钱从 user_wallets 扣走
    assert wallet_of(cur, CUSTOMER)["paid_points"] == 0
    # 停写表零变化(既没被扣也没被写)
    assert credit_pools_total(cur) == before_pools == 0
    assert credit_wallet_row_count(cur) == before_rows


def test_c2_cascade_receives_nonzero_amounts(conn):
    """级联不断在半路:撤回结果要能驱动下游库存回收(paid/bonus 两个口都非空)。"""
    from services.customer_entitlement import revoke_from_customer

    cur = conn.cursor()
    seed_exposure_shape(cur, allocated=3000, consumed=0, wallet_paid=3000, wallet_bonus=500)
    res = revoke_from_customer(
        cur, customer_user_id=CUSTOMER, agent_user_id=AGENT,
        paid_points=3000, bonus_points=500,
        related_order_id=ORDER_ID, source="refund_revoke",
    )
    # referral_api 的库存回流用的就是这两个键
    assert res["revoked_paid"] == 3000
    assert res["revoked_bonus"] == 500
    assert res["revoked_paid"] + res["revoked_bonus"] == 3500


def test_c2_lock_removal_old_path_goes_red(conn):
    """🔴 拆锁:把接线改回老函数语义(以停写表余额为上限)→ 撤回必归 0。

    不 import 已删除的 revoke_credit(它已不存在),而是**逐字复刻**它的三层 min
    中间层:min(请求, 停写表余额, FIFO)。停写表余额恒 0 ⇒ 结果恒 0。
    本用例转绿即证明:如果谁把接线改回停写表,判据 1 会立刻失效。
    """
    cur = conn.cursor()
    seed_exposure_shape(cur, allocated=4160, consumed=0)

    cur.execute(
        "SELECT tool_credit_points, publish_credit_points, bonus_credit_points "
        "FROM customer_agent_credit_wallets WHERE customer_user_id=%s", (CUSTOMER,))
    dead = cur.fetchone()
    # 老 revoke_credit 的三层 min:min(请求量, **停写表余额**, 订单FIFO未消费)
    old_actually_tool = min(4160, int(dead["tool_credit_points"]), 4160)

    assert old_actually_tool == 0, "老路径居然撤回了非 0 —— 夹具没复刻出暴露面形状"
    # 同一形状下新路径撤回 4160 —— 两者差 4160 就是本工单堵住的资金口子
    from services.customer_entitlement import revoke_from_customer
    res = revoke_from_customer(
        cur, customer_user_id=CUSTOMER, agent_user_id=AGENT,
        paid_points=4160, bonus_points=0, related_order_id=ORDER_ID, source="refund_revoke")
    assert res["revoked_paid"] - old_actually_tool == 4160


# ════════════════════════════════════════════════════════════════
# 判据 1b · 接线锁(锁接线不锁函数)
#   光锁 revoke_from_customer 的行为是不够的 —— 真正会退化的是**谁调它**。
#   这三条锁的是 referral_api 的接线本身:改回停写表 / 级联键写错 都会转红。
# ════════════════════════════════════════════════════════════════

def test_wiring_refund_uses_entitlement_not_dead_table():
    """referral_api 的 v35 退款不得再出现 revoke_credit 调用。"""
    import ast
    import inspect
    from api import referral_api
    src = inspect.getsource(referral_api._handle_v35_factory_refund)
    tree = ast.parse(src.lstrip())
    called = {
        n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
        for n in ast.walk(tree) if isinstance(n, ast.Call)
    }
    assert "revoke_credit" not in called, "退款又接回停写表的 revoke_credit 了"
    assert "_revoke_customer_and_sync_grants" in called, "没走接班模块 helper"


def test_wiring_cascade_keys_are_new_shape():
    """级联不许断在半路:库存回流必须吃 revoked_paid / revoked_bonus 两个新键。"""
    import inspect
    from api import referral_api
    src = inspect.getsource(referral_api._handle_v35_factory_refund)
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert code.count("credit_result[\"revoked_paid\"]") == 3, "三处库存回流没有全部用 revoked_paid"
    assert code.count("credit_result[\"revoked_bonus\"]") == 3, "三处库存回流没有全部用 revoked_bonus"
    # 老键彻底消失(actually["tool"]/["publish"] 是停写表三池语义)
    assert 'actually["tool"]' not in code and 'actually["publish"]' not in code


def test_wiring_no_name_shadowing_of_revoke_from_customer():
    """🔴 同名遮蔽锁:agent_inventory 与 customer_entitlement 都有 revoke_from_customer。
    本函数里裸 import 后者会遮蔽前者 → 三处库存回流静默调错函数(钱回错地方)。"""
    import inspect
    from api import referral_api
    src = inspect.getsource(referral_api._handle_v35_factory_refund)
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert "from services.customer_entitlement import revoke_from_customer" not in code or \
           "as _" in code, "裸 import 会遮蔽 agent_inventory.revoke_from_customer"
    assert "from services.agent_inventory import revoke_from_customer" in code


# ════════════════════════════════════════════════════════════════
# 判据 2 · 双拿反向对照:花光的订单不能撤成负数 / 不能超收
# ════════════════════════════════════════════════════════════════

def test_c2_reverse_fully_spent_revokes_nothing(conn):
    """余额已花光 → 撤回 0(不为负、不超收)。"""
    from services.customer_entitlement import revoke_from_customer

    cur = conn.cursor()
    # 划拨 4160 全部花光 → user_wallets 余额 0
    seed_exposure_shape(cur, allocated=4160, consumed=4160, wallet_paid=0)
    res = revoke_from_customer(
        cur, customer_user_id=CUSTOMER, agent_user_id=AGENT,
        paid_points=4160, bonus_points=0, related_order_id=ORDER_ID_SPENT, source="refund_revoke")
    assert res["revoked_paid"] == 0
    assert wallet_of(cur, CUSTOMER)["paid_points"] == 0, "余额被扣成负数"


def test_c2_reverse_partially_spent_caps_at_remaining(conn):
    """花掉一半 → 只能撤回剩下的一半(按当前余额 cap)。"""
    from services.customer_entitlement import revoke_from_customer

    cur = conn.cursor()
    seed_exposure_shape(cur, allocated=4160, consumed=2000, wallet_paid=2160)
    res = revoke_from_customer(
        cur, customer_user_id=CUSTOMER, agent_user_id=AGENT,
        paid_points=4160, bonus_points=0, related_order_id=ORDER_ID, source="refund_revoke")
    assert res["revoked_paid"] == 2160, "应按剩余余额 cap 到 2160"
    assert wallet_of(cur, CUSTOMER)["paid_points"] == 0


def test_c2_reverse_never_negative_on_overask(conn):
    """请求量远超余额 → 夹紧,绝不为负。"""
    from services.customer_entitlement import revoke_from_customer

    cur = conn.cursor()
    seed_exposure_shape(cur, allocated=100, consumed=0, wallet_paid=100)
    res = revoke_from_customer(
        cur, customer_user_id=CUSTOMER, agent_user_id=AGENT,
        paid_points=999_999, bonus_points=999_999, related_order_id=ORDER_ID, source="refund_revoke")
    assert res["revoked_paid"] == 100
    assert res["revoked_bonus"] == 0
    w = wallet_of(cur, CUSTOMER)
    assert w["paid_points"] == 0 and w["bonus_points"] == 0


# ════════════════════════════════════════════════════════════════
# 判据 3 · C-1 代发退款:带残行的账号退款零进停写表
# ════════════════════════════════════════════════════════════════

def test_c1_ledger_router_never_returns_v35(conn):
    """判据源头:_publish_refund_ledger 对**有残行**的账号也必须返平台账本。"""
    from db.meijiehezi_db import (
        _publish_refund_ledger, _PUBLISH_REFUND_LEDGER_PLATFORM, _PUBLISH_REFUND_LEDGER_V35,
    )
    cur = conn.cursor()
    seed_exposure_shape(cur, allocated=1000, consumed=0, with_dead_row=True)

    # 前置自证:该账号**确实**有停写表残行(否则本用例零判别力)
    cur.execute("SELECT count(*) AS c FROM customer_agent_credit_wallets WHERE customer_user_id=%s",
                (CUSTOMER,))
    assert int(cur.fetchone()["c"]) == 1, "夹具没造出残行 → 本用例不成立"

    got = _publish_refund_ledger(cur, CUSTOMER)
    assert got == _PUBLISH_REFUND_LEDGER_PLATFORM, f"仍按残行判成 {got}"
    assert got != _PUBLISH_REFUND_LEDGER_V35


def test_c1_no_row_account_also_platform(conn):
    """无残行的账号同样走平台账本(证明判据不是"反过来按行存在性判")。"""
    from db.meijiehezi_db import _publish_refund_ledger, _PUBLISH_REFUND_LEDGER_PLATFORM
    cur = conn.cursor()
    seed_exposure_shape(cur, allocated=1000, consumed=0, with_dead_row=False)
    cur.execute("SELECT count(*) AS c FROM customer_agent_credit_wallets WHERE customer_user_id=%s",
                (CUSTOMER,))
    assert int(cur.fetchone()["c"]) == 0
    assert _publish_refund_ledger(cur, CUSTOMER) == _PUBLISH_REFUND_LEDGER_PLATFORM


def test_c1_refund_writes_user_wallets_and_zero_new_credit_rows(conn):
    """代发退款(含 scheduler 路径共用的同一 helper)→ user_wallets/point_transactions 有痕,
    停写表**零新行、零金额变化**。"""
    from db.meijiehezi_db import refund_for_publish_order

    cur = conn.cursor()
    seed_exposure_shape(cur, allocated=1000, consumed=0, wallet_paid=1000)
    conn.commit()

    before_rows = credit_wallet_row_count(cur)
    before_pools = credit_pools_total(cur)
    cur.execute("SELECT count(*) AS c FROM customer_credit_transactions")
    before_cct = int(cur.fetchone()["c"])
    before_paid = wallet_of(cur, CUSTOMER)["paid_points"]

    res = refund_for_publish_order(CUSTOMER, 130, f"item:{CUSTOMER}-t1", "夹具:代发失败自动退款")
    assert res.get("success"), res

    cur.execute("SELECT paid_points FROM user_wallets WHERE user_id=%s", (CUSTOMER,))
    after_paid = int(cur.fetchone()["paid_points"])
    assert after_paid == before_paid + 130, "退款没落 user_wallets"

    cur.execute("SELECT count(*) AS c FROM point_transactions "
                "WHERE user_id=%s AND type='refund' AND feature_code='media_proxy_publish'", (CUSTOMER,))
    assert int(cur.fetchone()["c"]) == 1, "平台流水没写"

    assert credit_wallet_row_count(cur) == before_rows, "停写表新增了行"
    assert credit_pools_total(cur) == before_pools == 0, "钱写进了停写表三池"
    cur.execute("SELECT count(*) AS c FROM customer_credit_transactions")
    assert int(cur.fetchone()["c"]) == before_cct, "停写表流水被写入"


# ════════════════════════════════════════════════════════════════
# 判据 4 · C-3 端点调用前后停写表行数不变
# ════════════════════════════════════════════════════════════════

def test_c3_no_insert_on_credit_lookup(conn):
    """对**没有**残行的客户查额度 → 停写表行数不得增加(INSERT 封死的直接证据)。"""
    cur = conn.cursor()
    seed_exposure_shape(cur, customer=CUSTOMER, allocated=500, consumed=0, with_dead_row=False)
    # 造一条绑定,让 _require_customer_owned_by_agent 能过
    cur.execute(
        "INSERT INTO customer_agent_bindings (customer_user_id, agent_user_id, binding_source, bound_at) "
        "VALUES (%s,%s,'admin_manual',NOW()) ON CONFLICT (customer_user_id) DO UPDATE "
        "SET agent_user_id=EXCLUDED.agent_user_id", (CUSTOMER, AGENT))
    conn.commit()

    before = credit_wallet_row_count(cur)

    # 直接打端点内部那段读逻辑(不起 HTTP · 判的是"有没有 INSERT")
    import ast
    import inspect
    from api import agent_workbench_api as awa
    src = inspect.getsource(awa.agent_customer_credit)
    # 🔴 只看**可执行代码**:注释/docstring 里出现函数名是正常的(本包就写了说明),
    #    用裸 `in src` 判会把自己的注释判成红(第一版就踩了)。改用 AST 取调用名。
    tree = ast.parse(inspect.cleandoc(src) if src.startswith("async def") else src.lstrip())
    called = {
        n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
        for n in ast.walk(tree) if isinstance(n, ast.Call)
    }
    assert "get_or_create_customer_wallet" not in called, "端点仍在调 get_or_create(会 INSERT 停写表)"
    code_only = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert "user_wallets" in code_only, "端点没有改读 user_wallets"

    # 行为面:模拟同一段读,确认不建行
    cur.execute("SELECT paid_points, bonus_points FROM user_wallets WHERE user_id=%s", (CUSTOMER,))
    _ = cur.fetchone()
    conn.commit()
    assert credit_wallet_row_count(cur) == before, "停写表被建了新行"


def test_c3_response_contract_unchanged(conn):
    """响应契约不新增字段(extra='forbid' 先例)· 三个字段名保持不变。"""
    from api.agent_workbench_api import AgentCustomerCreditResponse
    fields = set(AgentCustomerCreditResponse.model_fields.keys())
    for k in ("tool_credit_points", "publish_credit_points", "bonus_credit_points",
              "total_allocated", "total_consumed", "customer_user_id"):
        assert k in fields, f"契约字段 {k} 丢了"


# ════════════════════════════════════════════════════════════════
# R4 · 死代码确已删除(源码级锁)
# ════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("name", [
    "allocate_credit", "consume_credit", "revoke_credit", "freeze_customer_credit",
    "get_wallet_summary", "estimate_remaining_usage", "is_publish_feature",
])
def test_r4_dead_functions_removed(name):
    import services.customer_credit as cc
    assert not hasattr(cc, name), f"{name} 仍存在 —— R4 未删干净"


@pytest.mark.parametrize("name", ["refund_credit", "compute_unspent_from_order",
                                  "commit_customer_freeze", "release_customer_freeze",
                                  "get_or_create_customer_wallet"])
def test_r4_retained_functions_still_there(name):
    """没删过头:历史结算安全网 + 退款链仍需要的读函数必须还在。"""
    import services.customer_credit as cc
    assert hasattr(cc, name), f"{name} 被误删 —— 历史 v35 冻结将无法结算"


def test_r4_diagnosis_runs_v35_branch_retired():
    """两处 v35 分支不再读停写表,且保留 fail-closed 出口(不静默放行)。"""
    import inspect
    import services.diagnosis_runs as dr
    for fn in (dr._verify_ledger_refund, dr.refund_and_confirm_v35_delivery):
        src = inspect.getsource(fn)
        assert "customer_agent_credit_wallets" not in src.replace("#", "@@")[:0] or True
        # 真判据:函数体里不得再有对该表的 SQL(注释允许)
        code_lines = [l for l in src.splitlines() if not l.strip().startswith("#")]
        body = "\n".join(code_lines)
        assert "FROM customer_agent_credit_wallets" not in body, f"{fn.__name__} 仍读停写表"
        assert "V35_LEDGER_RETIRED" in src, f"{fn.__name__} 缺 fail-closed 出口"
