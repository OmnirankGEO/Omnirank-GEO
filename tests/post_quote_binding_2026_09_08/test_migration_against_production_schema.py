"""#150 §3.2 返修 · 迁移必须打**生产 schema**,不是夹具建的库。

## 我上一版栽在哪

第一版的 4 维核验说「`geo_douyin_posts` 现无这两列」—— 那是在**夹具/init_db 建的库**
上量的。生产上 `quote_id` 早就有(migration_034,08-17,类型 **bigint**),
而且 `idx_geo_douyin_posts_quote` 已存在、定义还不同:
`(quote_id, contract_revision_id, batch_item_ordinal)
 WHERE quote_id IS NOT NULL AND deleted_at IS NULL`。

🔴 两个教训各记一条:

1. **「验了」不等于「验对了对象」**。我一边在迁移注释里写「夹具不是真相」,
   一边正拿夹具当真相。4 维核验的价值全在**referent 选对**,
   跑在错的库上,四维都绿也证明不了任何事。
2. **`IF NOT EXISTS` 的存在性守卫只看名字**。同名不同定义照样跳过 ——
   「我以为建了、其实没建」,而且没有任何东西会说话。

所以本文件的断言一律打**从 09-05 生产 dump 建起来、再跑本迁移**的库,
读 `information_schema` / `pg_indexes`,不扫迁移文本。
"""

from __future__ import annotations

import os
import pathlib
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import RealDictCursor  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
MIGRATION = REPO / "db" / "migration_055_geo_douyin_post_quote_binding_2026_09_08.sql"
#: 与本仓既有做法同形(GEOIMG_PROD_SCHEMA_SQL / DEFGEO_P0FIX_PROD_SCHEMA_SQL)。
PROD_SCHEMA = pathlib.Path(os.getenv(
    "GDQ_PROD_SCHEMA_SQL", r"C:/AI-Test/.deploy_toolkit/prod_schema_2026-09-05.sql"))
DSN = os.getenv("TEST_DATABASE_URL")


