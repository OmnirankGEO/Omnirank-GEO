# -*- coding: utf-8 -*-
"""P2 批3(audit · 2026-06-10):
#9(无成本半截)每日订阅监测提问口径对齐 monitoring_query(豆包模式/search_mode 不动·成本不变)
#16 选词会话回退跨签约/收款步骤 → 复位服务生命周期 + 取消监测订阅 + fail-closed"""
import inspect
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# ---------- #9 提问口径 ----------

def test_9_run_detection_accepts_monitoring_query():
    import api.monitoring_api as m
    sig = inspect.signature(m.run_detection_for_keyword)
    assert "monitoring_query" in sig.parameters, "run_detection_for_keyword 必须接 monitoring_query"


def test_9_question_prefers_custom_query():
    src = (ROOT / "api" / "monitoring_api.py").read_text(encoding="utf-8")
    i = src.find("async def run_detection_for_keyword")
    blk = src[i:i + 2200]
    assert '_custom_q = (monitoring_query or "").strip()' in blk
    assert 'question = _custom_q or f"{keyword}哪家好' in blk, "自定义 query 优先,缺失才回落默认句式"


def test_9_no_doubao_mode_change():
    """老板拍板豆包不切:本批禁止动 search_mode / standard→enhanced / 豆包模式(成本不变)。"""
    src = (ROOT / "api" / "monitoring_api.py").read_text(encoding="utf-8")
    i = src.find("async def run_detection_for_keyword")
    blk = src[i:i + 2600]
    # run_detection_for_keyword 内不得新引入 search_mode 切换
    assert "search_mode=" not in blk, "本批不动 search_mode(豆包不切·成本红线)"


def test_9_scheduler_passes_monitoring_query():
    src = (ROOT / "scheduler.py").read_text(encoding="utf-8")
    assert src.count("monitoring_query=kw.get('monitoring_query')") == 2, "daily 两处调用都要透传(扣费链+admin兜底)"


# ---------- #16 会话回退生命周期 ----------

def test_16_revert_resets_service_lifecycle():
    src = (ROOT / "api" / "selection_api.py").read_text(encoding="utf-8")
    i = src.find("def _revert_selection_session")
    blk = src[i:i + 4800]
    # 复位服务锚三字段
    assert "service_status = NULL, service_start_date = NULL, service_end_date = NULL" in blk
    # 取消该 quote 监测订阅(quote 级)
    assert "cancel_subscriptions_by_quote" in blk
    # fail-closed:清理/复位失败不翻状态
    assert "fail-closed" in blk and "中止回退" in blk
    # update_session(翻状态)必须在复位之后
    i_reset = blk.find("service_status = NULL")
    i_flip = blk.find("update_session(token, status=new_status)")
    assert 0 < i_reset < i_flip, "状态翻转必须在服务复位之后"


def test_16_paid_at_not_cleared():
    """收款事实留痕:回退不抹 paid_at/paid_amount(退款走独立流程)。"""
    src = (ROOT / "api" / "selection_api.py").read_text(encoding="utf-8")
    i = src.find("def _revert_selection_session")
    blk = src[i:i + 4800]
    reset_sql = blk[blk.find("UPDATE quotes"):blk.find("WHERE id = %s", blk.find("UPDATE quotes"))]
    assert "paid_at" not in reset_sql and "paid_amount" not in reset_sql


def test_16_quote_level_not_brand_level():
    """取消订阅用 quote 级(不波及同 brand 其他 quote)。"""
    src = (ROOT / "api" / "selection_api.py").read_text(encoding="utf-8")
    i = src.find("def _revert_selection_session")
    blk = src[i:i + 4800]
    assert "cancel_subscriptions_by_quote" in blk
    assert "cancel_subscriptions_by_brand" not in blk, "回退单 quote 不能 brand 级误杀同品牌其他订阅"


# ---------- #8 Fable 返修(2026-06-10):回退业务态守卫 + 连接 try/finally ----------

