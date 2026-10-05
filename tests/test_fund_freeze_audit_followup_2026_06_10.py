"""独立审计方(Fable)对 5 敏感 commit 深审后的修复 · 回归守护

A. 冻结链路由根治(退算力自动退正确性 · billing 红线 + 周边)
   A1 sweeper 始终传 user_id(原 legacy 丢 None 反致撞号跨客户错退) + _lock_customer_freeze/
      billing legacy 查询带 user 约束
   A2 双 frozen 撞号 → _route_freeze_table 返回 'ambiguous' · commit/release fail-closed 不动钱
   A3 frozen 探测带 FOR UPDATE 消 TOCTOU
   (A0 freeze 句柄带 table 标记 = 大改·V3.5 上线前置闸·单独批·本文件不覆盖)
B. c1d1149b 漏的展示层 sink:margin 预览/校验按 per-agent 出厂折扣(非全局)
C1. 工单 execute CAS(status='approved' 乐观锁)防并发双执行
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# ---------- A1 静态守护 ----------

def test_a1_sweeper_always_passes_user_id():
    src = (ROOT / "services" / "freeze_sweeper.py").read_text(encoding="utf-8")
    assert 'uid = fz.get("user_id")' in src, "sweeper 两路径都须传真实 user_id"
    # 不再有 legacy 丢 None 的反向逻辑
    assert 'uid = fz.get("user_id") if fz.get("_v35") else None' not in src, \
        "残留 legacy 丢 user_id=None → 撞号跨客户错退未修"


def test_a1_lock_customer_freeze_user_constraint():
    src = (ROOT / "services" / "customer_credit.py").read_text(encoding="utf-8")
    s = src.find("def _lock_customer_freeze")
    seg = src[s: s + 800]
    assert "customer_user_id" in seg and "AND customer_user_id = %s" in seg, \
        "_lock_customer_freeze 须支持 customer_user_id 约束防跨表撞号锁错客户"


def test_a1_billing_legacy_lookup_user_constraint():
    src = (ROOT / "middleware" / "billing.py").read_text(encoding="utf-8")
    assert src.count("SELECT * FROM point_freezes WHERE id = %s AND user_id = %s FOR UPDATE") == 2, \
        "commit/release legacy freeze_id 查询都须带 user_id 约束"


# ---------- A3 静态守护 ----------

def test_a3_frozen_probe_for_update():
    src = (ROOT / "middleware" / "billing.py").read_text(encoding="utf-8")
    s = src.find("def _route_freeze_table")
    seg = src[s: s + 2600]
    assert 'lock_cond = " FOR UPDATE" if (only_frozen and user_id is not None) else ""' in seg, \
        "frozen 探测须带 FOR UPDATE 锁定候选行消 TOCTOU(且 user-scope:user_id 非空才加锁)"
    assert "'ambiguous'" in seg or '"ambiguous"' in seg, "双 frozen 须返回 ambiguous"


# ---------- B margin per-agent override ----------

def test_b_per_agent_factory_override_helper(monkeypatch):
    import services.agent_pricing as ap
    import services.agent_pricing_overrides as apo
    # 无 override → None(用全局·零回归)
    monkeypatch.setattr(apo, "get_agent_wholesale_override", lambda a: None)
    assert ap._per_agent_factory_override(123, 13000) is None
    # 有 override(150/325 更优折扣)→ 返回该折扣 factory(< 全局 225/325)
    monkeypatch.setattr(apo, "get_agent_wholesale_override", lambda a: (150, 325))
    f = ap._per_agent_factory_override(123, 13000)
    assert f == ap.calc_factory_cents(13000, 150, 325)
    assert f < ap.calc_factory_cents(13000, 225, 325)  # 比全局低 → margin 警示才准
    # agent_user_id 缺省 / points<=0 → None
    assert ap._per_agent_factory_override(None, 13000) is None
    assert ap._per_agent_factory_override(123, 0) is None


def test_b_margin_callsites_pass_override():
    src = (ROOT / "services" / "agent_pricing.py").read_text(encoding="utf-8")
    # get_sku_for_agent + _validate_and_calc_margin 两处 calc_settlement 都传 per-agent factory
    assert "factory_cents_override=_per_agent_factory_override(" in src, \
        "兼容 agent 视图必须传 per-agent factory override"
    assert "factory_cents_override=factory_override" in src, \
        "canonical 校验器必须传已解析的 per-agent factory override"
    # canonical create/update/markup callers都必须把 agent_user_id 传进统一计算器。
    assert src.count("agent_user_id=agent_user_id") >= 3


# ---------- C1 工单 execute CAS ----------

def test_c1_save_execution_result_cas():
    src = (ROOT / "db" / "refund_work_order_db.py").read_text(encoding="utf-8")
    s = src.find("def save_execution_result")
    seg = src[s: s + 1200]
    assert "WHERE id = %s AND status = 'approved'" in seg, \
        "save_execution_result 须 CAS(status='approved')防并发双执行"


# ========== Fable 复核补:真行为级测试(替静态 grep 假绿) ==========

def test_c1_cas_behavioral_second_execute_raises(monkeypatch):
    """[C1 行为级] CAS 匹配 0 行(并发第二次 execute · status 已非 approved)→ raise fail-closed。"""
    import pytest
    import db.refund_work_order_db as rwo

    class _Cur:
        def execute(self, sql, params=None):
            self._sql = " ".join(sql.split())
        def fetchone(self):
            # UPDATE ... WHERE status='approved' RETURNING * 匹配 0 行 → None
            return None

    class _Conn:
        def cursor(self): return _Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(rwo, "get_db", lambda: _Conn())
    with pytest.raises(ValueError):
        rwo.save_execution_result(1, 99, {"x": 1}, "completed")


def test_a1_lock_customer_constraint_blocks_cross_customer_behavioral():
    """[A1 行为级] _lock_customer_freeze 带 customer_user_id 约束 → 跨客户同 id 撞号被挡(返回 None·不锁别人行)。"""
    import services.customer_credit as cc

    class _Cur:
        def __init__(self, row):
            self.row = row
            self._hit = None
        def execute(self, sql, params=None):
            s = " ".join(sql.split())
            if "customer_user_id = %s" in s:
                # 行属 customer=100;带约束查询仅 params 末位命中 100 才返回(模拟 SQL WHERE 过滤)
                self._hit = self.row if (params and params[-1] == 100) else None
            else:
                self._hit = self.row  # 无约束(向后兼容)
        def fetchone(self):
            return self._hit

    row = {"id": 5, "status": "frozen", "customer_user_id": 100}
    # 跨客户:查 customer=200 → 约束挡住 → None(绝不锁别客户的同 id 行)
    assert cc._lock_customer_freeze(_Cur(row), freeze_id=5, customer_user_id=200) is None
    # 本客户:查 customer=100 → 命中
    assert cc._lock_customer_freeze(_Cur(row), freeze_id=5, customer_user_id=100) == row
    # 无约束(customer_user_id=None · 向后兼容)→ 命中
    assert cc._lock_customer_freeze(_Cur(row), freeze_id=5) == row


# ---------- BUG#1 / BUG#2 (Fable 独立新发现) ----------

def test_bug2_ambiguous_rollback_and_userscope_lock():
    src = (ROOT / "middleware" / "billing.py").read_text(encoding="utf-8")
    # ambiguous → conn.rollback() 释放探测锁(commit + release 两处)
    assert src.count("conn.rollback()") >= 2, "[BUG#2] commit/release ambiguous 须 rollback 释放探测锁"
    # FOR UPDATE 仅在 user_id 非空时加(消跨客户锁串扰)
    assert 'lock_cond = " FOR UPDATE" if (only_frozen and user_id is not None) else ""' in src, \
        "[BUG#2] frozen 探测 FOR UPDATE 须 user-scope(user_id 非空才加)"


import pytest


@pytest.mark.skip(reason="需真 PG:两表插同 id 双 frozen 验 ambiguous 端到端 · Deploy staging 跑(本机无 PG)")
def test_real_pg_double_frozen_ambiguous_integration():
    """[Deploy staging 待跑] 真 PG:point_freezes id=N frozen + customer_credit_freezes id=N frozen(同 user)
    → commit_freeze/release_freeze 返回 ambiguous · 两笔钱都不动 · 锁随 rollback 释放。"""
    pass