@pytest.fixture(scope="module")
def prod_db():
    """从生产 dump 建一个一次性库,跑本迁移,交出 DSN。

    ⚠️ 缺 DSN 或缺 dump 时 skip —— 但 skip **不是通过**:
       这一整组是「打真 schema」的唯一保障,跳过等于本次没验。
       报读数时要把 skip 数一起报。
    """
    if not DSN or not PROD_SCHEMA.is_file():
        pytest.skip("需要 TEST_DATABASE_URL 与生产 schema dump(缺一则本组未验证)")
    name = "gdq_prodschema_%s" % uuid.uuid4().hex[:8]
    admin = psycopg2.connect(DSN.rsplit("/", 1)[0] + "/postgres")
    admin.autocommit = True
    admin.cursor().execute('CREATE DATABASE "%s"' % name)
    admin.close()
    dsn = DSN.rsplit("/", 1)[0] + "/" + name
    conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    conn.autocommit = True
    cur = conn.cursor()
    # dump 里有 GRANT 给生产角色 —— 本地没有这些角色会让整份载入失败。
    # 🔴 建角色而不是把 GRANT 过滤掉:过滤等于让「权限相关的 DDL」整类不进这个库,
    #    而我要断言的是**这张表长什么样**,不该顺手削掉 schema 的一部分。
    for _role in ("ai_ops_runner", "geo_readonly"):
        cur.execute(
            "DO $$BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=%s)"
            " THEN EXECUTE format('CREATE ROLE %%I', %s); END IF; END$$",
            (_role, _role))
    cur.execute("\n".join(
        l for l in PROD_SCHEMA.read_text(encoding="utf-8", errors="ignore").splitlines()
        if not l.startswith("\\restrict") and not l.startswith("\\unrestrict")))
    cur.execute("SET search_path = public")
    cur.execute(MIGRATION.read_text(encoding="utf-8"))
    conn.close()
    try:
        yield dsn
    finally:
        admin = psycopg2.connect(DSN.rsplit("/", 1)[0] + "/postgres")
        admin.autocommit = True
        admin.cursor().execute('DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % name)
        admin.close()


def _q(dsn, sql, args=()):
    conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    try:
        cur = conn.cursor()
        cur.execute(sql, args)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def test_the_migration_adds_exactly_one_column(prod_db):
    """🔴 本迁移只新增 `confirmed_keyword_id`,类型 integer。

    `quotes.id` / `confirmed_keywords.id` 生产上都是 integer ⇒ 对齐。
    """
    rows = _q(prod_db,
              "SELECT column_name, data_type FROM information_schema.columns"
              " WHERE table_name='geo_douyin_posts'"
              " AND column_name IN ('confirmed_keyword_id','quote_id')"
              " ORDER BY column_name")
    got = {r["column_name"]: r["data_type"] for r in rows}
    assert got.get("confirmed_keyword_id") == "integer", got
    # quote_id 是 034 留下的**既有 bigint**,本迁移不碰它。
    # 断言它仍是 bigint —— 万一有人"顺手统一"成 integer,那是窄化,会截断。
    assert got.get("quote_id") == "bigint", (
        "quote_id 不再是 034 的 bigint —— 窄化会截断:%r" % got)


def test_the_referenced_ids_are_integer_on_production(prod_db):
    """类型对齐的**依据**也打真库,不靠记忆。

    (第一版我把 `scripts/fixtures` 里的 BIGSERIAL 当成了真相。)
    """
    rows = _q(prod_db,
              "SELECT table_name, data_type FROM information_schema.columns"
              " WHERE column_name='id' AND table_name IN ('quotes','confirmed_keywords')")
    got = {r["table_name"]: r["data_type"] for r in rows}
    assert got == {"quotes": "integer", "confirmed_keywords": "integer"}, got


def test_the_migration_does_not_touch_the_existing_quote_index(prod_db):
    """🔴 既有的 `idx_geo_douyin_posts_quote` 必须**原样保留**。

    它的定义是 `(quote_id, contract_revision_id, batch_item_ordinal)
    WHERE quote_id IS NOT NULL AND deleted_at IS NULL` ——
    比我原来想建的更严。`CREATE INDEX IF NOT EXISTS` 只按名字判存,
    同名不同定义会**静默跳过**;我原来那条既建不出来、又让我以为建了。
    """
    rows = _q(prod_db,
              "SELECT indexdef FROM pg_indexes"
              " WHERE tablename='geo_douyin_posts'"
              " AND indexname='idx_geo_douyin_posts_quote'")
    assert len(rows) == 1, "既有报价索引不见了:%r" % rows
    d = rows[0]["indexdef"]
    assert "contract_revision_id" in d and "batch_item_ordinal" in d, (
        "既有索引被改了定义:%s" % d)
    assert "deleted_at IS NULL" in d, d


def test_the_new_keyword_index_really_exists(prod_db):
    """新索引**真的建出来了** —— 名字未被占用,所以不是静默跳过。

    只断言「迁移里写了 CREATE INDEX」证明不了这件事,那正是上一版的洞。
    """
    rows = _q(prod_db,
              "SELECT indexdef FROM pg_indexes WHERE tablename='geo_douyin_posts'"
              " AND indexname='idx_geo_douyin_posts_confirmed_keyword'")
    assert len(rows) == 1, "词维度索引没建出来(可能被同名守卫静默跳过)"
    assert "confirmed_keyword_id" in rows[0]["indexdef"]
    assert "IS NOT NULL" in rows[0]["indexdef"], "没做成部分索引"


def test_replaying_the_migration_is_idempotent(prod_db):
    """幂等重放:prestart 会重复跑它。"""
    conn = psycopg2.connect(prod_db)
    conn.autocommit = True
    try:
        conn.cursor().execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        conn.close()
