"""[v12 · Deploy-CTO 8项返工] 行为级判别测试 —— item4/5/6/7(item1-3 refund 状态机+证据 gate 在 pricing_ssot + v11)。

  item4 fund_recovery 唯一约束缺失 → 补偿创建 fail-closed(不 fail-open 建重复);
  item5 migration_v10b forward→forward→rollback→forward 真幂等(备份表唯一键·不覆盖首份);
  item6 系数统一 canonical(normalize_quote_markup_ratio 行为 + set_markup_ratio 源码锁);
  item7 删除一律 tombstone(不硬删)+ restore 清 deleted_at + apply_markup 不 resurrect。
判别性:删修复 → 转红(见各 docstring)。
"""
from __future__ import annotations
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
    # [test 隔离] item4/5 会 DROP uniq_fund_recovery_channel_open + 留本文件专用 channel_revenue 行 →
    #   清本文件数据 + 恢复唯一键,防后续测试的 channel_revenue 补偿因 item4 fail-closed 守卫误触。
    try:
        with _conn() as c:
            cur = c.cursor()
            cur.execute("DELETE FROM fund_recovery_orders WHERE ref_key IN ('ord-fc-1','ord-fc-2','ord-v12-rt') OR ref_key='q-fc-3'")
            cur.execute("DROP INDEX IF EXISTS uniq_fund_recovery_channel_open")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uniq_fund_recovery_channel_open "
                        "ON fund_recovery_orders(source, ref_key, kind) "
                        "WHERE source='channel_revenue' AND ref_key IS NOT NULL AND status IN ('pending','processing','manual')")
            c.commit()
    except Exception:
        pass


# ============================================================
# item6 · 系数统一 canonical(清第二套换算)
# ============================================================

def test_normalize_quote_markup_ratio_uses_canonical_not_round():
    """🔴 [v12 item6] normalize_quote_markup_ratio 用 canonical(Decimal ROUND_HALF_UP)非 round(banker/float)。
    2.675 → 2.68(canonical HALF_UP)· round(2.675,2)=2.67(float/banker)→ 退回 round 转红。clamp 仍生效。"""
    from services.quote_pricing_preferences import normalize_quote_markup_ratio
    assert normalize_quote_markup_ratio(2.675) == 2.68, "🔴 canonical HALF_UP:2.675→2.68(非 round 的 2.67)"
    assert normalize_quote_markup_ratio(1.234) == 1.23
    assert normalize_quote_markup_ratio(1.235) == 1.24
    assert normalize_quote_markup_ratio(0.5) == 1.0, "clamp 下限"
    assert normalize_quote_markup_ratio(9.0) == 5.0, "clamp 上限"


def test_set_markup_ratio_stores_canonical_source_lock():
    """🔴 [v12 item6] set_markup_ratio 存 agent_sku_markup_ratio 必用 resolve_canonical_markup(与 apply-markup 同源)· 禁 round。
    退回 round(req.ratio,2) → 同系数两存值(apply-markup canonical vs set round)→ 转红。"""
    src = (ROOT / "api" / "agent_workbench_api.py").read_text(encoding="utf-8")
    i = src.find("async def set_markup_ratio(")
    assert i > 0, "set_markup_ratio not found"
    body = src[i:i + 900]
    assert "resolve_canonical_markup(req.ratio)" in body, "🔴 set_markup_ratio 必用 resolve_canonical_markup"
    assert "round(req.ratio, 2)" not in body, "🔴 set_markup_ratio 不得再用 round(banker/float)"


# ============================================================
# item4 · fund_recovery 唯一约束缺失 → 补偿创建 fail-closed
# ============================================================

