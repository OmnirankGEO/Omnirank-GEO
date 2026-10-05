"""工单 §4.1 关系路由测试(对应 R1–R5)。

编号与工单 §4.1 一一对应,方便复审逐条核对。
🔴 第 4、5、8、9 条是本包的四条硬锁,单独成组。
"""

from __future__ import annotations

import pytest

from services.channel_partner_requests import (
    ChannelPartnerRequestError,
    NoRelationError,
    quote_downstream_purchase,
    resolve_bound_cost_multiplier_bps,
    resolve_relationship,
)
from services.relationship_privacy import find_private_relationship_fields

from .conftest import TEST_ID_BASE, make_binding, make_channel, make_user

UP = TEST_ID_BASE + 1        # 上游服务商(相当于生产 u46)
DOWN_PARTNER = TEST_ID_BASE + 2   # 下线服务商(相当于 u18) · 仅渠道关系
CUSTOMER = TEST_ID_BASE + 3       # 普通客户 · 仅客户绑定
BOTH = TEST_ID_BASE + 4           # 两种关系都有 + 服务商(= 存量 binding 67 形态)
STRANGER = TEST_ID_BASE + 5       # 陌生服务商(与 UP 无任何关系)
GHOST = TEST_ID_BASE + 99         # 从不创建的账号("不存在")

BOUND_BPS = 13000          # 与生产 relation id=3 同值(u18←u46)
DEFAULT_BPS = 10000        # "回落默认值" —— 计价锁要钉死不能取到它


@pytest.fixture
def world(db):
    cur = db.cursor()
    make_user(cur, UP, phone="13900000001", name="上游服务商", agent_level=2)
    make_user(cur, DOWN_PARTNER, phone="13900000002", name="下线服务商", agent_level=1)
    make_user(cur, CUSTOMER, phone="13900000003", name="普通客户", agent_level=0)
    make_user(cur, BOTH, phone="13900000004", name="下线兼客户", agent_level=1)
    make_user(cur, STRANGER, phone="13900000005", name="陌生服务商", agent_level=1)

    make_channel(cur, buyer_dealer_id=DOWN_PARTNER, upstream_user_id=UP, bps=BOUND_BPS)
    make_binding(cur, customer_user_id=CUSTOMER, agent_user_id=UP)
    make_binding(cur, customer_user_id=BOTH, agent_user_id=UP)
    make_channel(cur, buyer_dealer_id=BOTH, upstream_user_id=UP, bps=12000)
    db.commit()
    return cur


# ============================================================
# §4.1-1 普通客户 + 客户绑定 → 搜得到、可划拨、进可用钱包
# ============================================================

def test_01_ordinary_customer_routes_to_available_wallet(world):
    rel = resolve_relationship(world, UP, CUSTOMER)
    assert rel["relation"] == "customer"
    assert rel["target_identity"] == "level0"
    assert rel["ledger_note"] == "available_wallet"
    assert rel["primary_action"]["action"] == "allocate_customer"


# ============================================================
# §4.1-2 🔴 下线服务商 + 渠道关系(无客户绑定)→ 搜索必须命中(改造前的缺陷)
# ============================================================

def test_02_downstream_partner_is_resolvable(world):
    rel = resolve_relationship(world, UP, DOWN_PARTNER)
    assert rel["relation"] == "downstream_partner"
    assert rel["target_identity"] == "service_provider"
    assert rel["has_channel_relationship"] is True
    assert rel["ledger_note"] == "inventory_wallet"
    assert "下线服务商" in rel["headline"]


