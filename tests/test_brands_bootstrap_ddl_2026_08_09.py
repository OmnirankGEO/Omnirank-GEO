"""建表兜底 DDL 与生产迁移对齐 · 判据锁(WO P1 2026-08-09)

缺陷:`db/diagnosis_db.py` 的建表兜底写的是 `name TEXT UNIQUE NOT NULL`,
而生产早就不是那样了 —— 实查只有 `brands_pkey` 与
`brands_name_owner_key UNIQUE (name, owner_user_id) WHERE is_deleted = false`。
后果:任何 `init_db` 自举出来的**新库**(CI / 新 staging / 重建预演)
会长回全局约束,第二个用户用同一个品牌名就**直冒 500**;
而且它**没有**那条对的 partial index —— 新库其实是"错的约束有、对的约束没有"。

🔴 判据在**真 PG 的独立 schema 里把源码里那段 DDL 原样跑一遍**再插数据。
不抄 DDL(抄了就测不到源码改动 —— 上一件的变异 M05 教训),
不用 `init_db()` 整跑(它会拉起全库几百张表,慢且噪声大)。
"""
from __future__ import annotations

import re

import psycopg2
import pytest

SCHEMA = "w3_brands_ddl_probe"


def _source() -> str:
    """brands 建表/索引的源码所在文件。

    🔴 [R5 批2 2026-08-21] 原来读的是 `db/diagnosis_db.py`,**被我自己的批 1 改断了**:
       批 1 把 brands 的建表 SQL / 自愈列 / 索引三段整体搬到 `db/brands_schema.py`
       (零副作用叶子模块,给夹具当窄出口),于是这里那条正则抽不到东西,
       整个文件会挂在「抽不出 brands 建表语句 —— 判据锚点失效」上。
       实证:同一条正则在底 9d359800d 上 anchor found=True,批 1 之后 False。
       教训跟我记过的那条一模一样 —— **抽常量会移动结构锚**;批 1 的 A/B 是逐目录跑的,
       没跑到 tests/ 根下这个文件,所以没抓到。
    """
    with open("db/brands_schema.py", encoding="utf-8") as handle:
        return handle.read()


def brands_create_table_sql() -> str:
    """拿 brands 的建表语句。

    🔴 [R5 批2] 改成**直接取 SSOT 常量**,不再对源码做正则考古:
       `_BRANDS_CREATE_TABLE_SQL` 就是 init_db 和 ensure_brands_schema 真正执行的
       那一份对象 —— 比"从文本里抠一段"更硬,也不会再被下一次搬家/换缩进打断。
       (原设计「不抄 DDL,否则测不到源码改动」的意图**完全保留**:取的是同一个对象,
        源码改了这里立刻跟着改;抄一份常量才是那条禁令要防的事。)
    """
    from db.brands_schema import _BRANDS_CREATE_TABLE_SQL

    m = re.search(r"(CREATE TABLE IF NOT EXISTS brands \(.*\n\s*\))",
                  _BRANDS_CREATE_TABLE_SQL, re.S)
    assert m, "SSOT 常量里抽不出 brands 建表语句 —— 判据锚点失效,先修判据"
    # 🔴 剥掉 SQL 注释再返回:本包在建表语句里写了一段说明,里面**复述**了
    #    `name TEXT UNIQUE NOT NULL` 这个原句 —— 不剥的话「不许再有全局 UNIQUE」
    #    那条判据恒假(A1 那件刚踩过一模一样的坑,这是第二次)。
    stripped = [
        (line.split("--")[0].rstrip() if "--" in line else line)
        for line in m.group(1).split("\n")
    ]
    return "\n".join(l for l in stripped if l.strip())


def brands_index_sql() -> list[str]:
    """拿 brands 的索引/唯一约束语句。

    🔴 [R5 批2] 同上改成取 SSOT 常量。原来的正则锚是
       `# brands表索引 ... # 文章表索引` 两个注释之间那一段 —— 批 1 之后
       **上锚跟着常量搬进了 db/brands_schema.py、下锚还留在 db/diagnosis_db.py**,
       两个锚从此不在同一个文件里,这条正则在任何单一文件上都抽不到。
       (「结构锚被重构移动」的第二个现场,同一次改动打断了两处。)
    """
    from db.brands_schema import _BRANDS_INDEX_STATEMENTS

    stmts = [s.strip() for s in _BRANDS_INDEX_STATEMENTS]
    assert stmts, "SSOT 常量里一条 brands 索引语句都没有 —— 零分母,判据等于恒绿"
    return [s for s in stmts if s.strip()]


