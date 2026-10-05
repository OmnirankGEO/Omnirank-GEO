# -*- coding: utf-8 -*-
"""P3 GEO IDOR/bug 批(audit · 2026-06-10):
#489 监测 GET 端点(config/rollback-tasks/archives)缺归属 → 加 brand RBAC
#654 scheduler calculate_rate_change 参数顺序错位(rate_change 恒 0 + 潜在 TypeError)
#731 WS 进度订阅只验 JWT 不验 session 归属 → 加归属校验(防订阅他人诊断进度)
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / "server.py").read_text(encoding="utf-8")
SCHED = (ROOT / "api" / "scheduler.py").read_text(encoding="utf-8")
SESSION_ACCESS = (ROOT / "auth" / "session_access.py").read_text(encoding="utf-8")


def _ep(name: str) -> str:
    i = SERVER.find(f"def {name}(")
    assert i >= 0, f"未找到 {name}"
    cands = [SERVER.find("\n@app.", i + 10), SERVER.find("\nasync def ", i + 10), SERVER.find("\ndef ", i + 10)]
    ends = [x for x in cands if x > 0]
    return SERVER[i:(min(ends) if ends else len(SERVER))]


# ---------- #654 scheduler 参数顺序 ----------

def test_654_rate_change_param_order():
    assert 'calculate_rate_change(kid, source, rate, "daily")' in SCHED, "参数顺序须 (kid, source, rate, 'daily')"
    assert 'calculate_rate_change(kid, source, "daily", today_str)' not in SCHED, "旧错位调用须移除"


# ---------- #489 监测 GET RBAC ----------

def test_489_config_rbac():
    b = _ep("get_monitoring_config")
    assert "request: Request" in b, "config 端点须收 request"
    assert "require_brand_access(request, brand_id" in b, "config 须按 brand 归属校验"


def test_489_rollback_tasks_rbac():
    b = _ep("get_rollback_tasks")
    assert "request: Request" in b, "rollback/tasks 须收 request"
    assert "require_brand_access(request, brand_id" in b, "rollback/tasks 须按 brand 归属校验"


def test_489_archives_rbac():
    b = _ep("list_archives_endpoint")
    assert "request: Request" in b, "archives 须收 request"
    assert "require_brand_access(request, brand_id" in b, "archives 须按 brand 归属校验"
    assert "get_user_brand_filter" in b, "archives 无 brand_id 时须按授权过滤(非 admin 不列全部归档)"


# ---------- #731 WS 归属校验 ----------

def test_731_ws_authorize_helper():
    assert "def _ws_authorize_session(" in SERVER, "缺 WS 归属校验 helper"
    h = _ep("_ws_authorize_session")
    assert "from auth.session_access import authorize_session" in h, "WS helper 须委托统一 session RBAC SSOT"
    assert "return authorize_session(user, session_id)" in h, "WS helper 不得绕过统一 session RBAC"
    assert "SELECT brand_id FROM diagnosis_records WHERE session_id" in SESSION_ACCESS, \
        "权威模块须反查 diagnosis_records 取 brand"
    assert "SELECT owner_user_id FROM brands WHERE id" in SESSION_ACCESS, "权威模块须比对 brand owner"
    assert "return False  # 无 run 且无 record" in SESSION_ACCESS, "未知 session 须拒(防猜测枚举)"


def test_731_both_ws_endpoints_guarded():
    assert SERVER.count("_ws_authorize_session(user, session_id)") == 2, "两个 WS 端点都须调归属校验"
    assert SERVER.count("code=4003") >= 2, "不匹配须 close(4003)"


# ---------- #13 Fable 返修(2026-06-10)----------

def test_13_archives_pushes_filter_not_full_table():
    """[#13 返修] archives 无 brand_id:admin 全量;非 admin 有授权 brand → 逐 brand 下推
    list_archives(bid)(不再 list_archives(None) 读全租户);无授权 → 空。"""
    b = _ep("list_archives_endpoint")
    assert "list_archives(None)" in b, "admin 仍可全量"
    assert "for _abid in allowed:" in b, "非 admin 须按授权 brand 逐个下推过滤"
    assert "list_archives(_abid)" in b, "逐 brand 调既有 list_archives(带 WHERE brand_id)"
    # list_archives(None) 只能出现在 admin(allowed is None)分支,不在非 admin 路径
    assert b.find("allowed is None") < b.find("list_archives(None)"), "list_archives(None) 须在 admin 分支内"


def test_13_config_client_id_bypass_closed():
    """[#13 返修] config 端点 brand_id=None + 具体 client_id(实为 quote_id)→ 须 require_quote_access
    校验归属(原只校验 brand_id → client_id 旁路越权读他人配置)。"""
    b = _ep("get_monitoring_config")
    assert 'client_id != "_global_"' in b, "须区分全局 vs 具体 client_id"
    assert "require_quote_access(request, _cfg_qid)" in b, "具体 client_id 须按 quote 归属校验"


def _run_ws_auth(diag_row, uid, brand_owner=None, client_match=False, is_admin=False):
    """exec 提取 _ws_authorize_session + 假 db.connection(按 SQL 分流返回),跑归属校验。
    diag_row=diagnosis_records 行(只含 brand_id);brand_owner=brands.owner_user_id;client_match=user_clients 命中。"""
    import sys
    import types
    import re
    import textwrap
    m = re.search(r"(def _ws_authorize_session\(user[\s\S]*?)\n\n@app\.websocket", SERVER)
    assert m, "未定位 _ws_authorize_session"
    src = m.group(1)

    class _Cur:
        def execute(self, sql, params=None):
            self._sql = sql

        def fetchone(self):
            if "diagnosis_records" in self._sql:
                return diag_row
            if "FROM brands" in self._sql:
                return {"owner_user_id": brand_owner} if brand_owner is not None else None
            if "user_clients" in self._sql:
                return (1,) if client_match else None
            return None

        def close(self):
            pass

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

    fake = types.ModuleType("db.connection")
    fake.get_connection = lambda: _Conn()
    old = sys.modules.get("db.connection")
    sys.modules["db.connection"] = fake
    try:
        ns = {}
        exec(textwrap.dedent(src), ns)
        return ns["_ws_authorize_session"]({"user_id": uid, "is_admin": is_admin}, "sess-x")
    finally:
        if old is not None:
            sys.modules["db.connection"] = old
        else:
            sys.modules.pop("db.connection", None)


def test_13_ws_brandless_fail_closed():
    """[#13 返修v2 · Fable P0] 无 brand 临时诊断 → fail-closed 拒(原 fail-open 越权 + 虚构列假绿根因)。
    无可靠归属对象(brand owner / user_clients 都基于 brand_id)→ 不放行。"""
    assert _run_ws_auth({"brand_id": None}, uid=46) is False, "无 brand 必须 fail-closed"
    assert _run_ws_auth({"brand_id": None}, uid=99) is False
    assert _run_ws_auth(None, uid=46) is False, "未知 session 拒"


def test_13_ws_with_brand_owner_paths():
    """[#13 返修v2] 有 brand 时归属基于【确定存在的】brands.owner_user_id + user_clients(非诊断表列)。"""
    assert _run_ws_auth({"brand_id": 9}, uid=46, brand_owner=46) is True, "brand owner 本人放行"
    assert _run_ws_auth({"brand_id": 9}, uid=99, brand_owner=46, client_match=False) is False, "非 owner 无分配拒"
    assert _run_ws_auth({"brand_id": 9}, uid=99, brand_owner=46, client_match=True) is True, "有 user_clients 分配放行"


def test_13_ws_db_error_fail_closed():
    """[#13 返修v3 · Fable] DB 异常(池耗尽/连接断)→ fail-CLOSED return False(原 except return True
    是后门:构造 DB 故障即可绕过越权订阅)。admin 在 DB 前 return True 不受影响。"""
    import sys
    import types
    import re
    import textwrap
    m = re.search(r"(def _ws_authorize_session\(user[\s\S]*?)\n\n@app\.websocket", SERVER)
    src = m.group(1)
    fake = types.ModuleType("db.connection")
    fake.get_connection = lambda: (_ for _ in ()).throw(RuntimeError("pool exhausted"))
    old = sys.modules.get("db.connection")
    sys.modules["db.connection"] = fake

    class _L:
        def warning(self, *a, **k):
            pass

        def info(self, *a, **k):
            pass

    try:
        ns = {"logger": _L()}
        exec(textwrap.dedent(src), ns)
        assert ns["_ws_authorize_session"]({"user_id": 7, "is_admin": False}, "s") is False, "DB 故障须 fail-closed"
        assert ns["_ws_authorize_session"]({"user_id": 1, "is_admin": True}, "s") is True, "admin DB 前放行不受影响"
    finally:
        if old is not None:
            sys.modules["db.connection"] = old
        else:
            sys.modules.pop("db.connection", None)


def test_13_ws_except_returns_false_not_open():
    """[#13 返修v3] 源级:外层 except 不得 return True(fail-open 后门)。"""
    marker = "except Exception as e:\n        # DB 异常 fail-CLOSED"
    start = SESSION_ACCESS.find(marker)
    _exc = SESSION_ACCESS[start:] if start >= 0 else ""
    assert _exc, "未定位 except 块"
    assert "return False" in _exc, "except 须 fail-closed return False"
    assert "return True" not in _exc, "except 不得 return True(fail-open 后门)"


def test_13_ws_no_diagnosis_user_column_dependency():
    """[#13 返修v2] WS SELECT 不得取 diagnosis_records 的 user 归属列 —— 本机声明 owner_user_id 但 prod
    \\d 实证只有 operator_user_id(列名相反),取任一都 UndefinedColumn → 被 except 吞成 fail-open(P0)。"""
    # 针对实际下发的 SQL 字面量(不误命中注释里的列名)
    assert "SELECT brand_id FROM diagnosis_records WHERE session_id" in SESSION_ACCESS, \
        "diagnosis_records SELECT 只取 brand_id"
    assert "owner_user_id FROM diagnosis_records" not in SESSION_ACCESS, \
        "WS SELECT 不得取 owner_user_id(prod 无此列)"
    assert "operator_user_id FROM diagnosis_records" not in SESSION_ACCESS, \
        "WS SELECT 不得赌 operator_user_id(未 prod 实证前不依赖)"