def test_02b_lookup_sql_finds_downstream_by_phone(world, db):
    """🔴 Owner 亲口指出的那条:上游按手机号搜自己的下线,必须搜得到。

    生产实证(2026-08-13 只读取证):改造前 u46 搜 13800138000 命中 **0**。
    这里用同一段 `_lookup_agent_customers` 逐字复跑。
    """
    from api.agent_workbench_api import _lookup_agent_customers

    rows = _lookup_agent_customers(db.cursor(), UP, "13900000002", owned_only=True, limit=8)
    assert [r["customer_user_id"] for r in rows] == [DOWN_PARTNER]
    assert rows[0]["binding_status"] == "downstream_partner"
    # [P0 热修 2026-08-13] 原断言主动作 route == "/agent/channel-partners",
    # 那锁的是「找得到但不许操作」的旧行为 —— Owner 生产实测被挡,已推翻。
    # 现在主动作必须是**能执行**的供货;"查看渠道关系"降为次动作但仍在。
    assert rows[0]["primary_action"]["action"] == "supply_downstream"
    assert any(a.get("route") == "/agent/channel-partners"
               for a in rows[0]["secondary_actions"])


def test_02c_reverse_control_search_still_finds_plain_customer(world, db):
    """反向对照:加了渠道分支后,原来的普通客户搜索不能被搞坏。"""
    from api.agent_workbench_api import _lookup_agent_customers

    rows = _lookup_agent_customers(db.cursor(), UP, "13900000003", owned_only=True, limit=8)
    assert [r["customer_user_id"] for r in rows] == [CUSTOMER]
    assert rows[0]["binding_status"] == "owned"


# ============================================================
# §4.1-3 🔴 两种关系都有(= 存量 5 条形态)→ 默认走渠道账本
# ============================================================

def test_03_both_relations_default_to_channel_ledger(world):
    rel = resolve_relationship(world, UP, BOTH)
    assert rel["relation"] == "both"
    assert rel["has_customer_binding"] and rel["has_channel_relationship"]
    # [P0 热修] 主动作从 view_channel 改为**能执行的供货**;按客户划拨仍是次动作。
    assert rel["primary_action"]["action"] == "supply_downstream"
    assert "allocate_customer" in rel["allowed_actions"]
    assert rel["ledger_note"] == "inventory_wallet"
    # R1:身份升级不使关系失效 —— 绑定仍在,人仍搜得到
    assert rel["target_display_name"] == "下线兼客户"


# ============================================================
# §4.1-4 🔴 计价锁:必须取**已绑定**值,回落默认值必须转红
# ============================================================

def test_04_pricing_lock_uses_bound_multiplier(world):
    assert resolve_bound_cost_multiplier_bps(world, UP, DOWN_PARTNER) == BOUND_BPS
    quote = quote_downstream_purchase(world, UP, DOWN_PARTNER, 10_000)
    # 10000 分 × 13000/10000 = 13000 分。回落默认 10000bps 会得 10000 → 本断言转红。
    assert quote["price_cents"] == 13000
    assert quote["price_cents"] != 10_000, "回落默认系数 = 计价锁失效"


def test_04b_pricing_follows_the_bound_value_when_admin_changes_it(world, db):
    """把已绑定系数改成另一个非默认值,价格必须跟着变(证明不是写死的常数)。"""
    world.execute(
        "UPDATE channel_pricing_relationships SET cost_multiplier_bps=%s "
        "WHERE buyer_dealer_id=%s AND upstream_channel_account_id=%s",
        (11700, DOWN_PARTNER, UP),
    )
    db.commit()
    assert quote_downstream_purchase(world, UP, DOWN_PARTNER, 10_000)["price_cents"] == 11700


def test_04c_no_bound_relationship_raises_instead_of_defaulting(world):
    """没有已绑定关系时必须**抛错**,不得静默回落默认系数(R3)。"""
    with pytest.raises(ChannelPartnerRequestError) as exc:
        resolve_bound_cost_multiplier_bps(world, UP, CUSTOMER)
    assert exc.value.code == "NO_BOUND_CHANNEL_RELATIONSHIP"


def test_04d_bound_multiplier_never_appears_in_any_response(world):
    """已绑定系数只许在后端流转 —— 任何面向非 admin 的返回体里都不许出现(R5)。"""
    for target in (DOWN_PARTNER, BOTH, CUSTOMER):
        assert find_private_relationship_fields(resolve_relationship(world, UP, target)) == []
    assert find_private_relationship_fields(
        quote_downstream_purchase(world, UP, DOWN_PARTNER, 10_000)
    ) == []


