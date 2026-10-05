"""服务锚口径统一(service_start_date 权威 · paid_at 降级)· 2026-05-29 · 真执行测试

老板拍板 D1-D5 + 优先级修正:全链路单一服务锚 COALESCE(service_start_date, paid_at)。
覆盖:
  - update_quote_status('confirmed'/'paid') 写 service_start_date=COALESCE(.,CURRENT_DATE)(SSOT 单点修锚)
  - confirmed/paid 不再取消监测订阅(D4)· draft/archived 仍取消
  - _get_effective_window:service_start 优先于 paid_start(D2 锚翻转 · 行为级)
  - 可见性 filter / 窗口 SQL 含 COALESCE(service_start_date, paid_at)(锚翻转落地)
  - check_hidden_real_customers 哨兵返被隐藏真客户

注:db.diagnosis_db import 时即连 DB(模块级)→ 不能直接 import,用 AST 抽函数源码 exec(同安全批 server.py 技法)。
   db.monitoring_db import 干净,直接 import。
"""
from __future__ import annotations

import ast
import datetime
import os
import sys
import types

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _read(rel):
    with open(os.path.join(_ROOT, rel), "r", encoding="utf-8") as f:
        return f.read()


def _extract_func(rel, name):
    """从源文件抽顶层函数源码(不 import 整模块 · 规避 import 时连 DB)。"""
    src = _read(rel)
    tree = ast.parse(src)
    lines = src.splitlines()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            seg = lines[node.lineno - 1: node.end_lineno]
            while seg and seg[0].lstrip().startswith("@"):
                seg = seg[1:]
            return "\n".join(seg)
    raise AssertionError(f"{rel} 未找到顶层函数 {name}")


def _load(rel, name, ns):
    g = {"__name__": "svc_anchor_test"}
    g.update(ns)
    exec(compile(_extract_func(rel, name), f"<{name}>", "exec"), g)
    return g[name]


# ============================================================
# SSOT · update_quote_status 写服务锚(AST 抽取 exec)
# ============================================================
class _CapCur:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def close(self):
        pass


class _CapConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def commit(self):
        pass

    def close(self):
        pass


def _run_update_quote_status(status):
    cur = _CapCur()
    fn = _load("db/diagnosis_db.py", "update_quote_status", {
        "get_connection": lambda: _CapConn(cur),
        "_sync_keyword_monitor_state_on_status_change": lambda *a, **k: None,
    })
    fn(289, status)
    return cur.executed[0][0]


def test_confirm_writes_service_anchor():
    sql = _run_update_quote_status("confirmed")
    assert "service_start_date = COALESCE(service_start_date, CURRENT_DATE)" in sql
    assert "confirmed_at" in sql           # 仍写 confirmed_at
    assert "paid_at" not in sql            # confirmed 不强写 paid_at(降级财务字段)


def test_paid_also_writes_service_anchor():
    sql = _run_update_quote_status("paid")
    assert "service_start_date = COALESCE(service_start_date, CURRENT_DATE)" in sql
    assert "paid_at = CURRENT_TIMESTAMP" in sql   # paid 仍写 paid_at(财务)


# ============================================================
# D4 · confirmed/paid 不取消监测 · draft 取消(AST 抽取 _sync)
# ============================================================
def _run_sync(status, calls):
    fake_mdb = types.ModuleType("db.monitoring_db")
    fake_mdb.cancel_subscriptions_by_quote = lambda quote_id, reason=None: calls.append((quote_id, reason))
    saved = sys.modules.get("db.monitoring_db")
    sys.modules["db.monitoring_db"] = fake_mdb
    try:
        fn = _load("db/diagnosis_db.py", "_sync_keyword_monitor_state_on_status_change", {})
        fn(status[0], status[1])
    finally:
        if saved is not None:
            sys.modules["db.monitoring_db"] = saved
        else:
            sys.modules.pop("db.monitoring_db", None)


def test_confirmed_and_paid_do_not_cancel_monitoring():
    calls = []
    _run_sync((1, "confirmed"), calls)
    _run_sync((2, "paid"), calls)
    assert calls == []                     # confirmed/paid 都不取消(D4)


