"""#184 d2 · 下单后回写作品观测列 —— 判据包夹具。

夹具库**从生产 schema dump 生成**,不手写 CREATE TABLE:
手写会漏掉 034 那条 `uq_geo_douyin_task_active_generation`
(同 post 只允许一个 pending/running 且未 superseded 的任务),
本包**另起一个库**(不是 d1 那个):复核期间 Review 在 d1 的库上注毒,
共用一个库两边都会读到对方的中间态。
"""
import io
import json
import os
import subprocess
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

PROD_SCHEMA = Path("C:/AI-Test/.deploy_toolkit/prod_schema_2026-09-05.sql")
PG_CONTAINER = "defgeo-c14-62-pg"
DB_NAME = "geo_c14_184d2_test"
ADMIN_DSN = "postgresql://geo_admin:testpw@localhost:55492/postgres"
TEST_DSN = f"postgresql://geo_admin:testpw@localhost:55492/{DB_NAME}"

BRAND_ID = 918421
USER_ID = 918422


def _sql(dsn, statements, autocommit=True):
    conn = psycopg2.connect(dsn)
    conn.autocommit = autocommit
    try:
        cur = conn.cursor()
        for stmt, args in statements:
            cur.execute(stmt, args)
    finally:
        conn.close()


def _database_exists() -> bool:
    conn = psycopg2.connect(ADMIN_DSN)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (DB_NAME,))
        return cur.fetchone() is not None
    finally:
        conn.close()


def _build_database():
    if not PROD_SCHEMA.exists():
        raise RuntimeError(f"生产 schema dump 不在: {PROD_SCHEMA}")
    _sql(ADMIN_DSN, [(f'CREATE DATABASE "{DB_NAME}"', None)])
    sql = io.open(PROD_SCHEMA, encoding="utf-8").read()
    sql = "\n".join(
        line for line in sql.splitlines()
        if not line.startswith(chr(92))   # psql 元命令行(以反斜杠开头),psycopg2 不认
    )
    proc = subprocess.run(
        ["docker", "exec", "-i", PG_CONTAINER, "psql", "-v", "ON_ERROR_STOP=0",
         "-U", "geo_admin", "-d", DB_NAME],
        input=sql.encode("utf-8"), capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"恢复生产 schema 失败: {proc.stderr.decode('utf-8', 'replace')[-2000:]}")


def _shape_is_intact() -> bool:
    """库还是不是「生产 schema 的样子」—— 库在 ≠ 库是我要的那个世界。

    这台机器上多个判据包共用一个 pg 容器,邻包里有专门把表改成 dump 形状的判据;
    09-12 我就是这么被坑的:先全绿,跑完邻包再跑整包红在「列不存在」。
    """
    conn = psycopg2.connect(TEST_DSN)
    try:
        cur = conn.cursor()
        for table, column in (("users", "username"),
                              ("geo_douyin_posts", "active_revision_id"),
                              ("geo_douyin_posts", "generation_epoch"),
                              ("geo_douyin_post_tasks", "superseded_at"),
                              ("geo_douyin_post_revisions", "asset_manifest")):
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name=%s AND column_name=%s", (table, column))
            if cur.fetchone() is None:
                return False
        # 🔴 本包 J2 钉的就是这条唯一索引,它必须真的在
        cur.execute("SELECT 1 FROM pg_class WHERE relname="
                    "'uq_geo_douyin_task_active_generation'")
        return cur.fetchone() is not None
    finally:
        conn.close()


