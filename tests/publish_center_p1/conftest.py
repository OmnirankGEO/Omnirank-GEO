import pytest


@pytest.fixture(scope="session", autouse=True)
def publish_history_schema():
    """Initialize only the durable sources used by the unified read projection."""
    from db.meijiehezi_db import init_mhz_tables
    # [WO_273] publish_records 的建表随插件后端退役,原样搬到 db/publish_records_schema.py(SSOT 换址,不是新写)
    from db.publish_records_schema import init_publish_records_table
    from db.connection import get_connection

    # 🔴 [2026-08-20 WO-D ②] 这里原来是
    #     CREATE TABLE IF NOT EXISTS brands (id SERIAL PRIMARY KEY, name TEXT NOT NULL)
    #   —— 一张**手搓的两列 brands**。它有两个方向的伤害,方向②才是致命的:
    #
    #   ① 正向:一次性库里若已存在**生产形态**的 brands,IF NOT EXISTS 是 no-op,
    #      本文件照常绿 —— 所以这条 DDL 平时看不出毛病。
    #   ② 反向(实测已复现):一次性库里若 brands **还不存在**,这条 DDL 就把它
    #      钉成两列。之后同一个库里**任何**调 `db.diagnosis_db.init_db()` 的测试
    #      都会炸 —— init_db 的 `CREATE TABLE IF NOT EXISTS brands` 同样是 no-op,
    #      紧接着建 `idx_brands_company` 时:
    #          psycopg2.errors.UndefinedColumn: column "company_name" does not exist
    #      于是**下游整片文件红**,红因与它们自己被测的代码零关系。
    #      (2026-08-20 复现:空库 → 先跑本文件 → init_db() 当场 UndefinedColumn。)
    #
    #   ⇒ 夹具不许自己**author** 生产表的 schema。brands 的 SSOT 是
    #     `db.diagnosis_db.init_db()`,这里直接调它,拿到与生产同形的 brands。
    #     配套判据:`test_brands_fixture_matches_production_schema`
    #     (把这段换回两列 DDL,那条判据必红)。
    from db.diagnosis_db import init_db

    init_db()               # brands 等生产表的唯一权威建法
    init_mhz_tables()
    init_publish_records_table()


@pytest.fixture(autouse=True)
def clean_publish_history_tables(publish_history_schema):
    from db.connection import get_connection

    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("""
            TRUNCATE TABLE
                mhz_refund_requests,
                publish_records,
                mhz_synced_orders,
                mhz_publish_order_items,
                mhz_publish_orders
            RESTART IDENTITY CASCADE
        """)
        c.execute("DELETE FROM brands WHERE id >= 900000")
        conn.commit()
    finally:
        conn.close()
    yield
