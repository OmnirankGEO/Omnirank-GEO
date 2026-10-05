"""[V3.5 v7 批2B · 2026-06-08] V3.5 客户长任务冻结链单测(mock · 不依赖真 DB)

覆盖:
- customer_credit.freeze_customer_credit:consume_credit 真扣 + 各池拆分(before−after)+ 落表
- customer_credit.commit_customer_freeze:翻 status='committed' · 幂等 no-op
- customer_credit.release_customer_freeze:refund_credit 按拆分退回(source='tool_fail_refund')· 幂等防重退(sweeper)
- billing.commit_freeze / release_freeze:[BUG-P2] 按冻结实际所在表路由(_route_freeze_table:
  legacy point_freezes 命中即 legacy · 否则查 customer_credit_freezes · freeze_id 撞号靠 user_id 消歧)·
  防客户运行期 legacy→V3.5 漂移致跨表错路由 → sweeper 白退
- freeze→release 对称(扣多少退多少 · 各池不串)

真 DB 集成(新表 INSERT + refund_credit 真退 + source CHECK)由 Deploy staging 验证。
设计 A:无 frozen 列 · freeze 即实扣 · commit 翻 status · release refund_credit 退回(与 _refund_v35 对称)。
"""
import asyncio
import pytest


class _Cur:
    """mock cursor · 记录 SQL · fetchone 走预置队列。"""
    def __init__(self):
        self.sqls = []
        self.last_params = None
        self._q = []

    def execute(self, sql, params=None):
        self.sqls.append(" ".join(sql.split()))
        self.last_params = params

    def fetchone(self):
        return self._q.pop(0) if self._q else None

    def feed(self, *rows):
        self._q.extend(rows)
        return self


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ============================================================
# freeze_customer_credit
# ============================================================

@pytest.mark.skip(reason="[单账本接线 2026-08-17 · R4] 本用例测 customer_credit.freeze_customer_credit —— 造 v35 冻结的那一半已随单账本收敛删除(freeze_points 硬编码 legacy,结构上不可能再产生 v35 冻结)。commit/release 的**结算**通道仍保留并仍被本文件其余用例覆盖。")
def test_freeze_tool_split(monkeypatch):
    """工具类冻结 · bonus 优先 tool 补差 · 各池实扣量 = before − after。"""
    import services.customer_credit as cc
    monkeypatch.setattr(cc, "get_or_create_customer_wallet",
                        lambda c, u: {"agent_user_id": 7, "tool_credit_points": 100,
                                      "publish_credit_points": 0, "bonus_credit_points": 50})
    # cost=80 · bonus 50 全扣 + tool 30 → after tool=70 bonus=0
    monkeypatch.setattr(cc, "consume_credit",
                        lambda c, u, f, cost, related_order_id=None, description=None, requires_paid=False:
                        {"tool_credit_points": 70, "publish_credit_points": 0,
                         "bonus_credit_points": 0, "consumed_from_pool": "bonus+tool"})
    cur = _Cur().feed(None, {"id": 99})  # [P2-3] 幂等查 None(无 existing) + INSERT RETURNING id
    r = cc.freeze_customer_credit(cur, 28, "geo_diagnosis", 80, task_ref="sess1", brand_id=5)
    assert r["freeze_id"] == 99
    assert r["amount"] == 80
    assert r["amount_bonus"] == 50
    assert r["amount_tool"] == 30
    assert r["amount_publish"] == 0
    # 落表 INSERT customer_credit_freezes status=frozen
    assert any("INSERT INTO customer_credit_freezes" in s and "'frozen'" in s for s in cur.sqls)


@pytest.mark.skip(reason="[单账本接线 2026-08-17 · R4] 本用例测 customer_credit.freeze_customer_credit —— 造 v35 冻结的那一半已随单账本收敛删除(freeze_points 硬编码 legacy,结构上不可能再产生 v35 冻结)。commit/release 的**结算**通道仍保留并仍被本文件其余用例覆盖。")
def test_freeze_publish_pool(monkeypatch):
    """publish-only 长任务 · 扣 publish 池(paid-only)· 不串 tool/bonus。"""
    import services.customer_credit as cc
    monkeypatch.setattr(cc, "get_or_create_customer_wallet",
                        lambda c, u: {"agent_user_id": 7, "tool_credit_points": 10,
                                      "publish_credit_points": 200, "bonus_credit_points": 10})
    monkeypatch.setattr(cc, "consume_credit",
                        lambda c, u, f, cost, related_order_id=None, description=None, requires_paid=False:
                        {"tool_credit_points": 10, "publish_credit_points": 120,
                         "bonus_credit_points": 10, "consumed_from_pool": "publish"})
    cur = _Cur().feed(None, {"id": 100})  # [P2-3] 幂等查 None + INSERT id
    r = cc.freeze_customer_credit(cur, 28, "publish_single", 80, task_ref="pub1", requires_paid=True)
    assert r["amount_publish"] == 80
    assert r["amount_tool"] == 0 and r["amount_bonus"] == 0


