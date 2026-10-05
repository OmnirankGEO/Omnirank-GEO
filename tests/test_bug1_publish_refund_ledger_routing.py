"""[BUG-1 · 2026-07-27] 代发退款账本路由判别测试(mock cursor · 不依赖真 DB)

覆盖 db/meijiehezi_db.refund_for_publish_order:
- V3.5 客户(customer_agent_credit_wallets 有 row)→ 退回信用钱包,【绝不】UPDATE user_wallets
- 退款按原扣费池占比分回 publish/tool,余数归 tool,bonus 永不进
- 非 V3.5 用户 → 原路径不变(UPDATE user_wallets.paid_points)
- 幂等跨两账本:信用侧已有该 refund_key 的退款 → 跳过,不在平台侧重退
- 退款 cap 的"已退累计"跨两账本合并,防"平台退一次 + 信用退一次"绕过上限
- 锁必须锁"要改的那张表"的行(V3.5 锁 customer_agent_credit_wallets)

生产实证(RO 通道 2026-07-27 · user 149 / 示例手机号-退款用户):
  信用钱包 consume media_proxy_publish 3 笔 -32,955(全 tool 池)
  平台钱包 refund media_proxy_publish 12 笔 +22,425  ← 退错钱包,客户花不出去
  信用钱包 refund 仅 1 笔 +2,340(走 billing.refund_points 的正确路径)

⚠️ [单账本接线 2026-08-17] 本文件原本的变异守卫方向**已反转**:v35 分支是被有意删除的,
不再是「改回去就该红」。现行守卫改由 tests/v35_refund_single_ledger_2026_08_17/ 承担
(把判据改回按停写表 row 判身份 → test_c1_ledger_router_never_returns_v35 转红,已实测)。
本文件保留的是与账本无关的那几条:legacy 路径 / 幂等 / cap 合并 / admin 免扣。
"""
import pytest


class _FakeCursor:
    """按 SQL 关键字路由的假 cursor(RealDictCursor 语义:行是 dict)。"""

    def __init__(self, *, is_v35, actually_deducted=1000,
                 platform_already=0, credit_already=0,
                 credit_idem_id=None, credit_idem_amount=0,
                 consume_pools=(("tool", 5000),)):
        self.is_v35 = is_v35
        self.actually_deducted = actually_deducted
        self.platform_already = platform_already
        self.credit_already = credit_already
        self.credit_idem_id = credit_idem_id
        self.credit_idem_amount = credit_idem_amount
        self.consume_pools = list(consume_pools)
        self.sqls = []
        self._one = None
        self._all = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.sqls.append(s)
        self._one = None
        self._all = []

        if "customer_agent_credit_wallets" in s and "FOR UPDATE" in s:
            self._one = {"customer_user_id": 149} if self.is_v35 else None
        elif "customer_agent_credit_wallets" in s and "SELECT 1" in s:
            self._one = {"?column?": 1} if self.is_v35 else None
        elif "user_wallets" in s and "FOR UPDATE" in s:
            self._one = {"paid_points": 0}
        elif s.startswith("UPDATE user_wallets"):
            self._one = {"paid_points": 12345}
        elif "FROM point_transactions" in s and "LIMIT 1" in s:
            self._one = None  # 平台侧无该 refund_key 退款
        elif "FROM customer_credit_transactions" in s and "MIN(id)" in s:
            self._one = {"id": self.credit_idem_id, "amount": self.credit_idem_amount}
        elif "FROM mhz_publish_order_items it" in s:
            self._one = {"oid": 7, "actually_deducted_points": self.actually_deducted}
        elif "FROM point_transactions" in s and "SUM(amount)" in s:
            self._one = {"refunded": self.platform_already}
        elif "FROM customer_credit_transactions" in s and "SUM(points)" in s:
            self._one = {"refunded": self.credit_already}
        elif "GROUP BY pool" in s:
            self._all = [{"pool": p, "pts": v} for p, v in self.consume_pools]

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