# ============================================================
# §4.1-5 🔴 关系表零变化:运行时任何路径都不写关系表(R2)
# ============================================================

def _relation_snapshot(cur):
    cur.execute(
        "SELECT id, buyer_dealer_id, upstream_channel_account_id, cost_multiplier_bps, "
        "status, relationship_version FROM channel_pricing_relationships ORDER BY id"
    )
    channel = [dict(r) for r in cur.fetchall()]
    cur.execute(
        "SELECT id, customer_user_id, agent_user_id, binding_source, dispute_status "
        "FROM customer_agent_bindings ORDER BY id"
    )
    binding = [dict(r) for r in cur.fetchall()]
    return channel, binding


def test_05_runtime_paths_never_write_relationship_tables(world, db):
    """跑完全部关系解析/搜索/计价路径后,两张关系表**逐行逐字段**零变化。

    🔴 判据是**差分**,不是"必须为 0 行" —— 后者在有存量数据时恒真或恒红,
       两种都等于没有判据(本仓 lot_drift 踩过)。
    """
    from api.agent_workbench_api import _lookup_agent_customers

    before = _relation_snapshot(world)

    for target in (CUSTOMER, DOWN_PARTNER, BOTH, STRANGER, GHOST):
        try:
            resolve_relationship(world, UP, target)
        except NoRelationError:
            pass
    for kw in ("13900000002", "13900000003", "13900000004", "13900000005", "不存在的名字"):
        _lookup_agent_customers(db.cursor(), UP, kw, owned_only=True, limit=8)
    quote_downstream_purchase(world, UP, DOWN_PARTNER, 10_000)
    db.commit()

    assert _relation_snapshot(world) == before


# ============================================================
# §4.1-6 目标在提交前升为服务商 → 后端重新复核并原位改道
# ============================================================

def test_06_identity_is_reread_every_time(world, db):
    """§P0-1 第 5 条:身份每次重新读,不吃"有 binding 就当普通客户"的旧分支。"""
    assert resolve_relationship(world, UP, CUSTOMER)["target_identity"] == "level0"
    world.execute("UPDATE user_wallets SET agent_level=1 WHERE user_id=%s", (CUSTOMER,))
    db.commit()
    rel = resolve_relationship(world, UP, CUSTOMER)
    assert rel["target_identity"] == "service_provider"
    # R1:关系仍在、人仍搜得到;但主动作改成建渠道关系,按客户划拨降为次动作并写明后果
    assert rel["relation"] == "customer"
    # [P0 热修] 这一支只有「划拨给客户」可执行(没有渠道关系就没有已绑定进货价,
    # 供货无从计价),那它就该是主按钮 —— 不再把唯一能执行的动作降为次动作。
    assert rel["primary_action"]["action"] == "allocate_customer"
    assert "allocate_customer" in rel["allowed_actions"]
    assert "不能再向下分销" in rel["effect_note"]


# ============================================================
# §4.1-7 关系读取失败 ≠ 没有关系
# ============================================================

def test_07_read_failure_is_not_reported_as_no_relation(world):
    """DB 报错必须往上抛(前端显示"暂时无法读取"+重试),不得被吞成 NoRelation。"""
    class BoomCursor:
        def execute(self, *a, **k):
            raise RuntimeError("connection reset")

    with pytest.raises(RuntimeError):
        resolve_relationship(BoomCursor(), UP, DOWN_PARTNER)


# ============================================================
# §4.1-8 🔴 不可区分性:陌生账号 vs 不存在账号
# ============================================================

def _no_relation_payload(cur, actor, target):
    try:
        resolve_relationship(cur, actor, target)
    except ChannelPartnerRequestError as exc:
        return {"code": exc.code, "message": str(exc), "details": exc.details}
    raise AssertionError(f"target {target} 不该有关系")


