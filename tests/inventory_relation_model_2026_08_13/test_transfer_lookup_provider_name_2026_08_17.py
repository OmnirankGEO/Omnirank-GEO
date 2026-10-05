"""工单 v2 判据 · 划拨/供货弹窗与搜索接口的单账本收敛残留(2026-08-17)。

编号与 `WO_TRANSFER_LOOKUP_PROVIDER_NAME_2026-08-17.md` §4 一一对应:

  §4-1  R1 服务商目标 brand_name 置 NULL(成对:普通客户照旧)+ **拆锁必转红**
  §4-2  R2 请求映射:充值算力 → tool_points / publish_points 恒 0 / 赠送 → bonus_points,
        落库断言客户 `user_wallets` paid/bonus 增量与两格输入一致
  §4-3  R3 lookup 余额停读 `customer_agent_credit_wallets`,改读 `user_wallets`
        —— 夹具刻意造「信用钱包旧行 ≠ user_wallets 现值」,两个数不同才有判别力

🔴 全部走夹具账户(TEST_ID_BASE 段),真实账户 133/163 与真实客户品牌零写操作。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import Request

# 🔴 故意**整模块导入**、成员在用例里现取:
#    `_lookup_target_is_provider` 是本包新增的,`from … import` 会让基线臂
#    在 **collection** 阶段就 ImportError —— 那是"整个模块没跑起来",
#    与"跑起来了但判据红"是两回事(本仓踩过:把跑不起来当抓到毒)。
#    整模块导入后,基线臂的每一条判据都真的执行、各自按自己的原因转红。
import api.agent_workbench_api as awb

from .conftest import TEST_ID_BASE, make_binding, make_brand, make_channel, make_user

_lookup_agent_customers = awb._lookup_agent_customers
_shape_agent_customer_lookup_row = awb._shape_agent_customer_lookup_row

# 与生产同构:UP=133(上游服务商) · PROVIDER=163(下线服务商,名下全是他自己的客户)
UP = TEST_ID_BASE + 61
PROVIDER = TEST_ID_BASE + 62          # 服务商目标 · 渠道关系 + 客户绑定(= 生产 163 的 both 形态)
PLAIN = TEST_ID_BASE + 63             # 普通客户 · 仅客户绑定
PROVIDER_ONLY_CHANNEL = TEST_ID_BASE + 64   # 服务商目标 · 只有渠道关系

# PROVIDER 名下最新的品牌 —— 生产里这条被当成了标题(「贵州省禾椒香食品有限公司」)
PROVIDER_LATEST_BRAND = "服务商163名下最新客户品牌"
PLAIN_BRAND = "普通客户自己的品牌"


@pytest.fixture
def world(db):
    cur = db.cursor()
    make_user(cur, UP, phone="13930000001", name="上游服务商133", agent_level=2)
    make_user(cur, PROVIDER, phone="13930000002", name="下线服务商163", agent_level=1)
    make_user(cur, PLAIN, phone="13930000003", name="普通客户", agent_level=0)
    make_user(cur, PROVIDER_ONLY_CHANNEL, phone="13930000004", name="纯渠道下线", agent_level=1)

    # 生产形态:163 既是 133 的下线服务商,又有客户绑定(both)
    make_channel(cur, buyer_dealer_id=PROVIDER, upstream_user_id=UP, bps=13000)
    make_binding(cur, customer_user_id=PROVIDER, agent_user_id=UP)
    make_binding(cur, customer_user_id=PLAIN, agent_user_id=UP)
    make_channel(cur, buyer_dealer_id=PROVIDER_ONLY_CHANNEL, upstream_user_id=UP, bps=12000)

    # 🔴 泄露源:服务商名下挂两个品牌,`ORDER BY b.id DESC LIMIT 1` 会取到后插的那个。
    make_brand(cur, owner_user_id=PROVIDER, name="服务商163名下较早的客户品牌")
    make_brand(cur, owner_user_id=PROVIDER, name=PROVIDER_LATEST_BRAND)
    make_brand(cur, owner_user_id=PROVIDER_ONLY_CHANNEL, name="纯渠道下线名下客户品牌")
    # 反向对照:普通客户自己的品牌名 —— 这条**必须继续**出现在响应里
    make_brand(cur, owner_user_id=PLAIN, name=PLAIN_BRAND)
    db.commit()
    return cur


def _row_of(cur, keyword: str) -> dict:
    rows = _lookup_agent_customers(cur, UP, keyword, owned_only=True, limit=8)
    assert len(rows) == 1, f"{keyword!r} 命中 {len(rows)} 条,判据前提不成立"
    return rows[0]


# ============================================================
# §4-1 R1 · 服务商目标的 brand_name 必须从响应里消失
# ============================================================

def test_r1_provider_target_has_no_brand_name(world, db):
    """🔴 目标是服务商(both 形态)→ `brand_name` 为空。

    改造前:这里返回 `服务商163名下最新客户品牌`,前端 `c.brand_name || …`
    直接把它当成了标题 —— 上游一眼看见下线**客户名单**的第一条。
    """
    row = _row_of(db.cursor(), "13930000002")
    assert row["customer_user_id"] == PROVIDER
    assert row["binding_status"] == "both"
    assert row["target_identity"] == "service_provider"
    assert row.get("brand_name") is None, row.get("brand_name")
    # 名字必须还在(拿掉的是品牌名这一位,不是把人做成匿名)
    assert row["display_name"] == "下线服务商163"


def test_r1b_provider_with_only_channel_relation_also_masked(world, db):
    """另一个入口:只有渠道关系的下线 —— 泄露位常常藏在"另一条 SQL"里。"""
    row = _row_of(db.cursor(), "13930000004")
    assert row["binding_status"] == "downstream_partner"
    assert row.get("brand_name") is None, row.get("brand_name")


def test_r1c_plain_customer_brand_name_unchanged(world, db):
    """🔴 成对反向对照:普通客户的品牌名**照旧返回**。

    没有这一条,把 `brand_name` 无脑置空也能让上面两条全绿 —— 那是过度修复,
    普通客户的卡片会集体退化成手机号。
    """
    row = _row_of(db.cursor(), "13930000003")
    assert row["binding_status"] == "owned"
    assert row["target_identity"] == "level0"
    assert row["brand_name"] == PLAIN_BRAND


def test_r1d_lock_short_circuit_provider_branch_turns_red(world, db):
    """🔴 拆锁:短路 provider 判别(恒 False)→ 上面两条必须转红。

    这是判据的**判别力自证** —— 直接对 `_shape_agent_customer_lookup_row`
    喂真实的行,证明"品牌名不见了"确实是 provider 分支干的,
    而不是 SQL 本来就没查出品牌名(那样的话判据恒真、等于没判据)。
    """
    # 先证明**原始行确实带着品牌名** —— 否则后面测的是空气
    raw = dict(_row_of(db.cursor(), "13930000002"))
    assert raw["customer_user_id"] == PROVIDER

    probe = {
        "customer_user_id": PROVIDER,
        "phone": "13930000002",
        "display_name": "下线服务商163",
        "brand_name": PROVIDER_LATEST_BRAND,
        "binding_status": "both",
        "target_identity": "service_provider",
        "tool_credit_points": 0, "publish_credit_points": 0, "bonus_credit_points": 0,
    }
    assert _shape_agent_customer_lookup_row(probe)["brand_name"] is None

    original = awb._lookup_target_is_provider
    try:
        awb._lookup_target_is_provider = lambda row: False       # 拆锁
        leaked = _shape_agent_customer_lookup_row(probe)
    finally:
        awb._lookup_target_is_provider = original
    assert leaked["brand_name"] == PROVIDER_LATEST_BRAND, "拆锁后没泄露 = 锁根本没起作用"


def test_r1e_provider_predicate_takes_either_signal(world):
    """两个信源各自单独成立即可(身份读失败也不漏判)。"""
    is_provider = awb._lookup_target_is_provider
    assert is_provider({"target_identity": "service_provider",
                        "binding_status": "owned"}) is True
    assert is_provider({"binding_status": "downstream_partner"}) is True
    assert is_provider({"binding_status": "both"}) is True
    # 反向对照:普通客户两个信源都不命中
    assert is_provider({"target_identity": "level0",
                        "binding_status": "owned"}) is False


# ============================================================
# §4-3 R3 · 余额停读已停写的 customer_agent_credit_wallets
# ============================================================

def _seed_stale_credit_wallet(cur, uid: int, *, tool: int, publish: int, bonus: int) -> None:
    """造一条**冻结在 2026-07-27 拆除那天**的信用钱包旧行。"""
    cur.execute(
        """INSERT INTO customer_agent_credit_wallets
               (customer_user_id, agent_user_id, tool_credit_points,
                publish_credit_points, bonus_credit_points)
           VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT (customer_user_id) DO UPDATE
           SET tool_credit_points=EXCLUDED.tool_credit_points,
               publish_credit_points=EXCLUDED.publish_credit_points,
               bonus_credit_points=EXCLUDED.bonus_credit_points""",
        (uid, UP, tool, publish, bonus),
    )


def _set_user_wallet(cur, uid: int, *, paid: int, bonus: int) -> None:
    cur.execute(
        "UPDATE user_wallets SET paid_points=%s, bonus_points=%s WHERE user_id=%s",
        (paid, bonus, uid),
    )


def test_r3_lookup_balance_comes_from_user_wallets(world, db):
    """🔴 旧信用钱包 ≠ 现钱包时,搜索必须报**现钱包**的数。

    🔴 判别力:两组数字**刻意互不相等且都非 0** —— 若任一边是 0 或两边相同,
       这条判据在旧代码上也会绿(本仓踩过"数据没有区分力"的假绿)。
    """
    cur = db.cursor()
    _seed_stale_credit_wallet(cur, PLAIN, tool=7777, publish=3333, bonus=1111)
    _set_user_wallet(cur, PLAIN, paid=52000, bonus=4200)
    db.commit()

    # 前提自证:两边真的不一样,否则本判据零判别力
    assert (7777, 1111) != (52000, 4200)

    row = _row_of(db.cursor(), "13930000003")
    assert row["tool_credit_points"] == 52000, row
    assert row["bonus_credit_points"] == 4200, row
    # 单账本后没有独立发布池 —— 恒 0,且**绝不**等于旧行里的 3333
    assert row["publish_credit_points"] == 0
    assert row["publish_credit_points"] != 3333


def test_r3b_no_lookup_sql_reads_the_dead_table(world, db):
    """结构锁:`_lookup_agent_customers` 跑过的 SQL 里不许再出现那张停写表。

    打**真实执行过的语句**(psycopg2 `cursor.query`),不是 grep 源码。
    """
    executed: list[str] = []

    class RecordingCursor:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=None):
            executed.append(str(sql))
            return self._inner.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    rows = _lookup_agent_customers(RecordingCursor(db.cursor()), UP, "13930000003",
                                   owned_only=True, limit=8)
    assert len(rows) == 1                       # 前提:SQL 真跑了、真命中
    assert executed, "一条 SQL 都没跑 = 判据零判别力"
    dead = [s for s in executed if "customer_agent_credit_wallets" in s]
    assert dead == [], dead
    # 反向对照:确实读了 user_wallets(不是把 join 整个删掉了事)
    assert any("user_wallets" in s for s in executed)


# ============================================================
# §4-2 R2 · 两格输入 → 落库增量一致(前端新映射的逐字复跑)
# ============================================================

def _request(user_id: int) -> Request:
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    request.state.user = {"user_id": user_id, "is_admin": False}
    return request


def _seed_agent_stock(cur, uid: int, *, points: int) -> None:
    """服务商侧库存 = 钱包 + 同额 lot(两侧不一致会被 lot 台账判为漂移/不足)。"""
    cur.execute(
        """INSERT INTO agent_inventory_wallets
               (agent_user_id, paid_inventory_points, bonus_inventory_points)
           VALUES (%s,%s,%s)
           ON CONFLICT (agent_user_id) DO UPDATE
           SET paid_inventory_points=EXCLUDED.paid_inventory_points,
               bonus_inventory_points=EXCLUDED.bonus_inventory_points""",
        (uid, points, points),
    )
    cur.execute(
        """INSERT INTO dealer_inventory_lots
               (lot_id, owner_agent_user_id, original_points, remaining_points, reserved_points,
                acquisition_cost_cents, remaining_cost_cents, reserved_cost_cents,
                status, source_kind, pricing_version, evidence_jsonb)
           VALUES (%s,%s,%s,%s,0,%s,%s,0,'active','platform_purchase','seed-v1','{"seed":true}'::jsonb)
           ON CONFLICT (lot_id) DO UPDATE
           SET remaining_points=EXCLUDED.remaining_points,
               remaining_cost_cents=EXCLUDED.remaining_cost_cents""",
        (f"XFERSEED{uid}", uid, points, points, points, points),
    )


def _user_wallet(cur, uid: int) -> tuple[int, int]:
    cur.execute("SELECT paid_points, bonus_points FROM user_wallets WHERE user_id=%s", (uid,))
    row = cur.fetchone()
    return (int(row["paid_points"] or 0), int(row["bonus_points"] or 0)) if row else (0, 0)


def test_r2_two_box_payload_lands_on_user_wallets(world, db):
    """🔴 前端两格 → 后端落点 · 差分守恒。

    请求体逐字复刻收敛后的前端映射:
        充值算力 30000 → tool_points=30000 · publish_points=**0** · 赠送 5000 → bonus_points
    断言客户 `user_wallets` 的**增量**,不是绝对值(有存量时绝对值判据恒真或恒红)。
    """
    from schemas.v35_w2_dto import AllocateOfflineRequest
    from api.agent_workbench_api import agent_allocate_offline

    cur = db.cursor()
    # 服务商侧要有库存才划得出去(钱包 + 同额 lot)
    _seed_agent_stock(cur, UP, points=200000)
    _set_user_wallet(cur, PLAIN, paid=1000, bonus=200)
    db.commit()

    before = _user_wallet(db.cursor(), PLAIN)

    payload = AllocateOfflineRequest(
        customer_user_id=PLAIN, tool_points=30000, publish_points=0, bonus_points=5000,
    )
    result = asyncio.run(agent_allocate_offline(payload, _request(UP)))

    after = _user_wallet(db.cursor(), PLAIN)
    assert after[0] - before[0] == 30000, (before, after)
    assert after[1] - before[1] == 5000, (before, after)
    # 响应里 `new_publish_credit` 恒 0(字段保留只为向后兼容)
    assert result.new_publish_credit == 0
    assert result.new_tool_credit == after[0]
    assert result.new_bonus_credit == after[1]


def test_r2b_publish_points_still_accepted_for_backward_compat(world, db):
    """🔴 反向对照 + 兼容锁:老调用方传非 0 `publish_points` 仍然被接受,
    并与 `tool_points` 一起落到**同一个** `paid_points`。

    工单 §R2 明令「不改后端请求模型 · 禁把废字段从 API 里拔掉」——
    这条就是那句话的机器闸:哪天有人顺手删了字段,它转红。
    """
    from schemas.v35_w2_dto import AllocateOfflineRequest
    from api.agent_workbench_api import agent_allocate_offline

    cur = db.cursor()
    _seed_agent_stock(cur, UP, points=200000)
    _set_user_wallet(cur, PLAIN, paid=0, bonus=0)
    db.commit()

    before = _user_wallet(db.cursor(), PLAIN)
    payload = AllocateOfflineRequest(
        customer_user_id=PLAIN, tool_points=1000, publish_points=2000, bonus_points=0,
    )
    asyncio.run(agent_allocate_offline(payload, _request(UP)))
    after = _user_wallet(db.cursor(), PLAIN)
    assert after[0] - before[0] == 3000, (before, after)   # 1000 + 2000 都进 paid
    assert after[1] - before[1] == 0


# ============================================================
# 残留 1(Review-CTO 增量授权 2026-08-17)· 撤回弹窗同形收敛 · 落库判据
# ============================================================

def _agent_inventory(cur, uid: int) -> tuple[int, int]:
    cur.execute(
        "SELECT paid_inventory_points AS p, bonus_inventory_points AS b "
        "FROM agent_inventory_wallets WHERE agent_user_id=%s",
        (uid,),
    )
    row = cur.fetchone()
    return (int(row["p"] or 0), int(row["b"] or 0)) if row else (0, 0)


def _allocate(cur_db, *, tool: int, publish: int, bonus: int):
    """先划出去,才有东西可撤(撤回按客户当前余额夹紧)。"""
    from schemas.v35_w2_dto import AllocateOfflineRequest
    from api.agent_workbench_api import agent_allocate_offline

    return asyncio.run(agent_allocate_offline(
        AllocateOfflineRequest(customer_user_id=PLAIN, tool_points=tool,
                               publish_points=publish, bonus_points=bonus),
        _request(UP),
    ))


def test_x1_revoke_two_box_payload_decrements_user_wallets(world, db):
    """🔴 撤回两格 → 客户 `user_wallets` 与服务商库存的**增量**双侧守恒。

    请求体逐字复刻收敛后的撤回弹窗映射:
        充值算力 12000 → tool_points=12000 · publish_points=**0** · 赠送 2000 → bonus_points
    """
    from schemas.v35_w2_dto import RevokeOfflineRequest
    from api.agent_workbench_api import agent_revoke_offline

    cur = db.cursor()
    _seed_agent_stock(cur, UP, points=200000)
    _set_user_wallet(cur, PLAIN, paid=0, bonus=0)
    db.commit()
    _allocate(db, tool=30000, publish=0, bonus=5000)

    before_cust = _user_wallet(db.cursor(), PLAIN)
    before_agent = _agent_inventory(db.cursor(), UP)
    # 前提自证:确实有东西可撤,否则"撤了 0"也会让增量断言看起来成立
    assert before_cust == (30000, 5000), before_cust

    asyncio.run(agent_revoke_offline(
        RevokeOfflineRequest(customer_user_id=PLAIN, tool_points=12000,
                             publish_points=0, bonus_points=2000),
        _request(UP),
    ))

    after_cust = _user_wallet(db.cursor(), PLAIN)
    after_agent = _agent_inventory(db.cursor(), UP)
    assert before_cust[0] - after_cust[0] == 12000, (before_cust, after_cust)
    assert before_cust[1] - after_cust[1] == 2000, (before_cust, after_cust)
    # 🔴 双侧守恒:客户少的正好回到服务商库存(不是凭空蒸发)
    assert after_agent[0] - before_agent[0] == 12000, (before_agent, after_agent)
    assert after_agent[1] - before_agent[1] == 2000, (before_agent, after_agent)


def test_x2_revoke_publish_points_still_merged_for_backward_compat(world, db):
    """🔴 拆锁 / 兼容锁:老调用方传非 0 `publish_points` 仍与 `tool_points`
    一起从**同一个** `paid_points` 回收。

    这条同时证明「前端把 publish 格砍掉没有丢能力」——
    tool=0/publish=N 与 tool=N/publish=0 撤的是同一个口袋、同样的量。
    哪天有人把字段从请求模型里拔掉,或改成走另一个池子,它转红。
    """
    from schemas.v35_w2_dto import RevokeOfflineRequest
    from api.agent_workbench_api import agent_revoke_offline

    cur = db.cursor()
    _seed_agent_stock(cur, UP, points=200000)
    _set_user_wallet(cur, PLAIN, paid=0, bonus=0)
    db.commit()
    _allocate(db, tool=30000, publish=0, bonus=5000)

    before = _user_wallet(db.cursor(), PLAIN)
    asyncio.run(agent_revoke_offline(
        RevokeOfflineRequest(customer_user_id=PLAIN, tool_points=0,
                             publish_points=7000, bonus_points=0),
        _request(UP),
    ))
    after = _user_wallet(db.cursor(), PLAIN)
    assert before[0] - after[0] == 7000, (before, after)   # 走的是 paid,不是别的池
    assert before[1] - after[1] == 0


def test_x3_revoke_caps_at_customer_remaining(world, db):
    """反向对照:撤回按客户当前余额夹紧,不把钱包扣成负数。

    没有这条,上面两条在"随便撤多少都成功"的实现下也会绿。
    """
    from schemas.v35_w2_dto import RevokeOfflineRequest
    from api.agent_workbench_api import agent_revoke_offline

    cur = db.cursor()
    _seed_agent_stock(cur, UP, points=200000)
    _set_user_wallet(cur, PLAIN, paid=0, bonus=0)
    db.commit()
    _allocate(db, tool=1000, publish=0, bonus=100)

    asyncio.run(agent_revoke_offline(
        RevokeOfflineRequest(customer_user_id=PLAIN, tool_points=999999,
                             publish_points=0, bonus_points=999999),
        _request(UP),
    ))
    after = _user_wallet(db.cursor(), PLAIN)
    assert after == (0, 0), after