def test_channel_revenue_compensation_fail_closed_when_index_missing():
    """🔴 [v12 item4] channel_revenue 补偿工单登记:唯一约束【不健康(索引缺失)】→ fail-closed 抛错
    (不 fail-open 退化裸插制造重复·不谎报耐久)。健康时正常登记;DROP 索引后 insert_recovery_order_cursor 抛错。
    非 channel_revenue source 不受影响。删 fail-closed 守卫 → 索引缺失仍插入 → 转红。"""
    from db.fund_recovery_db import (init_fund_recovery_tables, insert_recovery_order_cursor,
                                     channel_revenue_index_healthy)
    with _conn() as c:
        cur = c.cursor(); cur.execute("DELETE FROM fund_recovery_orders"); c.commit()
        init_fund_recovery_tables(c.cursor()); c.commit()
    with _conn() as c:  # 健康:索引在 → 登记成功
        cur = c.cursor()
        ok, why = channel_revenue_index_healthy(cur)
        assert ok, f"干净应健康·实际 {why}"
        wid = insert_recovery_order_cursor(cur, "channel_revenue", "record", ref_key="ord-fc-1",
                                           reason="t", payload={"order_id": "ord-fc-1"})
        assert wid is not None
        c.commit()
    with _conn() as c:  # 不健康:DROP 索引
        cur = c.cursor(); cur.execute("DROP INDEX IF EXISTS uniq_fund_recovery_channel_open"); c.commit()
    with _conn() as c:
        cur = c.cursor()
        ok, why = channel_revenue_index_healthy(cur)
        assert not ok, "索引缺失应判不健康"
        with pytest.raises(Exception, match="fail-closed|不健康"):
            insert_recovery_order_cursor(cur, "channel_revenue", "reverse", ref_key="ord-fc-2",
                                         reason="t", payload={"order_id": "ord-fc-2"})
    with _conn() as c:  # 非 channel_revenue 不受影响
        cur = c.cursor()
        cur.execute("INSERT INTO users (id, username) VALUES (9600,'u9600') ON CONFLICT (id) DO NOTHING")
        wid = insert_recovery_order_cursor(cur, "article_gen", "refund", ref_key="q-fc-3",
                                           user_id=9600, feature_code="article_gen")
        assert wid is not None
        c.commit()


# ============================================================
# item5 · v10b forward→forward→rollback→forward 真幂等
# ============================================================

def _strip_txn(sql: str) -> str:
    sql = re.sub(r'^\s*BEGIN\s*;\s*$', '', sql, flags=re.MULTILINE | re.IGNORECASE)
    return re.sub(r'^\s*COMMIT\s*;\s*$', '', sql, flags=re.MULTILINE | re.IGNORECASE)


