"""publish_records 冷启动建表 + 列 safe-add(新库 / 冷启动那条路)。

[WO_273 · 2026-09-23] 浏览器插件后端整体退役时,从插件后端模块的 ``_init_extension_tables``
**按字节原样**切出(基线 01cf3a9e2):那个函数是全仓**唯一**建 ``publish_records`` 的地方,
而插件退役后这张表仍有 15 处以上在役读者(已分发 UNION / 统一记录列表 / 数据健康 /
交付计划 / 实验登记 / 发布阶段投影 / 核实器 cron …),表与存量数据全部保留。

🔴 本仓规则(原注释,见下方 R3 §④):新列必须**同时**进 startup safe-add 与 schema contract
   (``services/geo_article_v14_schema_contract.py``)—— 迁移在 prestart 跑,
   而 safe-add 是**冷启动 / 新库**那条路。
🔴 搬家锁:本函数执行的 DDL 文本与基线**逐字相同**
   (``tests/extension_retirement_2026_09_23/test_publish_records_ddl_moved_verbatim.py``)。
   改这里的 SQL 之前先想清楚是不是真要改 —— 那把锁就是为了让「顺手改一下」当场红。
🔴 ``extension_profiles`` / ``bind_codes`` 的建表**没有**搬:它们唯一的读写方随插件一起删了;
   生产表与存量数据保留,不 DROP。
"""
import logging

logger = logging.getLogger("GEO-PublishRecords")


def _get_connection():
    from db.connection import get_connection
    return get_connection()


def init_publish_records_table():
    """确保 publish_records 表及其列存在(server.py 启动时同步调用;失败只记 warning,不阻断启动)"""
    try:
        conn = _get_connection()
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS publish_records (
                id SERIAL PRIMARY KEY,
                user_id TEXT,
                article_title TEXT,
                platform TEXT,
                account_name TEXT,
                profile_name TEXT,
                instance_id TEXT,
                status TEXT DEFAULT 'pending',
                draft_url TEXT,
                error_message TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_pr_user_time ON publish_records(user_id, created_at DESC)")
        try:
            c.execute("ALTER TABLE publish_records ADD COLUMN IF NOT EXISTS brand_id INTEGER")
        except Exception:
            pass
        # 新增：给 publish_records 加 request_id 字段，让 /publish-result fallback 能跨 worker 查
        try:
            c.execute("ALTER TABLE publish_records ADD COLUMN IF NOT EXISTS request_id TEXT")
            c.execute("CREATE INDEX IF NOT EXISTS idx_pr_request_id ON publish_records(request_id)")
        except Exception:
            pass
        # 新增：article_id 字段。原表只记 article_title 没法跟 articles 表 JOIN，导致前端
        # "已分发"分组（用 /api/meijiehezi/published-articles 判断）看不到自助发布的文章。
        # 加了 article_id 后，published-articles 接口可以 UNION 进 self-publish 成功记录。
        try:
            c.execute("ALTER TABLE publish_records ADD COLUMN IF NOT EXISTS article_id INTEGER")
            c.execute("CREATE INDEX IF NOT EXISTS idx_pr_article_id ON publish_records(article_id)")
        except Exception:
            pass
        for _column, _sql_type in (
            ("submitted_title_snapshot", "TEXT"),
            ("submitted_content_snapshot", "TEXT"),
            ("submitted_content_snapshot_hash", "CHAR(64)"),
            ("submitted_content_snapshot_at", "TIMESTAMPTZ"),
            ("submitted_content_snapshot_source", "VARCHAR(80)"),
            ("public_url", "TEXT"),
            ("public_url_reported_explicitly", "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("public_url_report_source", "VARCHAR(80)"),
            # [WO 自报收口 2026-08-19] 来源位(上面那个 reported_explicitly)之外再加
            # **权威位**:服务端核没核实过。两者不可混用 —— 混用正是本次病灶。
            # CHECK 约束与回填在 scripts/migration_publish_records_url_verification_2026_08_19.sql。
            ("public_url_verification_state", "VARCHAR(32) NOT NULL DEFAULT 'unverified'"),
            # 🔴 R3 §④:`_source` 漏在这张表外面过一次。本仓既有规则是"新列必须
            #    同时进 startup safe-add 与 schema contract" —— 迁移在 prestart 跑,
            #    而 safe-add 是**冷启动/新库**那条路;少一列就意味着某条路上的库
            #    没有它,而 verified 的合法性判定正挂在它身上。
            ("public_url_verification_source", "VARCHAR(40)"),
            ("public_url_verified_at", "TIMESTAMPTZ"),
            ("public_url_verification_method", "VARCHAR(80)"),
            ("public_url_verification_detail", "JSONB"),
            # [R3 §①] 可达轴(页面此刻还在不在)—— 与核实轴分开的第二根轴
            ("public_url_availability_state", "VARCHAR(32)"),
            ("public_url_availability_source", "VARCHAR(40)"),
            ("public_url_availability_checked_at", "TIMESTAMPTZ"),
            ("public_url_availability_detail", "JSONB"),
        ):
            try:
                c.execute(
                    f"ALTER TABLE publish_records ADD COLUMN IF NOT EXISTS {_column} {_sql_type}"
                )
            except Exception:
                pass
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"publish_records 表初始化: {e}")
