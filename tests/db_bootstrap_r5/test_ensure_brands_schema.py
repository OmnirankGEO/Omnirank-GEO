"""[R5 · Owner 裁定 b] brands 窄出口 `ensure_brands_schema` 的保真判据。

背景(为什么有这个出口):
    ⑤ 的本意是「夹具不许自己 author 生产表」。原方案(夹具改跑整个 init_db)已被
    **实测证伪**:tests/geo_observation 转换后 174 passed/0 failed → 138 passed/34 failed,
    跑时 194s → 589s。把 150 张表拖进一个只要十来张表的夹具,是拿更大的病换小病。
    ⇒ 改走窄出口:一句 `ensure_brands_schema(cursor)` 拿到与生产同形的 brands。

🔴 本文件最重要的一条是 test_narrow_export_column_set_identical_to_full_init_db:
    Owner 原话 —— 「全新库上『只调 ensure_brands_schema』与『跑完整 init_db』产出的
    brands 列集必须逐列相同 —— 这条锁死"两个 builder 各自漂移"的口子」。

🔴 关于它的**区分力**,必须说白(否则就是自欺):
    今天这条判据由**构造**保证 —— init_db 里那三段已整体删除,它调的就是本函数,
    两臂走的是同一行代码 ⇒ 今天它**零区分力**。这是**故意**的:它是**防漂移绊线**,
    守的是「哪天有人再在 init_db 里内联第二份 builder」。
    注毒实证见交付单(把窄出口改成只建表不补列 → 本条当场红:14 列 vs 32 列)。
    单独看它会被「两臂一起漂」骗过 ⇒ 必须与 tests/db_bootstrap_r4 里
    test_bootstrapped_brands_set_equals_production(对生产 dump 比集合)配对看。
"""
import os
import pathlib
import re
import subprocess
import sys

import psycopg2
import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
DIAGNOSIS_DB_PY = REPO / "db" / "diagnosis_db.py"
BRANDS_SCHEMA_PY = REPO / "db" / "brands_schema.py"


def _admin_dsn() -> str:
    return os.environ["TEST_DATABASE_URL"].rsplit("/", 1)[0] + "/postgres"


def _fresh_db(dbname: str) -> str:
    con = psycopg2.connect(_admin_dsn())
    con.autocommit = True
    con.cursor().execute('DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % dbname)
    con.cursor().execute('CREATE DATABASE "%s"' % dbname)
    con.close()
    dsn = os.environ["TEST_DATABASE_URL"].rsplit("/", 1)[0] + "/" + dbname
    c = psycopg2.connect(dsn)
    c.autocommit = True
    c.cursor().execute("CREATE EXTENSION IF NOT EXISTS vector")
    c.close()
    return dsn


def _columns(conn, table="brands") -> dict:
    cur = conn.cursor()
    cur.execute(
        "SELECT column_name, data_type FROM information_schema.columns"
        " WHERE table_schema='public' AND table_name=%s", (table,))
    return {r[0]: r[1] for r in cur.fetchall()}


def _indexdefs(conn, table="brands") -> set:
    cur = conn.cursor()
    cur.execute("SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND tablename=%s",
                (table,))
    return {r[0] for r in cur.fetchall()}


@pytest.fixture(scope="module")
def narrow():
    """A 臂:全新库上**只**调 ensure_brands_schema。"""
    dsn = _fresh_db("geo_test_r5_narrow_arm")
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    sys.path.insert(0, str(REPO))
    from db.brands_schema import ensure_brands_schema  # 零副作用叶子模块
    ensure_brands_schema(conn.cursor())
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def full():
    """B 臂:全新库上跑**完整 init_db**(另起进程 —— init_db 在模块体执行)。"""
    dsn = _fresh_db("geo_test_r5_full_arm")
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", "import db.diagnosis_db"],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=900,
        env={**os.environ, "PYTHONPATH": str(REPO), "DATABASE_URL": dsn},
    )
    assert proc.returncode == 0, (
        "空库上 init_db() 自己就没跑通 —— 本文件所有对照判据全部无效(零分母):\n"
        + (proc.stdout + proc.stderr)[-2500:])
    conn = psycopg2.connect(dsn)
    yield conn
    conn.close()


