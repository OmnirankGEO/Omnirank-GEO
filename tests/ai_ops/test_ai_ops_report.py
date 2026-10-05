"""services/ai_ops/report_builder.py 测试(DB)。"""
from datetime import date

from db import ai_ops_db as aiops_db
from services.ai_ops import report_builder


def test_build_daily_report_creates_row(clean_ai_ops):
    # 造点数据让 metrics 有内容
    aiops_db.create_task(kind="diagnose", title="a")
    t, _ = aiops_db.create_task(kind="ssh_action", title="b", risk_level="L3")
    aiops_db.create_approval(t["id"], "prod_status", risk_level="L3")

    d = date(2026, 7, 1)
    rid = report_builder.build_daily_report(d)
    report = aiops_db.get_report(d)
    assert report is not None
    assert report["status"] == "ready"
    assert "AI 运维与运营日报" in report["markdown"]
    assert isinstance(report["action_items_jsonb"], list)
    assert len(report["action_items_jsonb"]) <= 3
    # 有一条待审批 → action items 里应体现
    assert any("审批" in x for x in report["action_items_jsonb"])
    assert rid == report["id"]


def test_build_daily_report_idempotent(clean_ai_ops):
    d = date(2026, 7, 2)
    r1 = report_builder.build_daily_report(d)
    r2 = report_builder.build_daily_report(d)
    assert r1 == r2                       # (date,type) 幂等,同一行
    assert len(aiops_db.list_reports()) == 1


def test_run_daily_report_job_gated_when_disabled(clean_ai_ops):
    # 默认 ai_ops.enabled=false → 定时日报不生成(P0-2)
    report_builder.run_daily_report_job()
    assert len(aiops_db.list_reports()) == 0


def test_run_daily_report_job_when_enabled(clean_ai_ops):
    aiops_db.set_policy("ai_ops.enabled", {"enabled": True})
    report_builder.run_daily_report_job()
    reports = aiops_db.list_reports()
    assert len(reports) == 1
    assert reports[0]["generated_by_task_id"] is not None   # 走了任务总线(P1-2)


def test_generate_daily_report_with_task(clean_ai_ops):
    task, rid = report_builder.generate_daily_report_with_task(date(2026, 7, 3), source_type="manual")
    assert task["kind"] == "report"
    got_task = aiops_db.get_task(task["id"])
    assert got_task["status"] == "succeeded"
    events = aiops_db.list_events(task["id"])
    assert any(e["event_type"] == "report_generated" for e in events)
    report = aiops_db.get_report(date(2026, 7, 3))
    assert report["generated_by_task_id"] == task["id"]
    assert report["id"] == rid
    # 包B.1 日报体验:任务摘要放日报真实摘要(不是一句 report_id=N);
    # result 带 report_date/report_id,前端「打开日报正文」按钮据此直达
    r_summary = (report["summary"] or "").strip()
    if r_summary:
        assert got_task_final_summary(task["id"]) == r_summary[:400]
    assert f"report_id={rid}" not in got_task_final_summary(task["id"])
    result = aiops_db.get_task(task["id"])["result_jsonb"]
    assert result["report_date"] == "2026-07-03"
    assert result["report_id"] == rid


def got_task_final_summary(task_id: int) -> str:
    return aiops_db.get_task(task_id)["summary"]


# ==========================================
# 运营指标(日报 v2 · report_metrics 包 · brief §5)
# ==========================================

def test_report_v2_segments_all_present_and_fail_soft(clean_ai_ops):
    """测试库没有任何业务表 → 6 段全部如实 unavailable,日报照常 ready(段失败不摧毁整份)。"""
    d = date(2026, 7, 6)
    report_builder.build_daily_report(d)
    report = aiops_db.get_report(d)
    assert report["status"] == "ready"
    m = report["metrics_jsonb"]
    for seg in ("finance", "geo_delivery", "monitoring", "research_monitor", "llm_cost", "publishing"):
        assert seg in m, f"缺段 {seg}"
    assert "## 数据缺口" in report["markdown"]