@pytest.mark.skip(reason="[单账本接线 2026-08-17 · R4] 本用例测 customer_credit.freeze_customer_credit —— 造 v35 冻结的那一半已随单账本收敛删除(freeze_points 硬编码 legacy,结构上不可能再产生 v35 冻结)。commit/release 的**结算**通道仍保留并仍被本文件其余用例覆盖。")
def test_freeze_insufficient_raises(monkeypatch):
    """余额不足 → consume_credit 抛 · freeze 不落记录(冒泡给 billing 转 402)。"""
    import services.customer_credit as cc
    monkeypatch.setattr(cc, "get_or_create_customer_wallet",
                        lambda c, u: {"agent_user_id": 7, "tool_credit_points": 5,
                                      "publish_credit_points": 0, "bonus_credit_points": 0})

    def _boom(*a, **k):
        raise cc.InsufficientCreditError(28, "geo_diagnosis", 80, 5, "tool+bonus")
    monkeypatch.setattr(cc, "consume_credit", _boom)
    cur = _Cur()  # 幂等查 None(无 feed)→ consume 抛
    with pytest.raises(cc.InsufficientCreditError):
        cc.freeze_customer_credit(cur, 28, "geo_diagnosis", 80, task_ref="x")
    # 没 INSERT freeze 记录(扣失败不占位)
    assert not any("INSERT INTO customer_credit_freezes" in s for s in cur.sqls)


@pytest.mark.skip(reason="[单账本接线 2026-08-17 · R4] 本用例测 customer_credit.freeze_customer_credit —— 造 v35 冻结的那一半已随单账本收敛删除(freeze_points 硬编码 legacy,结构上不可能再产生 v35 冻结)。commit/release 的**结算**通道仍保留并仍被本文件其余用例覆盖。")
def test_freeze_idempotent_hit(monkeypatch):
    """[P2-3] 同 task_ref 已有活跃 frozen → 返回已有 · 不重复 consume(防重试/重投双扣)。"""
    import services.customer_credit as cc
    consumed = []
    monkeypatch.setattr(cc, "consume_credit", lambda *a, **k: consumed.append(1) or {})
    # 幂等查命中:返回已有 frozen 行(feed 第一个 fetchone)
    cur = _Cur().feed({"id": 77, "amount_total": 80, "amount_tool": 30, "amount_publish": 0, "amount_bonus": 50})
    r = cc.freeze_customer_credit(cur, 28, "geo_diagnosis", 80, task_ref="sess1")
    assert r["idempotent"] is True
    assert r["freeze_id"] == 77
    assert r["amount"] == 80
    assert r["amount_tool"] == 30 and r["amount_bonus"] == 50
    assert len(consumed) == 0  # 没重复扣
    assert not any("INSERT INTO customer_credit_freezes" in s for s in cur.sqls)
    # 幂等查带 FOR UPDATE(防并发)
    assert any("status = 'frozen'" in s and "FOR UPDATE" in s for s in cur.sqls)


# ============================================================
# commit_customer_freeze
# ============================================================

def test_commit_flips_status():
    import services.customer_credit as cc
    cur = _Cur().feed({"id": 99, "status": "frozen", "customer_user_id": 28, "amount_total": 80})
    r = cc.commit_customer_freeze(cur, freeze_id=99, reason="完成")
    assert r["success"] is True and r["freeze_id"] == 99 and r["amount"] == 80
    assert r["v35_customer_credit"] is True
    assert any("status = 'committed'" in s for s in cur.sqls)


def test_commit_idempotent():
    """已 committed 再 commit → no-op(防重复 settle)。"""
    import services.customer_credit as cc
    cur = _Cur().feed({"id": 99, "status": "committed", "customer_user_id": 28, "amount_total": 80})
    r = cc.commit_customer_freeze(cur, freeze_id=99)
    assert r["success"] is True and r["idempotent"] is True
    assert not any("UPDATE customer_credit_freezes" in s for s in cur.sqls)


def test_commit_not_found():
    import services.customer_credit as cc
    cur = _Cur()  # fetchone → None
    r = cc.commit_customer_freeze(cur, freeze_id=12345)
    assert r["success"] is False


# ============================================================
# release_customer_freeze
# ============================================================