# ------------------------------------------------------- Owner 裁定 b:同一性
def test_narrow_export_column_set_identical_to_full_init_db(narrow, full):
    """🔴 两臂 brands **列集合**逐列相同 —— 窄出口才有资格叫 SSOT 出口。"""
    a, b = _columns(narrow), _columns(full)
    assert a, "A 臂根本没建出 brands"
    assert b, "B 臂根本没建出 brands"
    assert not (set(b) - set(a)), "窄出口比 init_db **少**了列:%s" % sorted(set(b) - set(a))
    assert not (set(a) - set(b)), "窄出口比 init_db **多**了列:%s" % sorted(set(a) - set(b))


def test_narrow_export_column_types_identical_to_full_init_db(narrow, full):
    """同名不同型也是漂移(TEXT vs VARCHAR / TIMESTAMP vs TIMESTAMPTZ 都咬过人)。"""
    a, b = _columns(narrow), _columns(full)
    diff = {k: (a[k], b[k]) for k in set(a) & set(b) if a[k] != b[k]}
    assert not diff, "两臂同名列**类型不同**:%s" % diff


def test_narrow_export_indexes_identical_to_full_init_db(narrow, full):
    """brands 的索引/唯一约束也必须同形 —— 否则夹具拿到的是「没有唯一性的 brands」。"""
    a, b = _indexdefs(narrow), _indexdefs(full)
    assert not (b - a), "窄出口比 init_db **少**了索引:%s" % sorted(b - a)
    assert not (a - b), "窄出口比 init_db **多**了索引:%s" % sorted(a - b)


# ------------------------------------------------------- 窄出口必须真的「窄」
def test_narrow_export_builds_only_brands_not_the_whole_schema(narrow):
    """🔴 配对反向判据:窄出口**只许**建 brands 一张表。

    这条是整个方案的存在理由。它要是绿不了(比如哪天有人在 ensure_brands_schema 里
    顺手调了 init_db),窄出口就退化成被证伪的原方案,跑时从 0.2s 变回几十秒。
    """
    cur = narrow.cursor()
    cur.execute("SELECT table_name FROM information_schema.tables"
                " WHERE table_schema='public' ORDER BY 1")
    tables = [r[0] for r in cur.fetchall()]
    assert tables == ["brands"], "窄出口建了不止 brands:%s" % tables


def test_narrow_export_heals_a_preexisting_narrow_brands():
    """夹具最常见的形态:表**已经存在**且比生产窄(CREATE TABLE IF NOT EXISTS 救不了)。"""
    dsn = _fresh_db("geo_test_r5_preexisting")
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("CREATE TABLE brands (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    sys.path.insert(0, str(REPO))
    from db.brands_schema import ensure_brands_schema, _BRANDS_SELF_HEAL_COLUMNS
    ensure_brands_schema(cur)
    have = set(_columns(conn))
    want = {c for c, _ in _BRANDS_SELF_HEAL_COLUMNS} | {"id", "name"}
    assert not (want - have), "既存窄表没被补齐,缺 %s" % sorted(want - have)
    conn.close()


def test_narrow_export_is_idempotent():
    """连调两次不许炸(夹具 session/function 两层都可能调到)。"""
    dsn = _fresh_db("geo_test_r5_idem")
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    sys.path.insert(0, str(REPO))
    from db.brands_schema import ensure_brands_schema
    ensure_brands_schema(conn.cursor())
    first = _columns(conn)
    ensure_brands_schema(conn.cursor())
    assert _columns(conn) == first, "第二次调用改变了 brands 形状"
    conn.close()


def test_importing_the_narrow_export_does_not_build_the_whole_schema():
    """🔴 `import db.brands_schema` 必须**零副作用**。

    它要是反过来 import 了 db.diagnosis_db,模块体那句 init_db() 就会当场建 150 张表 ——
    窄出口的全部意义(夹具只要 brands)当场归零,而且**没有任何报错**。
    """
    dsn = _fresh_db("geo_test_r5_sideeffect")
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-c",
         "import sys; import db.brands_schema;"
         " print('LOADED_DIAGNOSIS_DB=%s' % ('db.diagnosis_db' in sys.modules))"],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=300,
        env={**os.environ, "PYTHONPATH": str(REPO), "DATABASE_URL": dsn},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "LOADED_DIAGNOSIS_DB=False" in proc.stdout, (
        "import db.brands_schema 把 db.diagnosis_db 一起拉起来了:\n"
        + proc.stdout + proc.stderr)
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
    n = cur.fetchone()[0]
    conn.close()
    assert n == 0, "只是 import 了一下,库里就冒出 %s 张表" % n