def _run_revert(quote_status, service_status, paid_at=None):
    """exec 提取 _revert_selection_session + 假依赖,跑真控制流。返回 (kind, obj, sink_sqls, flips)。"""
    import sys
    import types
    from fastapi import HTTPException
    src_full = (ROOT / "api" / "selection_api.py").read_text(encoding="utf-8")
    a = src_full.find("REVERT_MAP = {")
    b = src_full.find('@router.post("/keyword-selection/{token}/revert-status")', a)
    src = src_full[a:b]

    sink = []
    flips = []
    qrow = {"status": quote_status, "service_status": service_status, "paid_at": paid_at}

    class _Cur:
        def execute(self, sql, params=None):
            sink.append((sql, params))

        def fetchone(self):
            return qrow

        def close(self):
            pass

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    class _Logger:
        def info(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

        def error(self, *a, **k):
            pass

    fake_mon = types.ModuleType("db.monitoring_db")
    fake_mon.cancel_subscriptions_by_quote = lambda qid, reason=None: 0
    old = sys.modules.get("db.monitoring_db")
    sys.modules["db.monitoring_db"] = fake_mon
    ns = {
        "get_connection": lambda: _Conn(),
        "HTTPException": HTTPException,
        "logger": _Logger(),
        "update_session": lambda token, **kw: flips.append((token, kw)),
    }
    try:
        exec(src, ns)
        fn = ns["_revert_selection_session"]
        session = {"token": "tok1", "status": "active", "quote_id": 268}
        try:
            result = fn(session)
            return ("ok", result, sink, flips)
        except HTTPException as he:
            return ("http", he, sink, flips)
    finally:
        if old is not None:
            sys.modules["db.monitoring_db"] = old
        else:
            sys.modules.pop("db.monitoring_db", None)


def test_8_revert_blocks_paid_quote():
    """[#8 返修] 已付款报价(quotes.status='paid')一步回退须 409 拒绝,不得 DELETE/翻状态。"""
    kind, obj, sink, flips = _run_revert("paid", None)
    assert kind == "http" and obj.status_code == 409, f"paid 报价回退须 409,得到 {kind}/{obj}"
    assert not any("DELETE FROM confirmed_keywords" in s for s, _ in sink), "拒绝后绝不 DELETE confirmed_keywords"
    assert not flips, "拒绝后绝不翻转 session 状态"


def test_8_revert_blocks_active_service():
    """[#8 返修] 服务进行中(service_status='active')一步回退须 409 拒绝。"""
    kind, obj, sink, flips = _run_revert(None, "active")
    assert kind == "http" and obj.status_code == 409, f"active 服务回退须 409,得到 {kind}/{obj}"
    assert not any("DELETE FROM confirmed_keywords" in s for s, _ in sink)
    assert not flips


def test_8_revert_blocks_confirmed_delivery():
    """[#8 返修v2 · Fable] 玩法B 交付单(status='confirmed' + service_status='pending' · prod 156/124)
    携带履约数据,旧守卫(只挡 paid/active)漏挡 → 一步回退铲平在交付客户。confirmed 必须 409 拒。"""
    kind, obj, sink, flips = _run_revert("confirmed", "pending")
    assert kind == "http" and obj.status_code == 409, f"confirmed 交付单回退须 409,得到 {kind}/{obj}"
    assert not any("DELETE FROM confirmed_keywords" in s for s, _ in sink), "拒绝后绝不 DELETE confirmed_keywords"
    assert not flips


def test_8_revert_blocks_paid_at_set():
    """[#8 返修v2] paid_at 非空(收款留痕)即使 status 异常也须拒(资金留痕的单不能一步回退)。"""
    kind, obj, sink, flips = _run_revert("quoted", None, paid_at="2026-06-01 10:00:00")
    assert kind == "http" and obj.status_code == 409, f"paid_at 非空回退须 409,得到 {kind}/{obj}"
    assert not flips


def test_8_revert_allows_truly_unpaid_and_resets():
    """[#8 返修v2] 真正未付款/未确认(status='quoted' · 无 paid_at · 无 active)仍可回退:
    清 confirmed_keywords + 复位服务锚 + 翻 quoted。(原测试用 confirmed 当'放行'固化了洞,已纠正)。"""
    kind, obj, sink, flips = _run_revert("quoted", None, paid_at=None)
    assert kind == "ok", f"真正未付款报价应允许回退,得到 {kind}/{obj}"
    assert any("DELETE FROM confirmed_keywords" in s for s, _ in sink), "允许时须清 confirmed_keywords"
    assert any("service_status = NULL" in s for s, _ in sink), "允许时须复位服务锚"
    assert flips and flips[0][1].get("status") == "quoted", "允许时翻转到 quoted"


def test_8_revert_db_block_has_rollback_close():
    """[#8 返修 P3] revert DB 块 except 须 rollback + finally close(防连接泄漏 + in_failed_state)。"""
    src = (ROOT / "api" / "selection_api.py").read_text(encoding="utf-8")
    i = src.find("def _revert_selection_session")
    blk = src[i:i + 4800]
    assert "conn.rollback()" in blk, "DB 块失败须 rollback"
    assert "conn.close()" in blk, "DB 块须 finally close 归还连接"
