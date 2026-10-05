# -*- coding: utf-8 -*-
"""P0-4(audit · 2026-06-10 · 含 Fable 返修):订阅监测计费主体 = brands.owner_user_id。
prod 实证 18/19 在跑订阅 user_id=1(admin 帮客户点·admin 免扣)→ 白烧 4 引擎 + 漏收 + 假流水。

返修(2026-06-10):
  P0-1 装饰器错绑 → AST 验路由真实绑定(grep 测试漏过)。
  P0-2 resume SELECT 缺 brand_id → mock-cursor 截获实际 SQL 验。
  fail-closed:owner 不可确认时返 None,调用方拒绝创建(禁回落操作者免扣)。
  幂等复用分支:把存量错位订阅 user_id 改写回 owner。
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / "server.py").read_text(encoding="utf-8")
SCHED = (ROOT / "scheduler.py").read_text(encoding="utf-8")

_ENABLE_PATH = "/api/monitoring/keyword/{keyword_id}/enable"


def _resolver_src() -> str:
    i = SERVER.find("def _resolve_monitor_billing_user")
    j = SERVER.find('@app.post("' + _ENABLE_PATH + '")', i)
    assert i >= 0 and j > i, "未找到 _resolve_monitor_billing_user / enable 装饰器"
    return SERVER[i:j]


# ============================================================
# P0-1 装饰器错绑 · AST 验真实绑定(不是字符串邻近)
# ============================================================
def _func_has_enable_post(node) -> bool:
    for d in getattr(node, "decorator_list", []):
        if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "post":
            if d.args and isinstance(d.args[0], ast.Constant) and d.args[0].value == _ENABLE_PATH:
                return True
    return False


def test_enable_route_decorator_bound_to_real_handler():
    """[返修 P0-1] @app.post(.../enable) 必须装饰真 handler enable_keyword_monitor_subscription,
    不得错绑到 helper _resolve_monitor_billing_user(错绑 → 端点 422 全挂)。
    AST 解析装饰器挂在哪个 FunctionDef = FastAPI 实际绑定语义,比 grep 邻近强。"""
    tree = ast.parse(SERVER)
    funcs: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.setdefault(node.name, []).append(node)
    assert any(_func_has_enable_post(n) for n in funcs.get("enable_keyword_monitor_subscription", [])), \
        "enable 路由装饰器未绑定到真 handler(装饰器错绑 P0)"
    assert not any(_func_has_enable_post(n) for n in funcs.get("_resolve_monitor_billing_user", [])), \
        "helper _resolve_monitor_billing_user 错绑了 enable 路由装饰器(P0)"


# ============================================================
# P0-2 resume SELECT 缺 brand_id · 截获实际下发 SQL
# ============================================================
def test_resume_select_includes_brand_id(monkeypatch):
    """[返修 P0-2] list_paused_subscriptions_for_resume 的 SELECT 必须真取 brand_id,
    否则 scheduler 恢复段 _load_brand_owner_map({s['brand_id']}) 恒空 → 回落原主体免扣。"""
    import db.monitoring_db as mdb
    captured = {}

    class _Cur:
        def execute(self, sql, *a):
            captured["sql"] = sql

        def fetchall(self):
            return [{"id": 1, "user_id": 1, "keyword_id": 2, "brand_id": 9,
                     "daily_points": 130, "feature_code": "monitoring_keyword_daily"}]

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

    monkeypatch.setattr(mdb, "get_connection", lambda: _Conn())
    rows = mdb.list_paused_subscriptions_for_resume()
    sql = captured["sql"]
    sel = sql[sql.upper().find("SELECT"):sql.upper().find("FROM")]
    assert "brand_id" in sel, "resume SELECT 必须含 brand_id(否则 owner_map 恒空)"
    assert rows and "brand_id" in rows[0]


# ============================================================
# fail-closed 解析器 · mock-DB 值级行为
# ============================================================
def test_resolver_fail_closed_value_level():
    """[返修] owner 有值→用 owner;brand 空 / owner NULL / 查询失败 → 返 None(fail-closed,
    禁回落操作者免扣)。"""
    import sys
    import types
    import textwrap
    src = _resolver_src()

    class _FakeLogger:
        def info(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

    class _FakeCur:
        def __init__(self, row):
            self._row = row

        def execute(self, *a):
            pass

        def fetchone(self):
            return self._row

    def _make(row, raise_on_connect=False):
        class _FakeConn:
            def cursor(self):
                return _FakeCur(row)

            def close(self):
                pass

        fake_db = types.ModuleType("db.connection")
        if raise_on_connect:
            fake_db.get_connection = lambda: (_ for _ in ()).throw(RuntimeError("db down"))
        else:
            fake_db.get_connection = lambda: _FakeConn()
        old = sys.modules.get("db.connection")
        sys.modules["db.connection"] = fake_db
        ns = {"logger": _FakeLogger()}
        exec(textwrap.dedent(src), ns)
        return ns["_resolve_monitor_billing_user"], old

    fn, old = _make({"owner_user_id": 46})
    try:
        assert fn(1, 99) == 46       # owner 锚定
        assert fn(46, 99) == 46      # 操作者本就是 owner
        assert fn(1, None) is None   # 无 brand → fail-closed
    finally:
        if old is not None:
            sys.modules["db.connection"] = old

    fn, old = _make({"owner_user_id": None})
    try:
        assert fn(7, 99) is None     # owner NULL → fail-closed(不回落 7)
    finally:
        if old is not None:
            sys.modules["db.connection"] = old

    fn, old = _make(None, raise_on_connect=True)
    try:
        assert fn(7, 99) is None     # 查询失败 → fail-closed(不回落 7)
    finally:
        if old is not None:
            sys.modules["db.connection"] = old


# ============================================================
# 写入侧 · 创建分支 fail-closed + 锚 owner + 幂等改写
# ============================================================
def test_write_side_enable_anchors_owner_and_fail_closed():
    """单点 enable 创建分支:解析后 _billing_uid 为空时 raise(fail-closed),否则建订阅传 owner。"""
    assert "def _resolve_monitor_billing_user(" in SERVER
    i = SERVER.find("# 3. 创建订阅(create_keyword_monitor_subscription 内已幂等")
    _nx = SERVER.find(chr(10) + "@app.", i)
    blk = SERVER[i:_nx if _nx > 0 else i + 6000]
    assert "_billing_uid = await _asyncio.to_thread(_resolve_monitor_billing_user, user_id" in blk
    assert "if not _billing_uid:" in blk, "owner 解析不到必须 fail-closed 拒绝创建"
    assert "user_id=_billing_uid," in blk
    i2 = SERVER.find("# 4. 取余额预览", i)
    assert "get_wallet_balance, _billing_uid" in SERVER[i2:i2 + 600]


def test_write_side_idempotent_reuse_rewrites_owner():
    """[返修] 幂等复用分支(existing active/paused)必须把 user_id 改写回 owner(修存量错位)。"""
    i = SERVER.find('if existing and existing.get("status") in ("active", "paused_low_balance"):')
    # 🔴 [parity 2026-08-16] 原来写死 +1500。parity 的 P0-3 在这个复用分支里插了 extra 支的处理,
    #   两个 token 被推到相对 1735 / 2834 字符 → 窗外 → 断言红。
    #   **尺子量程不够,不是计费主体锚丢了**(实测两个 token 都还在,只是更靠后)。
    #   改成量到下一个路由定义之前的整段,不再用魔数。
    _nx = SERVER.find(chr(10) + "@app.", i)
    blk = SERVER[i:_nx if _nx > 0 else i + 6000]
    assert "_resolve_monitor_billing_user, user_id" in blk, "复用分支也要解析计费主体"
    assert "update_subscription_billing_user" in blk, "复用分支必须改写订阅 user_id"


def test_write_side_batch_enable_anchors_owner():
    """batch-enable 同口径(防 batch 绕过)。"""
    i = SERVER.find('"/api/monitoring/keyword/batch-enable"')
    blk = SERVER[i:i + 5000]
    assert "_resolve_monitor_billing_user, user_id" in blk
    assert "user_id=_billing_uid," in blk


def test_update_subscription_billing_user_helper_exists():
    """[返修] monitoring_db 新增 update_subscription_billing_user(单点改写订阅 user_id)。"""
    MDB = (ROOT / "db" / "monitoring_db.py").read_text(encoding="utf-8")
    assert "def update_subscription_billing_user(" in MDB
    fn = MDB[MDB.find("def update_subscription_billing_user("):]
    fn = fn[:fn.find("\ndef ", 1)]
    assert "UPDATE keyword_monitor_subscriptions SET user_id" in fn
    assert "conn.commit()" in fn


# ============================================================
# daily 链 · 三段同主体(返修前已对,保持)
# ============================================================
def test_daily_chain_uses_resolved_billing_uid():
    """daily 链:恢复/预检/实扣三段全用解析后主体(预检与实扣必须同主体)。"""
    assert "def _load_brand_owner_map(" in SCHED
    assert "def _billing_uid_for_sub(" in SCHED
    assert "check_balance_only(_billing_uid_for_sub(sub, _paused_owner_map)" in SCHED
    assert 'sub["_billing_user_id"] = _billing_uid_for_sub(sub, _owner_map)' in SCHED
    assert 'check_balance_only(sub["_billing_user_id"], sub["feature_code"])' in SCHED
    assert '"user_id": sub.get("_billing_user_id") or sub["user_id"],' in SCHED
    assert 'check_balance_only(sub["user_id"], sub["feature_code"])' not in SCHED