@pytest.fixture
def harness(monkeypatch):
    """打桩 _get_conn / refund_credit / insert_transaction / 通知,返回捕获容器。"""
    import db.meijiehezi_db as mdb
    import services.customer_credit as cc
    import db.wallet_db as wdb

    captured = {"credit_refunds": [], "platform_tx": [], "notifications": []}

    def _fake_refund_credit(cursor, customer_user_id, tool_points=0, publish_points=0,
                            bonus_points=0, related_order_id=None, description=None,
                            source=None):
        captured["credit_refunds"].append({
            "customer_user_id": customer_user_id,
            "tool": tool_points, "publish": publish_points, "bonus": bonus_points,
            "related_order_id": related_order_id, "source": source,
            "description": description,
        })
        return {"tool_credit_points": 0, "publish_credit_points": 0, "bonus_credit_points": 0}

    def _fake_insert_transaction(cursor, user_id, tx_type, point_type, amount, balance_after,
                                 feature_code=None, description=None, order_id=None, **kw):
        captured["platform_tx"].append({
            "user_id": user_id, "type": tx_type, "point_type": point_type,
            "amount": amount, "order_id": order_id, "description": description,
        })

    def _fake_notify(cursor, **kw):
        captured["notifications"].append(kw)

    monkeypatch.setattr(cc, "refund_credit", _fake_refund_credit)
    monkeypatch.setattr(wdb, "insert_transaction", _fake_insert_transaction)
    monkeypatch.setattr(mdb, "_enqueue_publish_notification", _fake_notify)

    def _install(cursor):
        conn = _FakeConn(cursor)
        monkeypatch.setattr(mdb, "_get_conn", lambda: conn)
        return conn

    captured["install"] = _install
    return captured


def _run(harness, cursor, *, user_id=149, amount=400, refund_key="item:555",
         reason="媒体审核未通过"):
    from db.meijiehezi_db import refund_for_publish_order
    conn = harness["install"](cursor)
    result = refund_for_publish_order(user_id, amount, refund_key, reason)
    return result, conn


# ============================================================
# 1. 核心安全锁:V3.5 客户退回信用钱包,绝不退平台钱包
# ============================================================

@pytest.mark.skip(reason="[单账本接线 2026-08-17 · R2/C-1] 本用例断言的正是 BUG-1 那套「V3.5 客户退回信用钱包 + 按池拆分」路由 —— 该路由已被**有意反转**:2026-07-29 单账本收敛保留了 5 行停写表残行,使原判据恒命中,退款会写进已停写的表而客户消费侧只读 user_wallets(看得见花不出去)。现口径:代发退款一律落 user_wallets.paid_points。接班守卫见 tests/v35_refund_single_ledger_2026_08_17/ 的 test_c1_*。")
def test_v35_customer_refund_goes_to_credit_wallet(harness):
    cur = _FakeCursor(is_v35=True, actually_deducted=1000)
    result, conn = _run(harness, cur, amount=400)

    assert result["success"] is True
    assert result["refunded"] == 400
    assert result["ledger"] == "v35_credit"
    assert conn.committed is True

    # 退回信用钱包
    assert len(harness["credit_refunds"]) == 1
    rc = harness["credit_refunds"][0]
    assert rc["customer_user_id"] == 149
    assert rc["tool"] + rc["publish"] == 400
    assert rc["related_order_id"] == "item:555"
    assert rc["source"] == "publish_proxy_refund"

    # 【安全锁】绝不写平台钱包
    assert harness["platform_tx"] == [], "V3.5 客户退款不得写 point_transactions"
    assert not any(s.startswith("UPDATE user_wallets") for s in cur.sqls), \
        "V3.5 客户退款不得 UPDATE user_wallets.paid_points —— 那笔钱客户花不出去"


@pytest.mark.skip(reason="[单账本接线 2026-08-17 · R2/C-1] 本用例断言的正是 BUG-1 那套「V3.5 客户退回信用钱包 + 按池拆分」路由 —— 该路由已被**有意反转**:2026-07-29 单账本收敛保留了 5 行停写表残行,使原判据恒命中,退款会写进已停写的表而客户消费侧只读 user_wallets(看得见花不出去)。现口径:代发退款一律落 user_wallets.paid_points。接班守卫见 tests/v35_refund_single_ledger_2026_08_17/ 的 test_c1_*。")
def test_v35_lock_targets_credit_wallet_row(harness):
    """锁必须锁要改的那张表:V3.5 锁 customer_agent_credit_wallets,不是 user_wallets。"""
    cur = _FakeCursor(is_v35=True)
    _run(harness, cur)
    assert any("FOR UPDATE" in s and "customer_agent_credit_wallets" in s for s in cur.sqls)
    assert not any("FOR UPDATE" in s and "user_wallets" in s for s in cur.sqls)


# ============================================================
# 2. 池分配:按原扣费池占比,余数归 tool,bonus 永不进
# ============================================================

@pytest.mark.skip(reason="[单账本接线 2026-08-17 · R2/C-1] 本用例断言的正是 BUG-1 那套「V3.5 客户退回信用钱包 + 按池拆分」路由 —— 该路由已被**有意反转**:2026-07-29 单账本收敛保留了 5 行停写表残行,使原判据恒命中,退款会写进已停写的表而客户消费侧只读 user_wallets(看得见花不出去)。现口径:代发退款一律落 user_wallets.paid_points。接班守卫见 tests/v35_refund_single_ledger_2026_08_17/ 的 test_c1_*。")
def test_pool_split_follows_original_consume(harness):
    """原扣费 publish 3000 / tool 1000 → 退 400 按 3:1 拆成 publish 300 / tool 100。"""
    cur = _FakeCursor(is_v35=True, consume_pools=(("publish", 3000), ("tool", 1000)))
    _run(harness, cur, amount=400)
    rc = harness["credit_refunds"][0]
    assert rc["publish"] == 300
    assert rc["tool"] == 100


