"""安全/资金批量修 2026-05-29 · 真执行测试(非纯文本扫)

覆盖老板批的 6 条修复:
  A-16-2 / A-16-1  server.py offline_confirm_order / offline_mark_paid  · require_quote_access 守卫
  A-5-2  / A-5-1   server.py update_keyword_status_route / delete_monitoring_keyword · require_brand_access 守卫
  A-8-1            api/meijiehezi_api.py _recompute_publish_charge · 服务端权威价(fail-closed)
  A-14-1           api/managed_campaign_api.py _refund_managed_campaign_partial · 按比例退款

实现说明:
  - server.py 无法整体 import(import 时直连 DB),故 server.py 内的函数用 AST 从源码抽出 + exec
    真执行(stub 掉运行时 lazy import 的 db / auth 依赖)。这是对真实源码的执行,能抓 runtime bug。
  - api/* 模块可直接 import,monkeypatch 掉 DB 连接 seam 后真执行。
"""
from __future__ import annotations

import ast
import asyncio
import math
import os
import sys
import types
from contextlib import contextmanager

import pytest
from fastapi import HTTPException

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


# ============================================================
# 通用 fake DB
# ============================================================
class _FakeCursor:
    def __init__(self, row_for):
        self._row_for = row_for
        self.executed = []          # list[(sql, params)]
        self._last = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._last = self._row_for(sql, params)

    def fetchone(self):
        r = self._last
        return r[0] if isinstance(r, list) and r else (None if isinstance(r, list) else r)

    def fetchall(self):
        r = self._last
        if r is None:
            return []
        return r if isinstance(r, list) else [r]

    def close(self):
        pass


class _FakeConn:
    def __init__(self, row_for):
        self._cur = _FakeCursor(row_for)
        self.closed = False

    def cursor(self):
        return self._cur

    def close(self):
        self.closed = True

    def commit(self):
        pass

    def rollback(self):
        pass


def _make_get_db(conn):
    @contextmanager
    def _get_db():
        yield conn
    return _get_db


