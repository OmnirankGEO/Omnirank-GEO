"""034 迁移 + GEO 图文 schema readiness 的 PG16 真库行为判据。

🔴 判据形态:每条「必须命中」都配一条「必须不命中」。
   本文件的判别力全靠 ③ ——「删掉任意一项 → 对应 blocker 出现」。
   没有 ③,①（跑过迁移零 blocker）可能只是因为检查器什么都没查。
"""
from __future__ import annotations

import os
import pathlib
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import RealDictCursor  # noqa: E402

from services.geo_douyin_schema_contract import (  # noqa: E402
    EXPECTED_COLUMNS,
    EXPECTED_CONSTRAINTS,
    EXPECTED_INDEXES,
    EXPECTED_TABLES,
    assert_schema_ready,
    schema_blockers,
)

REPO = pathlib.Path(__file__).resolve().parents[2]
MIGRATION = REPO / "db" / "migration_034_geo_image_note_contract_2026_08_17.sql"
# 生产 schema 夹具(schema-only,零客户数据)。缺失时整文件 skip 并说明原因,
# 不用"没有夹具所以全绿"冒充通过。
PROD_SCHEMA = pathlib.Path(
    os.getenv("GEOIMG_PROD_SCHEMA_SQL", r"C:/AI-Test/.deploy_toolkit/_geoimg_prodschema_20260817.sql")
)
DSN = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DSN or not PROD_SCHEMA.is_file(),
    reason="需要 TEST_DATABASE_URL 与生产 schema 夹具(GEOIMG_PROD_SCHEMA_SQL)",
)


def _admin_dsn() -> str:
    return DSN.rsplit("/", 1)[0] + "/postgres"


def _new_db() -> str:
    """每个测试一个独立库。

    🔴 必须 CREATE DATABASE 而不是 schema 隔离:生产 pg_dump 里带
       `set_config('search_path','',false)`,它会毒死同连接后续语句(本仓 2026-08-12 实证)。
    🔴 库名保留 `test` 字样 —— conftest 的安全栓靠它,库名不含 test 会让安全栓全灭
       (2026-08-15 实证:双臂 0 junit 的"无新增红"就是这么来的)。
    """
    name = f"geoimg_test_{uuid.uuid4().hex[:10]}"
    conn = psycopg2.connect(_admin_dsn())
    conn.autocommit = True
    conn.cursor().execute(f'CREATE DATABASE "{name}"')
    conn.close()
    return name


def _drop_db(name: str) -> None:
    conn = psycopg2.connect(_admin_dsn())
    conn.autocommit = True
    conn.cursor().execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    conn.close()


def _connect(name: str):
    return psycopg2.connect(
        DSN.rsplit("/", 1)[0] + "/" + name, cursor_factory=RealDictCursor
    )


def _load_prod_schema(cur) -> None:
    sql = "\n".join(
        line for line in PROD_SCHEMA.read_text(encoding="utf-8", errors="ignore").splitlines()
        # \restrict / \unrestrict 是 psql 元命令,psycopg2 不认;剥掉不影响 DDL
        if not line.startswith("\\restrict") and not line.startswith("\\unrestrict")
    )
    cur.execute(sql)
    # pg_dump 把 search_path 设成空 —— 不复位的话后续所有不带 schema 限定的语句都找不到表
    cur.execute("SET search_path = public")


def _run_migration(cur) -> None:
    cur.execute(MIGRATION.read_text(encoding="utf-8"))


@pytest.fixture()
def fresh_db():
    name = _new_db()
    try:
        yield name
    finally:
        _drop_db(name)


@pytest.fixture()
def migrated(fresh_db):
    conn = _connect(fresh_db)
    conn.autocommit = True
    cur = conn.cursor()
    _load_prod_schema(cur)
    _run_migration(cur)
    try:
        yield cur
    finally:
        conn.close()


@pytest.fixture()
def unmigrated(fresh_db):
    conn = _connect(fresh_db)
    conn.autocommit = True
    cur = conn.cursor()
    _load_prod_schema(cur)
    try:
        yield cur
    finally:
        conn.close()


# ============================================================
# ① 正向:跑过迁移 → 零 blocker
# ============================================================

def test_migrated_schema_is_ready(migrated):
    blockers = schema_blockers(migrated)
    assert blockers == [], f"跑过 034 迁移后仍有阻塞项:{blockers}"
    assert_schema_ready(migrated)  # 不抛


# ============================================================
# ② 反向对照:没跑迁移 → 必须报一堆 blocker
#    没有这条,①的"零 blocker"可能只是检查器空转
# ============================================================

