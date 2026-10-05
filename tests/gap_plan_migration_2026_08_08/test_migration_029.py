"""migration_029(P4 支撑迁移)的真库锁。

判据设计原则(照 .deploy_toolkit/README 「每个必须命中都要有成对的必须不命中」):
  每一条"必须存在"的断言,都配一条**同法查询的不存在物**作反向对照,
  证明这条判据不是恒真。
"""

from __future__ import annotations

import pytest


# ────────────────────────────────────────────────────────────────
# §1 media_outlets 加列
# ────────────────────────────────────────────────────────────────

EXPECTED_NEW_COLUMNS = {"entry_assessment", "verified_at", "verified_by", "entry_note"}


def _columns(conn, table: str) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type
              FROM information_schema.columns
             WHERE table_schema = current_schema() AND table_name = %s
            """,
            (table,),
        )
        return {r["column_name"]: r["data_type"] for r in cur.fetchall()}


def test_media_outlets_gains_exactly_the_four_entry_columns(fresh_db):
    conn, _ = fresh_db
    cols = _columns(conn, "media_outlets")

    missing = EXPECTED_NEW_COLUMNS - set(cols)
    assert not missing, f"迁移没加上这些列:{missing}"

    # 反向对照:同法查一个**不该存在**的列 → 必须查不到。
    # 没有这一条,上面那句 assert 在"查询恒返回全部列名"的实现下也会绿。
    assert "entry_assessment_ghost" not in cols

    # data_type 逐条钉死(SQL 4 维核验第 2 维:避免 TIMESTAMP vs TIMESTAMPTZ 错配)
    assert cols["entry_assessment"] == "text"
    assert cols["verified_at"] == "timestamp with time zone"
    assert cols["verified_by"] == "integer"
    assert cols["entry_note"] == "text"


def test_existing_media_outlets_row_survives_and_new_columns_are_null(fresh_db):
    """additive 的实质:存量行一行不动,新列对它们是 NULL(=尚未核实)。"""
    conn, _ = fresh_db
    with conn.cursor() as cur:
        cur.execute("SELECT name, ai_coverage_count, entry_assessment, verified_at FROM media_outlets")
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["name"] == "存量媒体甲"
    assert rows[0]["ai_coverage_count"] == 3          # 既有列的值没被改
    assert rows[0]["entry_assessment"] is None        # 新列 = 未核实,不是"进不去"
    assert rows[0]["verified_at"] is None


@pytest.mark.parametrize("value", ["self_service", "mediated", "unreachable", "unknown"])
def test_entry_assessment_accepts_the_four_controlled_values(fresh_db, value):
    conn, _ = fresh_db
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO media_outlets (name, platform, media_type, entry_assessment)"
            " VALUES (%s,'p','t',%s)",
            (f"outlet_{value}", value),
        )
        cur.execute("SELECT entry_assessment FROM media_outlets WHERE name=%s", (f"outlet_{value}",))
        assert cur.fetchone()["entry_assessment"] == value


def test_entry_assessment_rejects_anything_else(fresh_db):
    """反向对照:受控枚举必须真的挡得住。挡不住 = 那条 CHECK 是摆设。"""
    conn, _ = fresh_db
    import psycopg2

    with pytest.raises(psycopg2.errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO media_outlets (name, platform, media_type, entry_assessment)"
                " VALUES ('坏值','p','t','tier1_national')"
            )


def test_entry_assessment_still_allows_null(fresh_db):
    """NULL(没人看过)必须与 'unknown'(看过但不确定)都能存 —— 两者语义不同。"""
    conn, _ = fresh_db
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO media_outlets (name, platform, media_type) VALUES ('未核实','p2','t')"
        )
        cur.execute("SELECT entry_assessment FROM media_outlets WHERE name='未核实'")
        assert cur.fetchone()["entry_assessment"] is None


# ────────────────────────────────────────────────────────────────
# §2 五张新表
# ────────────────────────────────────────────────────────────────

EXPECTED_TABLES = {
    "gap_plan_snapshots",
    "gap_plan_items",
    "gap_plan_publications",
    "gap_plan_checkbacks",
    "gap_assistant_audit",
}


def _tables(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = current_schema()"
        )
        return {r["table_name"] for r in cur.fetchall()}


def test_all_five_gap_tables_created(fresh_db):
    conn, _ = fresh_db
    tables = _tables(conn)
    assert EXPECTED_TABLES <= tables, f"缺表:{EXPECTED_TABLES - tables}"
    # 反向对照:同法查一张不该存在的表
    assert "gap_plan_ghost" not in tables


def test_gap_plan_items_has_the_delivery_slot_bridge_column(fresh_db):
    """合流桥占位列:现在恒 NULL,等 P4 计划项真正落成交付槽时才写。

    容量口径已并轨到 services/article_capacity_contract,而那份合同裁定
    「容量占用单位 = topics(交付槽)」—— 两边将来要能对上号。
    """
    conn, _ = fresh_db
    cols = _columns(conn, "gap_plan_items")
    assert "delivery_slot_id" in cols
    assert cols["delivery_slot_id"] == "uuid"
    assert "delivery_slot_id_ghost" not in cols          # 反向对照

    # 占位 = 可空,且本包不写它
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT is_nullable FROM information_schema.columns
             WHERE table_schema=current_schema() AND table_name='gap_plan_items'
               AND column_name='delivery_slot_id'
            """
        )
        assert cur.fetchone()["is_nullable"] == "YES"


# ────────────────────────────────────────────────────────────────
# §3 不变式(CHECK / 唯一索引)
# ────────────────────────────────────────────────────────────────

