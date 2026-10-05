"""[R4 ①②③] init_db 自举保真判据 —— 全部**机械枚举**,不抽样。

守三件事:
  ① brands 自举出来的列集合 == 生产 dump 的列集合(不是列数相等,是集合相等);
  ② organization 自愈补列真的**执行到了**(它原来排在 9/10 张目标表的 CREATE 之前,
     配上静默吞异常 ⇒ 63 个列一个都没补上,而且零日志);
  ③ `_safe_add_column` 只吞 duplicate_column;表不存在必须**响亮抛出**。

🔴 为什么判据要打「全新空库单次 init_db」:
   生产靠 prestart 无条件重放迁移,这些列早就有了 ⇒ 生产**看不见**这个缺陷。
   只有自举路径(CI / 新 staging / 重建预演 / 一次性测试库)会踩,
   而那正是 2026-08-20 那两轮排查(夹具缺列 → 单线程自死锁)的现场。
"""
import os
import re
import pathlib
import subprocess
import sys

import psycopg2
import psycopg2.errors
import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
PROD_DUMP = REPO / "tests" / "article_self_report_2026_08_19" / "prod_schema_2026-08-19.sql"

#: init_db 自己创建的 organization 制品表(其余 4 张由别的模块/迁移拥有,见
#: db/diagnosis_db._safe_add_column_optional 的归属表)
ORG_TABLES_OWNED_BY_INIT_DB = (
    "client_materials", "diagnosis_records", "quotes",
    "keyword_selection_sessions", "article_generations", "articles",
)
ORG_TABLES_OWNED_ELSEWHERE = (
    "client_profiles", "media_publications", "monitoring_tasks", "monitoring_reports",
)
ORG_COLUMNS = (
    "brand_id", "organization_id", "created_by_user_id", "created_by_membership_id",
    "created_by_actor_kind", "responsible_user_id", "artifact_visibility",
)


def _prod_brands_columns() -> set:
    """从**生产整库 dump** 机械取 brands 全列 —— 不手抄常量。"""
    text = PROD_DUMP.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"CREATE TABLE public\.brands \((.*?)^\);", text, re.S | re.M)
    assert m, f"生产 dump 里没找到 brands 建表:{PROD_DUMP}"
    cols = set()
    for line in m.group(1).splitlines():
        line = line.strip().rstrip(",")
        if not line or line.startswith(("CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN")):
            continue
        cols.add(line.split()[0])
    assert len(cols) > 20, f"解析出的生产列只有 {len(cols)} 个 —— 正则大概率坏了,零分母的判据和恒绿一样废"
    # 🔴 [Owner 裁定 2026-08-20] user_id 缺失**不算红**。
    #   生产上它与 owner_user_id 并存,但全仓生产代码零引用 —— 化石列,不是现役契约。
    #   自愈清单表达的是「现役生产契约」,所以它**刻意不被自举补上**;
    #   判据这里同步豁免,否则会把一个有意的决定判成缺陷。
    #   (生产那一列原样不动 —— drop 是红线。)
    return cols - {"user_id"}


@pytest.fixture(scope="module")
def bootstrapped(request):
    """开一把**全新空库**,只跑一次 init_db(),把结果交给判据。"""
    admin = os.environ["TEST_DATABASE_URL"].rsplit("/", 1)[0] + "/postgres"
    dbname = "geo_test_r4_selfheal"
    con = psycopg2.connect(admin); con.autocommit = True
    con.cursor().execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
    con.cursor().execute(f'CREATE DATABASE "{dbname}"')
    con.close()
    dsn = os.environ["TEST_DATABASE_URL"].rsplit("/", 1)[0] + "/" + dbname
    c2 = psycopg2.connect(dsn); c2.autocommit = True
    c2.cursor().execute("CREATE EXTENSION IF NOT EXISTS vector"); c2.close()

    # 🔴 必须**另起进程**:init_db() 在模块体执行,本进程里可能早已 import 过。
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-c",
         "import os;os.environ['DATABASE_URL']=%r\nimport db.diagnosis_db" % dsn],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=600,
        env={**os.environ, "PYTHONPATH": str(REPO), "DATABASE_URL": dsn},
    )
    assert proc.returncode == 0, (
        "空库上 init_db() 自己就没跑通 —— 后面的判据全部无效:\n"
        + (proc.stdout + proc.stderr)[-2500:]
    )
    conn = psycopg2.connect(dsn)
    yield conn, proc.stdout + proc.stderr
    conn.close()


