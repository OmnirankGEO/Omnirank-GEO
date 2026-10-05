# -*- coding: utf-8 -*-
"""brands 表结构的**唯一** SSOT 出口(建表 + 自愈补列 + 索引),外加 _safe_add_column 家族。

🔴 [R5 2026-08-20] 为什么是独立模块而不是留在 db/diagnosis_db.py:
   diagnosis_db 的**模块体最后一行是 `init_db()`** —— `import db.diagnosis_db`
   等于当场建 150 张表。窄出口的全部意义就是「夹具只要 brands,不要那 150 张表」,
   留在那边等于没做。所以搬到这个**零副作用叶子模块**:
   只 import psycopg2,不 import 项目内任何东西,不成环,import 它什么也不会发生。

   (diagnosis_db 反过来 `from db.brands_schema import *` 拿回这些名字 —— 定义只有
    这一份。Owner 裁定 a 的原话:「同一谓词只许写一处 —— 抄第二份就是把 ⑤ 要治的
    病移植进解药」。)

用法(夹具):
    from db.brands_schema import ensure_brands_schema
    ensure_brands_schema(conn.cursor()); conn.commit()
"""

import re

from psycopg2.errors import DuplicateColumn

from db.schema_guard import constraint_def, run_ddl_with_lock_timeout

def _column_exists(cursor, table: str, column: str) -> bool:
    """检查当前 search_path 实际解析到的表是否存在该列。"""
    cursor.execute(
        """
        SELECT 1
          FROM pg_attribute
         WHERE attrelid = to_regclass(%s)
           AND attname = %s
           AND attnum > 0
           AND NOT attisdropped
        """,
        (table, column)
    )
    return cursor.fetchone() is not None


def _safe_add_column(cursor, table: str, column: str, col_type: str):
    """安全添加列：先检查再 ALTER，避免不必要的排他锁。

    🔴 [R4 ③ 2026-08-20] 这里原来是 `except Exception: pass`,注释写「并发竞态」。
       它真正吞掉的却是 **UndefinedTable**:organization 自愈循环当年排在绝大多数
       `CREATE TABLE` **之前**,10 张目标表里 9 张那时还不存在 —— 于是 63 个列的
       ALTER 全部静默失败,列永远补不上,而且**一个字的日志都没有**。
       代价是两轮排查:R2 追到「夹具手搓 quotes 缺 organization_id」,
       R3 才追到「模块体 init_db 在别人事务里发这条 ALTER → 单线程自死锁」。

       ⇒ 现在**只吞真正的竞态**(`duplicate_column`:另一个 worker 刚加过同一列),
         其余一律**响亮抛出**。表不存在 = 调用顺序错了,那是缺陷不是竞态。
    """
    if _column_exists(cursor, table, column):
        return
    try:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    except DuplicateColumn:
        pass  # 真并发竞态：另一个 worker 在我们检查之后、ALTER 之前刚加过


#: init_db 跳过的「表还不存在」记录 —— 供启动日志一次性报出,**不许再静默**
_SAFE_ADD_SKIPPED_TABLES: set = set()


def _safe_add_column_optional(cursor, table: str, column: str, col_type: str):
    """给**不由 init_db 拥有**的表补列:表不在就跳过,但把表名记下来。

    🔴 [R4 ③ 2026-08-20] 与 `_safe_add_column` 的分工是**故意**的:
       · `_safe_add_column`          —— 表**应该**已经在了;不在 = 调用顺序错 = 缺陷 ⇒ 抛。
       · `_safe_add_column_optional` —— 表由别的模块/迁移拥有,启动时序上可能还没建;
         跳过是合法的,但**必须留痕**,否则又变回当年那个吞掉 63 个列的静默黑洞。

       归属实查(2026-08-20 · grep 全仓 CREATE TABLE):
         client_profiles          db/migrate_v3.py
         media_publications       db/monitoring_db.py
         monitoring_tasks         db/monitoring_db.py
         monitoring_reports       db/monitoring_db.py
         price_quotes             scripts/migration_pricing_dual_ssot_2026_07_12.sql
         pricing_catalog_entries  scripts/migration_pricing_dual_ssot_2026_07_12.sql
         publish_orders           db/publish_db.py
         keyword_price_cache_llm  db/migration_008_pricing_llm_first.sql
    """
    cursor.execute("SELECT to_regclass(%s) AS t", (table,))
    _row = cursor.fetchone()
    if _row is None:
        _reg = None
    else:
        _reg = _row["t"] if isinstance(_row, dict) else _row[0]
    if _reg is None:
        _SAFE_ADD_SKIPPED_TABLES.add(table)
        return
    _safe_add_column(cursor, table, column, col_type)