def test_v10b_forward_twice_idempotent_then_rollback_then_forward():
    """🔴 [v12 item5] v10b 连续 forward 两次均成功零破坏(备份表唯一键 (order_id,batch_tag) 防撞 + 不覆盖首次备份)·
    rollback 逐行恢复 · 再 forward。删备份唯一键/ON CONFLICT → 二次 forward 撞唯一键或重复备份 → 转红。"""
    from db.fund_recovery_db import init_fund_recovery_tables
    v10b = (ROOT / "scripts" / "migration_v10b_dedup_channel_revenue_with_backup_2026_07_13.sql").read_text(encoding="utf-8")
    body = _strip_txn(v10b)
    with _conn() as c:
        init_fund_recovery_tables(c.cursor()); c.commit()
        cur = c.cursor()
        cur.execute("DROP TABLE IF EXISTS fund_recovery_orders_v10_dedup_backup")
        cur.execute("DELETE FROM fund_recovery_orders WHERE ref_key='ord-v12-rt'")
        cur.execute("DROP INDEX IF EXISTS uniq_fund_recovery_channel_open")
        cur.execute("INSERT INTO fund_recovery_orders (source,ref_key,kind,status,reason) "
                    "VALUES ('channel_revenue','ord-v12-rt','record','processing','older') RETURNING id")
        older = cur.fetchone()["id"]
        cur.execute("INSERT INTO fund_recovery_orders (source,ref_key,kind,status,reason) "
                    "VALUES ('channel_revenue','ord-v12-rt','record','pending','newer')")
        c.commit()

    def _status(_id):
        with _conn() as c:
            cur = c.cursor(); cur.execute("SELECT status FROM fund_recovery_orders WHERE id=%s", (_id,))
            return cur.fetchone()["status"]

    def _backup():
        with _conn() as c:
            cur = c.cursor()
            cur.execute("SELECT COUNT(*) AS n, MIN(orig_status) AS s FROM fund_recovery_orders_v10_dedup_backup WHERE order_id=%s", (older,))
            r = cur.fetchone(); return r["n"], r["s"]

    def _audit():
        with _conn() as c:
            cur = c.cursor()
            cur.execute("SELECT resolved_at, last_error FROM fund_recovery_orders WHERE id=%s", (older,))
            r = cur.fetchone(); return r["resolved_at"], r["last_error"]

    with _conn() as c:                       # forward 1
        cur = c.cursor(); cur.execute(body); c.commit()
    assert _status(older) == "resolved"
    assert _backup() == (1, "processing")
    _ra1, _le1 = _audit()
    with _conn() as c:                       # forward 2:幂等·不抛·不覆盖首份·不重复备份
        cur = c.cursor(); cur.execute(body); c.commit()
    assert _backup() == (1, "processing"), "🔴 二次 forward 不得覆盖/重复备份首次快照"
    assert _status(older) == "resolved"
    # [v12 集中审核 P3] 折叠 UPDATE 需真幂等:二次 forward 不得刷 resolved_at / 重复追加 last_error
    _ra2, _le2 = _audit()
    assert _ra1 == _ra2, "🔴 二次 forward 不得刷 resolved_at(审计时点漂移)"
    assert _le1 == _le2, "🔴 二次 forward 不得重复追加 last_error(无界累加)"
    with _conn() as c:                       # rollback 逐行恢复
        cur = c.cursor()
        cur.execute("""UPDATE fund_recovery_orders f
                         SET status=b.orig_status, resolved_at=b.orig_resolved_at,
                             updated_at=b.orig_updated_at, last_error=b.orig_last_error
                         FROM fund_recovery_orders_v10_dedup_backup b
                        WHERE f.id=b.order_id AND b.batch_tag='v10b_2026_07_13'""")
        c.commit()
    assert _status(older) == "processing", "🔴 rollback 必逐行恢复原状态"
    with _conn() as c:                       # 再 forward:成功
        cur = c.cursor(); cur.execute(body); c.commit()
    assert _status(older) == "resolved"
    with _conn() as c:
        cur = c.cursor()
        cur.execute("DELETE FROM fund_recovery_orders WHERE ref_key='ord-v12-rt'")
        cur.execute("DROP TABLE IF EXISTS fund_recovery_orders_v10_dedup_backup"); c.commit()


def test_workers4_prestart_manifest_contains_geofix_migrations_in_dependency_order():
    """最终整合包不得只合代码祖先而漏掉 geofix 首次上线 schema。"""
    from db.migration_manifest import MIGRATIONS

    geofix_chain = [
        "scripts/migration_v5_geo_plan_settlement_2026_07_13.sql",
        "scripts/migration_v6_dispute_escrow_2026_07_13.sql",
        "scripts/migration_v6_fund_recovery_2026_07_13.sql",
        "scripts/migration_v7_fund_recovery_fencing_2026_07_13.sql",
        "scripts/migration_v7_geoplan_settle_conflict_index_2026_07_13.sql",
        "scripts/migration_v9_fund_recovery_kind_widen_2026_07_13.sql",
        "scripts/migration_v10b_dedup_channel_revenue_with_backup_2026_07_13.sql",
        "scripts/migration_v10_channel_revenue_exactly_once_2026_07_13.sql",
        "scripts/migration_v11_agent_sku_override_deleted_at_2026_07_13.sql",
    ]
    assert [item for item in MIGRATIONS if item in geofix_chain] == geofix_chain
    assert MIGRATIONS.index(geofix_chain[-1]) < MIGRATIONS.index(
        "scripts/migration_workers4_scheduling_2026_07_13.sql"
    )