@pytest.mark.skip(reason="[单账本接线 2026-08-17 · R2/C-1] 本用例断言的正是 BUG-1 那套「V3.5 客户退回信用钱包 + 按池拆分」路由 —— 该路由已被**有意反转**:2026-07-29 单账本收敛保留了 5 行停写表残行,使原判据恒命中,退款会写进已停写的表而客户消费侧只读 user_wallets(看得见花不出去)。现口径:代发退款一律落 user_wallets.paid_points。接班守卫见 tests/v35_refund_single_ledger_2026_08_17/ 的 test_c1_*。")
def test_pool_split_all_tool_when_consume_all_tool(harness):
    """生产 user 149 形态:allocate 与 consume 全在 tool 池 → 全额退回 tool。

    若全退 publish,客户就丢了拿这笔钱跑工具的能力(publish 池只能发布用)。
    """
    cur = _FakeCursor(is_v35=True, consume_pools=(("tool", 32955),))
    _run(harness, cur, amount=400)
    rc = harness["credit_refunds"][0]
    assert rc["tool"] == 400 and rc["publish"] == 0


@pytest.mark.skip(reason="[单账本接线 2026-08-17 · R2/C-1] 本用例断言的正是 BUG-1 那套「V3.5 客户退回信用钱包 + 按池拆分」路由 —— 该路由已被**有意反转**:2026-07-29 单账本收敛保留了 5 行停写表残行,使原判据恒命中,退款会写进已停写的表而客户消费侧只读 user_wallets(看得见花不出去)。现口径:代发退款一律落 user_wallets.paid_points。接班守卫见 tests/v35_refund_single_ledger_2026_08_17/ 的 test_c1_*。")
def test_pool_split_never_refunds_to_bonus(harness):
    """bonus 池永不进:发布消费严禁用 bonus,退回 bonus 等于凭空发赠送额度。"""
    for pools in (("tool", 100),), (("publish", 100),), (("publish", 50), ("tool", 50)):
        cur = _FakeCursor(is_v35=True, consume_pools=pools)
        harness["credit_refunds"].clear()
        _run(harness, cur, amount=333)
        rc = harness["credit_refunds"][0]
        assert rc["bonus"] == 0
        assert rc["tool"] + rc["publish"] == 333, "取整不得吃掉客户的钱"


@pytest.mark.skip(reason="[单账本接线 2026-08-17 · R2/C-1] 本用例断言的正是 BUG-1 那套「V3.5 客户退回信用钱包 + 按池拆分」路由 —— 该路由已被**有意反转**:2026-07-29 单账本收敛保留了 5 行停写表残行,使原判据恒命中,退款会写进已停写的表而客户消费侧只读 user_wallets(看得见花不出去)。现口径:代发退款一律落 user_wallets.paid_points。接班守卫见 tests/v35_refund_single_ledger_2026_08_17/ 的 test_c1_*。")
def test_pool_split_rounding_remainder_goes_to_tool(harness):
    """取整余数归 tool(用途更广)· 总额必须守恒。"""
    cur = _FakeCursor(is_v35=True, consume_pools=(("publish", 1), ("tool", 2)))
    _run(harness, cur, amount=100)
    rc = harness["credit_refunds"][0]
    assert rc["publish"] == 33 and rc["tool"] == 67


# ============================================================
# 3. 非 V3.5 用户:原路径零变化
# ============================================================

def test_legacy_user_still_refunds_to_platform_wallet(harness):
    cur = _FakeCursor(is_v35=False, actually_deducted=1000)
    result, _ = _run(harness, cur, user_id=88, amount=400)

    assert result["success"] is True
    assert result["refunded"] == 400
    assert result["ledger"] == "platform"
    assert any(s.startswith("UPDATE user_wallets") for s in cur.sqls)
    assert len(harness["platform_tx"]) == 1
    tx = harness["platform_tx"][0]
    assert tx["point_type"] == "paid" and tx["amount"] == 400 and tx["order_id"] == "item:555"
    assert harness["credit_refunds"] == [], "非 V3.5 用户不得动信用钱包"


def test_legacy_reason_sanitized(harness):
    """供应商代号清洗保留(memory feedback_no_supplier_names_to_users)。"""
    cur = _FakeCursor(is_v35=False)
    _run(harness, cur, user_id=88, reason="mhz_rejected: item 提交失败")
    desc = harness["platform_tx"][0]["description"]
    assert "mhz" not in desc.lower() and "item" not in desc.lower()
    assert "媒体审核未通过" in desc