def test_release_refunds_pools(monkeypatch):
    """释放 → refund_credit 按 freeze 拆分退回三池 · source=tool_fail_refund · 翻 released。"""
    import services.customer_credit as cc
    cap = []
    monkeypatch.setattr(cc, "refund_credit",
                        lambda c, u, tool_points=0, publish_points=0, bonus_points=0,
                        related_order_id=None, description=None, source=None:
                        cap.append({"u": u, "tool": tool_points, "publish": publish_points,
                                    "bonus": bonus_points, "source": source, "rel": related_order_id})
                        or {})
    cur = _Cur().feed({"id": 99, "status": "frozen", "customer_user_id": 28, "amount_total": 80,
                       "amount_tool": 30, "amount_publish": 0, "amount_bonus": 50, "task_ref": "sess1"})
    r = cc.release_customer_freeze(cur, freeze_id=99, reason="失败")
    assert r["success"] is True
    assert len(cap) == 1
    assert cap[0]["tool"] == 30 and cap[0]["bonus"] == 50 and cap[0]["publish"] == 0
    assert cap[0]["source"] == "tool_fail_refund"
    assert cap[0]["rel"] == "sess1"
    assert any("status = 'released'" in s for s in cur.sqls)


def test_release_idempotent_no_double_refund(monkeypatch):
    """已 released 再 release(sweeper 重扫)→ 不再 refund(防双退)。"""
    import services.customer_credit as cc
    cap = []
    monkeypatch.setattr(cc, "refund_credit", lambda *a, **k: cap.append(1) or {})
    cur = _Cur().feed({"id": 99, "status": "released", "customer_user_id": 28, "amount_total": 80,
                       "amount_tool": 30, "amount_publish": 0, "amount_bonus": 50, "task_ref": "sess1"})
    r = cc.release_customer_freeze(cur, freeze_id=99)
    assert r["success"] is True and r["idempotent"] is True
    assert len(cap) == 0  # 不重退


def test_release_committed_no_refund(monkeypatch):
    """已 committed(钱已坐实)再 release → no-op 不退(防 commit 后误退)。"""
    import services.customer_credit as cc
    cap = []
    monkeypatch.setattr(cc, "refund_credit", lambda *a, **k: cap.append(1) or {})
    cur = _Cur().feed({"id": 99, "status": "committed", "customer_user_id": 28, "amount_total": 80,
                       "amount_tool": 30, "amount_publish": 0, "amount_bonus": 50, "task_ref": "s"})
    r = cc.release_customer_freeze(cur, freeze_id=99)
    assert r["idempotent"] is True
    assert len(cap) == 0


# ============================================================
# billing.commit_freeze / release_freeze user_id 路由
# ============================================================

def test_billing_commit_routes_v35(monkeypatch):
    # [BUG-P2] 路由改为按冻结【实际所在表】(_route_freeze_table):legacy 查空 → v35 命中 → 走新表。
    # 不再靠 _is_v35_customer(当前状态)·喂 None(point_freezes 未命中)+ (1,)(customer_credit_freezes 命中)。
    import middleware.billing as b
    import services.customer_credit as cc
    cur = _Cur().feed(None, (1,))
    monkeypatch.setattr(b, "get_db", lambda: _Conn(cur))
    called = {}
    monkeypatch.setattr(cc, "commit_customer_freeze",
                        lambda c, freeze_id=None, task_ref=None, reason=None, customer_user_id=None:
                        called.update(freeze_id=freeze_id, task_ref=task_ref, customer_user_id=customer_user_id)
                        or {"success": True, "v35_customer_credit": True})
    r = asyncio.run(b.commit_freeze(task_ref="sess1", user_id=28))
    assert r["v35_customer_credit"] is True
    assert called["task_ref"] == "sess1"


def test_billing_commit_legacy_when_not_v35(monkeypatch):
    """user_id 非 V3.5 客户 → 不调 V3.5 · 走 legacy point_freezes(本测只验不进 V3.5 分支)。"""
    import middleware.billing as b
    import services.customer_credit as cc
    cur = _Cur()  # legacy SELECT point_freezes → fetchone None → "未找到"
    monkeypatch.setattr(b, "get_db", lambda: _Conn(cur))
    # [单账本收敛 2026-07-27] _is_v35_customer 已删除,无需再 patch ——
    # 单账本后本来就不存在 V3.5 分支,本测试的意图(不走 V3.5、走 legacy)恒成立且更强。
    v35_called = []
    monkeypatch.setattr(cc, "commit_customer_freeze", lambda *a, **k: v35_called.append(1) or {})
    r = asyncio.run(b.commit_freeze(task_ref="sess1", user_id=999))
    assert len(v35_called) == 0  # 没走 V3.5
    # 走了 legacy point_freezes 查询
    assert any("point_freezes" in s for s in cur.sqls)