def _columns(conn, table) -> set:
    cur = conn.cursor()
    cur.execute("SELECT column_name FROM information_schema.columns"
                " WHERE table_schema='public' AND table_name=%s", (table,))
    return {r[0] for r in cur.fetchall()}


# ---------------------------------------------------------------- ①
def test_bootstrapped_brands_set_equals_production(bootstrapped):
    """① 自举 brands 的**列集合**必须与生产一致(缺=假绿的来源,多=夹具比生产宽)。"""
    conn, _ = bootstrapped
    prod, boot = _prod_brands_columns(), _columns(conn, "brands")
    assert boot, "自举后根本没有 brands 表"
    assert not (prod - boot), f"自举 brands 比生产**窄**,缺列 {sorted(prod - boot)}"
    assert not (boot - prod), f"自举 brands 比生产**宽**,多列 {sorted(boot - prod)}"


# ---------------------------------------------------------------- ②
def test_org_selfheal_columns_all_present_not_sampled(bootstrapped):
    """② init_db 拥有的每张 organization 制品表,7 列**逐列**断言,不抽样。

    修前实测:这 6 张表合计只有 10/42 列(自愈循环排在 CREATE TABLE 之前 + 静默吞异常)。
    """
    conn, _ = bootstrapped
    missing = []
    for t in ORG_TABLES_OWNED_BY_INIT_DB:
        have = _columns(conn, t)
        assert have, f"{t} 在自举后不存在 —— 它本该由 init_db 创建"
        for c in ORG_COLUMNS:
            if c not in have:
                missing.append(f"{t}.{c}")
    assert not missing, (
        f"organization 自愈补列没生效,缺 {len(missing)}/"
        f"{len(ORG_TABLES_OWNED_BY_INIT_DB) * len(ORG_COLUMNS)} 个:{missing}"
    )


def test_tables_owned_elsewhere_are_skipped_loudly_not_silently(bootstrapped):
    """② 反面:不由 init_db 拥有的表允许跳过,但**必须留痕**。

    当年就是「跳过 = 静默」让 63 个列消失,并要两轮排查才追到底。
    这条同时把「跳过集合」钉死:谁往 org 清单里加一张没人建的表,这里就会红。
    """
    _, output = bootstrapped
    assert "自愈补列跳过" in output, "跳过没有任何日志 —— 又变回静默黑洞了"
    for t in ORG_TABLES_OWNED_ELSEWHERE:
        assert t in output, f"{t} 被跳过却没在启动日志里报出来"


# ---------------------------------------------------------------- ③
def test_safe_add_column_raises_on_missing_table(bootstrapped):
    """③ 表不存在 = 调用顺序错 = 缺陷,必须**响亮抛出**(不是竞态)。"""
    from db.diagnosis_db import _safe_add_column
    conn, _ = bootstrapped
    cur = conn.cursor()
    # 🔴 rollback 必须放 finally:断言失败时也要把事务收干净,
    #    否则这一条红会把同一条连接上的后续判据一起打成 InFailedSqlTransaction
    #    ——「一条用例被另一条兜住」正是我要避免的假信号。
    try:
        with pytest.raises(psycopg2.errors.UndefinedTable):
            _safe_add_column(cur, "no_such_table_r4_probe", "c", "TEXT")
    finally:
        conn.rollback()


def test_safe_add_column_is_silent_when_column_already_exists(bootstrapped):
    """③ 反向对照:列已存在时必须静默通过 —— 否则上一条只是「什么都抛」。"""
    from db.diagnosis_db import _safe_add_column
    conn, _ = bootstrapped
    cur = conn.cursor()
    try:
        _safe_add_column(cur, "brands", "name", "TEXT")   # 已存在 → 不抛
    finally:
        conn.rollback()


def test_safe_add_column_optional_skips_missing_table_and_records_it(bootstrapped):
    """③ optional 变体:表不在→跳过**并留痕**;这是与上面那条「抛」的分工。"""
    import db.diagnosis_db as D
    conn, _ = bootstrapped
    cur = conn.cursor()
    try:
        D._safe_add_column_optional(cur, "no_such_table_r4_probe", "c", "TEXT")
        assert "no_such_table_r4_probe" in D._SAFE_ADD_SKIPPED_TABLES, "跳过了却没记下来"
    finally:
        conn.rollback()