@pytest.mark.skip(reason="[单账本接线 2026-08-17 · R2/C-1] 本用例断言的正是 BUG-1 那套「V3.5 客户退回信用钱包 + 按池拆分」路由 —— 该路由已被**有意反转**:2026-07-29 单账本收敛保留了 5 行停写表残行,使原判据恒命中,退款会写进已停写的表而客户消费侧只读 user_wallets(看得见花不出去)。现口径:代发退款一律落 user_wallets.paid_points。接班守卫见 tests/v35_refund_single_ledger_2026_08_17/ 的 test_c1_*。")
def test_v35_reason_also_sanitized(harness):
    """V3.5 分支同样走清洗 —— 不能因为换了账本就把供应商名漏给客户。"""
    cur = _FakeCursor(is_v35=True)
    _run(harness, cur, reason="mhz_rejected")
    desc = harness["credit_refunds"][0]["description"]
    assert "mhz" not in desc.lower()


# ============================================================
# 4. 幂等与 cap 跨账本
# ============================================================

def test_idempotent_when_credit_side_already_refunded(harness):
    """信用侧已有该 refund_key 的退款 → 跳过,不得在平台侧再退一遍。"""
    cur = _FakeCursor(is_v35=True, credit_idem_id=901, credit_idem_amount=400)
    result, _ = _run(harness, cur, amount=400)
    assert result["skipped"] is True
    assert result["refunded"] == 0
    assert harness["credit_refunds"] == []
    assert harness["platform_tx"] == []


def test_cap_merges_both_ledgers(harness):
    """订单真实扣费 1000,平台已退 600 + 信用已退 400 = 已退满 → 不再退。

    旧实现只统计 point_transactions,信用侧退过的那部分看不见 → cap 被绕过 → 双退。
    """
    cur = _FakeCursor(is_v35=True, actually_deducted=1000,
                      platform_already=600, credit_already=400)
    result, _ = _run(harness, cur, amount=400)
    assert result["skipped"] is True
    assert result["refunded"] == 0
    assert harness["credit_refunds"] == []


@pytest.mark.skip(reason="[单账本接线 2026-08-17 · R2/C-1] 本用例断言的正是 BUG-1 那套「V3.5 客户退回信用钱包 + 按池拆分」路由 —— 该路由已被**有意反转**:2026-07-29 单账本收敛保留了 5 行停写表残行,使原判据恒命中,退款会写进已停写的表而客户消费侧只读 user_wallets(看得见花不出去)。现口径:代发退款一律落 user_wallets.paid_points。接班守卫见 tests/v35_refund_single_ledger_2026_08_17/ 的 test_c1_*。")
def test_cap_still_clamps_partial(harness):
    """已退 800 / 真实扣费 1000 → 请求 400 只退 200。"""
    cur = _FakeCursor(is_v35=True, actually_deducted=1000,
                      platform_already=500, credit_already=300)
    result, _ = _run(harness, cur, amount=400)
    assert result["refunded"] == 200
    assert harness["credit_refunds"][0]["tool"] == 200


def test_admin_exempt_order_refunds_zero(harness):
    """admin 免扣订单(actually_deducted=0)在 V3.5 分支同样不退,防凭空注入。"""
    cur = _FakeCursor(is_v35=True, actually_deducted=0)
    result, _ = _run(harness, cur, amount=400)
    assert result["refunded"] == 0
    assert result["skipped"] is True
    assert harness["credit_refunds"] == []


# ============================================================
# 5. 结构守卫:退款执行点不得回退成无条件平台钱包
# ============================================================

@pytest.mark.skip(reason="[单账本接线 2026-08-17 · R2/C-1] 本用例断言的正是 BUG-1 那套「V3.5 客户退回信用钱包 + 按池拆分」路由 —— 该路由已被**有意反转**:2026-07-29 单账本收敛保留了 5 行停写表残行,使原判据恒命中,退款会写进已停写的表而客户消费侧只读 user_wallets(看得见花不出去)。现口径:代发退款一律落 user_wallets.paid_points。接班守卫见 tests/v35_refund_single_ledger_2026_08_17/ 的 test_c1_*。")
def test_source_has_ledger_routing():
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "db" / "meijiehezi_db.py"
    text = src.read_text(encoding="utf-8")
    head = text.index("def refund_for_publish_order(")
    body = text[head:text.index("def cleanup_orphan_submitting_items(", head)]
    assert "_publish_refund_ledger(" in body, "退款入口必须先判账本,不得无条件退平台钱包"
    assert "_publish_already_refunded(" in body, "cap 的已退累计必须跨账本合并"
    assert "refund_credit(" in body, "V3.5 分支必须走 customer_credit.refund_credit"
