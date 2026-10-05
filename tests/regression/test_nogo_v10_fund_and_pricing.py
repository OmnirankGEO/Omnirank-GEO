"""[v10 · Deploy-CTO 最终返工] 上线前闭环 · 行为级判别测试(fund_recovery / article refund / apply-markup)。

覆盖:
  Item2 文章退款全路径透传 ledger_type(handle_write_window_failure 行为 + 三处 refund 调用源码锁)。
  Item3 refund_status 唯一口径(classify/record_action/should_reverse 纯函数)+ defer_recovery_order(不累计 retry)。
  Item5 channel_revenue 工单 exactly-once 唯一键(v11 F6:干净则建键强制;有重复则不静默折叠·退化 app 层幂等)。
  Item1 apply-markup 根治(只重定价 canonical 零售包；平台模板绝不自动 materialize)。
渠道收益 record/reverse 真机器(item4/item6)在 tests/pricing_ssot。判别性(删修复→转红)见各 docstring。
"""
from __future__ import annotations
import sys
import asyncio
from pathlib import Path
import pytest
import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _dbsafe import resolve_test_db_url, require_destructive_allowed, _assert_safe_test_db  # noqa: E402

DB = resolve_test_db_url()
pytestmark = pytest.mark.skipif(not DB, reason="需 TEST_DATABASE_URL(本机 test/throwaway 库)")


def _conn():
    c = psycopg2.connect(DB); c.cursor_factory = psycopg2.extras.RealDictCursor
    return c


@pytest.fixture(autouse=True)
def _guard():
    require_destructive_allowed(); _assert_safe_test_db(DB)
    yield


# ============================================================
# Item3 · refund_status 唯一口径(纯函数)
# ============================================================

def test_refund_status_classification_partition():
    """🔴 [item3] refund_status 4 分区唯一口径:款未退(可记不冲)/在途(等)/已生效(不记要冲)/未知(fail-closed)。
    删/改分区表 → 转红。"""
    from services.channel_revenue_lifecycle import (
        classify_refund_status, record_action, should_reverse_on_refund,
        CLASS_REVENUE_OWED, CLASS_IN_FLIGHT, CLASS_REFUND_EFFECTIVE, CLASS_UNKNOWN,
    )
    # 款未退(订单成立·收益仍欠)
    for rs in (None, "", "none", "rejected", "failed"):
        assert classify_refund_status(rs) == CLASS_REVENUE_OWED, rs
        assert record_action(rs) == "record", rs
        assert should_reverse_on_refund(rs) is False, rs
    # 在途(等)· channel_refunding = [v11 P2] 虎皮椒 RD 退款中(专属 IN_FLIGHT 值·不复用平台 pending)
    for rs in ("pending", "approved", "channel_refunding"):
        assert classify_refund_status(rs) == CLASS_IN_FLIGHT, rs
        assert record_action(rs) == "defer", rs
        assert should_reverse_on_refund(rs) is False, rs
    # 已生效(不记·要冲)
    for rs in ("processed", "completed", "pending_review"):
        assert classify_refund_status(rs) == CLASS_REFUND_EFFECTIVE, rs
        assert record_action(rs) == "skip", rs
        assert should_reverse_on_refund(rs) is True, rs
    # 未知(fail-closed:不记不冲)
    for rs in ("weird_state", "REFUNDING", "xyz"):
        assert classify_refund_status(rs) == CLASS_UNKNOWN, rs
        assert record_action(rs) == "unknown", rs
        assert should_reverse_on_refund(rs) is False, rs
    # 大小写归一
    assert classify_refund_status("COMPLETED") == CLASS_REFUND_EFFECTIVE


# ============================================================
# Item3 · defer_recovery_order 不累计 retry_count
# ============================================================

