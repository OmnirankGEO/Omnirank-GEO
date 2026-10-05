"""
V3.5 W4 · inventory_audit publish 池违规审计闭环 — 端到端测试(2026-06-07 收口)

不变式(services/inventory_audit.py 模块 docstring):
    publish_credit 只能来自 paid_inventory · bonus 永不流入 publish

收口要求(老板 2026-06-07):
  1. publish_violations > 0 必须纳入审计失败条件 has_drift
  2. publish_violations > 0 必须写 inventory_audit_diffs(track='publish' · violation_type=bonus_to_publish)
  3. run_audit 返回值带 publish_violations / publish_overflow_points
  4. 测试覆盖完整流程(不只测 SQL count)

本测试**端到端真跑 run_audit(cursor)**:自建 6 张 temp 表(run_audit 读写的全部表),
RealDictCursor(run_audit 的 _g 只认 dict),用真 PG 验证:
  - BAD1:bonus 流出 > 客户 tool+bonus 收入 且 diff_total=0
          ⟹ has_drift=True · publish_violations=1 · diffs 有 publish/bonus_to_publish 明细
  - OK1 paid-only / OK2 bonus 被 tool+bonus 吸收 / OK3 offline · 零误报 · has_drift=False

无 PG 时优雅 skip(对齐 tests/test_v35_factory_inventory.py)。
本地实证:docker 一次性 postgres:16 真跑 4 用例全过(见 brief)。
"""
import json
import os
import pytest

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.extras  # noqa: E402

from services.inventory_audit import run_audit  # noqa: E402


_TEMP_DDL = """
CREATE TEMP TABLE agent_inventory_wallets (
    agent_user_id integer, paid_inventory_points integer, bonus_inventory_points integer,
    -- [2026-07-29 返修] 新等式把冻结池计入(冻结是池内搬运 · 不写流水)
    frozen_inventory_points integer DEFAULT 0
);
CREATE TEMP TABLE customer_agent_credit_wallets (
    customer_user_id integer, agent_user_id integer,
    tool_credit_points integer, publish_credit_points integer, bonus_credit_points integer
);
CREATE TEMP TABLE agent_inventory_transactions (
    agent_user_id integer, type text, pool text, points integer,
    related_customer_user_id integer, related_order_id text
);
CREATE TEMP TABLE customer_credit_transactions (
    customer_user_id integer, agent_user_id integer, type text, pool text,
    points integer, related_order_id text
);
CREATE TEMP TABLE inventory_audit_runs (
    id bigserial PRIMARY KEY, run_at timestamp DEFAULT now(),
    triggered_by text, triggered_by_user_id integer,
    agent_total_paid bigint, agent_total_bonus bigint,
    customer_total_tool bigint, customer_total_publish bigint, customer_total_bonus bigint,
    platform_consumed bigint, historical_purchased bigint, historical_admin_adjust bigint,
    refunded_or_revoked bigint,
    diff_paid bigint, diff_bonus bigint, diff_publish bigint, has_drift boolean, notes text,
    -- [2026-07-29 返修] 失败态 + 账实相符式两个量(与 migration_inventory_audit_equation_2026_07_29.sql 对齐)
    agent_total_frozen bigint DEFAULT 0,
    wallet_total bigint, ledger_total bigint,
    status text DEFAULT 'ok', error_message text
);
CREATE TEMP TABLE inventory_audit_diffs (
    id bigserial PRIMARY KEY, run_id bigint, track text, diff_points integer, detail jsonb
);
"""


def _conn():
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("无 TEST_DATABASE_URL / DATABASE_URL · 端到端 run_audit 测试跳过(本地实证走一次性 PG)")
    try:
        c = psycopg2.connect(url)
        c.cursor_factory = psycopg2.extras.RealDictCursor
        return c
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"无法连接测试库,跳过: {e}")


def _setup(cur):
    cur.execute(_TEMP_DDL)


def _seed_bad1(cur):
    # 进货 150(paid 100 + bonus 50) → historical_purchased=150
    cur.execute("INSERT INTO agent_inventory_transactions VALUES "
                "(1,'purchase_prepay','paid',100,NULL,'ORD1'),"
                "(1,'purchase_auto','bonus',50,9,'ORD1')")
    # 划拨:paid -100 + bonus -50 全给客户
    cur.execute("INSERT INTO agent_inventory_transactions VALUES "
                "(1,'allocate_to_customer','paid',-100,9,'ORD1'),"
                "(1,'allocate_to_customer','bonus',-50,9,'ORD1')")
    # 客户侧:tool 5 + bonus 5 (nonpub=10) + publish 140 → bonus 50 中 40 溢入 publish = 违规
    cur.execute("INSERT INTO customer_credit_transactions VALUES "
                "(9,1,'allocate','tool',5,'ORD1'),"
                "(9,1,'allocate','bonus',5,'ORD1'),"
                "(9,1,'allocate','publish',140,'ORD1')")
    # wallet 余额(划拨后 agent 清零 · 客户 tool5/publish140/bonus5 = 150)
    cur.execute("INSERT INTO agent_inventory_wallets VALUES (1,0,0)")
    cur.execute("INSERT INTO customer_agent_credit_wallets VALUES (9,1,5,140,5)")


