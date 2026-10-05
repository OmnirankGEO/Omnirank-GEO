"""[v11 · Deploy-CTO NO-GO 返工] 上线前闭环 · 行为级判别测试(退款工单假完成 / 系数单一口径 / 迁移往返)。

覆盖(生产真实 schema · 判别性:删修复→转红):
  F3 退款工单完成单事务:save_execution_result 对不可完成订单(0 行)必 raise · 不虚假完成(+ api 源码锁)。
  F5 系数唯一 canonical:resolve_canonical_markup(1.234)→ ratio 1.23 · bps 12300(由 canonical 派生非原始输入)。
  F6 v10b 备份去重:折叠前完整备份 + 文档化 rollback 逐行精确恢复(往返 row-identical)。
F1(RD/CD/RD→CD)与 F2(fail-closed)因需 channel_revenue_ledger 真机器 → 放 tests/pricing_ssot。
F4(inactive 物化)在 test_nogo_v10(复用 sku provisioning)。
"""
from __future__ import annotations
import asyncio
import re
import sys
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
# F5 · 系数唯一 canonical(纯函数 · 无 DB)
# ============================================================

def test_resolve_canonical_markup_single_source_of_truth():
    """🔴 [v11 F5] 系数量化成唯一 canonical(2 位)· bps 由 canonical 派生(非原始输入)· 单一口径。
    1.234 → ratio 1.23 · bps 12300(不是原始 1.234→12340)。bps 从原始输入算 → 转红。"""
    from decimal import Decimal
    from services.agent_pricing import resolve_canonical_markup
    r = resolve_canonical_markup(1.234)
    assert r["ratio"] == Decimal("1.23"), f"🔴 canonical 必量化到 2 位 · 实际 {r['ratio']}"
    assert r["bps"] == 12300, f"🔴 bps 必由 canonical 1.23 派生=12300(非原始 1.234→12340)· 实际 {r['bps']}"
    # bps 与 canonical【同源】:bps == int(canonical_ratio*10000)(存储/响应用同一 ratio)
    assert r["bps"] == int(r["ratio"] * Decimal(10000)), "🔴 bps 必与入库/响应的 canonical ratio 同源"
    r2 = resolve_canonical_markup(1.2)
    assert r2["ratio"] == Decimal("1.20") and r2["bps"] == 12000, "常规 1.2 稳定"
    assert resolve_canonical_markup(1.235)["ratio"] == Decimal("1.24"), "ROUND_HALF_UP:1.235→1.24"


# ============================================================
# F3 · 退款工单完成单事务(真实 refund_work_orders + recharge_orders schema)
# ============================================================

def _mk_order_and_wo(oid, refund_status, *, cd_evidence=False, evidence_type=None):
    """生产真实 schema:init_refund_work_order_tables + recharge_orders(补 refund_status 列)+ approved 工单。"""
    from db.refund_work_order_db import init_refund_work_order_tables
    from db.fund_recovery_db import init_fund_recovery_tables
    with _conn() as c:
        cur = c.cursor()
        init_refund_work_order_tables(cur)
        init_fund_recovery_tables(cur)   # reverse 若因缺 ledger 表异常 → 落耐久工单需此表在
        cur.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT)")
        cur.execute("""CREATE TABLE IF NOT EXISTS recharge_orders (
            id TEXT PRIMARY KEY, user_id INTEGER, amount_cents INTEGER,
            base_points BIGINT DEFAULT 0, bonus_points BIGINT DEFAULT 0,
            payment_status TEXT DEFAULT 'pending', paid_at TIMESTAMP)""")
        cur.execute("ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS refund_status TEXT")
        cur.execute("ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS refund_completed_at TIMESTAMP")
        cur.execute("ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS refund_requested_at TIMESTAMP")
        cur.execute("INSERT INTO users (id, username) VALUES (7100,'u7100') ON CONFLICT (id) DO NOTHING")
        cur.execute("DELETE FROM refund_work_orders WHERE source_order_id=%s", (oid,))
        cur.execute("DELETE FROM recharge_orders WHERE id=%s", (oid,))
        cur.execute("INSERT INTO recharge_orders (id,user_id,amount_cents,base_points,bonus_points,payment_status,refund_status,refund_completed_at) "
                    "VALUES (%s,7100,10000,0,0,'paid',%s,CASE WHEN %s THEN NOW() ELSE NULL END)",
                    (oid, refund_status, cd_evidence))
        cur.execute("INSERT INTO refund_work_orders (source_order_id, refund_method, status, requested_refund_cents) "
                    "VALUES (%s,'system_only','approved',10000) RETURNING id", (oid,))
        wid = cur.fetchone()["id"]
        if evidence_type:
            cur.execute("INSERT INTO refund_work_order_attachments "
                        "(work_order_id,file_url,file_name,file_type,evidence_type,file_size_bytes) "
                        "VALUES (%s,'/proof','proof.png','image',%s,1)", (wid, evidence_type))
        c.commit()
    return wid


