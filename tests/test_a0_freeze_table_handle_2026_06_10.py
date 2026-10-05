"""[A0 根治] freeze 句柄带 table 标记 · callers 回传显式指定表 · 根除 freeze_id 跨表撞号歧义

freeze_points 返回 freeze_table('legacy'|'v35');commit_freeze/release_freeze 接受 freeze_table 并
直接路由(不猜);freeze_id 路径调用方(freeze_sweeper/monitoring_api/geo_plan worker+api)回传该标记。
task_ref 路径(诊断/scheduler/batch_monitor)task_ref 天然单表无歧义,不需标记(A1+A2+A3 已安全)。
"""
import asyncio
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# ---------- _route_freeze_table 显式标记直接路由(不查 DB) ----------

class _NoDBCursor:
    """任何 execute 都视为违例(证明显式 freeze_table 不触发 DB 探测)。"""
    def execute(self, *a, **k):
        raise AssertionError("显式 freeze_table 不应触发任何 DB 探测")

    def fetchone(self):
        raise AssertionError("不应 fetch")


def test_explicit_table_routes_without_db():
    from middleware.billing import _route_freeze_table
    assert _route_freeze_table(_NoDBCursor(), freeze_id=5, user_id=100, freeze_table="v35") == "v35"
    assert _route_freeze_table(_NoDBCursor(), freeze_id=5, user_id=100, freeze_table="legacy") == "legacy"


def test_invalid_table_falls_back_to_disambiguation():
    # 非法/空 freeze_table → 回落消歧(会查 DB),用一个返回 None 的 cursor 验证回落路径不崩
    class _NullCur:
        def execute(self, *a, **k): pass
        def fetchone(self): return None
    from middleware.billing import _route_freeze_table
    assert _route_freeze_table(_NullCur(), freeze_id=5, user_id=100, freeze_table=None) is None
    assert _route_freeze_table(_NullCur(), freeze_id=5, user_id=100, freeze_table="garbage") is None


# ---------- billing commit/release 接受并按 freeze_table 路由 ----------

class _Cur:
    def __init__(self): self.sqls = []; self._q = []
    def execute(self, sql, params=None): self.sqls.append(" ".join(sql.split()))
    def fetchone(self): return self._q.pop(0) if self._q else None


class _Conn:
    def __init__(self, cur): self._cur = cur
    def cursor(self): return self._cur
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_commit_freeze_honors_explicit_table(monkeypatch):
    import middleware.billing as b
    import services.customer_credit as cc
    cur = _Cur()  # 不喂任何 fetchone → 若走消歧探测会得 None(legacy);显式 v35 应绕过探测
    monkeypatch.setattr(b, "get_db", lambda: _Conn(cur))
    called = {}
    monkeypatch.setattr(cc, "commit_customer_freeze",
                        lambda c, freeze_id=None, task_ref=None, reason=None, customer_user_id=None:
                        called.update(hit=True) or {"success": True, "v35_customer_credit": True})
    r = asyncio.run(b.commit_freeze(freeze_id=5, user_id=100, freeze_table="v35"))
    assert r.get("v35_customer_credit") is True and called.get("hit") is True
    # 显式 v35 → 不应有任何 point_freezes 探测 SQL
    assert not any("point_freezes" in s for s in cur.sqls)


# ---------- freeze_points 返回 freeze_table(静态守护) ----------

def test_freeze_points_returns_freeze_table():
    src = (ROOT / "middleware" / "billing.py").read_text(encoding="utf-8")
    # [单账本收敛 2026-07-27] 原有一条断言 freeze_points 会返回 freeze_table="v35"。
    # 单账本后【不再新建】v35 冻结,该返回分支已删,断言前提消失 —— 移除而不是放宽。
    # "已存在的 v35 冻结仍能 commit/release" 由 _route_freeze_table 保证,
    # 对应的锁在 tests/test_wallet_single_ledger_stage2.py:
    #   test_freeze_routing_by_table_not_identity / test_commit_release_keep_v35_settlement_path
    assert src.count('"freeze_table": "legacy"') >= 1, "legacy freeze 返回须带 freeze_table=legacy"


# ---------- 调用方静态守护 ----------

def test_sweeper_passes_explicit_table():
    src = (ROOT / "services" / "freeze_sweeper.py").read_text(encoding="utf-8")
    assert 'ftable = "v35" if fz.get("_v35") else "legacy"' in src
    assert "freeze_table=ftable" in src, "sweeper 扫描即知表 → 显式回传"


def test_monitoring_threads_table():
    src = (ROOT / "api" / "monitoring_api.py").read_text(encoding="utf-8")
    assert 'freeze_table = freeze_result.get("freeze_table")' in src
    assert src.count("freeze_table=freeze_table") >= 2, "commit+release 都回传"


def test_a0_migration_present():
    mig = ROOT / "scripts" / "migration_a0_geo_plan_freeze_table_2026_06_10.sql"
    assert mig.exists()
    txt = mig.read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS freeze_table" in txt