def _seed_snapshot(conn, quote_id=372, generation=1, snapshot_id="dps_test0001"):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO gap_plan_snapshots
                (snapshot_id, quote_id, brand_id, snapshot_version, rule_version,
                 data_version, authority_generation, capacity_source)
            VALUES (%s,%s,615,'gap-plan-v1','r1',%s,%s,'quotes.total_articles')
            """,
            (snapshot_id, quote_id, "0" * 64, generation),
        )
    return snapshot_id


def test_snapshot_generation_is_unique_per_quote(fresh_db):
    conn, _ = fresh_db
    import psycopg2

    _seed_snapshot(conn, generation=1, snapshot_id="dps_a")
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _seed_snapshot(conn, generation=1, snapshot_id="dps_b")


def _seed_publication(conn, **over):
    fields = dict(
        quote_id=372,
        plan_item_id="Q372-P01",
        publication_url="https://example.com/a",
        publication_url_normalized="example.com/a",
        published_at="2026-08-08T00:00:00+00:00",
        evidence_published=True,
        evidence_indexed=False,
        evidence_cited=False,
        evidence_recommended=False,
    )
    fields.update(over)
    cols = ",".join(fields)
    marks = ",".join(["%s"] * len(fields))
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO gap_plan_publications ({cols}) VALUES ({marks}) RETURNING id",
            tuple(fields.values()),
        )
        return cur.fetchone()["id"]


def test_evidence_chain_cannot_skip_levels(fresh_db):
    """🔴 证据链四格禁跳级:被引用必先收录,进入推荐必先被引用。

    这条不变式在服务层由白名单强制,DB CHECK 是兜底。
    没有它,一次错误的回写就能把"被 AI 引用"点亮而"已收录"是灰的 —— 对运营是假证据。
    """
    conn, _ = fresh_db
    import psycopg2

    # 正向:合法的逐级点亮必须能存
    pid = _seed_publication(
        conn, evidence_indexed=True, evidence_cited=True, evidence_recommended=True
    )
    assert pid

    # 反向:跳级必须被挡
    with pytest.raises(psycopg2.errors.CheckViolation):
        _seed_publication(
            conn, plan_item_id="Q372-P02", evidence_indexed=False, evidence_cited=True
        )


def test_one_publication_row_per_plan_item(fresh_db):
    conn, _ = fresh_db
    import psycopg2

    _seed_publication(conn)
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _seed_publication(conn, publication_url="https://example.com/b")


def test_checkback_only_accepts_7_14_30(fresh_db):
    conn, _ = fresh_db
    import psycopg2

    pid = _seed_publication(conn)
    with conn.cursor() as cur:
        for day in (7, 14, 30):
            cur.execute(
                "INSERT INTO gap_plan_checkbacks"
                " (publication_id, quote_id, plan_item_id, due_day, due_at)"
                " VALUES (%s,372,'Q372-P01',%s, NOW() + (%s || ' days')::interval)",
                (pid, day, day),
            )
    with pytest.raises(psycopg2.errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO gap_plan_checkbacks"
                " (publication_id, quote_id, plan_item_id, due_day, due_at)"
                " VALUES (%s,372,'Q372-P01',21, NOW())",
                (pid,),
            )


def test_assistant_audit_idempotency_boundary(fresh_db):
    """幂等边界 = assistant_request_id + snapshot_version(合同 §9.1 原文)。"""
    conn, _ = fresh_db
    import psycopg2

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO gap_assistant_audit (assistant_request_id, snapshot_version)"
            " VALUES ('req-1','gap-plan-v1')"
        )
        # 同 request 不同 version → 允许(换代际重问是新事实)
        cur.execute(
            "INSERT INTO gap_assistant_audit (assistant_request_id, snapshot_version)"
            " VALUES ('req-1','gap-plan-v2')"
        )
    with pytest.raises(psycopg2.errors.UniqueViolation):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO gap_assistant_audit (assistant_request_id, snapshot_version)"
                " VALUES ('req-1','gap-plan-v1')"
            )


# ────────────────────────────────────────────────────────────────
# §4 幂等性(部署清单会重复跑)
# ────────────────────────────────────────────────────────────────

def test_migration_is_idempotent(fresh_db):
    """再跑一遍必须不报错,且列/表/数据一个不多一个不少。"""
    conn, run_again = fresh_db

    cols_before = _columns(conn, "media_outlets")
    tables_before = _tables(conn)
    _seed_snapshot(conn)

    run_again()  # 第二次

    assert _columns(conn, "media_outlets") == cols_before
    assert _tables(conn) == tables_before
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM gap_plan_snapshots")
        assert cur.fetchone()["n"] == 1, "重复跑迁移不得影响已有数据"


def test_migration_is_registered_in_manifest():
    """未登记 = prestart 永远不跑它(2026-08-02 创作中心包实例)。"""
    from pathlib import Path
    import re

    root = Path(__file__).resolve().parents[2]
    src = (root / "db" / "migration_manifest.py").read_text(encoding="utf-8")
    # 🔴 先剥注释再找 —— 否则"注释里提到过"会被当成"已登记"
    stripped = re.sub(r"#.*$", "", src, flags=re.MULTILINE)
    block = re.search(r"MIGRATIONS\s*=\s*\[(.*?)^\]", stripped, re.S | re.M)
    assert block, "找不到 MIGRATIONS 列表"
    body = block.group(1)

    assert '"db/migration_029_gap_operation_plan_2026_08_08.sql"' in body
    # 反向对照:剥注释这一步真的有效 —— 一个只在注释里出现过的名字必须查不到
    assert "rollback_029" not in body