# ==================== [R5 2026-08-20] brands schema SSOT 出口 ====================
# 🔴 为什么把 brands 的建表/自愈/索引三段抽成常量 + 一个出口函数:
#   ⑤ 要治的病是「夹具自己 author 生产表」。原方案(夹具改跑整个 init_db)已被实测
#   证伪 —— tests/geo_observation 转换后 174 passed/0 failed → 138 passed/34 failed,
#   跑时 194s → 589s:把 150 张表拖进一个只要 13 张表的夹具,是拿更大的病换小病。
#   ⇒ 改走**窄出口**:夹具只要一句 `ensure_brands_schema(cursor)` 就拿到与生产同形的
#     brands,不必自己抄列、也不必拖全库。
#
# 🔴 [Owner 裁定 2026-08-20 a] 窄出口**必须复用同一份常量**,不许抄第二份:
#   「同一谓词只许写一处 —— 抄第二份就是把 ⑤ 要治的病移植进解药」。
#   所以 init_db() 里那三段已**整体删除**,改为调用本文件同一个 ensure_brands_schema。
#   现在全仓 brands 的 DDL 只有这一处,init_db 与夹具走的是**同一行代码**,
#   不是「两份长得一样的代码」。

_BRANDS_CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS brands (
        id SERIAL PRIMARY KEY,
        -- [WO P1 2026-08-09] 🔴 这里原来是 `name TEXT UNIQUE NOT NULL`（"品牌名唯一"）。
        --   生产早就不是那样了:实查只有 brands_pkey 与
        --     brands_name_owner_key UNIQUE (name, owner_user_id) WHERE is_deleted = false
        --   —— 唯一性是**按 owner** 的、而且**只管活行**。跨 owner 同名合法
        --   (不同代理服务同一家企业),生产上「浙江岱林生物技术股份有限公司」3 个
        --   owner、「运营方公司」一族 9 个 owner。
        --   建表兜底还留着全局 UNIQUE 的后果:任何 init_db 自举出来的新库
        --   (CI / 新 staging / 重建预演)会长回全局约束,第二个用户用同一个名字
        --   就**直接 500**。真实约束改由下面 §索引 那段建(与生产迁移对齐)。
        name TEXT NOT NULL,               -- 品牌名（唯一性见 brands_name_owner_key)
        -- [21 班合后修 2026-08-10] 🔴 这一列必须在建表里,不能只靠历史迁移。
        --   同一个 init_db() 在下面 §索引 那段要建
        --     `brands_name_owner_key ... WHERE is_deleted = false`,
        --   而全仓**没有任何** `_safe_add_column(cursor,"brands","is_deleted",...)`
        --   (反向对照:social_enabled/cities/business_type 都有,唯独它没有)。
        --   于是全新库自举 = 建表没这列 → 建索引 UndefinedColumn → init_db 整个炸,
        --   正是 tiertgt 那一版声称要修的「CI / 新 staging / 重建预演」场景本身。
        --   生产不受影响(列由历史迁移早已存在,两条 DDL 都是 no-op)—— 也正因为
        --   这样它才一直没被发现:那套锁的 fixture 自己 ALTER TABLE 把列补上了。
        --   类型对齐生产实查:boolean · nullable · default false。
        is_deleted BOOLEAN DEFAULT FALSE, -- 软删除标记（唯一索引只管活行）
        company_name TEXT,                -- 公司名（可选，同一公司多品牌）
        industry TEXT,                    -- 行业
        industry_category TEXT,           -- 行业大类
        contact TEXT,                     -- 联系人
        notes TEXT,                       -- 备注
        owner_user_id INTEGER,            -- 品牌所属用户ID（多租户）
        diagnosis_count INTEGER DEFAULT 0, -- 诊断次数
        latest_score INTEGER,             -- 最新评分
        latest_diagnosis_id INTEGER,      -- 最新诊断ID
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
"""

# 🔴 [R4 ① 2026-08-20] 位置说明:这段**必须夹在**「CREATE TABLE brands」之后、
#   「CREATE INDEX idx_brands_company/idx_brands_industry」(见本函数 §索引)之前。
#   放 init_db 末尾是不行的 —— 实测索引先建、列还没补,照样 UndefinedColumn。
#   (与 §② 那个 organization 块相反:那块的目标表要等 CREATE TABLE,所以排末尾。
#    同一个函数里两段自愈,**约束方向相反**,别照抄彼此的位置。)
# ==================== [R4 ① 2026-08-20] brands 自愈补列 ====================
# 🔴 brands 长期是**全仓唯一一张没有自愈补列的表**:init_db 要给它建
#   idx_brands_company / idx_brands_industry,却从不补列 ⇒ 任何**已存在但比生产窄**
#   的 brands 都会让 init_db 当场 UndefinedColumn 中断。该病 2026-08-20 一天内
#   犯到第三次(WO-D ② / WO-D-R2 ② / 全仓 14 个夹具各搓各的)。
#
# 🔴 为什么补**生产全集**而不是只补「生产有、建表没有」那 7 列:
#   那 7 列是拿生产 dump diff **本函数的建表语句**得来的,只对「表还不存在、
#   由本函数新建」的库成立。而真正会炸的是**表已经存在且比生产窄**的库
#   —— 那时 `CREATE TABLE IF NOT EXISTS` 是 no-op,建表里那 14 列一个也不会补上。
#   实测:只补 7 列时,(id,name) 两列的 brands 上跑 init_db 仍炸 company_name。
#   ⇒ 自愈清单 = **生产 brands 全列**(id/name 除外:PK 与 NOT NULL 无默认,
#     非空表上 ADD COLUMN 加不了,它们由 CREATE TABLE 负责)。
#
# 下面 31 列是**机械枚举**的,不是手抄:生产整库 dump
#   tests/article_self_report_2026_08_19/prod_schema_2026-08-19.sql 的
#   CREATE TABLE public.brands(33 列)去掉 id/name;类型逐字取自该 dump。
# 🔴 [Owner 裁定 2026-08-20] `user_id` **刻意不在这张表里**。
#   它在生产上与 owner_user_id 并存,但全仓生产代码**零引用** —— 是历史地层,
#   不是现役契约。裁定原话:「零代码引用的化石列不进自愈清单;自愈清单表达的是
#   现役生产契约,不是历史地层」。生产上那一列**原样不动**(drop 是红线)。
#   ⇒ 自举库缺 user_id **不算缺陷**,判据里已显式豁免。
_BRANDS_SELF_HEAL_COLUMNS = (
    ("agent_payment_note", "jsonb"),
    ("brand_code", "text"),
    ("brand_display_names", "text"),
    ("brand_type", "text DEFAULT 'legacy'"),
    ("business_type", "VARCHAR(20) DEFAULT 'B2C'"),
    ("cities", "text"),
    ("city_scope", "VARCHAR(20) DEFAULT 'local'"),
    ("company_name", "text"),
    ("competitors_jsonb", "jsonb DEFAULT '[]'"),
    ("contact", "text"),
    ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
    ("deleted_at", "TIMESTAMP"),
    ("deleted_reason", "text"),
    ("diagnosis_count", "integer DEFAULT 0"),
    ("industry", "text"),
    ("industry_category", "text"),
    ("is_deleted", "boolean DEFAULT false"),
    ("is_starred", "boolean DEFAULT false NOT NULL"),
    ("is_test", "boolean DEFAULT false"),
    ("is_test_locked", "boolean DEFAULT false"),
    ("latest_diagnosis_id", "integer"),
    ("latest_score", "integer"),
    ("notes", "text"),
    ("owner_user_id", "integer"),
    ("parent_brand_id", "integer"),
    ("seed_keywords", "jsonb DEFAULT '[]'"),
    ("social_enabled", "boolean DEFAULT false"),
    ("starred_at", "TIMESTAMP"),
    ("status", "text DEFAULT 'active'"),
    ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
)

#: brands 的索引/唯一约束 —— 与生产逐字一致,原来长在 init_db 的 indexes 列表里。
_BRANDS_INDEX_STATEMENTS = (
    # brands表索引
    "CREATE INDEX IF NOT EXISTS idx_brands_name ON brands(name)",
    "CREATE INDEX IF NOT EXISTS idx_brands_company ON brands(company_name)",
    "CREATE INDEX IF NOT EXISTS idx_brands_industry ON brands(industry_category)",
    "CREATE INDEX IF NOT EXISTS idx_brands_owner_user_id ON brands(owner_user_id)",
    # [WO P1 2026-08-09] 品牌名唯一性的**真实**口径,与生产逐字一致。
    #   生产已有同名索引 → IF NOT EXISTS 是 no-op;新库靠这一条才拿到约束
    #   (在此之前:新库既有错的全局 UNIQUE、又没有这条对的)。
    # 🔴 存量对齐:早于本次修复自举出来的库(CI / 老 staging)已经长了
    #   全局 UNIQUE。**生产没有这个约束**,所以这一句在生产上是 no-op;
    #   它只把那些库拉回生产口径。这是本包唯一一条自愈 DDL,已在交付说明里点名。
    #   [WO_285b] 那条 DROP 挪到下面 _BRANDS_LEGACY_UNIQUE_DROP(游标形态先查约束在不在再执行)。
    "CREATE UNIQUE INDEX IF NOT EXISTS brands_name_owner_key "
    "ON brands (name, owner_user_id) WHERE is_deleted = false",
)

#: [WO_285b] 老库的全局 UNIQUE(brands_name_key)清理。**生产没有这个约束**。
#   游标形态(ensure_brands_schema,init_db 在请求路径上会调)只在约束真存在时执行:
#   `ALTER TABLE … DROP CONSTRAINT IF EXISTS` 即使约束不存在也要先拿 brands 的 ACCESS EXCLUSIVE,
#   发车 pg_dump 期间会排队卡死(本机实测:C 端 GEO 方案任务 API 的测试(已随开源 E3 · B3c G4 删除)就是被这一句与夹具的未提交事务互等锁死)。
#   纯 SQL 形态(brands_schema_sql,给夹具)照旧无条件发射,位置不变:在唯一索引之前。
_BRANDS_LEGACY_UNIQUE_DROP = "ALTER TABLE brands DROP CONSTRAINT IF EXISTS brands_name_key"


def ensure_brands_schema(cursor):
    """把 brands 建成/补成**生产同形**,幂等。init_db 与测试夹具共用的唯一入口。

    做三件事,顺序不可换:
      1. CREATE TABLE IF NOT EXISTS brands —— 表不存在时建;已存在则 no-op。
      2. 自愈补列 —— 覆盖「表已存在但比生产窄」那种库(no-op 的 IF NOT EXISTS
         一列也补不上,这正是 2026-08-20 一天犯三次的那个病)。
      3. 建索引 —— **必须排在补列之后**:brands_name_owner_key 要读 is_deleted,
         idx_brands_company 要读 company_name,列没补上就当场 UndefinedColumn。

    🔴 [Owner 裁定 2026-08-20 b] 同一性判据钉死在
       tests/db_bootstrap_r5/test_ensure_brands_schema.py:
       全新库上「只调本函数」与「跑完整 init_db」产出的 brands 列集必须**逐列相同**。
       今天这条判据由构造保证(init_db 就是调的本函数)⇒ **零区分力**,这是**故意**的:
       它是防漂移绊线 —— 哪天有人再在 init_db 里内联第二份 builder,它当场变红。
       (已亲手注毒验过它会红,见该文件顶部注释。)

    参数只收 cursor 不收 conn:调用方自己管事务/autocommit,
    免得夹具在别人的事务里被本函数偷偷 commit。
    """
    cursor.execute(_BRANDS_CREATE_TABLE_SQL)
    for _bcol, _btype in _BRANDS_SELF_HEAL_COLUMNS:
        _safe_add_column(cursor, "brands", _bcol, _btype)
    for _idx_sql in _BRANDS_INDEX_STATEMENTS:
        if _idx_sql.startswith("CREATE UNIQUE INDEX IF NOT EXISTS brands_name_owner_key"):
            # 原顺序:先清老库的全局 UNIQUE,再建正确的部分唯一索引
            if constraint_def(cursor, "brands", "brands_name_key") is not None:
                run_ddl_with_lock_timeout(cursor, [_BRANDS_LEGACY_UNIQUE_DROP], "brands.brands_name_key 清理")
        cursor.execute(_idx_sql)


def brands_schema_sql() -> str:
    """同一份 SSOT 的**纯 SQL 形态**,给「夹具是一个 .sql 字符串」的场合用。

    用处只有一个:tests/organization_internal_seats 的夹具是
    `BASE_SQL = base_fixture.sql` 这样一整段字符串,还要被 `.replace(...)` 做变体,
    没有 cursor 可交。给它一段 SQL 比把 5 个调用点全改成函数调用干净。

    🔴 它**不是第二个 builder**:三份常量与 ensure_brands_schema 用的是同一份,
       这里只换了"发射方式"(ALTER TABLE ... ADD COLUMN IF NOT EXISTS 代替
       先查后加)。两者产出必须一致,由判据
       tests/db_bootstrap_r5/test_ensure_brands_schema.py 里
       test_sql_emission_matches_the_cursor_emission 钉死 —— 那条**有真区分力**
       (两条路径确实是不同代码,不是构造保证)。
    """
    parts = [_BRANDS_CREATE_TABLE_SQL.strip().rstrip(";") + ";"]
    for _bcol, _btype in _BRANDS_SELF_HEAL_COLUMNS:
        parts.append("ALTER TABLE brands ADD COLUMN IF NOT EXISTS %s %s;" % (_bcol, _btype))
    for _idx_sql in _BRANDS_INDEX_STATEMENTS:
        if _idx_sql.startswith("CREATE UNIQUE INDEX IF NOT EXISTS brands_name_owner_key"):
            parts.append(_BRANDS_LEGACY_UNIQUE_DROP + ";")   # 位置与改前相同(夹具形态照旧无条件)
        parts.append(_idx_sql.strip().rstrip(";") + ";")
    return "\n".join(parts) + "\n"


#: 建索引时被跳过的 (表, 缺的列) —— 供启动日志一次性报出,**不许静默**
_INDEX_SKIPPED: list = []

#: 解析 `... ON <table> (<col>, <col>) [WHERE ...]`
_INDEX_TARGET_RE = re.compile(
    r"\bON\s+(?:public\.)?([a-z_][a-z0-9_]*)\s*\(([^)]*)\)", re.I)


def execute_index_guarded(cursor, idx_sql: str):
    """建索引前先确认目标表/列都在;缺了就**跳过并记账**,不让整个 init_db 当场炸。

    🔴 为什么要这个守卫(2026-08-20 · R2/R4/R5 三轮同一族病):
       init_db 的建索引段假设「表和列都是我自己刚建的」。可**表已经存在且比生产窄**
       的库(一次性测试库 / 老 CI / 老 staging)里,`CREATE TABLE IF NOT EXISTS` 是
       no-op,列一个也补不上,于是
         CREATE INDEX idx_brand_name ON diagnosis_records(brand_name)
       当场 UndefinedColumn ⇒ **整个 init_db 中断**,后面几十张表全没建成。
       实测:tests/geo_observation 那把夹具上一次量到 8 个这样的 (表,列) 缺口。

    🔴 它**不会掩盖**真正的顺序缺陷 —— 配对判据钉死:
       「全新空库单次 init_db 跑完,_INDEX_SKIPPED 必须为**空**」。
       全新库上一张表都不是既存的,任何跳过都只能是 init_db 自己的建表顺序错了。
       (这条是本守卫的必要条件:它绿,守卫才只在「别人的窄表」上生效;
        它红,说明守卫正在替一个真缺陷背锅。)

    解析不出目标(表达式索引 / ALTER TABLE 之类)就**原样执行**,不猜。
    """
    m = _INDEX_TARGET_RE.search(idx_sql)
    if not m:
        cursor.execute(idx_sql)
        return
    table = m.group(1)
    cols = [c.strip().strip('"') for c in m.group(2).split(",")]
    if any((not c) or (not re.fullmatch(r"[a-z_][a-z0-9_]*", c, re.I)) for c in cols):
        cursor.execute(idx_sql)      # 表达式/函数索引 —— 不解析,交给数据库
        return
    cursor.execute("SELECT to_regclass(%s) AS t", (table,))
    _row = cursor.fetchone()
    _reg = None if _row is None else (_row["t"] if isinstance(_row, dict) else _row[0])
    if _reg is None:
        _INDEX_SKIPPED.append((table, ["<表不存在>"]))
        return
    missing = [c for c in cols if not _column_exists(cursor, table, c)]
    if missing:
        _INDEX_SKIPPED.append((table, missing))
        return
    cursor.execute(idx_sql)
