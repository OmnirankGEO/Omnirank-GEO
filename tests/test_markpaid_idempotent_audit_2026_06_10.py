# -*- coding: utf-8 -*-
"""#17(audit · 2026-06-10):mark_paid 幂等 / fail-closed。

根治:原先函数最前就把 session 翻转 active,再"尽力"同步关键词;同步(save_* 纯 INSERT 非幂等)
抛异常仅 log 不回滚 → session active 但 confirmed_keywords 空/不全 → 再调 mark_paid 被入口 400 拒
→ 永久空交付(收款了却没关键词)。修复:状态翻转后移到同步成功之后 + 同步失败 fail-closed raise
(保 pending_payment 可重试)+ 同步前 clear 残留幂等(防重试重复落库)。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEL = (ROOT / "api" / "selection_api.py").read_text(encoding="utf-8")


def _mark_paid_block() -> str:
    i = SEL.find("async def mark_paid(")
    assert i >= 0, "未找到 mark_paid"
    j = SEL.find("\n@router.", i + 10)
    j2 = SEL.find("\nasync def ", i + 10)
    ends = [x for x in (j, j2) if x > 0]
    return SEL[i:(min(ends) if ends else len(SEL))]


def test_session_active_flip_after_sync():
    """session active 翻转须在关键词同步(clear+重建)成功之后,不再在函数最前。
    [#12 返修v3] 翻转改用 _ml_cur 同事务 UPDATE(原 update_session 另开连接会自死锁)。"""
    blk = _mark_paid_block()
    i_clear = blk.find("_clear_quote_pending_keywords_and_clusters(session")
    i_flip = blk.find("UPDATE keyword_selection_sessions SET status = 'active'")
    assert i_clear > 0, "同步前须有幂等 clear"
    assert i_flip > 0, "须有 session active 翻转(_ml_conn 同事务)"
    assert i_flip > i_clear, "session active 翻转须在关键词同步之后(同步成功才翻转)"


def test_only_one_active_flip():
    """active 翻转只剩一处(原函数最前那处已删)。[#12 返修v3] 翻转用 _ml_cur UPDATE。"""
    assert _mark_paid_block().count("UPDATE keyword_selection_sessions SET status = 'active'") == 1, \
        "session active 翻转只保留同步成功后一处(_ml_conn 同事务)"
    assert 'update_session(token, status="active"' not in _mark_paid_block(), \
        "禁用 update_session 翻 active(另开连接→自死锁)"


def test_sync_failure_fail_closed_raise():
    """同步失败 fail-closed raise 500(不再静默 log 留 active 僵尸空交付)。"""
    blk = _mark_paid_block()
    assert "fail-closed" in blk, "同步 except 须 fail-closed"
    assert "status_code=500" in blk, "同步失败须 raise 500 可重试"
    assert "创建写作任务失败" not in blk, "原静默 log「创建写作任务失败」须改为 raise"


def test_idempotent_clear_before_sync():
    """同步前 clear 残留(幂等)· clear helper 只删 pending confirmed + 本 quote clusters · 清失败 raise。"""
    assert "_clear_quote_pending_keywords_and_clusters(session" in _mark_paid_block()
    i = SEL.find("def _clear_quote_pending_keywords_and_clusters")
    assert i > 0, "缺 clear helper"
    h = SEL[i:i + 1500]
    assert "DELETE FROM confirmed_keywords WHERE quote_id = %s AND status = 'pending'" in h, \
        "只删 pending confirmed_keywords(不误删正式态)"
    assert "DELETE FROM keyword_clusters" in h, "清本 quote clusters"
    assert "status <> 'pending'" in h, "clusters DELETE 须只删无 writing/published 引用(红队 P2 守卫)"
    # 先删 confirmed_keywords(引用 cluster)再删 clusters,避免 FK 冲突
    assert h.find("DELETE FROM confirmed_keywords") < h.find("DELETE FROM keyword_clusters"), \
        "须先删 confirmed_keywords 再删 clusters(FK 顺序)"
    assert "raise" in h, "清失败须 raise(fail-closed·不带残留重建)"


def test_clear_helper_defined_before_mark_paid():
    """helper 在 mark_paid 之前定义(运行时可调用)。"""
    i_h = SEL.find("def _clear_quote_pending_keywords_and_clusters")
    i_m = SEL.find("async def mark_paid(")
    assert 0 < i_h < i_m, "helper 须在 mark_paid 之前定义"


def test_return_shape_unchanged():
    """返回体不变(前端兼容):success/status/keywords_created/clusters_created。"""
    blk = _mark_paid_block()
    assert '"success": True' in blk and '"status": "active"' in blk, "成功返回体须保持兼容"


# ============================================================
# #12 Fable 返修(2026-06-10)
# ============================================================
def test_clear_helper_topics_fk_guard_behavior():
    """[#12 返修 · 行为] clear 的 keyword_clusters DELETE 必须同时避开两条 FK 子表
    (confirmed_keywords + topics),否则有 topics 引用的 cluster 删父撞 topics_cluster_id_fkey
    永久 500。exec 提取函数 + mock 连接截获实际下发 SQL 与参数(本机无 DB 也可跑)。"""
    import sys
    import types
    import textwrap

    i = SEL.find("def _clear_quote_pending_keywords_and_clusters")
    j = SEL.find('@router.post("/keyword-selection/{token}/mark-paid")', i)
    assert 0 < i < j, "未定位 clear helper 源"
    src = SEL[i:j]

    executed = []

    class _Cur:
        def execute(self, sql, params=None):
            executed.append((sql, params))

        def fetchone(self):
            return None

        def fetchall(self):
            return []

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
        ns = {}
        exec(textwrap.dedent(src), ns)
        ns["_clear_quote_pending_keywords_and_clusters"](123)
    finally:
        if old is not None:
            sys.modules["db.diagnosis_db"] = old
        else:
            sys.modules.pop("db.diagnosis_db", None)

    cl = [(s, p) for (s, p) in executed if "DELETE FROM keyword_clusters" in s]
    assert cl, "未发出 keyword_clusters DELETE"
    sql, params = cl[0]
    assert "FROM confirmed_keywords" in sql, "须避 confirmed_keywords FK"
    assert "FROM topics" in sql, "[#12] 须避 topics FK(topics.cluster_id → keyword_clusters)"
    assert params and len(params) == 3, f"两条 NOT IN + 主 WHERE = 3 个 quote_id 参数,实际 {params}"
    # 先删 confirmed_keywords(子)再删 keyword_clusters(父)
    order = [s for (s, _) in executed]
    assert any("DELETE FROM confirmed_keywords" in s for s in order)


def test_uqs_pushed_after_sync_success():
    """[#12 返修] quote.status='confirmed' 推送(_uqs)须在关键词同步成功之后:
    位于同步 fail-closed raise 500 之后、session 翻转之前 → 同步失败时 quote.status 不残留
    confirmed 空交付(prod quote 268)。"""
    blk = _mark_paid_block()
    i_raise = blk.find("status_code=500")
    i_uqs = blk.find("update_quote_status as _uqs")
    i_flip = blk.find("UPDATE keyword_selection_sessions SET status = 'active'")
    assert i_raise > 0 and i_uqs > 0 and i_flip > 0
    assert i_uqs > i_raise, "_uqs 推送须在同步失败 raise 之后(同步成功才走到)"
    assert i_uqs < i_flip, "_uqs 推送须在 session 翻转之前(quote.status 与 session 同进退)"
    assert blk.count("update_quote_status as _uqs") == 1, "_uqs 推送只剩同步成功后一处(旧位置已删)"


def test_advisory_lock_serializes_mark_paid():
    """[#12 返修 P2 + v2 老板升必须] mark_paid 须 pg_advisory_lock(quote_id) 串行化 + session 行
    FOR UPDATE 双保险 + 锁内读权威 status 幂等重检,finally 显式 unlock(闸全量放行→并发安全顶格)。"""
    blk = _mark_paid_block()
    assert "pg_advisory_lock" in blk, "须 advisory lock 串行化防双跑"
    assert "pg_advisory_unlock" in blk, "finally 须显式释放 advisory lock"
    # [#12 返修v2] session 行 FOR UPDATE 双保险
    assert "FROM keyword_selection_sessions WHERE token = %s FOR UPDATE" in blk, "须对 session 行 FOR UPDATE 行锁"
    i_lock = blk.find("pg_advisory_lock")
    i_forupdate = blk.find("FOR UPDATE")
    i_recheck = blk.find('_locked_status == "active"')
    assert 0 < i_lock < i_forupdate, "FOR UPDATE 须在 advisory lock 之后(锁内读)"
    assert 0 < i_forupdate < i_recheck, "持锁后读权威 status 做幂等判定(并发第二 runner 阻塞→读 active→幂等返回)"


def test_12_session_flip_same_conn_no_deadlock():
    """[#12 返修v3 · Fable 自死锁修] session active 翻转必须用 _ml_cur(持 FOR UPDATE 行锁的同一连接
    同一事务),【不得】调 update_session ——后者另开池连接 UPDATE 同一 session 行 → 等本连接 FOR UPDATE
    行锁 → 单请求自死锁(PG 检测不到[应用层等]· timeout=0 永久挂 → 池耗尽 mark_paid 全瘫)。
    本测试结构性证伪该死锁模式(真并发/真行锁运行级验证需 Deploy-CTO/审计方在有 DB 环境跑)。"""
    blk = _mark_paid_block()
    assert "UPDATE keyword_selection_sessions SET status = 'active'" in blk, "session 翻转须 _ml_conn 同事务直 UPDATE"
    # 翻转的 UPDATE 与 FOR UPDATE 同一游标 _ml_cur(同连接同事务)
    i_forupdate = blk.find("FOR UPDATE")
    i_flip = blk.find("UPDATE keyword_selection_sessions SET status = 'active'")
    assert 0 < i_forupdate < i_flip, "翻转 UPDATE 须在 FOR UPDATE 之后(同事务)"
    # 锁块内不得再用 update_session 翻 active(另开连接 → 自死锁)
    assert 'update_session(token, status="active"' not in blk, "禁用 update_session 翻 active(另开连接→自死锁)"
    # 翻转语句确属 _ml_cur(同连接);定位翻转前最近的 execute 游标
    _seg = blk[max(0, i_flip - 200):i_flip + 10]
    assert "_ml_cur.execute(" in _seg, "翻转 UPDATE 须由 _ml_cur(持 FOR UPDATE 的连接)执行"


def test_clear_keyword_clusters_topics_guard_source():
    """[#12 返修] 源级双保险:clear helper 的 clusters DELETE 文本含 topics 子查询。"""
    i = SEL.find("def _clear_quote_pending_keywords_and_clusters")
    h = SEL[i:i + 1800]
    assert "SELECT cluster_id FROM topics" in h, "clusters DELETE 须并入 topics FK 守卫"