@pytest.mark.parametrize("bad_status", [None, "failed", "rejected", "channel_refunding"])
def test_save_execution_result_rejects_completion_when_order_not_completable(bad_status):
    """🔴 [v11 F3/P1-3] 订单处于 failed/rejected(不可完成态)→ save_execution_result 完成时 UPDATE recharge_orders
    命中 0 行 → 必 raise → 整事务回滚 → 工单【不虚假完成】(仍 approved)。删 RETURNING 命中校验 → 假完成 → 转红。
    (注:pending_review 是可完成态 · 见 P0-14 铁律 · 在 completes 正例中覆盖。)"""
    from db.refund_work_order_db import save_execution_result, get_refund_work_order
    oid = f"ord-v11-{bad_status}"
    wid = _mk_order_and_wo(oid, bad_status)
    with pytest.raises(Exception):
        save_execution_result(wid, 1, {"note": "系统冲账"}, "completed")
    wo = get_refund_work_order(wid)
    assert wo["status"] == "approved", "🔴 订单不可完成 → 工单必回滚保持 approved(不显示完成)"
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT refund_status FROM recharge_orders WHERE id=%s", (oid,))
        assert cur.fetchone()["refund_status"] == bad_status, "🔴 订单 refund_status 不得被改(整事务回滚)"


@pytest.mark.parametrize("ok_status,cd_evidence", [("pending", False), ("pending_review", True)])
def test_save_execution_result_completes_when_order_completable(ok_status, cd_evidence):
    """[v11 F3 · P0-14] 正例:订单 pending(可完成)/ pending_review(渠道已退现金·系统冲账反向内账·P0-14 不得成死角)
    → save_execution_result 完成 → 工单 completed + 订单 completed(happy path + P0-14 都不破)。"""
    from db.refund_work_order_db import save_execution_result, get_refund_work_order
    oid = f"ord-v11-ok-{ok_status}"
    wid = _mk_order_and_wo(oid, ok_status, cd_evidence=cd_evidence)
    save_execution_result(wid, 1, {"note": "系统冲账"}, "completed")
    wo = get_refund_work_order(wid)
    assert wo["status"] == "completed", "可完成订单 → 工单 completed"
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT refund_status FROM recharge_orders WHERE id=%s", (oid,))
        assert cur.fetchone()["refund_status"] == "completed", "订单推进到 completed"


def test_pending_review_without_cd_or_manual_evidence_cannot_complete():
    from db.refund_work_order_db import save_execution_result, get_refund_work_order
    oid = "ord-v12-pr-no-evidence"
    wid = _mk_order_and_wo(oid, "pending_review")
    with pytest.raises(Exception, match="缺少可信 CD"):
        save_execution_result(wid, 1, {"note": "系统冲账"}, "completed")
    assert get_refund_work_order(wid)["status"] == "approved"


def test_channel_refunding_with_admin_refund_proof_can_complete():
    from db.refund_work_order_db import save_execution_result, get_refund_work_order
    oid = "ord-v12-rd-proof"
    wid = _mk_order_and_wo(oid, "channel_refunding", evidence_type="channel_refund_proof")
    save_execution_result(wid, 1, {"note": "渠道实际退款凭证已人工核验"}, "completed")
    assert get_refund_work_order(wid)["status"] == "completed"