# ---------------------------------------------------------------- ④
def test_portal_authority_import_chain_is_eager_not_lazy():
    """④ 「首次 import db.diagnosis_db」不许发生在事务里 —— 用**行为**判,不是数 import 行数。

    判据:在一个**全新进程**里只 import services.portal_token_authority,
    结束后 db.diagnosis_db 必须已经在 sys.modules 里(= 那次会发 DDL 的 import
    发生在**加载期**,此时没有任何打开的事务),而不是留到请求中途、事务里才发生。

    🔴 反向对照(拆锁会红):把那四个 import 任一条退回函数体内,
       本条立刻红 —— 因为 import 链断了,diagnosis_db 不会在加载期被拉进来。
    🔴 为什么不能只上提 brand_access 内部那层:实测仍 rc=124,
       炸弹只是挪到「portal_token_authority 首次 import brand_access」那一层。
    """
    import subprocess
    import sys as _sys

    code = ("import sys\n"
            "import services.portal_token_authority\n"
            "print('LOADED' if 'db.diagnosis_db' in sys.modules else 'LAZY')\n")
    proc = subprocess.run(
        [_sys.executable, "-X", "utf8", "-c", code],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=600,
        env={**os.environ, "PYTHONPATH": str(REPO)},
    )
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]
    assert "LOADED" in proc.stdout, (
        "import services.portal_token_authority 之后 db.diagnosis_db 仍未加载 ⇒ "
        "那条会发 DDL 的 import 还留在函数体里,随时可能落进某个打开的事务 "
        "(R3 已实测这会造成单线程自死锁,rc=124)。\n" + proc.stdout + proc.stderr[-800:]
    )

# ---------------------------------------------------------------- ⑤ 根因
def test_narrow_hand_authored_brands_is_healed_to_production_shape():
    """⑤ 根因:**已存在但比生产窄**的 brands,init_db 必须能把它自愈到生产形状。

    这正是全仓 14 个夹具各搓各的 brands 造成的形态(最窄的只有 id/name)。
    修前:init_db 在 `CREATE INDEX idx_brands_company` 那一行当场 UndefinedColumn 中断,
    于是「夹具收编 SSOT」得一个一个手改;修后 init_db 自己就把列补齐。

    🔴 与上面那条 brands 判据的分工:
      · test_bootstrapped_brands_set_equals_production —— 表**不存在**、由 init_db 新建;
      · 本条 —— 表**已存在且窄**,`CREATE TABLE IF NOT EXISTS` 是 no-op,只能靠自愈补列。
        两条形态完全不同,少任何一条都会漏掉一整类库。
    """
    import subprocess
    import sys as _sys

    admin = os.environ["TEST_DATABASE_URL"].rsplit("/", 1)[0]
    dbname = "geo_test_r4_narrow_brands"
    con = psycopg2.connect(admin + "/postgres"); con.autocommit = True
    con.cursor().execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
    con.cursor().execute(f'CREATE DATABASE "{dbname}"')
    con.close()
    dsn = admin + "/" + dbname
    c2 = psycopg2.connect(dsn); c2.autocommit = True
    c2.cursor().execute("CREATE EXTENSION IF NOT EXISTS vector")
    # 复刻夹具最窄的那种手搓 brands
    c2.cursor().execute("CREATE TABLE brands (id SERIAL PRIMARY KEY, name TEXT NOT NULL)")
    c2.close()

    proc = subprocess.run(
        [_sys.executable, "-X", "utf8", "-c",
         "import os;os.environ['DATABASE_URL']=%r\nimport db.diagnosis_db" % dsn],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=600,
        env={**os.environ, "PYTHONPATH": str(REPO), "DATABASE_URL": dsn},
    )
    assert proc.returncode == 0, (
        "窄 brands 上 init_db 没跑通 —— 这正是 14 个夹具要一个个手改的根因:"
        + (proc.stdout + proc.stderr)[-2000:]
    )
    conn = psycopg2.connect(dsn)
    try:
        healed = _columns(conn, "brands")
    finally:
        conn.close()
    prod = _prod_brands_columns()
    assert not (prod - healed), f"窄 brands 没被自愈到生产形状,仍缺 {sorted(prod - healed)}"