@pytest.fixture
def fresh_schema():
    """一个干净 schema，模拟"刚被 init_db 自举出来的新库"。"""
    from db.connection import get_db

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
    yield
    with get_db() as conn:
        conn.cursor().execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


def _bootstrap(conn, *, create_sql=None, index_sql=None):
    cur = conn.cursor()
    cur.execute(f"SET search_path TO {SCHEMA}")
    cur.execute(create_sql if create_sql is not None else brands_create_table_sql())
    # 🔴 [21 班合后修 2026-08-10] 这里原来有一行
    #     ALTER TABLE brands ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE
    #   注释写着「is_deleted 是后加列,建表语句里没有 —— 补上,否则 partial index 建不出来」。
    #   那一行**正是让这套锁瞎掉的原因**:夹具替源码把列补上了,于是「建表少一列 →
    #   partial index 建不出来 → 全新库 init_db 整个炸」这个真缺陷,锁永远看不见
    #   (真库实证:去掉这行后 test_fresh_bootstrap_* 全红,报 UndefinedColumn)。
    #   现已把该列补进 db/diagnosis_db.py 的 CREATE TABLE + _safe_add_column 两处,
    #   夹具**不再代劳** —— 这套锁从此真的在测「源码自举出来的库能不能用」。
    for stmt in (index_sql if index_sql is not None else brands_index_sql()):
        cur.execute(stmt)
    return cur


def _insert(cur, name, owner, deleted=False):
    cur.execute(
        "INSERT INTO brands (name, owner_user_id, is_deleted) VALUES (%s, %s, %s) RETURNING id",
        (name, owner, deleted),
    )
    return int(cur.fetchone()["id"])


# ══════════════════════════════════════════════════════════════════════════
# §1 新库自举后:跨 owner 同名不撞 500
# ══════════════════════════════════════════════════════════════════════════

def test_fresh_bootstrap_allows_same_name_across_owners(fresh_schema):
    """【必须命中】这就是工单要的那条:新库自举后,第二个用户用同名**不许 500**。"""
    from db.connection import get_db

    with get_db() as conn:
        cur = _bootstrap(conn)
        _insert(cur, "浙江岱林生物技术股份有限公司", 28)
        _insert(cur, "浙江岱林生物技术股份有限公司", 102)   # 不抛就是通过


def test_the_old_ddl_would_have_500ed(fresh_schema):
    """【反向对照】把 `UNIQUE` 加回去(= 修复前那份 DDL)→ **同一组数据当场炸**。

    没有这条,上面那条会被"约束根本没建出来"这种情况拿满分。
    这也是复审说的"真 PG 复刻实证":500 是真的,不是推断。
    """
    from db.connection import get_db

    legacy_create = brands_create_table_sql().replace(
        "name TEXT NOT NULL,", "name TEXT UNIQUE NOT NULL,"
    )
    assert "name TEXT UNIQUE NOT NULL," in legacy_create, "反向对照构造失败"

    with get_db() as conn:
        cur = _bootstrap(conn, create_sql=legacy_create, index_sql=[])
        _insert(cur, "浙江岱林生物技术股份有限公司", 28)
        with pytest.raises(psycopg2.errors.UniqueViolation):
            _insert(cur, "浙江岱林生物技术股份有限公司", 102)


def test_fresh_bootstrap_still_blocks_same_owner_duplicate(fresh_schema):
    """【必须不命中】同 owner 同名**仍然**要被库拦住 —— 不是把约束删光了事。"""
    from db.connection import get_db

    with get_db() as conn:
        cur = _bootstrap(conn)
        _insert(cur, "碧玉良缘", 20)
        with pytest.raises(psycopg2.errors.UniqueViolation):
            _insert(cur, "碧玉良缘", 20)


def test_fresh_bootstrap_ignores_soft_deleted_rows(fresh_schema):
    """【必须命中】软删的行不参与唯一性(partial index 的 WHERE 真的生效了)。"""
    from db.connection import get_db

    with get_db() as conn:
        cur = _bootstrap(conn)
        _insert(cur, "碧玉良缘", 20, deleted=True)
        _insert(cur, "碧玉良缘", 20)          # 活行只有这一条 → 允许


# ══════════════════════════════════════════════════════════════════════════
# §2 新库拿到的约束集合 == 生产那一套
# ══════════════════════════════════════════════════════════════════════════

