# -*- coding: utf-8 -*-
"""P2 批4(audit · 2026-06-10):
#1 P0-6 校验前置(防 paid+active 0 关键词僵尸订单;原钉一键激活端点,G3a 删后改钉线下标记收款)
#6 quote_numeric_repair 读路径不回填候选池 quote(防 B1 置 0 总价被 markdown 回填污染)"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / "server.py").read_text(encoding="utf-8")
REPAIR = (ROOT / "services" / "quote_numeric_repair.py").read_text(encoding="utf-8")


# [开源 E3 · WO_323 G3a · 2026-10-02] #1 原钉在一键激活端点;该端点随 G3a 删。同一个 P0-6 前置修复
#   (#9 返修)在役落在线下标记收款 offline_mark_paid —— 三格改钉在它的函数体内,判据不变:
#   0 词拒绝必须发生在 UPDATE 激活之前,关键词同步也在 UPDATE 之前,不留 paid+active 0 词僵尸。
def _offline_mark_paid_block() -> str:
    i = SERVER.find("async def offline_mark_paid(")
    assert i > 0, "offline_mark_paid 不见了 —— 判据失去对象"
    j = SERVER.find("\n@app.", i)
    return SERVER[i:j] if j > 0 else SERVER[i:]


def test_1_p06_check_before_activation():
    """P0-6 关键词校验(NO_KEYWORDS_FOUND 拒绝)必须在 UPDATE quotes 激活之前 → 拒绝时无僵尸。"""
    block = _offline_mark_paid_block()
    i_p06 = block.find("P0-6 关键词校验【前置】到激活之前")
    assert i_p06 > 0, "P0-6 前置块缺失"
    i_raise = block.find('"code": "NO_KEYWORDS_FOUND"', i_p06)
    i_update = block.find("UPDATE quotes SET status = 'paid'", i_p06)
    assert 0 < i_p06 < i_raise < i_update, "P0-6 拒绝 raise 必须在 UPDATE quotes 激活之前(否则留 paid+active 0 词僵尸)"


def test_1_no_duplicate_sync_after_activation():
    """激活后不再有第二次「同步 + 校验」;全文件只剩这一处 NO_KEYWORDS_FOUND(一键激活那处随端点删)。"""
    block = _offline_mark_paid_block()
    i_update = block.find("UPDATE quotes SET status = 'paid'")
    assert i_update > 0
    assert "sync_quote_keywords_to_confirmed" not in block[i_update:], "激活之后又同步关键词 = 退回「激活后才校验」"
    assert SERVER.count('"code": "NO_KEYWORDS_FOUND"') == 1


def test_1_sync_before_update_in_source_order():
    """sync_quote_keywords_to_confirmed 调用位置在 UPDATE quotes 之前。"""
    block = _offline_mark_paid_block()
    i_sync = block.find("_pre_kw, _ = sync_quote_keywords_to_confirmed(quote_id)")
    i_update = block.find("UPDATE quotes SET status = 'paid'")
    assert 0 < i_sync < i_update, "关键词同步必须在 UPDATE quotes 激活之前"


def test_6_repair_gate_pending_session():
    """有进行中选词会话 → 不落库(只返回展示值);判定 SQL + 落库条件齐全。"""
    assert "_has_pending_session" in REPAIR
    assert "if updates and not _has_pending_session:" in REPAIR
    assert "keyword_selection_sessions WHERE quote_id" in REPAIR
    # 落库被 gate 收窄(不再裸 if updates:)
    assert REPAIR.count("if updates:") == 0, "裸 if updates: 落库必须被 session gate 收窄"


def test_6_repair_status_set_covers_candidate_pool():
    """gate 的会话状态集覆盖 B1 候选池态(quoted/pricing_pending_review/adding_keywords)。"""
    i = REPAIR.find("keyword_selection_sessions WHERE quote_id")
    blk = REPAIR[i:i + 260]
    for st in ("quoted", "pricing_pending_review", "adding_keywords"):
        assert st in blk


# ============================================================
# #9 Fable 返修(2026-06-10)
# ============================================================
def test_9_offline_mark_paid_guard_before_activation():
    """[#9 返修] offline_mark_paid 真源:sync + NO_KEYWORDS_FOUND 拒绝须在 UPDATE quotes SET
    status='paid' 之前 → 拒绝时不留 paid+active 0 词僵尸(prod 327/329 根因之一)。"""
    i_ep = SERVER.find('@app.post("/api/quotes/{quote_id}/offline-mark-paid")')
    assert i_ep > 0
    blk = SERVER[i_ep:i_ep + 3500]
    i_sync = blk.find("sync_quote_keywords_to_confirmed(quote_id)")
    i_reject = blk.find('"code": "NO_KEYWORDS_FOUND"')
    i_update = blk.find("UPDATE quotes SET status = 'paid'")
    assert 0 < i_sync < i_update, "sync 须在 UPDATE paid 之前"
    assert 0 < i_reject < i_update, "0 词拒绝须在 UPDATE paid 之前(否则留僵尸)"


# [开源 E3 · WO_323 G3a · 2026-10-02] test_9_already_active_self_heal 退役:「已激活 0 词重跑同步自愈」只存在于
#   一键激活端点的幂等分支里(全文件唯一一处),端点随 G3a 删;端点不许回来由
#   tests/oss_e3a_routers_2026_09_28/test_e3a_routers_retired.py 的 G3A_RETIRED 锁接替。


def _run_repair(pending: bool):
    """exec 整个 repair 模块源 + 预置 sys.modules 假 db.diagnosis_db(本机无 DB · import 会 eager 连库,
    故不能直接 import;exec 后 fn 内 `from db.diagnosis_db import get_connection` 读到假模块)。"""
    import sys
    import types

    class _Cur:
        def execute(self, sql, params=None):
            pass

        def fetchone(self):
            return (1,) if pending else None

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

    fake = types.ModuleType("db.diagnosis_db")
    fake.get_connection = lambda: _Conn()
    old = sys.modules.get("db.diagnosis_db")
    sys.modules["db.diagnosis_db"] = fake
    try:
        ns: dict = {}
        exec(compile(REPAIR, "quote_numeric_repair.py", "exec"), ns)
        ns["_package_numbers"] = lambda cur, qid, tier: (5800, 10)  # 候选池 quote 的包价
        q = {"id": 7, "tier": "standard", "monthly_price": 0, "total_articles": 0, "markdown": ""}
        return ns["repair_quote_numeric_fields"](7, quote=dict(q))
    finally:
        if old is not None:
            sys.modules["db.diagnosis_db"] = old
        else:
            sys.modules.pop("db.diagnosis_db", None)


def test_9_repair_pending_session_no_headline_price():
    """[#9 返修 行为] repair 显示层:有进行中选词会话 → 返回值不回显 markdown 回算的虚高价
    (对齐 B1 候选池总价不显 headline);无会话才回显 repaired。"""
    res = _run_repair(pending=True)
    assert res["monthly_price"] == 0, "pending session 时不回显 markdown 回算虚高价(应返候选池 0)"
    assert res["updated"] is False, "pending session 不落库"

    res2 = _run_repair(pending=False)
    assert res2["monthly_price"] == 5800, "无 pending session 应回显 repaired 价"


def test_9_zombie_inventory_script_exists():
    """[#9 返修] 存量僵尸单(paid+active+0词)盘点脚本存在且口径正确。"""
    p = ROOT / "scripts" / "zombie_paid_active_no_keywords_2026_06_10.py"
    assert p.exists(), "缺存量僵尸单盘点脚本"
    s = p.read_text(encoding="utf-8")
    assert "status = 'paid'" in s and "service_status = 'active'" in s
    assert "NOT EXISTS" in s and "confirmed_keywords" in s