# ------------------------------------------------------- 结构锁:不许有第二个 builder
def test_no_second_brands_create_table_in_production_code():
    """🔴 Owner 裁定 a:同一谓词只许写一处。

    全仓生产代码(db/ api/ services/ workflows/ tools/)里,
    `CREATE TABLE ... brands (` 只允许出现在 db/brands_schema.py 的那个常量里。
    """
    hits = []
    pat = re.compile(r"CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?(public\.)?brands\s*\(", re.I)
    for sub in ("db", "api", "services", "workflows", "tools"):
        for f in (REPO / sub).rglob("*.py"):
            if f == BRANDS_SCHEMA_PY:
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if pat.search(text):
                hits.append(str(f.relative_to(REPO)))
    assert not hits, "brands 建表语句出现在 SSOT 之外:%s" % hits


def test_legacy_inline_brands_alters_are_a_strict_subset_of_the_ssot_list():
    """diagnosis_db 里那 13 句历史 `_safe_add_column(cursor,"brands",...)` 现在是死码。

    它们排在 ensure_brands_schema 之后 ⇒ 列早就有了 ⇒ 句句 no-op。**故意留着**:
    每一句上面那段注释记着这列当年为什么加(C.6 软删 / A.2 测试客户隔离 …),
    删掉等于烧掉出处。但留着就有漂移风险(有人改了它以为在改活的),
    所以用这把锁钉死:它们只许是 SSOT 清单的**子集**,不许出现 SSOT 没有的列。
    """
    sys.path.insert(0, str(REPO))
    from db.brands_schema import _BRANDS_SELF_HEAL_COLUMNS
    ssot = {c for c, _ in _BRANDS_SELF_HEAL_COLUMNS}
    text = DIAGNOSIS_DB_PY.read_text(encoding="utf-8", errors="ignore")
    inline = set(re.findall(r'_safe_add_column\(\s*cursor,\s*"brands",\s*"([a-z_]+)"', text))
    assert inline, "一句都没匹配到 —— 正则坏了,零分母判据等于恒绿"
    assert not (inline - ssot), (
        "diagnosis_db 里有 SSOT 清单**没有**的 brands 列:%s "
        "—— 那就是第二个 builder,正是 Owner 裁定 a 要堵的口子" % sorted(inline - ssot))


def test_sql_emission_matches_the_cursor_emission():
    """brands_schema_sql()(纯 SQL 形态)与 ensure_brands_schema()(游标形态)必须同形。

    🔴 这条与上面那条同一性判据不同:它**有真区分力** —— 两条路径是**两段不同的代码**
       (先查后加 vs ALTER ... ADD COLUMN IF NOT EXISTS),只共用三份常量。
       给 tests/organization_internal_seats 那种「夹具是一整段 SQL 字符串」的场合用。
    """
    sys.path.insert(0, str(REPO))
    from db.brands_schema import brands_schema_sql

    dsn = _fresh_db("geo_test_r5_sqlemit")
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    conn.cursor().execute(brands_schema_sql())
    a_cols, a_idx = _columns(conn), _indexdefs(conn)
    conn.close()

    dsn2 = _fresh_db("geo_test_r5_curemit")
    conn2 = psycopg2.connect(dsn2)
    conn2.autocommit = True
    from db.brands_schema import ensure_brands_schema
    ensure_brands_schema(conn2.cursor())
    b_cols, b_idx = _columns(conn2), _indexdefs(conn2)
    conn2.close()

    assert a_cols == b_cols, "两种发射方式列不同:SQL 独有 %s / 游标独有 %s" % (
        sorted(set(a_cols) - set(b_cols)), sorted(set(b_cols) - set(a_cols)))
    assert a_idx == b_idx, "两种发射方式索引不同:SQL 独有 %s / 游标独有 %s" % (
        sorted(a_idx - b_idx), sorted(b_idx - a_idx))