def test_bad1_violation_fails_audit_even_when_diff_total_zero():
    conn = _conn()
    try:
        cur = conn.cursor()
        _setup(cur)
        _seed_bad1(cur)
        result = run_audit(cur, triggered_by="test_bad1")

        # 金额总差为 0,但 publish 违规必须让审计失败
        assert result["diff_paid"] == 0, f"diff_total 应为 0,实际 {result['diff_paid']}"
        assert result["has_drift"] is True, "publish 违规必须纳入 has_drift"
        assert result["publish_violations"] == 1, f"应检出 1 个违规,实际 {result['publish_violations']}"
        assert result["publish_overflow_points"] == 40, f"溢出点数应为 40,实际 {result['publish_overflow_points']}"
        assert "ORD1" in result["publish_violation_orders"]

        # 必须写 inventory_audit_diffs(track='publish' · bonus_to_publish · 不只 notes/日志)
        cur.execute(
            "SELECT track, diff_points, detail FROM inventory_audit_diffs "
            "WHERE run_id = %s AND track = 'publish'", (result["run_id"],)
        )
        drow = cur.fetchone()
        assert drow is not None, "publish 违规必须写 inventory_audit_diffs 明细"
        detail = drow["detail"]
        if isinstance(detail, str):
            detail = json.loads(detail)
        assert detail["violation_type"] == "bonus_to_publish"
        assert detail["violation_count"] == 1
        assert detail["overflow_points"] == 40
        assert "ORD1" in detail["affected_order_ids"]
        assert drow["diff_points"] == 40
    finally:
        conn.rollback()
        conn.close()


def test_ok1_paid_only_no_false_positive():
    conn = _conn()
    try:
        cur = conn.cursor()
        _setup(cur)
        cur.execute("INSERT INTO agent_inventory_transactions VALUES "
                    "(1,'purchase_prepay','paid',100,NULL,'ORD1'),"
                    "(1,'allocate_to_customer','paid',-100,9,'ORD1')")
        cur.execute("INSERT INTO customer_credit_transactions VALUES "
                    "(9,1,'allocate','tool',60,'ORD1'),(9,1,'allocate','publish',40,'ORD1')")
        cur.execute("INSERT INTO agent_inventory_wallets VALUES (1,0,0)")
        cur.execute("INSERT INTO customer_agent_credit_wallets VALUES (9,1,60,40,0)")
        result = run_audit(cur, triggered_by="test_ok1")
        assert result["publish_violations"] == 0, "paid-only 合法不应误报"
        assert result["has_drift"] is False, "全合法且 diff_total=0 应 has_drift=False"
    finally:
        conn.rollback()
        conn.close()


def test_ok2_bonus_absorbed_no_false_positive():
    conn = _conn()
    try:
        cur = conn.cursor()
        _setup(cur)
        cur.execute("INSERT INTO agent_inventory_transactions VALUES "
                    "(1,'purchase_prepay','paid',100,NULL,'ORD1'),"
                    "(1,'purchase_auto','bonus',50,9,'ORD1'),"
                    "(1,'allocate_to_customer','paid',-100,9,'ORD1'),"
                    "(1,'allocate_to_customer','bonus',-50,9,'ORD1')")
        cur.execute("INSERT INTO customer_credit_transactions VALUES "
                    "(9,1,'allocate','tool',80,'ORD1'),(9,1,'allocate','bonus',50,'ORD1'),"
                    "(9,1,'allocate','publish',20,'ORD1')")
        cur.execute("INSERT INTO agent_inventory_wallets VALUES (1,0,0)")
        cur.execute("INSERT INTO customer_agent_credit_wallets VALUES (9,1,80,20,50)")
        result = run_audit(cur, triggered_by="test_ok2")
        # bonus_out=50 全被客户 tool+bonus(130)吸收 → 不违规
        assert result["publish_violations"] == 0, "bonus 被 tool+bonus 吸收不应误报"
        assert result["has_drift"] is False
    finally:
        conn.rollback()
        conn.close()


def test_ok3_offline_no_false_positive():
    conn = _conn()
    try:
        cur = conn.cursor()
        _setup(cur)
        cur.execute("INSERT INTO agent_inventory_transactions VALUES "
                    "(1,'purchase_prepay','bonus',30,NULL,'ORD1'),"
                    "(1,'allocate_to_customer_offline','bonus',-30,9,'ORD1')")
        cur.execute("INSERT INTO customer_credit_transactions VALUES "
                    "(9,1,'allocate','bonus',30,'ORD1')")
        cur.execute("INSERT INTO agent_inventory_wallets VALUES (1,0,0)")
        cur.execute("INSERT INTO customer_agent_credit_wallets VALUES (9,1,0,0,30)")
        result = run_audit(cur, triggered_by="test_ok3")
        # offline bonus 30 全被客户 bonus 30 吸收 → 不违规
        assert result["publish_violations"] == 0, "offline 合法不应误报"
        assert result["has_drift"] is False
    finally:
        conn.rollback()
        conn.close()