def test_draft_archived_still_cancel_monitoring():
    calls = []
    _run_sync((3, "draft"), calls)
    _run_sync((4, "archived"), calls)
    ids = [c[0] for c in calls]
    assert 3 in ids and 4 in ids           # 未开始/已退出仍取消


# ============================================================
# D2 · _get_effective_window service_start 优先(行为级 · db.monitoring_db import 干净)
# ============================================================
class _WinCur:
    def __init__(self, row):
        self._row = row

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return self._row


def test_effective_window_service_start_takes_priority():
    import db.monitoring_db as mdb
    # [服务期 SSOT 2026-08-06] 上界改读 service_end_date(不再拿 service_days 推)。
    #   起点优先级这条契约未变,只把 fixture 换成新行形。
    row = {
        "paid_start": datetime.date(2026, 5, 8),
        "service_start": datetime.date(2026, 3, 21),   # 更早 · 应被优先
        "service_end": datetime.date(2026, 4, 20),
        "kms_start": None,
    }
    start, end = mdb._get_effective_window(_WinCur(row), 1, 1)
    assert start == datetime.date(2026, 3, 21)                       # service_start 优先(非 paid_start)
    # 上界 = max(service_end_date, 今天):A 方案不设自然日历封顶,详见 helper docstring
    assert end == max(datetime.date(2026, 4, 20), datetime.date.today())


def test_effective_window_falls_back_to_paid_when_no_service_start():
    import db.monitoring_db as mdb
    row = {"paid_start": datetime.date(2026, 5, 8), "service_start": None,
           "service_end": datetime.date(2026, 6, 7), "kms_start": None}
    start, _ = mdb._get_effective_window(_WinCur(row), 1, 1)
    assert start == datetime.date(2026, 5, 8)                        # 无 service_start → 兜底 paid_at


def test_effective_window_none_when_both_null():
    import db.monitoring_db as mdb
    row = {"paid_start": None, "service_start": None, "service_end": None, "kms_start": None}
    assert mdb._get_effective_window(_WinCur(row), 1, 1) == (None, None)
    # [服务期 SSOT 2026-08-06] 新增:有服务锚但**没有服务期**也必须 fail-closed
    row2 = {"paid_start": datetime.date(2026, 5, 8), "service_start": datetime.date(2026, 5, 8),
            "service_end": None, "kms_start": None}
    assert mdb._get_effective_window(_WinCur(row2), 1, 1) == (None, None)


# ============================================================
# 锚翻转落地(源码断言 · 防回退)
# ============================================================
def test_visibility_filter_and_window_use_service_anchor_first():
    src = _read("db/monitoring_db.py")
    assert "COALESCE(service_start_date, paid_at) IS NOT NULL" in src      # filter 用服务锚
    assert "COALESCE(q.service_start_date, q.paid_at::date)" in src        # 窗口/scheduler 用服务锚


def test_portal_contract_start_service_first():
    src = _read("api/monitoring_api.py")
    # portal 起算点:service_start_date 优先分支在 paid_at 之前
    # [服务期 SSOT 2026-08-06] 写法改成先赋值再兜底,优先级契约未变。
    i_svc = src.find("_contract_start = _svc_start")
    i_paid = src.find("if _contract_start is None and _paid_at:")
    assert 0 < i_svc < i_paid


# ============================================================
# D6 · 哨兵
# ============================================================
def test_sentinel_returns_hidden_real_customers(monkeypatch):
    import db.monitoring_db as mdb

    class _Cur:
        def execute(self, sql, params=None):
            assert "status = 'confirmed'" in sql
            assert "service_start_date IS NULL" in sql and "paid_at IS NULL" in sql
            assert "status = 'active'" in sql      # 有 active KMS 才算被隐藏

        def fetchall(self):
            return [{"quote_id": 999, "brand_id": 5, "brand_name": "X", "active_kms": 3}]

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            pass
    monkeypatch.setattr(mdb, "get_connection", lambda: _Conn())
    out = mdb.check_hidden_real_customers()
    assert out and out[0]["quote_id"] == 999