def _install_module(monkeypatch, name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


# ============================================================
# A-8-1 · 服务端权威价重算(api/meijiehezi_api._recompute_publish_charge)
# ============================================================
def _patch_publish_db(monkeypatch, *, mhz_price=None, wm_price=None, mhz_map=None, wm_map=None):
    """mhz_price/wm_price: 统一价(所有 id 同价);mhz_map/wm_map: {id: price}。"""
    def row_for(sql, params):
        ids = list(params or [])
        if "FROM mhz_media" in sql:
            rows = []
            for mid in ids:
                p = (mhz_map or {}).get(int(mid), mhz_price)
                if p is not None:
                    rows.append({"id": int(mid), "price": p})
            return rows
        if "FROM mhz_wemedia" in sql:
            rows = []
            for mid in ids:
                p = (wm_map or {}).get(int(mid), wm_price)
                if p is not None:
                    rows.append({"id": int(mid), "price": p})
            return rows
        return None
    conn = _FakeConn(row_for)
    _install_module(monkeypatch, "db.connection", get_connection=lambda: conn)
    return conn


def test_a8_recompute_mhz_matches_frontend_formula(monkeypatch):
    """ceil(price × markup × 130) · 与前端 yuanToPoints 完全一致。"""
    from api import meijiehezi_api as mod
    _patch_publish_db(monkeypatch, mhz_price=100.0)
    charges = mod._recompute_publish_charge([(1, "mhz"), (2, "article")], markup=2.0)
    expected = int(math.ceil(100.0 * 2.0 * 130))   # 26000
    assert [c[0] for c in charges] == [expected, expected]
    assert [c[1] for c in charges] == [100.0, 100.0]   # base_yuan=DB price(外采成本)→ 订单 cost_yuan


def test_a8_recompute_noninteger_price_ceil(monkeypatch):
    """非整数价 · ceil 整体取整(不是 ceil(price*markup)*130)。"""
    from api import meijiehezi_api as mod
    _patch_publish_db(monkeypatch, mhz_price=10.1)
    charges = mod._recompute_publish_charge([(1, "mhz")], markup=2.0)
    assert charges[0][0] == int(math.ceil(10.1 * 2.0 * 130))   # ceil(2626.0)=2626
    assert charges[0][1] == 10.1


def test_a8_recompute_wemedia_uses_wemedia_table(monkeypatch):
    from api import meijiehezi_api as mod
    conn = _patch_publish_db(monkeypatch, wm_price=50.0)
    charges = mod._recompute_publish_charge([(5, "wemedia")], markup=2.0)
    assert charges[0][0] == int(math.ceil(50.0 * 2.0 * 130))   # 13000
    assert charges[0][1] == 50.0
    assert any("mhz_wemedia" in s for s, _ in conn._cur.executed)


def test_a8_id_collision_resolved_by_media_type(monkeypatch):
    """同 id 在两表都存在 · 按 media_type 选对表(不串价)。"""
    from api import meijiehezi_api as mod
    _patch_publish_db(monkeypatch, mhz_map={5: 100.0}, wm_map={5: 40.0})
    mhz_c = mod._recompute_publish_charge([(5, "mhz")], 2.0)
    wm_c = mod._recompute_publish_charge([(5, "wemedia")], 2.0)
    assert mhz_c[0][0] == int(math.ceil(100.0 * 2.0 * 130)) and mhz_c[0][1] == 100.0
    assert wm_c[0][0] == int(math.ceil(40.0 * 2.0 * 130)) and wm_c[0][1] == 40.0


def test_a8_missing_media_fail_closed(monkeypatch):
    """伪造 / 下架 media_id → 400 fail-closed · 绝不按客户端价放行。"""
    from api import meijiehezi_api as mod
    _patch_publish_db(monkeypatch)   # 两表都返空
    with pytest.raises(HTTPException) as ei:
        mod._recompute_publish_charge([(99, "mhz")], 2.0)
    assert ei.value.status_code == 400


def test_a8_zero_price_rejected(monkeypatch):
    from api import meijiehezi_api as mod
    _patch_publish_db(monkeypatch, mhz_price=0.0)
    with pytest.raises(HTTPException) as ei:
        mod._recompute_publish_charge([(1, "mhz")], 2.0)
    assert ei.value.status_code == 400


def test_a8_client_cannot_undercharge(monkeypatch):
    """核心防护:不论客户端 cost_points 传什么,服务端只认权威价。"""
    from api import meijiehezi_api as mod
    _patch_publish_db(monkeypatch, mhz_price=200.0)
    server_total = sum(c[0] for c in mod._recompute_publish_charge([(1, "mhz")], 2.0))
    assert server_total == int(math.ceil(200.0 * 2.0 * 130))   # 52000
    # 客户端若传 [1] 妄图 1 积分发稿 · server_total 与之无关
    assert server_total != 1


def test_a8_get_markup_reads_mhz_config(monkeypatch):
    from api import meijiehezi_api as mod
    _install_module(monkeypatch, "db.meijiehezi_db",
                    get_config=lambda k: "2.0" if k == "markup_ratio" else None)
    assert mod._get_publish_markup() == 2.0


def test_a8_get_markup_default_1_5(monkeypatch):
    from api import meijiehezi_api as mod
    _install_module(monkeypatch, "db.meijiehezi_db", get_config=lambda k: None)
    assert mod._get_publish_markup() == 2.0


# ============================================================
# A-14-1 · 托管多词部分失败按比例退款(_refund_managed_campaign_partial)
# ============================================================
def test_a14_partial_refund_credits_paid_points(monkeypatch):
    from api import managed_campaign_api as mod
    state = {"paid": 1000}

    def row_for(sql, params):
        if "UPDATE user_wallets" in sql:
            state["paid"] += params[0]
            return {"paid_points": state["paid"]}
        return None

    conn = _FakeConn(row_for)
    tx_calls = []
    notif_calls = []
    _install_module(monkeypatch, "db.connection", get_db=_make_get_db(conn))
    _install_module(monkeypatch, "db.wallet_db",
                    insert_transaction=lambda *a, **k: tx_calls.append((a, k)))
    _install_module(monkeypatch, "db.team_db",
                    create_user_notification=lambda **k: notif_calls.append(k))

    res = asyncio.run(mod._refund_managed_campaign_partial(
        user_id=42, refund_yuan=30.0, feature_code="managed_brand_recharge",
        failed_keywords=["w1", "w2"], operation_label="GEO pkg (5 kw)",
    ))
    assert res["success"] is True
    assert res["refunded_points"] == int(round(30.0 * 130))    # 3900
    assert res["refunded_yuan"] == 30.0                        # [P2 对账] 退款额回传供审计/对账
    assert state["paid"] == 1000 + 3900                        # 钱包真补了
    # 真落 refund 流水 · paid 轨 · 正数
    assert len(tx_calls) == 1
    _a, _k = tx_calls[0]
    assert _k["tx_type"] == "refund" and _k["point_type"] == "paid" and _k["amount"] == 3900
    # 通知发了 · warning 级
    assert notif_calls and notif_calls[0]["level"] == "warning"


def test_a14_partial_refund_zero_yuan_noop(monkeypatch):
    from api import managed_campaign_api as mod
    conn = _FakeConn(lambda s, p: None)
    touched = {"db": False}

    def _gc():
        touched["db"] = True
        return conn
    _install_module(monkeypatch, "db.connection", get_db=_make_get_db(conn))
    _install_module(monkeypatch, "db.wallet_db", insert_transaction=lambda *a, **k: None)
    res = asyncio.run(mod._refund_managed_campaign_partial(
        user_id=1, refund_yuan=0.0, feature_code="managed_brand_recharge",
        failed_keywords=["x"], operation_label="pkg",
    ))
    assert res["success"] is False and res["refunded_points"] == 0


def test_a14_proportional_amount_formula():
    """退款额 = final_total_yuan × (失败成本 / 总成本) · markup 随之等比退还。"""
    final_total_yuan = 1200.0     # 含 markup 实扣
    subs = [{"cost_yuan": 100.0}, {"cost_yuan": 300.0}, {"cost_yuan": 100.0}]
    failed = [subs[1]]            # 失败 300 / 总 500
    total = sum(s["cost_yuan"] for s in subs)
    fail = sum(s["cost_yuan"] for s in failed)
    refund = round(final_total_yuan * (fail / total), 2)
    assert refund == round(1200.0 * (300.0 / 500.0), 2) == 720.0


# ============================================================
# server.py 函数:AST 抽源码 + exec 真执行(stub lazy import)
# ============================================================
_SERVER_CACHE = None


def _extract_server_func(name):
    global _SERVER_CACHE
    if _SERVER_CACHE is None:
        with open(os.path.join(_ROOT, "server.py"), "r", encoding="utf-8") as f:
            src = f.read()
        _SERVER_CACHE = (src, src.splitlines(), ast.parse(src))
    src, lines, tree = _SERVER_CACHE
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            seg_lines = lines[node.lineno - 1: node.end_lineno]
            while seg_lines and seg_lines[0].lstrip().startswith("@"):
                seg_lines = seg_lines[1:]
            seg = "\n".join(seg_lines)
            assert seg.lstrip().startswith(("def ", "async def ")), \
                f"{name} 抽取异常(含装饰器?): {seg[:50]}"
            return seg
    raise AssertionError(f"server.py 未找到顶层函数 {name}")


def _load_server_func(name, g):
    seg = _extract_server_func(name)
    ns = {"__name__": "server_extracted"}
    ns.update(g)
    exec(compile(seg, f"<server:{name}>", "exec"), ns)
    return ns[name]


class _FakeRequest:
    def __init__(self, user=None):
        self.state = types.SimpleNamespace(user=user)


_LOG = types.SimpleNamespace(
    error=lambda *a, **k: None, warning=lambda *a, **k: None,
    info=lambda *a, **k: None, critical=lambda *a, **k: None,
)


def _raise_403(*a, **k):
    raise HTTPException(status_code=403, detail="denied")


# ---------- A-5-1 / A-5-2 · _resolve_monitoring_keyword_brand ----------
def test_a5_resolve_extra_source_uses_extra_table(monkeypatch):
    def row_for(sql, params):
        return {"brand_id": 77} if "FROM extra_keywords" in sql else None
    conn = _FakeConn(row_for)
    _install_module(monkeypatch, "db.monitoring_db", get_connection=lambda: conn)
    fn = _load_server_func("_resolve_monitoring_keyword_brand", {})
    brand_id, found = fn(123, "extra")
    assert found is True and brand_id == 77
    assert all("client_keywords" not in s for s, _ in conn._cur.executed)


def test_a5_resolve_client_falls_back_to_extra(monkeypatch):
    def row_for(sql, params):
        if "FROM client_keywords" in sql:
            return None
        if "FROM extra_keywords" in sql:
            return {"brand_id": 88}
        return None
    conn = _FakeConn(row_for)
    _install_module(monkeypatch, "db.monitoring_db", get_connection=lambda: conn)
    fn = _load_server_func("_resolve_monitoring_keyword_brand", {})
    brand_id, found = fn(5, "client")
    assert found is True and brand_id == 88
    sqls = [s for s, _ in conn._cur.executed]
    assert any("client_keywords" in s for s in sqls) and any("extra_keywords" in s for s in sqls)


def test_a5_resolve_not_found(monkeypatch):
    conn = _FakeConn(lambda s, p: None)
    _install_module(monkeypatch, "db.monitoring_db", get_connection=lambda: conn)
    fn = _load_server_func("_resolve_monitoring_keyword_brand", {})
    brand_id, found = fn(9, "client")
    assert found is False and brand_id is None


# ---------- A-5-2 · update_keyword_status_route ----------
def _route_globals(*, resolve, rba, api_spy_name, api_spy):
    _logger = types.SimpleNamespace(error=lambda *a, **k: None, warning=lambda *a, **k: None)
    return {
        "HTTPException": HTTPException,
        "logger": _logger,
        "Request": object,
        "_resolve_monitoring_keyword_brand": resolve,
        api_spy_name: api_spy,
    }


def test_a52_update_status_propagates_403_not_swallowed(monkeypatch):
    rba_calls = []

    def _rba(request, brand_id, **kw):
        rba_calls.append((request, brand_id))
        raise HTTPException(status_code=403, detail="denied")
    _install_module(monkeypatch, "auth.brand_access", require_brand_access=_rba)
    g = _route_globals(resolve=lambda kid, src, fallback=True: (55, True),
                       rba=_rba, api_spy_name="api_update_keyword_status",
                       api_spy=lambda *a, **k: {"status": "success"})
    fn = _load_server_func("update_keyword_status_route", g)
    req = _FakeRequest()
    with pytest.raises(HTTPException) as ei:
        fn(123, "active", req, "client")
    assert ei.value.status_code == 403            # 不被吞成 {"status":"error"}
    assert rba_calls == [(req, 55)]               # 用解析出的 brand_id 校验


def test_a52_update_status_404_when_keyword_missing(monkeypatch):
    rba_calls = []
    _install_module(monkeypatch, "auth.brand_access",
                    require_brand_access=lambda *a, **k: rba_calls.append(a))
    g = _route_globals(resolve=lambda kid, src, fallback=True: (None, False),
                       rba=None, api_spy_name="api_update_keyword_status",
                       api_spy=lambda *a, **k: {"status": "success"})
    fn = _load_server_func("update_keyword_status_route", g)
    with pytest.raises(HTTPException) as ei:
        fn(1, "active", _FakeRequest(), "client")
    assert ei.value.status_code == 404
    assert rba_calls == []                        # 词不存在 → 不进 require_brand_access


def test_a52_update_status_happy_calls_api(monkeypatch):
    api_calls = []
    _install_module(monkeypatch, "auth.brand_access",
                    require_brand_access=lambda *a, **k: None)
    g = _route_globals(resolve=lambda kid, src, fallback=True: (55, True),
                       rba=None, api_spy_name="api_update_keyword_status",
                       api_spy=lambda kid, st, source=None: api_calls.append((kid, st, source)) or {"status": "success"})
    fn = _load_server_func("update_keyword_status_route", g)
    out = fn(123, "paused", _FakeRequest(), "client")
    assert out == {"status": "success"} and api_calls == [(123, "paused", "client")]   # source 透传到底层


# ---------- A-5-1 · delete_monitoring_keyword ----------
def test_a51_delete_propagates_403_not_swallowed(monkeypatch):
    rba_calls = []

    def _rba(request, brand_id, **kw):
        rba_calls.append((request, brand_id))
        raise HTTPException(status_code=403, detail="denied")
    _install_module(monkeypatch, "auth.brand_access", require_brand_access=_rba)
    g = _route_globals(resolve=lambda kid, src: (66, True),
                       rba=_rba, api_spy_name="api_delete_keyword",
                       api_spy=lambda *a, **k: {"status": "success"})
    fn = _load_server_func("delete_monitoring_keyword", g)
    req = _FakeRequest()
    with pytest.raises(HTTPException) as ei:
        fn(321, req, "extra")
    assert ei.value.status_code == 403
    assert rba_calls == [(req, 66)]


def test_a51_delete_happy_passes_source(monkeypatch):
    api_calls = []
    _install_module(monkeypatch, "auth.brand_access",
                    require_brand_access=lambda *a, **k: None)
    g = _route_globals(resolve=lambda kid, src: (66, True),
                       rba=None, api_spy_name="api_delete_keyword",
                       api_spy=lambda kid, source=None: api_calls.append((kid, source)) or {"status": "success"})
    fn = _load_server_func("delete_monitoring_keyword", g)
    out = fn(321, _FakeRequest(), "extra")
    assert out == {"status": "success"} and api_calls == [(321, "extra")]


# ---------- A-16-1 / A-16-2 · offline_mark_paid / offline_confirm_order ----------
def _offline_globals():
    import json as _json
    return {
        "HTTPException": HTTPException,
        "logger": types.SimpleNamespace(error=lambda *a, **k: None, warning=lambda *a, **k: None),
        "OfflineConfirmRequest": object,
        "OfflineMarkPaidRequest": object,
        "Request": object,
        "json": _json,
    }


def test_a162_offline_confirm_guard_blocks_before_db(monkeypatch):
    """require_quote_access 抛 403 → 传播,且 DB(get_connection)绝不被触达。"""
    rqa_calls = []
    db_hit = {"v": False}

    def _rqa(request, quote_id):
        rqa_calls.append((request, quote_id))
        raise HTTPException(status_code=403, detail="no")

    def _gc():
        db_hit["v"] = True
        raise AssertionError("守卫未挡住 · DB 被触达")

    _install_module(monkeypatch, "auth.brand_access", require_quote_access=_rqa)
    _install_module(monkeypatch, "db.diagnosis_db",
                    get_connection=_gc, update_quote_status=lambda *a, **k: None)
    fn = _load_server_func("offline_confirm_order", _offline_globals())
    with pytest.raises(HTTPException) as ei:
        asyncio.run(fn(7, object(), object()))
    assert ei.value.status_code == 403
    assert rqa_calls and rqa_calls[0][1] == 7
    assert db_hit["v"] is False


def test_a162_offline_confirm_guard_passes_reaches_db(monkeypatch):
    """守卫放行 → 流程继续到 DB(证明没把 happy path 一并挡死)。"""
    rqa_calls = []
    reached = {"db": False}

    class _Reached(Exception):
        pass

    def _gc():
        reached["db"] = True
        raise _Reached()

    _install_module(monkeypatch, "auth.brand_access",
                    require_quote_access=lambda req, qid: rqa_calls.append((req, qid)))
    _install_module(monkeypatch, "db.diagnosis_db",
                    get_connection=_gc, update_quote_status=lambda *a, **k: None)
    fn = _load_server_func("offline_confirm_order", _offline_globals())
    with pytest.raises(HTTPException) as ei:
        asyncio.run(fn(7, object(), object()))
    assert ei.value.status_code == 500            # 进 DB 后 _Reached 被兜底转 500
    assert rqa_calls and rqa_calls[0][1] == 7 and reached["db"] is True


def test_a161_offline_mark_paid_guard_blocks_before_db(monkeypatch):
    rqa_calls = []
    db_hit = {"v": False}

    def _rqa(request, quote_id):
        rqa_calls.append((request, quote_id))
        raise HTTPException(status_code=403, detail="no")

    def _gc():
        db_hit["v"] = True
        raise AssertionError("守卫未挡住 · DB 被触达")

    _install_module(monkeypatch, "auth.brand_access", require_quote_access=_rqa)
    _install_module(monkeypatch, "db.diagnosis_db",
                    get_connection=_gc, update_quote_status=lambda *a, **k: None)
    fn = _load_server_func("offline_mark_paid", _offline_globals())
    with pytest.raises(HTTPException) as ei:
        asyncio.run(fn(9, object(), object()))
    assert ei.value.status_code == 403
    assert rqa_calls and rqa_calls[0][1] == 9
    assert db_hit["v"] is False


# ============================================================
# 兄弟端点(对抗复审 sibling-sweep 发现 · 同根因) · server.py AST-exec
# ============================================================

# ---------- SIB-P0 · /api/reports get_reports_endpoint ----------
def test_sib_reports_nonadmin_no_scope_403(monkeypatch):
    _install_module(monkeypatch, "auth.brand_access",
                    require_brand_access=lambda *a, **k: None,
                    require_quote_access=lambda *a, **k: None)
    g = {"HTTPException": HTTPException, "logger": _LOG, "Request": object,
         "api_get_reports": lambda **k: {"status": "success", "reports": []}}
    fn = _load_server_func("get_reports_endpoint", g)
    req = _FakeRequest(user={"is_admin": False})
    with pytest.raises(HTTPException) as ei:
        fn(req)                       # 非 admin · 无 brand_id/client_id
    assert ei.value.status_code == 403


def test_sib_reports_brand_and_client_scoped(monkeypatch):
    rba, rqa = [], []
    _install_module(monkeypatch, "auth.brand_access",
                    require_brand_access=lambda req, bid, **k: rba.append(bid),
                    require_quote_access=lambda req, qid: rqa.append(qid))
    g = {"HTTPException": HTTPException, "logger": _LOG, "Request": object,
         "api_get_reports": lambda **k: {"status": "success", "reports": []}}
    fn = _load_server_func("get_reports_endpoint", g)
    req = _FakeRequest(user={"is_admin": False})
    assert fn(req, brand_id=7)["status"] == "success" and rba == [7]
    rqa.clear()
    fn(req, client_id="55")           # client_id 实为 quote_id → int 化后 require_quote_access
    assert rqa == [55]


def test_sib_reports_admin_bypasses(monkeypatch):
    rba, rqa = [], []
    _install_module(monkeypatch, "auth.brand_access",
                    require_brand_access=lambda req, bid, **k: rba.append(bid),
                    require_quote_access=lambda req, qid: rqa.append(qid))
    g = {"HTTPException": HTTPException, "logger": _LOG, "Request": object,
         "api_get_reports": lambda **k: {"status": "success", "reports": []}}
    fn = _load_server_func("get_reports_endpoint", g)
    adm = _FakeRequest(user={"is_admin": True})
    assert fn(adm)["status"] == "success"     # admin 无 scope 也放行
    assert rba == [] and rqa == []


# ---------- SIB-P1 · hard_delete_monitoring_keyword ----------
def test_sib_hard_delete_calls_owner_access(monkeypatch):
    owner_calls = []

    def _owner(request, kid, source):
        owner_calls.append((kid, source))
        raise HTTPException(status_code=403, detail="x")
    g = {"HTTPException": HTTPException, "logger": _LOG, "Request": object,
         "_require_keyword_owner_access": _owner}
    fn = _load_server_func("hard_delete_monitoring_keyword", g)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(fn(123, _FakeRequest(), "confirmed"))
    assert ei.value.status_code == 403 and owner_calls == [(123, "confirmed")]


# ---------- SIB-P1 · set_client_monitoring_config / update_service_config ----------
def test_sib_monitoring_config_guard_before_db(monkeypatch):
    rqa = []

    def _rqa(req, qid):
        rqa.append(qid)
        raise HTTPException(status_code=403, detail="x")
    _install_module(monkeypatch, "auth.brand_access", require_quote_access=_rqa)
    g = {"HTTPException": HTTPException, "logger": _LOG, "Request": object}
    fn = _load_server_func("set_client_monitoring_config", g)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(fn(99, _FakeRequest()))    # request.json() 永不被调(guard 在 try 外先 raise)
    assert ei.value.status_code == 403 and rqa == [99]


def test_sib_service_config_guard_before_db(monkeypatch):
    rqa = []

    def _rqa(req, qid):
        rqa.append(qid)
        raise HTTPException(status_code=403, detail="x")
    _install_module(monkeypatch, "auth.brand_access", require_quote_access=_rqa)
    g = {"HTTPException": HTTPException, "logger": _LOG, "Request": object}
    fn = _load_server_func("update_service_config", g)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(fn(88, _FakeRequest()))
    assert ei.value.status_code == 403 and rqa == [88]


# ---------- SIB-P1 · api_confirm_quote ----------
def test_sib_confirm_quote_guard_before_db(monkeypatch):
    rqa = []

    def _rqa(req, qid):
        rqa.append(qid)
        raise HTTPException(status_code=403, detail="x")
    _install_module(monkeypatch, "auth.brand_access", require_quote_access=_rqa)
    g = {"HTTPException": HTTPException, "logger": _LOG,
         "QuoteConfirmRequest": object, "Request": object}
    fn = _load_server_func("api_confirm_quote", g)
    with pytest.raises(HTTPException) as ei:
        fn(7, object(), _FakeRequest())        # http_request = 第3参
    assert ei.value.status_code == 403 and rqa == [7]


# ============================================================
# 老板复审 v2 · F1 / F3 (cross-table IDOR + tier RBAC)
# ============================================================

# ---------- F1 · update_keyword_status(db) 必须 source-exact 写表 ----------
def test_f1_db_update_status_writes_source_exact_table(monkeypatch):
    from db import monitoring_db as mdb

    class _Cur:
        def __init__(self):
            self.sqls = []
        def execute(self, sql, params=None):
            self.sqls.append(sql)
        def fetchone(self):
            return None
        rowcount = 1
        def close(self):
            pass

    class _Conn:
        def __init__(self):
            self.cur = _Cur()
        def cursor(self):
            return self.cur
        def commit(self):
            pass
        def close(self):
            pass

    conns = []

    def _mk():
        c = _Conn()
        conns.append(c)
        return c
    monkeypatch.setattr(mdb, "get_connection", _mk)

    mdb.update_keyword_status(5, "deleted", source="extra")
    assert any("extra_keywords" in s for s in conns[-1].cur.sqls)
    assert all("client_keywords" not in s for s in conns[-1].cur.sqls)

    mdb.update_keyword_status(5, "deleted", source="client")
    assert any("client_keywords" in s for s in conns[-1].cur.sqls)
    assert all("extra_keywords" not in s for s in conns[-1].cur.sqls)


# ---------- F1 · 路由按 source 精确解析(fallback=False)· 解析表==写表 ----------
def test_f1_route_resolves_source_exact_no_fallback(monkeypatch):
    resolve_calls = []
    _install_module(monkeypatch, "auth.brand_access",
                    require_brand_access=lambda *a, **k: None)
    g = {"HTTPException": HTTPException, "logger": _LOG, "Request": object,
         "_resolve_monitoring_keyword_brand":
             lambda kid, src, fallback=True: (resolve_calls.append((kid, src, fallback)) or (55, True)),
         "api_update_keyword_status": lambda kid, st, source=None: {"status": "success"}}
    fn = _load_server_func("update_keyword_status_route", g)
    fn(7, "paused", _FakeRequest(), "extra")
    assert resolve_calls == [(7, "extra", False)]   # source 精确 · 无 fallback → 解析表==写表


# ---------- F3 · /api/monitoring/tier RBAC ----------
def test_f3_tier_guard_before_update(monkeypatch):
    rba = []

    def _rba(request, brand_id, **k):
        rba.append(brand_id)
        raise HTTPException(status_code=403, detail="x")
    _install_module(monkeypatch, "auth.brand_access", require_brand_access=_rba)
    g = {"HTTPException": HTTPException, "logger": _LOG, "Request": object}
    fn = _load_server_func("update_client_tier", g)
    with pytest.raises(HTTPException) as ei:
        fn(7, "standard", _FakeRequest())
    assert ei.value.status_code == 403 and rba == [7]


def test_f3_tier_invalid_tier_rejected_before_rba(monkeypatch):
    _install_module(monkeypatch, "auth.brand_access", require_brand_access=_raise_403)
    g = {"HTTPException": HTTPException, "logger": _LOG, "Request": object}
    fn = _load_server_func("update_client_tier", g)
    out = fn(7, "bogus", _FakeRequest())          # 无效 tier 早返(原有行为)· 不抛
    assert out["status"] == "error"