def test_billing_commit_no_userid_legacy(monkeypatch):
    """不传 user_id(legacy 调用方零感知)→ 走 legacy · 不碰 _is_v35_customer。"""
    import middleware.billing as b
    import services.customer_credit as cc
    cur = _Cur()
    monkeypatch.setattr(b, "get_db", lambda: _Conn(cur))
    v35_called = []
    monkeypatch.setattr(cc, "commit_customer_freeze", lambda *a, **k: v35_called.append(1) or {})
    # [单账本收敛 2026-07-27] 原来这里 patch _is_v35_customer 让它一被调用就抛错,
    # 用以证明 user_id is None 时不会误判身份。该函数已随 V3.5 分流删除 ——
    # 现在"不按身份分流"是结构性保证,不需要再用 patch 去证明。
    r = asyncio.run(b.commit_freeze(task_ref="sess1"))
    assert len(v35_called) == 0
    assert any("point_freezes" in s for s in cur.sqls)


def test_billing_commit_ambiguous_fail_closed(monkeypatch):
    # [A2] 跨表双 frozen 撞号 → commit_freeze 拒绝动钱 · 不调 V3.5 · 不查 legacy point_freezes
    import middleware.billing as b
    import services.customer_credit as cc
    cur = _Cur().feed((1,), (1,))  # leg_frozen 命中 + v35_frozen 命中 → ambiguous
    monkeypatch.setattr(b, "get_db", lambda: _Conn(cur))
    v35_called = []
    monkeypatch.setattr(cc, "commit_customer_freeze", lambda *a, **k: v35_called.append(1) or {})
    r = asyncio.run(b.commit_freeze(freeze_id=5, user_id=100))
    assert r["success"] is False and r.get("ambiguous") is True
    assert len(v35_called) == 0  # 没动 V3.5
    assert not any("UPDATE point_freezes" in s for s in cur.sqls)  # 没动 legacy 钱


def test_billing_release_routes_v35(monkeypatch):
    # [BUG-P2] 路由改为按冻结【实际所在表】(_route_freeze_table):legacy 查空 → v35 命中 → 走新表。
    import middleware.billing as b
    import services.customer_credit as cc
    cur = _Cur().feed(None, (1,))  # point_freezes 未命中 + customer_credit_freezes 命中
    monkeypatch.setattr(b, "get_db", lambda: _Conn(cur))
    called = {}
    monkeypatch.setattr(cc, "release_customer_freeze",
                        lambda c, freeze_id=None, task_ref=None, reason=None, customer_user_id=None:
                        called.update(freeze_id=freeze_id, customer_user_id=customer_user_id)
                        or {"success": True, "v35_customer_credit": True})
    r = asyncio.run(b.release_freeze(freeze_id=99, user_id=28))
    assert r["v35_customer_credit"] is True
    assert called["freeze_id"] == 99


# ============================================================
# freeze→release 对称(端到端 mock · 扣多少退多少)
# ============================================================

@pytest.mark.skip(reason="[单账本接线 2026-08-17 · R4] 本用例测 customer_credit.freeze_customer_credit —— 造 v35 冻结的那一半已随单账本收敛删除(freeze_points 硬编码 legacy,结构上不可能再产生 v35 冻结)。commit/release 的**结算**通道仍保留并仍被本文件其余用例覆盖。")
def test_freeze_release_symmetry(monkeypatch):
    """freeze 扣 bonus50+tool30 · release 必须退回 bonus50+tool30(各池对称不串)。"""
    import services.customer_credit as cc
    monkeypatch.setattr(cc, "get_or_create_customer_wallet",
                        lambda c, u: {"agent_user_id": 7, "tool_credit_points": 100,
                                      "publish_credit_points": 0, "bonus_credit_points": 50})
    monkeypatch.setattr(cc, "consume_credit",
                        lambda c, u, f, cost, related_order_id=None, description=None, requires_paid=False:
                        {"tool_credit_points": 70, "publish_credit_points": 0,
                         "bonus_credit_points": 0, "consumed_from_pool": "bonus+tool"})
    cur = _Cur().feed(None, {"id": 99})  # [P2-3] 幂等查 None + INSERT id
    fr = cc.freeze_customer_credit(cur, 28, "geo_diagnosis", 80, task_ref="sess1")

    cap = []
    monkeypatch.setattr(cc, "refund_credit",
                        lambda c, u, tool_points=0, publish_points=0, bonus_points=0,
                        related_order_id=None, description=None, source=None:
                        cap.append({"tool": tool_points, "publish": publish_points, "bonus": bonus_points})
                        or {})
    cur2 = _Cur().feed({"id": 99, "status": "frozen", "customer_user_id": 28, "amount_total": 80,
                        "amount_tool": fr["amount_tool"], "amount_publish": fr["amount_publish"],
                        "amount_bonus": fr["amount_bonus"], "task_ref": "sess1"})
    cc.release_customer_freeze(cur2, task_ref="sess1")
    assert cap[0]["tool"] == 30 and cap[0]["bonus"] == 50 and cap[0]["publish"] == 0