@pytest.mark.parametrize("refund_status", ["channel_refunding", "pending_review"])
def test_v35_admin_refund_blocks_unproven_external_state_before_fund_mutation(monkeypatch, refund_status):
    """No CD/proof means the direct admin path must stop before the V3.5 ledger handler."""
    from fastapi import HTTPException
    from api.wallet_api import RefundRequest, _v35_admin_refund
    import api.referral_api as referral_api

    oid = f"ord-v12-admin-block-{refund_status}"
    with _conn() as c:
        cur = c.cursor()
        cur.execute("ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS agent_user_id INTEGER")
        cur.execute("ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS settlement_mode TEXT")
        cur.execute("DELETE FROM refund_work_orders WHERE source_order_id=%s", (oid,))
        cur.execute("DELETE FROM recharge_orders WHERE id=%s", (oid,))
        cur.execute(
            "INSERT INTO recharge_orders "
            "(id,user_id,amount_cents,base_points,bonus_points,payment_status,paid_at,refund_status,refund_completed_at) "
            "VALUES (%s,7100,10000,100,0,'paid',NOW(),%s,NULL)",
            (oid, refund_status),
        )
        c.commit()

    calls = []
    monkeypatch.setattr(referral_api, "_handle_v35_factory_refund", lambda *a, **k: calls.append((a, k)))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_v35_admin_refund(
            RefundRequest(
                order_id=oid, reason_category="system_failure",
                evidence={"regression": "unproven-external-state"},
            ),
            {"user_id": 1, "username": "admin", "is_admin": True},
            None,
        ))
    assert exc.value.status_code == 400
    assert calls == [], "无可信退款证据时不得触达任何 V3.5 内账反向处理器"
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT refund_status FROM recharge_orders WHERE id=%s", (oid,))
        assert cur.fetchone()["refund_status"] == refund_status, "拒绝路径必须零写入"


def test_external_refund_evidence_gate_allows_cd_or_reviewed_manual_proof():
    from api.wallet_api import _require_external_refund_evidence_cur

    cd_oid = "ord-v12-admin-cd-proof"
    with _conn() as c:
        cur = c.cursor()
        cur.execute("DELETE FROM recharge_orders WHERE id=%s", (cd_oid,))
        cur.execute(
            "INSERT INTO recharge_orders "
            "(id,user_id,amount_cents,base_points,bonus_points,payment_status,paid_at,refund_status,refund_completed_at) "
            "VALUES (%s,7100,10000,100,0,'paid',NOW(),'pending_review',NOW())",
            (cd_oid,),
        )
        cur.execute("SELECT id,refund_status,refund_completed_at FROM recharge_orders WHERE id=%s", (cd_oid,))
        _require_external_refund_evidence_cur(cur, dict(cur.fetchone()))

    manual_oid = "ord-v12-admin-manual-proof"
    _mk_order_and_wo(manual_oid, "channel_refunding", evidence_type="channel_refund_proof")
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id,refund_status,refund_completed_at FROM recharge_orders WHERE id=%s", (manual_oid,))
        _require_external_refund_evidence_cur(cur, dict(cur.fetchone()))


def test_api_complete_work_order_has_returning_guard():
    """🔴 [v11 F3] api /complete 单事务:UPDATE recharge_orders ... RETURNING + fetchone() is None → 拒绝完成。
    源码锁:删 RETURNING 命中校验 / 退回另开事务 transition → 转红。"""
    src = (ROOT / "api" / "refund_work_order_api.py").read_text(encoding="utf-8")
    # /complete 是文件最后一个路由 → 切到 EOF(无后续 @router. 作锚)
    m = re.search(r'@router\.post\("/\{work_order_id\}/complete"\)(.*)$', src, re.DOTALL)
    assert m, "complete endpoint not found"
    body = m.group(1)
    assert "RETURNING id" in body, "🔴 recharge_orders 完成必 UPDATE ... RETURNING(查命中)"
    assert "if _upd is None" in body, "🔴 0 行必 raise 拒绝虚假完成"
    assert "assert_refund_completion_allowed_cur" in body, "🔴 完成前必须核验 CD/人工退款证据"
    assert "_transition_refund_work_order_cur" in body, "🔴 工单终态必在同一事务内推进(不另开事务)"
    assert "FOR UPDATE" in body, "🔴 完成前须锁工单行(并发防重复完成)"


# ============================================================
# F6 · v10b 备份去重 migration 往返 row-identical(真跑 SQL 文件)
# ============================================================

def _strip_txn(sql: str) -> str:
    sql = re.sub(r'^\s*BEGIN\s*;\s*$', '', sql, flags=re.MULTILINE | re.IGNORECASE)
    sql = re.sub(r'^\s*COMMIT\s*;\s*$', '', sql, flags=re.MULTILINE | re.IGNORECASE)
    return sql


