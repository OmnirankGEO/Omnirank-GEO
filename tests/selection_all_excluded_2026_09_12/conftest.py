"""#178 客户选词页「全部排除」死胡同 —— 判据包夹具。

夹具库**从生产 schema dump 生成**,不手写 CREATE TABLE:
手写夹具会漏 CHECK 约束,于是判据能写进生产会拒绝的值,
证明的是一件生产上不成立的事(#171 记的就是这个)。
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
DB_NAME = "geo_c14_178_test"
ADMIN_DSN = "postgresql://geo_admin:testpw@localhost:55492/postgres"
TEST_DSN = f"postgresql://geo_admin:testpw@localhost:55492/{DB_NAME}"

BRAND_NAME = "韵宝钢琴"
USER_ID = 917801
BRAND_ID = 917802

# 🔴 一个 quote 只能有一条选词会话:`keyword_selection_sessions.quote_id` 上有
#    UNIQUE 约束(生产 schema 里就有 —— 手写夹具时我不会想到加,于是判据会在
#    一个生产上不可能存在的世界里全绿)。所以每条测试会话配自己的 quote。
def quote_id_for(token: str) -> int:
    import zlib
    return 917900 + (zlib.crc32(token.encode("utf-8")) % 9000)

# 经 services.commercial_query_policy.evaluate 实测分档(不是猜的,见交付物 §2 读数表):
#   knowledge   —— 传不传品牌名都不可交付
#   brand_direct—— **只有**传了品牌名才可交付(本单判别力全在这一行上)
#   commercial  —— 传不传都可交付(反向对照:证明"全绿"不是因为什么都放行)
KW_KNOWLEDGE = ["钢琴为什么有88个键", "钢琴的历史", "什么是三角钢琴"]
KW_BRAND_DIRECT = "韵宝钢琴怎么样"
KW_COMMERCIAL = "钢琴培训哪家好"


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
    # \restrict / \unrestrict 是 psql 元命令,psycopg2 不认;逐行剔掉。
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
    """库还是不是"生产 schema 的样子"。

    🔴 为什么不是"库在就行":这台机器上多个判据包共用一个 pg 容器,
       邻包里有专门把表改成"dump 形状"的判据(defgeo_v5a 的 x1)。
       我第一次就是这么被坑的:我的包先全绿,跑完邻包再跑就整包红在
       `users.username 不存在` —— 库名没变、连得上、只是**形状被别人改了**。
       库存在 ≠ 库是我要的那个世界。
    """
    conn = psycopg2.connect(TEST_DSN)
    try:
        cur = conn.cursor()
        for table, column in (("users", "username"), ("quotes", "brand_name"),
                              ("keyword_selection_sessions", "business_lines"),
                              ("notification_outbox", "event_key")):
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name=%s AND column_name=%s", (table, column))
            if cur.fetchone() is None:
                return False
        return True
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
       共用一个库时,谁先 import db.connection 谁定 DSN,另一个包安静地跑在
       别人的库上。所以这里直接覆盖,而不是 setdefault。
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


def make_keywords(include_brand_direct=False, include_commercial=False, line_id=1):
    items = [
        {"id": idx + 1, "keyword": text, "business_line_id": line_id, "intent": None}
        for idx, text in enumerate(KW_KNOWLEDGE)
    ]
    if include_brand_direct:
        items.append({"id": 90, "keyword": KW_BRAND_DIRECT, "business_line_id": line_id, "intent": None})
    if include_commercial:
        items.append({"id": 91, "keyword": KW_COMMERCIAL, "business_line_id": line_id, "intent": None})
    return items


def seed_session(token: str, *, keywords, status="selecting", selected_keyword_ids=None,
                 business_line_selected=False, extra_line=False):
    """种一条选词会话(以及它依赖的 user/brand/quote)。

    extra_line=True 时多一条**没有任何关键词挂靠**的业务方向(id=2),
    用来造「该方向下一条候选词都没有」那一档(#178-B)。
    """
    business_lines = [{
        "id": 1, "name": "钢琴培训", "description": "",
        "is_selected": bool(business_line_selected),
        "example_scenarios": [],
    }]
    if extra_line:
        business_lines.append({
            "id": 2, "name": "钢琴租赁", "description": "",
            "is_selected": False, "example_scenarios": [],
        })
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO users (id, username, password_hash, display_name)
               VALUES (%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING""",
            (USER_ID, f"c178_owner_{USER_ID}", "x", "#178 报价方"),
        )
        cur.execute(
            """INSERT INTO brands (id, name, owner_user_id)
               VALUES (%s,%s,%s) ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name""",
            (BRAND_ID, BRAND_NAME, USER_ID),
        )
        cur.execute(
            """INSERT INTO quotes (id, brand_id, brand_name)
               VALUES (%s,%s,%s) ON CONFLICT (id) DO UPDATE SET brand_name=EXCLUDED.brand_name""",
            (quote_id_for(token), BRAND_ID, BRAND_NAME),
        )
        cur.execute("DELETE FROM keyword_selection_sessions WHERE token=%s", (token,))
        cur.execute(
            """INSERT INTO keyword_selection_sessions
                   (token, quote_id, brand_id, keywords_snapshot, status, expires_at,
                    business_lines, selected_keyword_ids)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (token, quote_id_for(token), BRAND_ID, json.dumps(keywords, ensure_ascii=False), status,
             "2099-01-01 00:00:00", json.dumps(business_lines, ensure_ascii=False),
             json.dumps(selected_keyword_ids) if selected_keyword_ids is not None else None),
        )
    finally:
        conn.close()
    return token


def read_session(token: str) -> dict:
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM keyword_selection_sessions WHERE token=%s", (token,))
        return dict(cur.fetchone())
    finally:
        conn.close()


def outbox_rows(token: str, event_type="selection.no_deliverable_keywords"):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, event_key, title, content, route FROM notification_outbox "
            "WHERE event_type=%s AND business_id=%s ORDER BY id",
            (event_type, f"quote:{quote_id_for(token)}"),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def clear_outbox(token: str):
    _sql(TEST_DSN, [("DELETE FROM notification_outbox WHERE business_id=%s",
                     (f"quote:{quote_id_for(token)}",))])


@pytest.fixture(scope="session")
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from db.connection import get_connection
    from api.selection_api import router

    # 🔴 被测代码**实际**连上的是哪个库 —— 不是我 export 的那个,是它自己解析出来的。
    #    和别的包同进程跑时,先 import db.connection 的那个包定 DSN,我会安静地
    #    在别人的库上出读数。这一行让那种情况当场红,而不是给出一份关于另一个世界的绿。
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT current_database() AS db")
        row = cur.fetchone()
        actual = row["db"] if isinstance(row, dict) else row[0]
    finally:
        conn.close()
    assert actual == DB_NAME, (
        f"被测代码连的是 {actual!r},不是本包的 {DB_NAME!r} —— "
        "多半是和别的判据包同进程跑,DSN 被先 import 的那个包定死了。本包请单独跑。")

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)