def test_defer_recovery_order_never_escalates_manual():
    """🔴 [item3] 退款在途 → defer_recovery_order 只推 next_retry_at·【绝不累计 retry_count】→ 6+ 轮仍不转 manual。
    删 defer(改用 requeue)→ retry_count 累计 → 6 轮后转 manual → 转红。"""
    from db.fund_recovery_db import (init_fund_recovery_tables, create_recovery_order,
                                     claim_next_recovery_order, defer_recovery_order, get_recovery_order,
                                     MAX_AUTO_ATTEMPTS)
    with _conn() as c:
        init_fund_recovery_tables(c.cursor()); c.commit()
        cur = c.cursor(); cur.execute("DELETE FROM fund_recovery_orders"); c.commit()
    oid = create_recovery_order("channel_revenue", "record", ref_key="ord-defer-1",
                                reason="t", payload={"order_id": "ord-defer-1"}, status="pending")
    # 模拟 MAX+3 轮 defer:每轮 claim→defer(需把 next_retry_at 拨回以便再次 claim)
    for _ in range(MAX_AUTO_ATTEMPTS + 3):
        with _conn() as c:
            cur = c.cursor()
            cur.execute("UPDATE fund_recovery_orders SET next_retry_at=NOW()-interval '1s' WHERE id=%s", (oid,))
            c.commit()
        wo = claim_next_recovery_order("t")
        assert wo and wo["id"] == oid
        assert defer_recovery_order(oid, wo.get("claim_token"), note="在途延后") is True
    row = get_recovery_order(oid)
    assert row["status"] == "pending", "🔴 defer 后必回 pending(不转 manual)"
    assert (row.get("retry_count") or 0) == 0, "🔴 defer 绝不累计 retry_count(否则会误触 MAX→manual)"


# ============================================================
# Item5 · channel_revenue 工单 exactly-once 唯一键
# ============================================================

def test_channel_revenue_init_never_silently_resolves_dupes():
    """🔴 [v11 F6] init 侧【禁止静默把未终结工单改 resolved】:seed 2 条同 (source,ref_key,kind) 未终结工单 →
    init 探测到重复 → 【不折叠·不建唯一键】(退化 app 层 ON CONFLICT 幂等)· 两条仍原样 open。
    若退回旧的"静默 pre-dedup 折叠" → 其中一条变 resolved(只剩 1 open)→ 转红。"""
    from db.fund_recovery_db import init_fund_recovery_tables
    with _conn() as c:
        init_fund_recovery_tables(c.cursor()); c.commit()
        cur = c.cursor(); cur.execute("DELETE FROM fund_recovery_orders")
        cur.execute("DROP INDEX IF EXISTS uniq_fund_recovery_channel_open")
        for _ in range(2):
            cur.execute("INSERT INTO fund_recovery_orders (source,ref_key,kind,status,reason) "
                        "VALUES ('channel_revenue','ord-dup-noresolve','record','pending','dup')")
        c.commit()
    # init 再跑 → 探测到重复 → 不折叠、不建键(绝不静默 resolve)
    with _conn() as c:
        init_fund_recovery_tables(c.cursor()); c.commit()
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM fund_recovery_orders WHERE source='channel_revenue' "
                    "AND ref_key='ord-dup-noresolve' AND kind='record' AND status='pending'")
        assert cur.fetchone()["n"] == 2, "🔴 init 绝不静默折叠重复未终结工单(两条须原样保留 · 交人工 v10b 备份去重)"
        # 有重复时唯一键不得建(否则说明发生了静默折叠)
        cur.execute("SELECT COUNT(*) AS n FROM pg_indexes WHERE indexname='uniq_fund_recovery_channel_open'")
        assert cur.fetchone()["n"] == 0, "🔴 存在重复时不得建唯一键(建了=已静默折叠)"
        cur.execute("DELETE FROM fund_recovery_orders"); c.commit()   # 清理:不把重复+缺键状态泄漏给后续测试