def test_unmigrated_schema_is_not_ready(unmigrated):
    blockers = schema_blockers(unmigrated)
    assert blockers, "没跑迁移却报就绪 = 检查器恒真,readiness 形同虚设"
    # 该报的三类都要报到,不能只报一类
    assert any(b.startswith("missing_table:") for b in blockers), blockers[:10]
    assert any(b.startswith("missing_column:") for b in blockers), blockers[:10]
    assert any(b.startswith("missing_constraint:") for b in blockers), blockers[:10]
    with pytest.raises(RuntimeError, match="GEO_IMAGE_NOTE_SCHEMA_NOT_READY"):
        assert_schema_ready(unmigrated)


# ============================================================
# ③ 判别力:逐项拆掉 → 对应 blocker 必须出现
# ============================================================

@pytest.mark.parametrize("table,column", [
    ("geo_douyin_posts", "delivery_slot_key"),
    ("geo_douyin_posts", "generation_epoch"),
    ("mhz_publish_order_items", "settlement_authority"),
    ("mhz_publish_order_items", "capacity_state"),
    ("publish_idempotency_keys", "command_id"),
])
def test_dropping_a_column_turns_it_red(migrated, table, column):
    assert schema_blockers(migrated) == []
    migrated.execute(f"ALTER TABLE {table} DROP COLUMN {column} CASCADE")
    blockers = schema_blockers(migrated)
    assert f"missing_column:{table}.{column}" in blockers, (
        f"删掉 {table}.{column} 后 readiness 仍全绿 —— 该列没有被真正检查"
    )


@pytest.mark.parametrize("constraint,table", [
    ("ck_mhz_item_authority_shape", "mhz_publish_order_items"),
    ("ck_mhz_item_freeze_mode_pairing", "mhz_publish_order_items"),
    ("ck_geo_douyin_task_authority_shape", "geo_douyin_post_tasks"),
    ("ck_publish_idem_kind_shape", "publish_idempotency_keys"),
])
def test_dropping_a_funding_shape_constraint_turns_it_red(migrated, constraint, table):
    """资金/身份形态约束是 H0 的物理保证,删了必须红。"""
    assert schema_blockers(migrated) == []
    migrated.execute(f"ALTER TABLE {table} DROP CONSTRAINT {constraint}")
    assert f"missing_constraint:{constraint}" in schema_blockers(migrated)


def test_dropping_one_to_one_index_turns_it_red(migrated):
    """「同一 active post revision 最多一个有效发布根」的物理保证。"""
    assert schema_blockers(migrated) == []
    migrated.execute("DROP INDEX uq_mhz_item_live_revision_root")
    assert "missing_index:uq_mhz_item_live_revision_root" in schema_blockers(migrated)


# ============================================================
# ④ 契约自身不许是空壳
# ============================================================

def test_contract_is_not_empty():
    """「零 blocker」若是因为期望集合为空,那就是最廉价的恒真。分母先立住。"""
    assert len(EXPECTED_TABLES) >= 3
    assert sum(len(v) for v in EXPECTED_COLUMNS.values()) >= 100
    assert len(EXPECTED_CONSTRAINTS) >= 15
    assert len(EXPECTED_INDEXES) >= 15


# ============================================================
# ⑤ 迁移幂等:重放两遍不炸(prestart 无条件重放)
# ============================================================

def test_migration_is_idempotent_on_replay(migrated):
    _run_migration(migrated)   # 第二遍
    _run_migration(migrated)   # 第三遍
    assert schema_blockers(migrated) == []


# ============================================================
# ⑥ 零 DML:迁移文件文本级 + 反向对照
# ============================================================

def test_migration_contains_no_dml():
    import re
    text = MIGRATION.read_text(encoding="utf-8")
    pattern = re.compile(r"^\s*(UPDATE|DELETE|INSERT)\s", re.IGNORECASE | re.MULTILINE)
    assert not pattern.findall(text), "迁移含 DML,违反工单 §2「迁移 additive 零 DML」"
    # 反向对照:判据对合成 DML 必须命中,否则"无 DML"只是正则抓不到
    assert pattern.findall("UPDATE geo_douyin_posts SET quote_id = 1;\n")


def test_counts_toward_contract_has_no_default(migrated):
    """规格 02 §3.2:旧行**不靠 ADD DEFAULT** 自动改语义。

    给它加 DEFAULT true 会让 30 条历史手工作品一夜之间"计入合同",
    这正是对象身份 H0 要防的事。
    """
    migrated.execute(
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_name='geo_douyin_posts' AND column_name='counts_toward_contract'"
    )
    row = migrated.fetchone()
    assert row is not None
    assert row["column_default"] is None, "counts_toward_contract 被加了 DEFAULT —— 会静默改写历史行语义"
