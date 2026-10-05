"""工单B(发布链执行侧)判据底座 —— 真 PG16 一次性库。

安全栓沿用窗A/B/C:库名必须**同时**含 ``defgeo`` 与 ``test``,再加 ``pkge``。
2026-08-15 实测过库名不含 ``test`` 会让安全栓整个失效、双臂 0 junit 假绿;
2026-08-19 又实测过共享库让 A/B 三跑三答案 —— 所以本包锁死在**自己**那一把库上。

schema 从哪来 —— 生产 pg_dump + 本包运行期依赖的迁移
----------------------------------------------------
与窗C 同一份真库快照,再按 manifest **现扫**重放:

  · 042/043  activation 指针与 activation outbox(物化器读写的就是它们)
  · 044      发布链六表
  · 046      监测 lineage(façade 的某些只读投影会碰)
  · 047      **本包拥有的**迁移:发布命令的供应商单据引用两列

🔴 迁移一律从 manifest 现扫:漏登记 = 上线后永远不会跑,那要在这里就红。
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
    "postgresql://geo_admin:testpw@localhost:55850/geo_defgeo_wob_test"
)

_REQUIRED_DB_TOKENS = ("defgeo", "wob", "test")

PROD_SCHEMA = ROOT / "tests" / "article_self_report_2026_08_19" / "prod_schema_2026-08-19.sql"

#: 🔴 **本包拥有的**迁移。轴的作用域 = 本包的作用域。
_PACKAGE_OWNED_MIGRATIONS = (
    "db/migration_051_defgeo_publish_settlement_guards_2026_08_25.sql",
)

#: 运行期前置:不是本包拥有,但本包的代码在运行时真的会读它们。
_PREREQ_MIGRATIONS = (
    "db/migration_042_defgeo_customer_accepted_snapshot_2026_08_21.sql",
    "db/migration_043_defgeo_activation_outbox_2026_08_21.sql",
    "db/migration_044_defgeo_publish_decision_2026_08_21.sql",
    "db/migration_046_defgeo_monitoring_lineage_2026_08_22.sql",
    "db/migration_047_defgeo_publish_provider_ref_2026_08_24.sql",
    # 🔴 [工单C 052 · 2026-08-25 · 由工单B 顺手补] C 把
    #    ``activation_materializer`` 改成读 ``defgeo_activation_outbox`` 的
    #    三列**冻结付款人身份**(payer_user_id / payer_funding_policy /
    #    payer_principal_kind)。本包的真链夹具会调 ``materialize_pending()``
    #    ⇒ 不装 052 就是 UndefinedColumn,整包 error ——
    #    那是"夹具没装起来",不是被测代码坏了(与 047/051 那两次同形)。
    "db/migration_052_defgeo_activation_frozen_payer_2026_08_25.sql",
)

#: 本包判据会数增量的既有表。缺一张,「零副作用」会退化成「零查询」全绿。
_REQUIRED_EXISTING_TABLES = (
    "mhz_media", "user_wallets", "point_freezes", "feature_pricing",
    "quote_pricing_snapshots", "articles", "brands",
)

_PACKAGE_TABLES = (
    "defgeo_publish_slots",
    "defgeo_publish_decision_snapshots",
    "defgeo_publish_commands",
    "defgeo_publish_outbox",
    "defgeo_settlement_review_entries",
    "defgeo_provider_execution_budgets",
    "defgeo_activation_outbox",
)

_SEARCH_PATH_POISON = "SELECT pg_catalog.set_config('search_path', '', false);"


def _resolve_url() -> str:
    configured = os.environ.get("TEST_DATABASE_URL", "").split("?", 1)[0]
    dbname = configured.rsplit("/", 1)[-1].lower()
    missing = [t for t in _REQUIRED_DB_TOKENS if t not in dbname]
    if not configured or missing:
        raise RuntimeError(
            "工单B 判据锁死在本包一次性库上:库名必须同时含 {0};实得 {1!r}(缺 {2})。"
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
    sql = sql.replace(_SEARCH_PATH_POISON, "-- [wob conftest] search_path 毒行已中和")

    stripped = 0
    lines: list[str] = []
    for ln in sql.splitlines():
        if ln.lstrip().startswith(("\\restrict", "\\unrestrict")):
            stripped += 1
            lines.append("-- [wob conftest] psql 元命令已剥离(第 %d 行)" % (stripped,))
        else:
            lines.append(ln)
    if stripped == 0:
        raise AssertionError(
            f"生产 dump 里一条 psql 元命令都没有 —— 剥离规则可能已经过时:{PROD_SCHEMA}"
        )
    sql = "\n".join(lines)
    if "\\restrict" in sql or "\\unrestrict" in sql:
        raise AssertionError("psql 元命令没有被完全剥离")

    # 🔴 归一必须在 join **之后**(窗C 记过:写在 join 之前会被整段覆盖回原文,
    #    注释说在守、实际是死代码)。
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
            cur.execute("SELECT pg_advisory_lock(%s)", (0x64656667_00000051,))
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.mhz_media') AS r")
                already = cur.fetchone()[0] is not None
            if not already:
                _load_prod_schema(conn)
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (0x64656667_00000051,))
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
            # 051 逐项自证(本包拥有的迁移,漏了整包判据都没有被测对象)
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='defgeo_publish_commands' "
                "  AND column_name IN ('settlement_attempts','last_settlement_error',"
                "                      'provider_order_ref','provider_last_polled_at')"
            )
            got = {r[0] for r in cur.fetchall()}
            assert got == {"settlement_attempts", "last_settlement_error",
                           "provider_order_ref", "provider_last_polled_at"}, (
                f"047/051 的列没装齐(实得 {sorted(got)})"
            )
            cur.execute(
                "SELECT indexdef FROM pg_indexes WHERE schemaname='public' "
                "AND indexname='uq_defgeo_pcmd_provider_order_ref'")
            row = cur.fetchone()
            assert row is not None and "UNIQUE" in row[0], (
                "051 的 provider_order_ref 唯一索引没装上 —— B-5 判据会全绿地测空气")
        c.rollback()
    finally:
        c.close()
    yield


#: 本包判据自己写的表。清理顺序 = FK 反向(outbox → command → snapshot → slot)。
#: 🔴 顺序 = **子表在前、父表在后**。这不是风格问题:
#:    ``defgeo_settlement_review_entries`` 对 ``defgeo_publish_commands`` 有外键,
#:    先删父表会 ``ForeignKeyViolation`` ⇒ session fixture 当场死 ⇒
#:    **整包 155 条全 error**(不是"全被杀死",是尺子坏了)。
#:    R3 之前没有任何判据往 Z-1 留痕表里写,所以这个顺序一直没被验过 ——
#:    新增判据开始写一张老表时,分母清理的**顺序**也要跟着核一遍。
_PACKAGE_WRITE_TABLES = (
    "defgeo_settlement_review_entries",     # ← 子:FK → defgeo_publish_commands
    "defgeo_publish_outbox",                # ← 子:FK → defgeo_publish_commands
    "defgeo_publish_commands",
    "defgeo_publish_decision_snapshots",
    "defgeo_publish_slots",
)


@pytest.fixture(scope="session", autouse=True)
def _clean_queue(_schema):                                # noqa: ANN001
    """🔴 **跨 session 的分母清理** —— 一次性库会被反复复用。

    ═══════════════════════════════════════════════════════════════════
    这条夹具是被一次真实的假红逼出来的
    ═══════════════════════════════════════════════════════════════════
    ``dispatch_pending`` 的批量是 10。跑到第 9 个 session 时,库里攒下了
    **433 条 command / 17 条 pending outbox**(其中若干是判据故意造的坏行:
    ``test_15`` 把正文改坏,那条命令的 outbox 会一直躺在 pending)。
    于是新 session 的第一发 ``dispatch_pending`` 把批量全被旧行占满,
    **本条判据自己的命令根本没进批次** ⇒ ``external_start_at is None`` ⇒ 红。

    那种红与被测代码毫无关系 —— 它把「派发没接上」和「队列里排在我前面」
    混成一团(本仓记过:两边都红先比错误签名)。所以每个 session 开局先清空
    本包自己写的那几张表。

    🔴 三条安全栓:
      · conftest 的库名硬闸已经保证这是本包一次性库(必须同时含
        ``defgeo`` / ``pkge`` / ``test``),这里再复核一次;
      · 只删**本包自己写的表** + 只删 ``task_ref`` 带本链前缀的冻结行 ——
        不碰品牌/钱包/目录这些种子行(它们是幂等的,删了反而要重建);
      · 删完做反向自证:该空的必须真的空了。删不掉却继续跑,
        等于把假绿藏到下游。
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
            # 冻结行只删本链的(``task_ref`` 前缀由 publish_funding 写死)。
            cur.execute("DELETE FROM point_freezes WHERE task_ref LIKE 'defgeo_publish_%%'")
            # activation 队列同理:上一 session 没物化完的会顶掉本 session 的批量。
            cur.execute("DELETE FROM defgeo_activation_outbox")
            cur.execute("DELETE FROM defgeo_provider_execution_budgets")

            # ══════════════════════════════════════════════════════════════
            # 🔴 [R2 返修 · Review 实测] **身份轴**也必须由本包自己重建
            # ══════════════════════════════════════════════════════════════
            # Review 侧按交付单 §8 的命令在一把**攒过的**共享库上复核,得 53 假红;
            # 全新库恰 146。真因是身份轴被"继承"了:``user_roles`` 是库里攒下来的,
            # 而 R2 之后**付款方判别是现查角色**的 —— 一条攒下来的 admin 角色行
            # 会把本该走个人钱包的租户整条改判成平台腿,于是个人钱包那一臂
            # (以及依赖它的整条主链)全红。
            #
            # 那种红与被测代码毫无关系,却长得跟"判别位写错了"一模一样。
            # 所以本包用到的每一个测试身份,角色行在 session 开局**先清掉**,
            # 由 ``install_base_rows`` 按本包的意图重建 —— 判据依赖的身份必须是
            # 本包自己建的,不能是库里攒下来的。
            from tests.defgeo_wob_publish_funding_2026_08_25 import _seed   # noqa: PLC0415

            cur.execute("DELETE FROM user_roles WHERE user_id = ANY(%s)",
                        (list(_seed.TEST_USER_IDS),))

            # ── 反向自证:该空的真的空了 ────────────────────────────────
            for table in (*_PACKAGE_WRITE_TABLES, "defgeo_activation_outbox",
                          "defgeo_provider_execution_budgets"):
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                left = cur.fetchone()[0]
                assert left == 0, f"{table} 清理后还剩 {left} 行 —— 分母没清干净"
            cur.execute("SELECT COUNT(*) FROM user_roles WHERE user_id = ANY(%s)",
                        (list(_seed.TEST_USER_IDS),))
            left = cur.fetchone()[0]
            assert left == 0, f"本包测试身份还剩 {left} 条角色行 —— 身份轴没清干净"
    finally:
        conn.close()

    # 🔴 同理:``PLATFORM_DIRECT_SERVICE_USER_ID`` 是**进程环境**里的一个轴。
    #    外部 shell 里恰好设了它(或上一次跑崩没还原),同样会让判别位改判。
    #    session 开局归零,需要它的判据自己设自己还原。
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