def test_channel_revenue_unique_index_enforces_when_clean():
    """🔴 [item5] 无重复(生产真实态:该能力从未部署)→ init 建部分唯一键 → 阻止新重复。
    干净 init → insert 同 (source,ref_key,kind) 第二次 ON CONFLICT DO NOTHING 返 None;不同 kind 可另建。
    删唯一键 → 第二次不再去重 → 转红。"""
    from db.fund_recovery_db import init_fund_recovery_tables, insert_recovery_order_cursor
    with _conn() as c:
        cur = c.cursor(); cur.execute("DELETE FROM fund_recovery_orders"); c.commit()
        init_fund_recovery_tables(c.cursor()); c.commit()   # 干净 → 建唯一键
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM pg_indexes WHERE indexname='uniq_fund_recovery_channel_open'")
        assert cur.fetchone()["n"] == 1, "🔴 无重复时必建唯一键"
        wid = insert_recovery_order_cursor(cur, "channel_revenue", "record", ref_key="ord-uniq-1",
                                           reason="first", payload={"order_id": "ord-uniq-1"})
        assert wid is not None, "首条 record 应建成"
        wid_dup = insert_recovery_order_cursor(cur, "channel_revenue", "record", ref_key="ord-uniq-1",
                                               reason="again", payload={"order_id": "ord-uniq-1"})
        assert wid_dup is None, "🔴 同 (source,ref_key,kind) 未终结工单已存在 → 唯一键 + ON CONFLICT 去重返 None"
        wid2 = insert_recovery_order_cursor(cur, "channel_revenue", "reverse", ref_key="ord-uniq-1",
                                            reason="rev", payload={"order_id": "ord-uniq-1"})
        assert wid2 is not None, "reverse 是不同 kind → 允许另一条"
        c.commit()


# ============================================================
# Item2 · 文章退款全路径透传 ledger_type
# ============================================================

def test_write_window_failure_passes_ledger_type():
    """🔴 [item2] handle_write_window_failure 必须把 ledger_type 透传给 refund_fn(退原扣费账本)。
    删透传 → captured['ledger_type'] 为 None → 转红。"""
    from services.article_write_recovery import handle_write_window_failure
    captured = {"ledger_type": "SENTINEL"}

    async def _refund(uid, feature, *, reason=None, charge_tx_id=None, ledger_type=None):
        captured["ledger_type"] = ledger_type
        return {"success": True}

    res = asyncio.run(handle_write_window_failure(
        accepted=[1, 2], thread_started=False, deducted=True,
        bill_user_id=42, charge_tx_id=999, quote_id=7, ledger_type="v35",
        refund_fn=_refund, release_fn=lambda ids, q: None,
        timeout_fn=lambda ids, q: None, recovery_fn=lambda *a, **k: None,
    ))
    assert res["outcome"] == "refunded_and_released"
    assert captured["ledger_type"] == "v35", "🔴 窗口退款必须透传 ledger_type=v35 给 refund_fn"


def test_article_refund_paths_wire_ledger_type_source():
    """🔴 [item2] server.py 三处文章退款(部分失败/线程全失败/窗口)+ 线程前 copy 都必须带 ledger_type。
    源码锁:删任一透传 → 转红。"""
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    assert "_ag_ledger_type_copy = _ag_ledger_type" in src, "🔴 线程前必复制 ledger_type_copy(避免可变闭包)"
    assert "ledger_type=_ag_ledger_type_copy" in src, "🔴 部分失败/线程退款必带 ledger_type_copy"
    assert "ledger_type=_ag_ledger_type," in src, "🔴 handle_write_window_failure 调用必带 ledger_type"
    # 线程退款失败必落耐久工单(不静默)
    assert '"article_gen_thread_fail", "refund"' in src, "🔴 线程内退费失败必落耐久补偿工单(带 ledger_type)"


# ============================================================
# 第1轮对抗审修复 · 判别测试
# ============================================================