def test_fresh_bootstrap_index_set_matches_production(fresh_schema):
    """【必须命中 · 元判据】自举出来的唯一性索引集合与生产逐字一致。

    生产实查(2026-08-09):brands 上的 UNIQUE 只有
      · brands_pkey (id)
      · brands_name_owner_key (name, owner_user_id) WHERE is_deleted = false
    没有 brands_name_key。
    """
    from db.connection import get_db

    with get_db() as conn:
        cur = _bootstrap(conn)
        cur.execute(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = %s AND tablename = 'brands' AND indexdef ILIKE '%%unique%%' "
            "ORDER BY indexname",
            (SCHEMA,),
        )
        got = {dict(r)["indexname"]: dict(r)["indexdef"] for r in cur.fetchall()}

    assert "brands_name_key" not in got, "🔴 新库不许再长出全局唯一约束"
    assert "brands_name_owner_key" in got, "🔴 新库必须拿到生产那条 partial unique"
    assert "(name, owner_user_id)" in got["brands_name_owner_key"].replace('"', "")
    assert "is_deleted = false" in got["brands_name_owner_key"]
    assert set(got) == {"brands_pkey", "brands_name_owner_key"}, (
        f"唯一性索引集合与生产不一致:{sorted(got)}"
    )


def test_bootstrap_realigns_a_stale_database(fresh_schema):
    """【必须命中】**已经**长了全局约束的老库,再跑一次自举要被拉回生产口径。

    只改 CREATE TABLE 只救新库;CI / 老 staging 已经建过表,IF NOT EXISTS 是空操作,
    所以还需要那条 `DROP CONSTRAINT IF EXISTS brands_name_key`。
    """
    from db.connection import get_db

    # 🔴 UniqueViolation 会**中止整个事务** —— 与它同一个 `with get_db()` 块里
    #    之前做的 DDL 会一起回滚(第一版就是这么写的,后面报 relation does not exist)。
    #    所以每一步各用一个事务。
    with get_db() as conn:                      # ① 造一个"老库":带全局唯一约束
        cur = conn.cursor()
        cur.execute(f"SET search_path TO {SCHEMA}")
        # 列要凑齐索引段用得到的那几个(company_name / industry_category),
        # 否则 ③ 那步会死在 idx_brands_company 上,测不到唯一性对齐这件事本身。
        cur.execute("CREATE TABLE brands (id SERIAL PRIMARY KEY, name TEXT NOT NULL, "
                    "company_name TEXT, industry_category TEXT, "
                    "owner_user_id INTEGER, is_deleted BOOLEAN DEFAULT FALSE)")
        cur.execute("ALTER TABLE brands ADD CONSTRAINT brands_name_key UNIQUE (name)")
        _insert(cur, "浙江岱林生物技术股份有限公司", 28)

    with pytest.raises(psycopg2.errors.UniqueViolation):   # ② 对齐前:撞车 = 500
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(f"SET search_path TO {SCHEMA}")
            _insert(cur, "浙江岱林生物技术股份有限公司", 102)

    with get_db() as conn:                      # ③ 再跑一次自举(真入口)
        cur = conn.cursor()
        cur.execute(f"SET search_path TO {SCHEMA}")
        # [WO_285b 改指] 原来逐条重放 _BRANDS_INDEX_STATEMENTS;那条 DROP 已搬到
        #   db/brands_schema._BRANDS_LEGACY_UNIQUE_DROP,由 ensure_brands_schema「约束真存在才执行」
        #   (无条件 ALTER 在请求路径上会排在发车 pg_dump 后面卡死)。这里改调真入口,
        #   正好量到那条条件分支:本库有 brands_name_key ⇒ 必须真的删掉。
        from db.brands_schema import ensure_brands_schema
        ensure_brands_schema(cur)

    with get_db() as conn:                      # ④ 对齐后:必须放行
        cur = conn.cursor()
        cur.execute(f"SET search_path TO {SCHEMA}")
        _insert(cur, "浙江岱林生物技术股份有限公司", 102)


def test_source_no_longer_declares_a_global_unique_name(fresh_schema):
    """【必须不命中 · 打在源码上】建表语句里不许再有 `name TEXT UNIQUE`。"""
    create = brands_create_table_sql()
    assert "name TEXT UNIQUE" not in create, "全局 UNIQUE 又回来了"
    assert "name TEXT NOT NULL," in create, "NOT NULL 不该被一起删掉"


def test_app_precheck_and_db_constraint_agree(fresh_schema):
    """【必须命中 · 跨层一致】应用层预查(A1 那件修的)与库约束是同一口径。

    两层任何一层单独改都会让另一层失配 —— 这条把它们绑在一起。
    """
    with open("api/brand_api.py", encoding="utf-8") as handle:
        api_src = handle.read()
    assert "owner_user_id = %s" in api_src and "is_deleted = false" in api_src
    idx = [s for s in brands_index_sql() if "brands_name_owner_key" in s]
    assert idx, "库侧 partial unique 不见了"
    assert "name, owner_user_id" in idx[0] and "is_deleted = false" in idx[0]