def test_report_without_recharge_table_fails_soft(clean_ai_ops, pg_conn):
    """测试库默认没有 recharge_orders → 充值子指标如实 unavailable,绝不编数字,日报照常生成。"""
    cur = pg_conn.cursor()
    cur.execute("DROP TABLE IF EXISTS recharge_orders")
    pg_conn.commit()

    d = date(2026, 7, 3)
    report_builder.build_daily_report(d)
    report = aiops_db.get_report(d)
    assert report["status"] == "ready"
    fin = report["metrics_jsonb"]["finance"]
    assert fin["recharge"]["status"] == "unavailable"   # 子指标如实不可用,带原因
    assert fin["recharge"]["error"]
    assert "不可用" in report["markdown"]
    assert "¥" not in report["markdown"]          # 没有任何伪造金额


def test_report_with_recharge_orders_real_metrics(clean_ai_ops, pg_conn):
    """建最小 recharge_orders(列名同 db/wallet_db.py 建表)+ 插真数据 → 日报出真实金额。"""
    cur = pg_conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS recharge_orders (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            amount_cents INTEGER NOT NULL,
            base_points BIGINT NOT NULL DEFAULT 0,
            bonus_points BIGINT NOT NULL DEFAULT 0,
            payment_status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            paid_at TIMESTAMP
        )
    """)
    d = date(2026, 7, 4)
    # 当日 2 笔 paid(¥100.00 + ¥58.88)· 1 笔 pending(不计)· 上月 1 笔 paid(不计当日/当月)
    cur.execute("DELETE FROM recharge_orders")
    cur.execute("INSERT INTO recharge_orders (id, user_id, amount_cents, payment_status, paid_at) "
                "VALUES ('o1', 1, 10000, 'paid', %s)", (d,))
    cur.execute("INSERT INTO recharge_orders (id, user_id, amount_cents, payment_status, paid_at) "
                "VALUES ('o2', 1, 5888, 'paid', %s)", (d,))
    cur.execute("INSERT INTO recharge_orders (id, user_id, amount_cents, payment_status, created_at) "
                "VALUES ('o3', 1, 99999, 'pending', %s)", (d,))
    cur.execute("INSERT INTO recharge_orders (id, user_id, amount_cents, payment_status, paid_at) "
                "VALUES ('o4', 1, 7777, 'paid', %s)", (date(2026, 6, 15),))
    pg_conn.commit()

    try:
        report_builder.build_daily_report(d)
        report = aiops_db.get_report(d)
        rc = report["metrics_jsonb"]["finance"]["recharge"]
        assert rc["day_paid_count"] == 2
        assert rc["day_paid_amount_cents"] == 15888
        assert rc["month_paid_count"] == 2           # 上月 o4 不计入 7 月
        assert "¥158.88" in report["markdown"]        # 真实金额进日报
        assert "今日充值 2 笔" in report["markdown"]  # 一句话总览也带
        # 同段其他子指标(退款/提现/结算)表不存在 → 独立 unavailable,不拖垮充值子指标
        assert report["metrics_jsonb"]["finance"]["refund"]["status"] == "unavailable"
    finally:
        cur.execute("DROP TABLE IF EXISTS recharge_orders")
        pg_conn.commit()


def test_report_v2_finance_pending_actions(clean_ai_ops, pg_conn):
    """提现/结算 pending 有真数据 → 进日报 + 进老板待办(资金优先级最高)。"""
    cur = pg_conn.cursor()
    # 最小列建表(列名同 db/withdrawal_db.py / migration_v35 建表,只建本查询用到的列)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS withdrawal_requests (
            id SERIAL PRIMARY KEY,
            amount_yuan NUMERIC(10,2) NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS agent_settlement_requests (
            id SERIAL PRIMARY KEY,
            request_amount_cents INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("DELETE FROM withdrawal_requests")
    cur.execute("DELETE FROM agent_settlement_requests")
    cur.execute("INSERT INTO withdrawal_requests (amount_yuan, status) VALUES (200.50, 'pending')")
    cur.execute("INSERT INTO withdrawal_requests (amount_yuan, status) VALUES (99.50, 'paid')")   # 不计
    cur.execute("INSERT INTO agent_settlement_requests (request_amount_cents, status) VALUES (30000, 'pending')")
    pg_conn.commit()

    d = date(2026, 7, 7)
    try:
        report_builder.build_daily_report(d)
        report = aiops_db.get_report(d)
        fin = report["metrics_jsonb"]["finance"]
        assert fin["withdrawal"]["pending_count"] == 1
        assert fin["withdrawal"]["pending_amount_yuan"] == 200.50
        assert fin["settlement"]["pending_count"] == 1
        assert fin["settlement"]["pending_amount_cents"] == 30000
        # 资金待办进老板 3 件事,且排第 1(资金 > 其他)
        items = report["action_items_jsonb"]
        assert items and items[0].startswith("资金待办")
        assert "提现待审 1 笔" in items[0]
    finally:
        cur.execute("DROP TABLE IF EXISTS withdrawal_requests")
        cur.execute("DROP TABLE IF EXISTS agent_settlement_requests")
        pg_conn.commit()


def test_report_publishing_counts_mhz_synced_orders(clean_ai_ops, pg_conn):
    """P1-1 返修:现役代发在 mhz_synced_orders(status 整数),日报发布段必须统计到它,
    不能只查已废弃的 publish_orders。构造当日 mhz 数据 → 断言主指标出真实状态分布。"""
    cur = pg_conn.cursor()
    # 最小列建表(列名/类型同 db/meijiehezi_db.py:183 建表)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mhz_synced_orders (
            id TEXT PRIMARY KEY,
            status INTEGER DEFAULT 0,
            media_name TEXT DEFAULT '',
            created_at TIMESTAMP,
            synced_at TIMESTAMP DEFAULT NOW(),
            user_id INTEGER DEFAULT 0
        )
    """)
    cur.execute("DELETE FROM mhz_synced_orders")
    d = date(2026, 7, 8)
    # 当日:2 已完成(status=2) · 1 发布中(1) · 1 拒稿(-1);另 1 条昨日已完成(不计当日)
    cur.execute("INSERT INTO mhz_synced_orders (id, status, created_at) VALUES ('s1', 2, %s)", (d,))
    cur.execute("INSERT INTO mhz_synced_orders (id, status, created_at) VALUES ('s2', 2, %s)", (d,))
    cur.execute("INSERT INTO mhz_synced_orders (id, status, created_at) VALUES ('s3', 1, %s)", (d,))
    cur.execute("INSERT INTO mhz_synced_orders (id, status, created_at) VALUES ('s4', -1, %s)", (d,))
    cur.execute("INSERT INTO mhz_synced_orders (id, status, created_at) VALUES ('s5', 2, %s)", (date(2026, 7, 7),))
    pg_conn.commit()

    try:
        report_builder.build_daily_report(d)
        report = aiops_db.get_report(d)
        ms = report["metrics_jsonb"]["publishing"]["mhz_synced"]
        assert ms["today_orders"] == 4               # 昨日那条不计
        assert ms["today_completed"] == 2
        assert ms["today_in_progress"] == 1          # 发布中
        assert ms["today_rejected"] == 1
        assert ms["by_status"] == {"已完成": 2, "发布中": 1, "已拒稿": 1}
        # 日报 markdown 用现役口径,且带真实状态分布
        assert "今日代发订单(媒介盒子):4" in report["markdown"]
        assert "已完成 2" in report["markdown"]
    finally:
        cur.execute("DROP TABLE IF EXISTS mhz_synced_orders")
        pg_conn.commit()


def test_report_publishing_mhz_missing_fails_soft(clean_ai_ops):
    """测试库没有 mhz_synced_orders → 主指标如实 unavailable,日报照常;绝不静默显示 0 当真数据。"""
    d = date(2026, 7, 9)
    report_builder.build_daily_report(d)
    report = aiops_db.get_report(d)
    assert report["status"] == "ready"
    pub = report["metrics_jsonb"]["publishing"]
    assert pub["mhz_synced"]["status"] == "unavailable"   # 真实链路读不到就如实说
    assert "代发订单(媒介盒子):不可用" in report["markdown"]


def test_report_counts_new_bug_feedback_today(clean_ai_ops, make_bug_feedback, pg_conn):
    """当日新增 bug 反馈数进日报(created_at 归日)。"""
    fid = make_bug_feedback(message="今天报的bug")
    d = date(2026, 7, 5)
    cur = pg_conn.cursor()
    cur.execute("UPDATE faq_feedback SET created_at = %s WHERE id = %s", (d, fid))
    pg_conn.commit()

    report_builder.build_daily_report(d)
    report = aiops_db.get_report(d)
    assert report["metrics_jsonb"]["new_bug_feedback_today"] == 1
    assert "今日新增 bug 反馈:1 条" in report["markdown"]