# ============================================================
# item7 · 删除一律 tombstone(不硬删)+ restore + 不 resurrect
# ============================================================

def _provision_sku(c):
    cur = c.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, username TEXT, agent_sku_markup_ratio NUMERIC);
        CREATE TABLE IF NOT EXISTS sku_templates (id SERIAL PRIMARY KEY, template_code TEXT UNIQUE, sku_type TEXT,
            default_name TEXT, points_granted INTEGER, wholesale_cents INTEGER, suggested_retail_cents INTEGER,
            is_active BOOLEAN DEFAULT TRUE);
        CREATE TABLE IF NOT EXISTS agent_sku_overrides (id SERIAL PRIMARY KEY, agent_user_id INTEGER,
            sku_template_id INTEGER REFERENCES sku_templates(id), custom_name TEXT, custom_subtitle TEXT,
            custom_sales_pitch TEXT, custom_scene TEXT, retail_cents INTEGER NOT NULL, is_active BOOLEAN DEFAULT TRUE,
            sort_order INTEGER DEFAULT 0, margin_warning TEXT, deleted_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW(), updated_at TIMESTAMPTZ DEFAULT NOW());
        CREATE TABLE IF NOT EXISTS recharge_orders (id TEXT PRIMARY KEY, user_id INTEGER, agent_user_id INTEGER,
            order_type TEXT, sku_template_id INTEGER, binding_source TEXT, source_token TEXT,
            amount_cents INTEGER DEFAULT 0, base_points INTEGER DEFAULT 0, bonus_points INTEGER DEFAULT 0,
            payment_method TEXT, settlement_mode TEXT, pricing_snapshot_jsonb JSONB, override_id INTEGER);
    """)
    # 共享表 recharge_orders 用【非缩水超集】(含 source_token/settlement_mode 等)· 防本文件先建成缺列版
    # 遮蔽后续 test_nogo_v5 等需要的列(跨测试 CREATE IF NOT EXISTS 幂等 shadowing)。
    cur.execute("ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS override_id INTEGER")
    cur.execute("ALTER TABLE agent_sku_overrides ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ")
    # Phase 15 upgrades this shared fixture to the canonical retail SKU schema.
    migration = (ROOT / "scripts" / "migration_agent_retail_sku_decoupling_2026_07_17.sql").read_text(
        encoding="utf-8"
    )
    cur.execute(migration)
    # 共享测试库里其它回归可能已为本模板 materialize override；先按模板清关联行，
    # 再删模板，避免测试顺序依赖和 FK 假失败。
    cur.execute("""DELETE FROM agent_sku_overrides
                    WHERE agent_user_id=9500
                       OR sku_template_id IN (
                           SELECT id FROM sku_templates WHERE template_code='v12_a'
                       )""")
    cur.execute("DELETE FROM sku_templates WHERE template_code='v12_a'")
    cur.execute("INSERT INTO users (id, username) VALUES (9500,'a9500') ON CONFLICT (id) DO NOTHING")
    cur.execute(
        "INSERT INTO user_wallets (user_id,agent_level) VALUES (9500,1) "
        "ON CONFLICT (user_id) DO UPDATE SET agent_level=EXCLUDED.agent_level"
    )
    cur.execute("INSERT INTO sku_templates (template_code,sku_type,default_name,points_granted,wholesale_cents,suggested_retail_cents,is_active) "
                "VALUES ('v12_a','credit_pack','包A',130000,100000,150000,TRUE)")
    c.commit()


def test_delete_always_tombstones_never_hard_delete_and_restore_clears():
    """🔴 [v12 item7] 删除白标包【一律 tombstone·绝不硬删】(行还在 + is_active=F + deleted_at)· apply_markup 不 resurrect ·
    restore 清 deleted_at 复活。退回硬删(DELETE)→ 行消失 → apply_markup 可重新物化上架 → 转红。"""
    from services.agent_pricing import (create_agent_sku_override, delete_agent_sku_override,
                                        restore_agent_sku_override, apply_markup_for_agent)
    with _conn() as c:
        _provision_sku(c)
        cur = c.cursor()
        cur.execute("SELECT id FROM sku_templates WHERE template_code='v12_a'"); tid = cur.fetchone()["id"]
        r = create_agent_sku_override(cur, agent_user_id=9500, sku_template_id=tid, retail_cents=180000, is_active=True)
        oid = r["id"]; c.commit()
    # 无订单引用也 tombstone(不硬删)
    with _conn() as c:
        cur = c.cursor()
        res = delete_agent_sku_override(cur, 9500, oid); c.commit()
        assert res["action"] == "soft_deleted"
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT is_active, deleted_at FROM agent_sku_overrides WHERE id=%s", (oid,))
        row = cur.fetchone()
        assert row is not None, "🔴 不得硬删除(行必须还在做 tombstone)"
        assert row["is_active"] is False and row["deleted_at"] is not None, "🔴 删除必打 tombstone(is_active=F + deleted_at)"
    # apply_markup 不 resurrect
    with _conn() as c:
        cur = c.cursor()
        res = apply_markup_for_agent(cur, 9500, 12000); c.commit()
        assert all(x["sku"] != "包A" for x in res["created"]), "🔴 tombstone 规格不得被 materialize resurrect"
        assert all(x["sku"] != "包A" for x in res["updated"]), "🔴 tombstone 规格不得被重定价"
        # 只校验被 tombstone 的 v12_a 规格(其它模板 cred_* 被正常物化不影响本断言)
        cur.execute("SELECT COUNT(*) AS n FROM agent_sku_overrides ov JOIN sku_templates t ON ov.sku_template_id=t.id "
                    "WHERE ov.agent_user_id=9500 AND t.template_code='v12_a' AND ov.is_active=TRUE AND ov.deleted_at IS NULL")
        assert cur.fetchone()["n"] == 0, "🔴 tombstone 的 v12_a 后无活跃可售(未 resurrect)"
    # restore:清 deleted_at + 复活
    with _conn() as c:
        cur = c.cursor()
        rr = restore_agent_sku_override(cur, 9500, oid); c.commit()
        assert rr["action"] == "restored" and rr["was_tombstoned"] is True
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT is_active, deleted_at FROM agent_sku_overrides WHERE id=%s", (oid,))
        row = cur.fetchone()
        assert row["is_active"] is True and row["deleted_at"] is None, "🔴 restore 必清 deleted_at + 复活 active"


def test_update_override_cannot_resurrect_tombstone():
    """普通更新不能复活 tombstone；恢复后才能继续修改。"""
    from services.agent_pricing import (
        create_agent_sku_override,
        delete_agent_sku_override,
        restore_agent_sku_override,
        update_agent_sku_override,
    )
    with _conn() as c:
        _provision_sku(c)
        cur = c.cursor()
        cur.execute("SELECT id FROM sku_templates WHERE template_code='v12_a'")
        tid = cur.fetchone()["id"]
        oid = create_agent_sku_override(
            cur, agent_user_id=9500, sku_template_id=tid,
            retail_cents=180000, is_active=True,
        )["id"]
        c.commit()
    with _conn() as c:
        cur = c.cursor()
        delete_agent_sku_override(cur, 9500, oid)
        c.commit()
    with _conn() as c:
        cur = c.cursor()
        with pytest.raises(ValueError):
            update_agent_sku_override(
                cur, agent_user_id=9500, override_id=oid,
                retail_cents=200000, is_active=True,
            )
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT is_active, deleted_at FROM agent_sku_overrides WHERE id=%s", (oid,))
        row = cur.fetchone()
        assert row["is_active"] is False and row["deleted_at"] is not None, \
            "update 不得把 tombstone 置 active"
    with _conn() as c:
        cur = c.cursor()
        restore_agent_sku_override(cur, 9500, oid)
        update_agent_sku_override(
            cur, agent_user_id=9500, override_id=oid,
            retail_cents=200000, is_active=None,
        )
        c.commit()
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT retail_cents, deleted_at FROM agent_sku_overrides WHERE id=%s", (oid,))
        row = cur.fetchone()
        assert row["retail_cents"] == 200000 and row["deleted_at"] is None, \
            "restore 后可正常 update"


def test_tombstone_update_guard_is_atomic_source_lock():
    agent_pricing = (ROOT / "services" / "agent_pricing.py").read_text(encoding="utf-8")
    start = agent_pricing.find("def update_agent_sku_override(")
    end = agent_pricing.find("\ndef ", start + 1)
    body = agent_pricing[start:end]
    assert "FOR UPDATE" in body
    assert body.count("agent_user_id = %s AND deleted_at IS NULL") >= 3
    wallet_api = (ROOT / "api" / "wallet_api.py").read_text(encoding="utf-8")
    query = wallet_api.find("SELECT o.id AS override_id, o.retail_cents AS override_retail")
    assert query > 0 and "o.deleted_at IS NULL" in wallet_api[query:query + 1000]


def test_purchase_paths_use_canonical_rows_and_platform_price_restore_is_disabled():
    wallet_api = (ROOT / "api" / "wallet_api.py").read_text(encoding="utf-8")
    quoted_anchor = wallet_api.find('required_source = (')
    quoted_body = wallet_api[quoted_anchor:quoted_anchor + 900]
    assert quoted_anchor > 0
    assert '"retail_sku_id"' in quoted_body and '"retail_sku_version"' in quoted_body
    legacy_anchor = wallet_api.find("if req.override_id:")
    legacy_body = wallet_api[legacy_anchor:legacy_anchor + 3600]
    assert legacy_anchor > 0
    assert "o.points_granted" in legacy_body
    assert "o.deleted_at IS NULL" in legacy_body
    assert "旧 sku_template_id 链只能唯一命中显式零售行" in wallet_api
    assert "永不再回落平台模板售价" in wallet_api
    assert "any_override_count" not in legacy_body
    assert '"override_id": _resolved_override_id' in wallet_api
    assert "create_recharge_order(**_order_create_kwargs)" in wallet_api

    agent_api = (ROOT / "api" / "agent_workbench_api.py").read_text(encoding="utf-8")
    restore_anchor = agent_api.find("async def agent_pricing_sku_restore_suggested")
    restore_body = agent_api[restore_anchor:restore_anchor + 1800]
    assert "PLATFORM_SUGGESTED_PRICE_DISABLED" in restore_body
    assert "raise HTTPException(" in restore_body and "410" in restore_body
    assert "update_agent_sku_override" not in restore_body, \
        "🔴 平台建议售价不得再写入服务商零售 SSOT"

    admin_api = (ROOT / "api" / "admin_factory_api.py").read_text(encoding="utf-8")
    sync_anchor = admin_api.find("async def admin_pricing_sync_agent_prices")
    sync_end = admin_api.find("class GlobalPricingConfigRequest", sync_anchor)
    sync_body = admin_api[sync_anchor:sync_end]
    assert "PLATFORM_SUGGESTED_PRICE_SYNC_DISABLED" in sync_body
    assert "raise HTTPException(" in sync_body and "410" in sync_body
    assert "UPDATE agent_sku_overrides" not in sync_body
    assert "preview_agent_retail_sku" not in sync_body


def test_fixed_three_day_window_is_removed_but_external_evidence_gate_remains():
    src = (ROOT / "api" / "wallet_api.py").read_text(encoding="utf-8")
    assert "超过 3 天退款窗口期" not in src
    start = src.index("async def _v35_admin_refund(")
    end = src.index("\n\n@router", start)
    block = src[start:end]
    assert "_require_external_refund_evidence_cur(cursor, order)" in block
    assert '"pending_review", "channel_refunding"' in block