def test_processor_passes_amount_for_partial_refund_workorder():
    """🔴 [第1轮 P2] processor 退款分支必须把工单 amount_points 透传给 refund_points(amount=)·
    部分退款工单(如文章部分失败退 _err×base)重试只退指定额不退全额(防 over-refund)。
    删 amount 透传 → captured['amount'] 为 None → 转红。"""
    import services.fund_recovery_processor as P
    from db.fund_recovery_db import init_fund_recovery_tables, create_recovery_order
    captured = {"amount": "SENTINEL"}

    async def _fake_refund(uid, fc, reason="", charge_tx_id=None, amount=None, ledger_type=None):
        captured["amount"] = amount
        return {"success": True}
    import middleware.billing as B
    with _conn() as c:
        init_fund_recovery_tables(c.cursor()); c.commit()
        cur = c.cursor(); cur.execute("DELETE FROM fund_recovery_orders")
        cur.execute("INSERT INTO users (id, username) VALUES (700,'u700') ON CONFLICT (id) DO NOTHING"); c.commit()
    B.refund_points = _fake_refund  # process_pending 内 from middleware.billing import refund_points 取当前属性
    try:
        create_recovery_order("article_gen_partial_refund_fail", "refund", ref_key="q-700",
                              user_id=700, feature_code="article_gen", charge_tx_id=555, amount_points=1170)
        asyncio.run(P.process_pending(limit=5))
    finally:
        import importlib; importlib.reload(B)
    assert captured["amount"] == 1170, "🔴 部分退款工单必须透传 amount_points=1170(否则 over-refund 退全额)"


def test_article_refund_checks_success_return_not_only_exception():
    """🔴 [第2轮 P3] 文章退款(部分失败 + 全失败)必须检查 refund_points 【返回值】success —— refund_points
    用 success=False 报失败(钱包不存在/未找到/v35 路径)不抛异常 → 只 catch 异常会漏这条通道致静默漏退。
    两处调用后都须 raise 路由到耐久补偿。删任一检查 → 转红。"""
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    assert src.count("refund_points 返回未成功") >= 2, \
        "🔴 部分失败 + 全失败两处退款都必须显式检查 refund_points 返回的 success(不能只 catch 异常)"


def test_record_retry_locks_order_row_for_update():
    """🔴 [第1轮 P2] record 补偿重试读订单必须 FOR UPDATE(与退款冲销订单行锁跨事务串行化·防写偏序幽灵行)。
    源码锁:删 FOR UPDATE → 转红。"""
    src = (ROOT / "services" / "fund_recovery_processor.py").read_text(encoding="utf-8")
    assert "SELECT * FROM recharge_orders WHERE id=%s FOR UPDATE" in src, \
        "🔴 record 补偿重试必须 FOR UPDATE 锁订单行(与 canonical reverse 串行化)"


def test_defer_escalates_when_refund_stuck_inflight_long():
    """🔴 [第1轮 P3] 退款长期卡 in-flight(工单 >7d)→ defer 不再无限延后 · escalate 人工(requeue 累计→manual)。
    _defer_too_stale 对 >7d 判 True、对新工单判 False。"""
    from services.fund_recovery_processor import _defer_too_stale
    from datetime import datetime, timezone, timedelta
    assert _defer_too_stale({"created_at": datetime.now(timezone.utc) - timedelta(days=8)}) is True, \
        "🔴 >7d 在途工单必 escalate(不永久静默 defer)"
    assert _defer_too_stale({"created_at": datetime.now(timezone.utc) - timedelta(hours=1)}) is False, \
        "新工单继续正常 defer"
    assert _defer_too_stale({"created_at": None}) is False, "created_at 缺失保守 False(继续 defer)"


# ============================================================
# Item1 · apply-markup 根治(自建最小 schema)
# ============================================================