def test_08_stranger_and_nonexistent_are_byte_identical(world):
    stranger = _no_relation_payload(world, UP, STRANGER)
    ghost = _no_relation_payload(world, UP, GHOST)
    assert stranger == ghost
    assert stranger == {"code": "TARGET_NOT_FOUND", "message": "未找到该账号", "details": {}}
    # 反向对照:有关系的目标**不**走这条路径 —— 否则本测试恒真、等于没判据
    assert resolve_relationship(world, UP, DOWN_PARTNER)["relation"] == "downstream_partner"


def test_08b_lookup_returns_same_empty_for_stranger_and_ghost(world, db):
    from api.agent_workbench_api import _lookup_agent_customers

    assert _lookup_agent_customers(db.cursor(), UP, "13900000005", owned_only=True, limit=8) == []
    assert _lookup_agent_customers(db.cursor(), UP, "13900009999", owned_only=True, limit=8) == []


# ============================================================
# §4.1-9 🔴🔴 R5 有向性(Owner 红线)· 独立成组
# ============================================================

def test_09_downstream_searching_upstream_is_indistinguishable_from_nonexistent(world):
    """🔴 下级拿上级的 user_id 查 → 必须与"账号不存在"逐字节同构。

    DOWN_PARTNER 是 UP 的下线。反过来 DOWN_PARTNER 查 UP:
    关系**客观存在**,但方向是向上 —— 必须退化成"无任何关系"。
    """
    upward = _no_relation_payload(world, DOWN_PARTNER, UP)
    ghost = _no_relation_payload(world, DOWN_PARTNER, GHOST)
    assert upward == ghost
    assert upward == {"code": "TARGET_NOT_FOUND", "message": "未找到该账号", "details": {}}


def test_09b_downstream_searching_upstream_by_phone_finds_nothing(world, db):
    """🔴 换手机号入口同样必须查不到 —— 泄露位常常藏在"另一个入口"。"""
    from api.agent_workbench_api import _lookup_agent_customers

    assert _lookup_agent_customers(db.cursor(), DOWN_PARTNER, "13900000001", owned_only=True, limit=8) == []
    assert _lookup_agent_customers(db.cursor(), BOTH, "13900000001", owned_only=True, limit=8) == []
    # 反向对照:同一个 UP 从**上往下**看是看得见的 —— 证明上面的空不是因为整个搜索坏了
    assert len(_lookup_agent_customers(db.cursor(), UP, "13900000002", owned_only=True, limit=8)) == 1


def test_09c_no_upstream_enum_value_exists_anywhere(world):
    """🔴 「多一个枚举值就是一个泄露位」:relation 永远不许出现 upstream 类取值。"""
    seen = set()
    for actor, target in (
        (UP, CUSTOMER), (UP, DOWN_PARTNER), (UP, BOTH),
    ):
        seen.add(resolve_relationship(world, actor, target)["relation"])
    assert seen <= {"customer", "downstream_partner", "both"}
    assert not any("upstream" in v for v in seen)


def test_09d_mechanical_scan_all_non_admin_payloads(world):
    """🔴 机械扫描,不靠人眼:本包所有面向非 admin 的返回体零私有字段。"""
    payloads = [resolve_relationship(world, UP, t) for t in (CUSTOMER, DOWN_PARTNER, BOTH)]
    payloads.append(quote_downstream_purchase(world, UP, DOWN_PARTNER, 10_000))
    for payload in payloads:
        assert find_private_relationship_fields(payload) == [], payload


def test_09e_scanner_itself_is_not_vacuous():
    """🔴 反向对照:扫描器必须真能抓到东西 —— 否则第 9d 条是恒真的假绿。

    (本仓踩过「锁全绿是因为夹具是空的」「归并用『或』并特征 → 恒真」。)
    """
    dirty = {"ok": 1, "items": [{"cost_multiplier_bps": 13000}], "nested": {"upstream_user_id": 46}}
    hits = find_private_relationship_fields(dirty)
    assert "items[0].cost_multiplier_bps" in hits
    assert "nested.upstream_user_id" in hits
    assert "relationship_id" in find_private_relationship_fields({"relationship_id": 3})
