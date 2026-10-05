"""工单C(资金/错对象/假完成收口)判据底座 —— 真 PG16 一次性库。

安全栓沿用本仓既有形态(pkge/pkgf/pkgg):库名必须**同时**含 ``defgeo`` /
``woc`` / ``test``。2026-08-15 实测过库名不含 ``test`` 会让安全栓整个失效、
双臂 0 junit 假绿;2026-08-19 又实测过共享库让 A/B 三跑三答案 ——
所以本包锁死在**自己**那一把库上(``defgeo-woc-pg`` · 55484)。

schema 从哪来
-------------
生产 pg_dump(与窗C / 包E 同一份快照)+ 本包运行期真的会读的迁移,
**从 manifest 现扫**。漏登记 = 上线后永远不会跑,那要在这里就红。

  · 042  accepted 快照指针(activation 入队那一跳会写它)
  · 043  activation outbox 本体
  · 044  发布链六表(execution_budget_policy / store 会读)
  · 046  监测 lineage(``defgeo_monitoring_attempts`` = C-3 的被测对象)
  · 049  结算裁定表(C-8 的被测对象)
  · 052  **本包拥有的**迁移:activation outbox 冻结 payer 三列

🔴 pg_dump 的 ``search_path=''`` 与 psql 元命令是毒(2026-08-12 / 窗C 记过),
   装载前逐条中和,并且**形态变了要当场知道**(找不到毒行就报错,不静默放过)。
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

ROOT = Path(__file__).resolve().parents[2]

DEFAULT_THROWAWAY_URL = (
    "postgresql://geo_admin:testpw@localhost:55484/geo_defgeo_woc_test"
)

_REQUIRED_DB_TOKENS = ("defgeo", "woc", "test")

PROD_SCHEMA = ROOT / "tests" / "article_self_report_2026_08_19" / "prod_schema_2026-08-19.sql"

#: 🔴 **本包拥有的**迁移。轴的作用域 = 本包的作用域。
_PACKAGE_OWNED_MIGRATIONS = (
    "db/migration_052_defgeo_activation_frozen_payer_2026_08_25.sql",
)

#: 运行期前置:不是本包拥有,但本包的被测代码在运行时真的会读它们。
_PREREQ_MIGRATIONS = (
    "db/migration_042_defgeo_customer_accepted_snapshot_2026_08_21.sql",
    "db/migration_043_defgeo_activation_outbox_2026_08_21.sql",
    "db/migration_044_defgeo_publish_decision_2026_08_21.sql",
    "db/migration_046_defgeo_monitoring_lineage_2026_08_22.sql",
    "db/migration_049_settlement_adjudications_2026_08_25.sql",
    # [工单 E3-1 · 2026-08-26] 054 给 monitoring_run_cells 加冻结租户列。
    # ``run_ledger_bridge.close_unattempted_for_cells`` 现在**真的读它** ——
    # 不装 054 就是 UndefinedColumn,而那一跳是 fail-soft 的:
    # 异常被 SAVEPOINT 吞掉、返 0 行,判据红在"没闭合"上,
    # 而真因是**夹具没装起来**,不是被测代码坏了(与 047/051/052 那几次同形)。
    "db/migration_054_monitoring_cell_tenant_owner_2026_08_26.sql",
)

#: 本包判据会数增量的既有表。缺一张,「零副作用」会退化成「零查询」全绿。
_REQUIRED_EXISTING_TABLES = (
    "brands", "quotes", "keyword_selection_sessions", "client_access_tokens",
    "diagnosis_records", "articles", "monitoring_run_cells", "monitoring_tasks",
    "quote_pricing_snapshots", "users", "roles", "user_roles",
    "diagnosis_settlement_audit",
)

_PACKAGE_TABLES = (
    "defgeo_activation_outbox",
    "defgeo_monitoring_attempts",
    "defgeo_provider_execution_budgets",
    "diagnosis_settlement_adjudications",
)

_SEARCH_PATH_POISON = "SELECT pg_catalog.set_config('search_path', '', false);"


def _resolve_url() -> str:
    configured = os.environ.get("TEST_DATABASE_URL", "").split("?", 1)[0]
    dbname = configured.rsplit("/", 1)[-1].lower()
    missing = [t for t in _REQUIRED_DB_TOKENS if t not in dbname]
    if not configured or missing:
        raise RuntimeError(
            "工单C 判据锁死在本包一次性库上:库名必须同时含 {0};实得 {1!r}(缺 {2})。"
            "单跑用 {3}".format(
                _REQUIRED_DB_TOKENS, configured, missing, DEFAULT_THROWAWAY_URL
            )
        )
    return configured


EXACT_THROWAWAY_URL = _resolve_url()
os.environ["DATABASE_URL"] = EXACT_THROWAWAY_URL


def connect():
    conn = psycopg2.connect(EXACT_THROWAWAY_URL)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    with conn.cursor() as cur:
        cur.execute("SET search_path TO public")
    return conn


def package_migrations() -> list[Path]:
    """从 manifest **现扫**。漏登记 = 上线后永远不会跑 —— 在这里就要红。"""
    from db.migration_manifest import MIGRATIONS

    registered = set(MIGRATIONS)
    wanted = (*_PREREQ_MIGRATIONS, *_PACKAGE_OWNED_MIGRATIONS)
    missing = [m for m in wanted if m not in registered]
    if missing:
        raise RuntimeError(
            f"本包迁移/前置没进 manifest:{missing}。"
            "prestart 只按 manifest 跑、不 glob 目录,漏登记 = 上线后永远不会跑。"
        )
    # 顺序按 manifest 原顺序:依赖图不在我脑子里,在 manifest 的顺序里。
    return [ROOT / rel for rel in MIGRATIONS if rel in set(wanted)]


def _load_prod_schema(conn) -> None:
    if not PROD_SCHEMA.exists():              # pragma: no cover - 环境问题
        pytest.skip(f"缺生产 schema 快照:{PROD_SCHEMA}")
    sql = PROD_SCHEMA.read_text(encoding="utf-8", errors="replace")
    if _SEARCH_PATH_POISON not in sql:
        raise AssertionError(
            f"生产 dump 里找不到已知的 search_path 毒行;形态可能变了:{PROD_SCHEMA}"
        )
    sql = sql.replace(_SEARCH_PATH_POISON, "-- [woc conftest] search_path 毒行已中和")

    stripped = 0
    lines: list[str] = []
    for ln in sql.splitlines():
        if ln.lstrip().startswith(("\\restrict", "\\unrestrict")):
            stripped += 1
            lines.append("-- [woc conftest] psql 元命令已剥离(第 %d 行)" % (stripped,))
        else:
            lines.append(ln)
    if stripped == 0:
        raise AssertionError(
            f"生产 dump 里一条 psql 元命令都没有 —— 剥离规则可能已经过时:{PROD_SCHEMA}"
        )
    sql = "\n".join(lines)
    if "\\restrict" in sql or "\\unrestrict" in sql:
        raise AssertionError("psql 元命令没有被完全剥离")

    # 🔴 归一必须在 join **之后**(窗C 记过:写在 join 之前会被整段覆盖回原文)。
    sql = sql.replace("CREATE EXTENSION IF NOT EXISTS", "CREATE EXTENSION")
    sql = sql.replace("CREATE EXTENSION ", "CREATE EXTENSION IF NOT EXISTS ")
    assert "CREATE EXTENSION IF NOT EXISTS IF NOT EXISTS" not in sql, "归一写重了"

    with conn.cursor() as cur:
        cur.execute(sql)


@pytest.fixture(scope="session", autouse=True)
def _schema():
    conn = psycopg2.connect(EXACT_THROWAWAY_URL)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (0x64656667_00000063,))
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.brands') AS r")
                already = cur.fetchone()[0] is not None
            if not already:
                _load_prod_schema(conn)
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (0x64656667_00000063,))
    finally:
        conn.close()

    # 🔴 每条迁移一条新连接:某条失败后连接进 aborted 事务,
    #    后续每条都变成 InFailedSqlTransaction 被"跳过" —— 那是假绿的老形态。
    for path in package_migrations():
        sql = path.read_text(encoding="utf-8", errors="replace")
        c = psycopg2.connect(EXACT_THROWAWAY_URL)
        c.autocommit = True
        try:
            with c.cursor() as cur:
                cur.execute("SET search_path TO public")
                cur.execute(sql)
        finally:
            c.close()

    # ── 逐表反向自证:半装的库要在这里响亮地红,而不是在某条判据里假绿 ──────
    c = psycopg2.connect(EXACT_THROWAWAY_URL)
    try:
        with c.cursor() as cur:
            for t in (*_REQUIRED_EXISTING_TABLES, *_PACKAGE_TABLES):
                cur.execute("SELECT to_regclass(%s)", (f"public.{t}",))
                assert cur.fetchone()[0] is not None, (
                    f"{t} 不在库里 —— 夹具没装起来。此时"
                    "「零副作用 / 零泄漏」这类判据会退化成「零查询」全绿"
                )
            # 052 的三列逐列自证(它是本包拥有的迁移,漏了整包判据都没有被测对象)
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='defgeo_activation_outbox' "
                "  AND column_name IN "
                "      ('payer_user_id','payer_funding_policy','payer_principal_kind')"
            )
            got = {r[0] for r in cur.fetchall()}
            assert got == {"payer_user_id", "payer_funding_policy", "payer_principal_kind"}, (
                f"052 的三列没装上(实得 {sorted(got)})"
            )
        c.rollback()
    finally:
        c.close()
    yield


#: 本包判据自己写的表。清理顺序 = FK 反向(子表在前、父表在后)。
_PACKAGE_WRITE_TABLES = (
    "defgeo_monitoring_attempts",
    "monitoring_run_cells",
    "monitoring_tasks",
    "defgeo_activation_outbox",
    "defgeo_provider_execution_budgets",
    "diagnosis_settlement_adjudications",
    "diagnosis_settlement_audit",
    "client_access_tokens",
)


@pytest.fixture(scope="session", autouse=True)
def _clean(_schema):                                       # noqa: ANN001
    """🔴 **跨 session 的分母清理** —— 一次性库会被反复复用。

    包E 记过一次真实的假红:批量领取被上一 session 攒下的旧行占满,
    本 session 自己造的那一条根本没进批次 ⇒ 红,而那种红与被测代码无关。
    所以每个 session 开局先清空本包自己写的那几张表,并做**反向自证**。
    """
    dbname = EXACT_THROWAWAY_URL.rsplit("/", 1)[-1].lower()
    assert all(t in dbname for t in _REQUIRED_DB_TOKENS), dbname   # 二次复核

    conn = psycopg2.connect(EXACT_THROWAWAY_URL)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO public")
            for table in _PACKAGE_WRITE_TABLES:
                cur.execute(f"DELETE FROM {table}")
            for table in _PACKAGE_WRITE_TABLES:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                left = cur.fetchone()[0]
                assert left == 0, f"{table} 清理后还剩 {left} 行 —— 分母没清干净"
    finally:
        conn.close()

    # 🔴 ``PLATFORM_DIRECT_SERVICE_USER_ID`` 是**进程环境**里的一个轴:
    #    外部 shell 里恰好设了它会让付款方判别改判(包E R2 实测)。session 开局归零。
    _env_before = os.environ.pop("PLATFORM_DIRECT_SERVICE_USER_ID", None)
    try:
        yield
    finally:
        if _env_before is not None:
            os.environ["PLATFORM_DIRECT_SERVICE_USER_ID"] = _env_before


@pytest.fixture()
def db():
    conn = connect()
    try:
        yield conn
    finally:
        try:
            conn.rollback()
        except Exception:                                 # noqa: BLE001
            pass
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
# 角色探针的共享观测(C-7)
# ══════════════════════════════════════════════════════════════════════════
# 🔴 ``probes`` 放 conftest 而不是某一个测试文件里:C-7(1) 的设置保存判据与
#    C-7(2) 的角色归属判据读的是**同一次**子进程真导入。
#    各自再 import 一次 server 要多付两次全量启动的钱,而且两次观测可能不同源
#    —— 判据之间比对的东西必须来自同一次观测。
#: 角色探针自己的库。**不复用**本包主库:探针要跑全量 manifest 迁移,
#: 会把主库的 schema 改成另一副样子,后面的判据就不在同一个分母上了。
ROLE_DB = "geo_defgeo_woc_role_test"


def _admin_dsn() -> str:
    return EXACT_THROWAWAY_URL.rsplit("/", 1)[0] + "/postgres"


def _role_dsn() -> str:
    return EXACT_THROWAWAY_URL.rsplit("/", 1)[0] + "/" + ROLE_DB


@pytest.fixture(scope="session")
def role_db():
    """生产 schema dump + **全量 manifest** 迁移。

    🔴 为什么要全量:`server.py` 在 `ROLE=web|cron` 下会做 fail-closed
       schema 反查(缺表/缺列 ⇒ 拒绝启动)。只灌本包那 6 条迁移的话,
       两臂都会以「schema 不全」死掉 —— 那时"web 没注册"是真的,
       但原因是它根本没起来,与 ROLE 闸毫无关系。
       **两边都红 ⇒ 记「没验」不是「已验」**(本仓铁律)。
    """
    if not PROD_SCHEMA.exists():                          # pragma: no cover
        pytest.skip(f"缺生产 schema 快照:{PROD_SCHEMA}")

    admin = psycopg2.connect(_admin_dsn())
    admin.autocommit = True
    admin.cursor().execute(f'DROP DATABASE IF EXISTS "{ROLE_DB}" WITH (FORCE)')
    admin.cursor().execute(f'CREATE DATABASE "{ROLE_DB}"')
    admin.close()

    dsn = _role_dsn()
    sql = PROD_SCHEMA.read_text(encoding="utf-8", errors="replace")
    sql = sql.replace("SELECT pg_catalog.set_config('search_path', '', false);",
                      "-- [role probe] search_path 毒行已中和")
    sql = "\n".join(
        ("-- [role probe] psql 元命令已剥离"
         if ln.lstrip().startswith(("\\restrict", "\\unrestrict")) else ln)
        for ln in sql.splitlines())
    sql = sql.replace("CREATE EXTENSION IF NOT EXISTS", "CREATE EXTENSION")
    sql = sql.replace("CREATE EXTENSION ", "CREATE EXTENSION IF NOT EXISTS ")
    c = psycopg2.connect(dsn)
    c.autocommit = True
    c.cursor().execute(sql)
    c.close()

    from db.migration_manifest import MIGRATIONS

    applied = 0
    for rel in MIGRATIONS:
        path = ROOT / rel
        if not path.exists():
            continue
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        try:
            cur = conn.cursor()
            cur.execute("SET search_path TO public")
            cur.execute(path.read_text(encoding="utf-8", errors="replace"))
            applied += 1
        except Exception:                                 # noqa: BLE001
            # 个别历史迁移含 psql 元命令(``\set``),在 psycopg2 下跑不了。
            # 它们与角色闸无关;真正的把关是下面那条"两臂都必须起得来"。
            pass
        finally:
            conn.close()
    assert applied > 100, f"只跑起来 {applied} 条迁移 —— 角色探针的库不可信"
    try:
        yield dsn
    finally:
        admin = psycopg2.connect(_admin_dsn())
        admin.autocommit = True
        admin.cursor().execute(f'DROP DATABASE IF EXISTS "{ROLE_DB}" WITH (FORCE)')
        admin.close()



@pytest.fixture(scope="session")
def probes(role_db):                                       # noqa: ANN001
    from tests.defgeo_woc_closure_2026_08_25.test_c7_cron_role_runtime import _probe

    return {"web": _probe("web", role_db), "cron": _probe("cron", role_db)}