def _drop_database():
    _sql(ADMIN_DSN, [
        ("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (DB_NAME,)),
        (f'DROP DATABASE IF EXISTS "{DB_NAME}"', None),
    ])


def pytest_configure(config):
    """在任何业务模块 import 之前把库建好 —— db.connection 加载时会缓存 DSN。

    🔴 本包**钉死**在自己的库上,不接受外部 TEST_DATABASE_URL:
       共用一个库时,谁先 import db.connection 谁定 DSN,另一个包会安静地跑在别人的库上。
    """
    os.environ["TEST_DATABASE_URL"] = TEST_DSN
    os.environ["DATABASE_URL"] = TEST_DSN
    if not _database_exists():
        _build_database()
    elif not _shape_is_intact():
        _drop_database()
        _build_database()


def _conn():
    conn = psycopg2.connect(TEST_DSN)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    conn.autocommit = True
    return conn


def seed_brand_and_user():
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("""INSERT INTO users (id, username, password_hash, display_name)
                       VALUES (%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING""",
                    (USER_ID, "c184d2_owner", "x", "#184 服务商"))
        cur.execute("""INSERT INTO brands (id, name, owner_user_id)
                       VALUES (%s,%s,%s) ON CONFLICT (id) DO NOTHING""",
                    (BRAND_ID, "QZQZ木作", USER_ID))
    finally:
        conn.close()


def make_post(*, cards=3, status="generating") -> int:
    """种一条作品(N 张卡与对应 oss_keys),返回 post_id。"""
    seed_brand_and_user()
    oss = ["geo/img/c184/%d.png" % i for i in range(cards)]
    # 🔴 卡片要带 `kind` 与 `prompt`:`build_redraw_prompt` 先按 kind 重建,
    #    快照缺失时退回落库时存的原 prompt。两样都没有 ⇒ 直接返
    #    「这张卡缺少可重做的底稿」,重抽根本进不到要测的那一段。
    def _kind(i):
        if i == 0:
            return "cover"
        return "closing" if i == cards - 1 else "content"

    cards_json = json.dumps(
        [{"index": i, "oss_key": oss[i], "text": "卡%d" % i, "kind": _kind(i),
          "headline": "标题%d" % i, "prompt": "原始底稿 prompt %d" % i}
         for i in range(cards)], ensure_ascii=False)
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO geo_douyin_posts
                   (brand_id, created_by, industry_key, city, keyword, content_type,
                    title, body_text, hashtags, cards, oss_keys, cover_oss_key,
                    status, style_key, contact_enabled, aspect_ratio, generation_meta)
               VALUES (%s,%s,'muzuo','深圳','木作定制','cards',
                       '标题','正文', %s::jsonb, %s::jsonb, %s::jsonb, %s,
                       %s,'style_a', true, '3:4', %s::jsonb)
               RETURNING id""",
            (BRAND_ID, USER_ID, json.dumps(["#木作"]), cards_json,
             json.dumps(oss), oss[0], status, json.dumps({"style_catalog_version": "v1"})),
        )
        return int(cur.fetchone()["id"])
    finally:
        conn.close()


def post_row(post_id: int) -> dict:
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM geo_douyin_posts WHERE id=%s", (int(post_id),))
        return dict(cur.fetchone())
    finally:
        conn.close()


def tasks_of(post_id: int) -> list:
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, status, superseded_at FROM geo_douyin_post_tasks "
                    "WHERE post_id=%s ORDER BY id", (int(post_id),))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def revisions_of(post_id: int) -> list:
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM geo_douyin_post_revisions WHERE geo_post_id=%s "
                    "ORDER BY post_revision_id", (int(post_id),))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


@pytest.fixture(autouse=True, scope="session")
def _assert_code_under_test_uses_my_db():
    """让**被测代码自己**报它连的是哪个库。

    同进程跟别的判据包混跑时,先 import `db.connection` 的那个包定 DSN,
    我会安静地在别人的库上出读数 —— 那种绿是关于另一个世界的。
    """
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT current_database() AS db")
        row = cur.fetchone()
        actual = row["db"] if isinstance(row, dict) else row[0]
    finally:
        conn.close()
    assert actual == DB_NAME, (
        f"被测代码连的是 {actual!r},不是本包的 {DB_NAME!r} —— 本包请单独跑。")


@pytest.fixture(autouse=True)
def _clean_publish_tables():
    """每条判据前清掉发布侧残留 —— 本包库是私有的。

    🔴 不清的话上一次跑留下的行会让下一次红:`mhz_publish_order_items`
       上有 `source_post_revision_id` 全局唯一(一个版本只能被发一次),
       而夹具的 revision 是按 post_id 派生的,跨运行会撞。
       那种红看起来像被测对象坏了,实际是**残留**。判据包必须可重复跑。
    """
    conn = _conn()
    try:
        cur = conn.cursor()
        for t in ("mhz_publish_order_items", "mhz_publish_orders",
                  "geo_douyin_short_video_drafts"):
            try:
                cur.execute("DELETE FROM %s" % t)
            except Exception:
                conn.rollback()
        # 🔴 作品的发布态也要清:`converge_publishing_posts` 扫的是**全库**
        #    `publish_status='publishing'` 的作品,带 `LIMIT`。历次跑残留的
        #    publishing 作品会把 limit 占满,被测那条**根本进不了扫描集** ——
        #    于是断言从来没被评估过,判据恒绿。
        #    (这条是注毒抓出来的:去掉排除谓词,主臂居然不红。)
        # 🔴 **不吞异常**:清理失败必须当场炸。吞掉的失败会造出一个"错的已知"
        #    ——我以为清过了,实际没清,于是判据在一个我以为不存在的残留上跑。
        # `publish_item_ids` 是 **NOT NULL**(生产 schema),置 NULL 会违约 ——
        # 而这正是上一版被吞掉的那个异常:我以为清过了,实际一行没清。
        cur.execute("UPDATE geo_douyin_posts SET publish_status=NULL,"
                    " publish_order_id=NULL, publish_item_ids='[]'::jsonb,"
                    " published_url=NULL")
    finally:
        conn.close()
    yield