def _provision_sku_schema(c):
    cur = c.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, username TEXT, agent_sku_markup_ratio NUMERIC);
        CREATE TABLE IF NOT EXISTS customer_agent_bindings (
            id SERIAL PRIMARY KEY,
            customer_user_id INTEGER UNIQUE NOT NULL,
            agent_user_id INTEGER NOT NULL,
            binding_source TEXT,
            source_token TEXT,
            bound_at TIMESTAMPTZ DEFAULT NOW(),
            dispute_status TEXT,
            dispute_note TEXT,
            admin_override_user_id INTEGER,
            admin_override_at TIMESTAMPTZ);
        CREATE TABLE IF NOT EXISTS sku_templates (
            id SERIAL PRIMARY KEY, template_code TEXT UNIQUE, sku_type TEXT,
            default_name TEXT, default_subtitle TEXT, default_capability_pitch TEXT,
            points_granted INTEGER, wholesale_cents INTEGER, suggested_retail_cents INTEGER,
            is_active BOOLEAN DEFAULT TRUE);
        CREATE TABLE IF NOT EXISTS agent_sku_overrides (
            id SERIAL PRIMARY KEY, agent_user_id INTEGER, sku_template_id INTEGER REFERENCES sku_templates(id),
            custom_name TEXT, custom_subtitle TEXT, custom_sales_pitch TEXT, custom_scene TEXT,
            retail_cents INTEGER NOT NULL, is_active BOOLEAN DEFAULT TRUE, sort_order INTEGER DEFAULT 0,
            margin_warning TEXT, admin_approved_at TIMESTAMPTZ, admin_approved_by INTEGER,
            deleted_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW(), updated_at TIMESTAMPTZ DEFAULT NOW());
        ALTER TABLE agent_sku_overrides ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
        ALTER TABLE agent_sku_overrides ADD COLUMN IF NOT EXISTS retail_sku_id TEXT;
        ALTER TABLE agent_sku_overrides ADD COLUMN IF NOT EXISTS source_template_id INTEGER;
        ALTER TABLE agent_sku_overrides ADD COLUMN IF NOT EXISTS points_granted BIGINT;
        ALTER TABLE agent_sku_overrides ADD COLUMN IF NOT EXISTS version INTEGER DEFAULT 1;
        ALTER TABLE agent_sku_overrides ADD COLUMN IF NOT EXISTS client_request_id TEXT;
    """)
    migration = (ROOT / "scripts" / "migration_agent_retail_sku_decoupling_2026_07_17.sql").read_text(
        encoding="utf-8"
    )
    cur.execute(migration)
    # 外科式清理(只删本测试的 · 不碰 pricing_ssot 的 sku_templates 行 · 防跨批污染)
    cur.execute("""
        DELETE FROM agent_sku_overrides
        WHERE agent_user_id = 9001
           OR sku_template_id IN (
                SELECT id FROM sku_templates
                WHERE template_code IN ('cred_a','cred_b','cred_hidden')
           )
           OR source_template_id IN (
                SELECT id FROM sku_templates
                WHERE template_code IN ('cred_a','cred_b','cred_hidden')
           )
    """)
    cur.execute("DELETE FROM sku_templates WHERE template_code IN ('cred_a','cred_b','cred_hidden')")
    cur.execute("INSERT INTO users (id, username) VALUES (9001,'agent9001') ON CONFLICT (id) DO NOTHING")
    cur.execute(
        "INSERT INTO user_wallets (user_id,agent_level) VALUES (9001,1) "
        "ON CONFLICT (user_id) DO UPDATE SET agent_level=EXCLUDED.agent_level"
    )
    # 两个可售默认规格(active + suggested>0)+ 一个不可见规格(suggested=0 · 不该被 materialize)
    cur.execute("""INSERT INTO sku_templates (template_code,sku_type,default_name,points_granted,wholesale_cents,suggested_retail_cents,is_active)
                   VALUES ('cred_a','credit_pack','入门包',130000,100000,150000,TRUE),
                          ('cred_b','credit_pack','进阶包',260000,200000,300000,TRUE),
                          ('cred_hidden','credit_pack','隐藏包',130000,100000,0,TRUE)""")
    c.commit()


def test_apply_markup_never_materializes_platform_templates_for_empty_agent():
    """平台进货模板只是可选预填；批量定价不能替服务商创建零售商品。"""
    from services.agent_pricing import apply_markup_for_agent
    with _conn() as c:
        _provision_sku_schema(c)
        cur = c.cursor()
        res = apply_markup_for_agent(cur, 9001, 12000)  # 1.2
        c.commit()
    assert res == {"created": [], "updated": [], "skipped": []}
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM agent_sku_overrides WHERE agent_user_id=9001")
        assert cur.fetchone()["n"] == 0, "空服务商目录不得从任何平台模板回落或自动生成"


def test_apply_markup_does_not_reactivate_inactive_package_or_create_replacement():
    """下架是服务商事实；批量加价既不修改它，也不从来源模板造替代品。"""
    from services.agent_pricing import apply_markup_for_agent
    with _conn() as c:
        _provision_sku_schema(c)
        cur = c.cursor()
        cur.execute("SELECT id FROM sku_templates WHERE template_code='cred_a'")
        tid = cur.fetchone()["id"]
        # cred_a 造一个 inactive 历史包(客户不可见)
        cur.execute("""INSERT INTO agent_sku_overrides
                       (agent_user_id,sku_template_id,source_template_id,retail_sku_id,
                        points_granted,custom_name,retail_cents,is_active,version)
                       VALUES (9001,%s,%s,'RSKU-REG-INACTIVE-9001',130000,'已下架包',111111,FALSE,1)""",
                    (tid, tid))
        c.commit()
        res = apply_markup_for_agent(cur, 9001, 12000)  # 1.2
        c.commit()
    assert res == {"created": [], "updated": [], "skipped": []}
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT ov.is_active, ov.retail_cents FROM agent_sku_overrides ov "
                    "JOIN sku_templates t ON ov.sku_template_id=t.id "
                    "WHERE ov.agent_user_id=9001 AND t.template_code='cred_a'")
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0]["is_active"] is False and rows[0]["retail_cents"] == 111111


def test_apply_markup_does_not_resurrect_soft_deleted_sku():
    """显式 tombstone 以及其他空模板都不能被批量定价 materialize。"""
    from services.agent_pricing import apply_markup_for_agent
    with _conn() as c:
        _provision_sku_schema(c)
        cur = c.cursor()
        cur.execute("SELECT id FROM sku_templates WHERE template_code='cred_a'")
        tid_a = cur.fetchone()["id"]
        # cred_a:显式软删除(deleted_at 非空)→ 不得 resurrect
        cur.execute("""INSERT INTO agent_sku_overrides
                       (agent_user_id,sku_template_id,source_template_id,retail_sku_id,
                        points_granted,custom_name,retail_cents,is_active,deleted_at,version)
                       VALUES (9001,%s,%s,'RSKU-REG-DELETED-9001',130000,'已删除包',111111,FALSE,NOW(),1)""",
                    (tid_a, tid_a))
        c.commit()
        res = apply_markup_for_agent(cur, 9001, 12000)
        c.commit()
    assert res == {"created": [], "updated": [], "skipped": []}
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM agent_sku_overrides ov JOIN sku_templates t ON ov.sku_template_id=t.id "
                    "WHERE ov.agent_user_id=9001 AND t.template_code='cred_a' AND ov.is_active=TRUE")
        assert cur.fetchone()["n"] == 0, "🔴 cred_a 删除后仍无 active 包(未 resurrect)"


def test_apply_markup_double_click_no_duplicate():
    """重复点击只更新服务商显式创建的 canonical 包，不产生新行。"""
    from services.agent_pricing import apply_markup_for_agent
    with _conn() as c:
        _provision_sku_schema(c)
        cur = c.cursor()
        cur.execute("""INSERT INTO agent_sku_overrides
                       (agent_user_id,retail_sku_id,points_granted,custom_name,
                        retail_cents,is_active,version)
                       VALUES
                       (9001,'RSKU-REG-CANONICAL-A',130000,'自定义 A',100000,TRUE,1),
                       (9001,'RSKU-REG-CANONICAL-B',260000,'自定义 B',200000,TRUE,1)""")
        first = apply_markup_for_agent(cur, 9001, 12000); c.commit()
    assert len(first["created"]) == 0 and len(first["updated"]) == 2
    with _conn() as c:  # 第二次(模拟双击)
        cur = c.cursor()
        res2 = apply_markup_for_agent(cur, 9001, 15000); c.commit()
    assert len(res2["created"]) == 0, "🔴 第二次不得再 materialize(已存在 override)"
    updated = {item["sku"]: item for item in res2["updated"]}
    assert {"自定义 A", "自定义 B"} == updated.keys()
    # No channel relation: effective cost equals canonical root
    # ceil(points * 225 / 325): 90,000 / 180,000;
    # 1.5x therefore produces 135,000 / 270,000 integer cents.
    assert updated["自定义 A"]["retail_cents"] == 135000
    assert updated["自定义 B"]["retail_cents"] == 270000
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM agent_sku_overrides WHERE agent_user_id=9001")
        assert cur.fetchone()["n"] == 2