def test_v10b_dedup_migration_roundtrip_row_identical():
    """🔴 [v11 F6] v10b 备份去重脚本:折叠前【完整备份】· 文档化 rollback【逐行精确恢复】。
    seed 2 条重复(older=processing · newer=pending)→ 跑真 v10b forward → older 折叠 resolved + 备份捕获原值 →
    跑 restore → older 逐行恢复(status/last_error 与折叠前一致)。forward 不备份(旧版无法恢复)→ restore mismatch → 转红。"""
    from db.fund_recovery_db import init_fund_recovery_tables
    v10b_path = ROOT / "scripts" / "migration_v10b_dedup_channel_revenue_with_backup_2026_07_13.sql"
    v10b = v10b_path.read_text(encoding="utf-8")
    body = _strip_txn(v10b)
    with _conn() as c:
        init_fund_recovery_tables(c.cursor()); c.commit()
        cur = c.cursor()
        cur.execute("DROP TABLE IF EXISTS fund_recovery_orders_v10_dedup_backup")
        cur.execute("DELETE FROM fund_recovery_orders WHERE ref_key='ord-rt-1'")
        # 干净 init 会建唯一键 → 直插 2 条重复(模拟 prod 历史)前先删键(v10b 本身不建键)
        cur.execute("DROP INDEX IF EXISTS uniq_fund_recovery_channel_open")
        cur.execute("INSERT INTO fund_recovery_orders (source,ref_key,kind,status,reason) "
                    "VALUES ('channel_revenue','ord-rt-1','record','processing','older') RETURNING id")
        older = cur.fetchone()["id"]
        cur.execute("INSERT INTO fund_recovery_orders (source,ref_key,kind,status,reason) "
                    "VALUES ('channel_revenue','ord-rt-1','record','pending','newer') RETURNING id")
        newer = cur.fetchone()["id"]
        c.commit()
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT status, resolved_at, last_error FROM fund_recovery_orders WHERE id=%s", (older,))
        orig = dict(cur.fetchone())
    # forward:真跑 v10b(备份 + 折叠)
    with _conn() as c:
        cur = c.cursor(); cur.execute(body); c.commit()
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT status FROM fund_recovery_orders WHERE id=%s", (older,))
        assert cur.fetchone()["status"] == "resolved", "🔴 older 应被折叠为 resolved"
        cur.execute("SELECT status FROM fund_recovery_orders WHERE id=%s", (newer,))
        assert cur.fetchone()["status"] == "pending", "newer(保最新)不动"
        cur.execute("SELECT COUNT(*) AS n FROM fund_recovery_orders_v10_dedup_backup "
                    "WHERE order_id=%s AND batch_tag='v10b_2026_07_13'", (older,))
        assert cur.fetchone()["n"] == 1, "🔴 折叠前必完整备份 older 行(否则不可逐行恢复)"
    # rollback:文档化可执行逐行恢复
    with _conn() as c:
        cur = c.cursor()
        cur.execute("""UPDATE fund_recovery_orders f
                         SET status=b.orig_status, resolved_at=b.orig_resolved_at,
                             updated_at=b.orig_updated_at, last_error=b.orig_last_error
                         FROM fund_recovery_orders_v10_dedup_backup b
                        WHERE f.id=b.order_id AND b.batch_tag='v10b_2026_07_13'""")
        c.commit()
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT status, last_error FROM fund_recovery_orders WHERE id=%s", (older,))
        after = dict(cur.fetchone())
        assert after["status"] == orig["status"], f"🔴 rollback 必逐行恢复 status · {after['status']} != {orig['status']}"
        assert after["last_error"] == orig["last_error"], "🔴 rollback 必恢复 last_error(逐行一致)"
    # shipped 脚本确实提供可执行逐行 restore(与所测一致)
    assert "SET status=b.orig_status" in v10b, "🔴 v10b 必须提供可执行逐行恢复 UPDATE"
    # [v11 集中审核修] rollback 必须提示:v10 唯一键已建时先 DROP INDEX,否则恢复行撞唯一约束整事务 abort
    assert "DROP INDEX IF EXISTS uniq_fund_recovery_channel_open" in v10b, \
        "🔴 v10b rollback 必须先撤 v10 唯一键(否则逐行恢复撞唯一约束)"
    with _conn() as c:
        cur = c.cursor()
        cur.execute("DELETE FROM fund_recovery_orders WHERE ref_key='ord-rt-1'")
        cur.execute("DROP TABLE IF EXISTS fund_recovery_orders_v10_dedup_backup"); c.commit()
