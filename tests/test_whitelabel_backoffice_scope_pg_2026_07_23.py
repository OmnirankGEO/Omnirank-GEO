"""tests/test_whitelabel_backoffice_scope_pg_2026_07_23.py — 真实 PG16 矩阵（统一 R3 §七）

板块 C 白标作用域迁移/回滚在真实 PostgreSQL 上的强制矩阵：

  DB-A（fresh 空库链）
    1. fresh 空库 rollback            → 安全跳过（无 whitelabel_settings/whitelabel_audit）
    2. fresh rollback 2×              → 幂等
    3. forward                        → 建 audit + append-only 触发器（settings 列级守卫跳过）
    4. forward 2×                     → 幂等
    5. rollback                       → audit 归档改名（含数据保留），不 DROP 审计历史
    6. forward                        → 新 audit 重建；归档原样保留
    7. 再次 rollback                  → 归档名冲突，稳定 fail-closed
       WHITELABEL_BACKOFFICE_ROLLBACK_BLOCKED_ARCHIVE_CONFLICT；两表与数据完整保留

  DB-B（既有 whitelabel_settings 升级）
    1. 运行时 20 列基表 + users 132/456/789
    2. forward 2×                     → 新列补齐 + D2 映射幂等
    3. ID 132 固定 customer-only（即使 oem+active+unlocked）；合法 OEM(456) 保留授权；
       无法证明档位(789) fail-closed FALSE
    4. whitelabel_audit UPDATE/DELETE 继续被 DB 触发器拒绝（append-only）
    5. rollback                       → 新列移除；含行 audit 归档保留
    6. forward                        → 重放同值映射；append-only 再次生效

  DB-C（诱饵 search_path · R6 复审 P2）
    全限定 public.* + SET LOCAL 钉死：lure schema 里的同名诱饵表不得被迁移触碰，
    public 对象必须完整建立。

  DB-D（索引契约与归属 · R6/R7/R8 复审 P2 → 统一 R3 §七 单一合同）
    forward 遇 canonical 索引名被异物占用 → WHITELABEL_BACKOFFICE_FORWARD_CONTRACT_DRIFT
    fail-closed（不假成功）；rollback 遇同名异物索引 →
    WHITELABEL_BACKOFFICE_ROLLBACK_BLOCKED_INDEX_OWNERSHIP_CONFLICT fail-closed；
    并发回滚经咨询锁序列化 → 两者干净完成，终态一致。

  统一 R3 §七（2026-07-23 新增）
    · 单一 readiness 合同：migration §7 自验 / prestart / runtime 启动自检 /
      统一 release readiness 四处调同一 DB 合同函数
      public.whitelabel_backoffice_schema_blockers(boolean)（禁第二份口径）；
      合同对 ID132 违规/列 drift/触发器缺失/半成品索引逐一可判别。
    · forward 与 rollback 同一 pg_advisory_xact_lock（key=2026072201）+ 有界
      lock_timeout（5s）：锁被占用时两边都以 55P03 有界超时干净中止；
      forward×rollback 并发序列化、终态一致；rollback×rollback 顺序幂等。

环境纪律（不满足一律 skip 并说明，不得算绿）：
  · 全部用例：TEST_DATABASE_URL 指向一次性 postgres:16 容器；host 必须 loopback
    （localhost/127.0.0.1/::1），基础库名必须含 test 且不含 prod —— 拒生产 DSN。
  · 目录级破坏性用例（SET allow_system_table_mods 改 pg_catalog.pg_index 构造
    半成品索引）额外要求：显式 ALLOW_DESTRUCTIVE_TEST_DB=1 + throwaway 库名含
    test + 当前角色 rolsuper。测试基建只读 env；需要改 env 的用例一律 monkeypatch。
"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest


ROOT = Path(__file__).resolve().parents[1]
PG_URL = os.environ.get("TEST_DATABASE_URL")
FORWARD_SQL = ROOT / "scripts" / "migration_whitelabel_backoffice_scope_2026_07_22.sql"
ROLLBACK_SQL = ROOT / "scripts" / "rollback_whitelabel_backoffice_scope_2026_07_22.sql"

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
ADVISORY_LOCK_KEY = 2026072201

BASE_SETTINGS_DDL = """
CREATE TABLE public.users (id INTEGER PRIMARY KEY, username TEXT);
CREATE TABLE public.whitelabel_settings (
    user_id INTEGER PRIMARY KEY REFERENCES public.users(id),
    company_name TEXT,
    company_logo_url TEXT,
    logo_url TEXT,
    slogan TEXT,
    brand_color VARCHAR(20),
    contact_name TEXT,
    contact_phone TEXT,
    contact_wechat TEXT,
    contact_email TEXT,
    whitelabel_mode TEXT DEFAULT 'none',
    whitelabel_status TEXT DEFAULT 'locked',
    unlocked_by_admin BOOLEAN DEFAULT FALSE,
    approved_by INTEGER,
    approved_at TIMESTAMP,
    product_name TEXT,
    favicon_url TEXT,
    hide_platform_branding BOOLEAN DEFAULT FALSE,
    custom_domain TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def _loopback_test_dsn_or_skip(url: str | None, *, purpose: str) -> str:
    """一次性 loopback 测试库 DSN 硬性校验；不满足即 skip（拒生产 DSN）。"""
    if not url:
        pytest.skip(f"real PostgreSQL URL is required ({purpose} · TEST_DATABASE_URL)")
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host not in LOOPBACK_HOSTS:
        pytest.skip(f"{purpose}: 只允许 loopback 一次性容器 DSN，拒绝疑似生产 host: {host}")
    base_db = (parsed.path or "").lstrip("/").lower()
    if "prod" in base_db or "test" not in base_db:
        pytest.skip(f"{purpose}: 基础库名必须含 test 且不含 prod: {base_db}")
    return url


def _require_destructive_pg(connect, url) -> None:
    """目录级破坏性测试（改 pg_catalog 构造半成品）的硬性前提。

    五项纪律缺一不可，否则 skip（不得算绿）：
    显式 ALLOW_DESTRUCTIVE_TEST_DB=1 · loopback throwaway DSN · 库名含 test
    且不含 prod · rolsuper · 拒生产 DSN。env 只读；变更一律由 monkeypatch 完成。
    """
    if os.environ.get("ALLOW_DESTRUCTIVE_TEST_DB") != "1":
        pytest.skip(
            "destructive 测试需显式 ALLOW_DESTRUCTIVE_TEST_DB=1"
            "（将 SET allow_system_table_mods 改 pg_catalog.pg_index 构造半成品索引）"
        )
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host not in LOOPBACK_HOSTS:
        pytest.skip(f"destructive 测试拒绝非 loopback DSN（疑似生产）: {host}")
    dbname = (parsed.path or "").lstrip("/").lower()
    if "prod" in dbname or "test" not in dbname:
        pytest.skip(f"destructive 测试要求 throwaway 库名含 test 且不含 prod: {dbname}")
    rows = _exec(connect, url, "SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user")
    if not rows or not rows[0]["rolsuper"]:
        pytest.skip("destructive 测试需 rolsuper（SET allow_system_table_mods 改 pg_catalog）")


@pytest.fixture(scope="module")
def pg():
    """一次性独立测试库（module 级多个 throwaway 库：fresh 链/升级链/诱饵/锁等）。"""
    _loopback_test_dsn_or_skip(PG_URL, purpose="whitelabel 迁移矩阵")
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extras import RealDictCursor

    admin = psycopg2.connect(PG_URL)
    admin.autocommit = True
    databases = []
    parsed = urlsplit(PG_URL)

    def make_db(prefix: str):
        # throwaway 库名一律带 test 中缀（destructive 纪律 · 拒生产可见性双保险）
        name = f"{prefix}_test_{uuid.uuid4().hex[:10]}"
        assert "test" in name and "prod" not in name
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        databases.append(name)
        return urlunsplit((parsed.scheme, parsed.netloc, f"/{name}", parsed.query, parsed.fragment))

    def connect(url):
        return psycopg2.connect(url, cursor_factory=RealDictCursor)

    try:
        yield type("PG", (), {"make_db": staticmethod(make_db), "connect": staticmethod(connect)})
    finally:
        with admin.cursor() as cursor:
            for name in databases:
                cursor.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (name,),
                )
                cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
        admin.close()


def _exec(connect, url, statement, params=None):
    conn = connect(url)
    try:
        with conn, conn.cursor() as cursor:
            cursor.execute(statement, params)
            return cursor.fetchall() if cursor.description else None
    finally:
        conn.close()


def _contract_blockers(connect, url, require_settings: bool):
    return _exec(
        connect, url,
        "SELECT blocker FROM public.whitelabel_backoffice_schema_blockers(%s) ORDER BY blocker",
        (require_settings,),
    )


def _hold_advisory_lock(connect, url):
    """占用 forward/rollback 共用的事务级咨询锁（模拟在飞的另一方向迁移）。"""
    conn = connect(url)
    cur = conn.cursor()
    cur.execute("BEGIN")
    cur.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (ADVISORY_LOCK_KEY,))
    return conn


# ============================================================
# DB-A · fresh 空库链
# ============================================================

def test_fresh_rollback_chain_and_archive_conflict_fail_closed(pg):
    import psycopg2

    url = pg.make_db("wl_fresh_r4")
    forward = FORWARD_SQL.read_text(encoding="utf-8")
    rollback = ROLLBACK_SQL.read_text(encoding="utf-8")

    # 1/2. fresh 空库 rollback 连续两次 → 安全跳过
    _exec(pg.connect, url, rollback)
    _exec(pg.connect, url, rollback)

    # 3/4. forward 连续两次 → 幂等（settings 基表缺失 → 列级守卫跳过；audit+触发器建齐）
    _exec(pg.connect, url, forward)
    _exec(pg.connect, url, forward)

    # append-only 触发器已生效
    _exec(pg.connect, url, "INSERT INTO public.whitelabel_audit (user_id, field) VALUES (1, 'probe')")
    conn = pg.connect(url)
    try:
        with conn.cursor() as cursor:
            with pytest.raises(psycopg2.Error, match="append-only"):
                cursor.execute("UPDATE public.whitelabel_audit SET field = 'x' WHERE user_id = 1")
        conn.rollback()
    finally:
        conn.close()

    # 5. rollback → audit（含 1 行审计）RENAME 归档，不 DROP 历史；合同函数同批移除
    _exec(pg.connect, url, rollback)
    rows = _exec(pg.connect, url, "SELECT user_id, field FROM public.whitelabel_audit_archived_20260722")
    assert rows == [{"user_id": 1, "field": "probe"}], "归档必须保留审计行"
    assert _exec(pg.connect, url, "SELECT to_regclass('public.whitelabel_audit') AS t")[0]["t"] is None
    assert _exec(
        pg.connect, url,
        "SELECT to_regprocedure('public.whitelabel_backoffice_schema_blockers(boolean)') AS f",
    )[0]["f"] is None, "rollback 必须同批移除合同函数（本批新增对象）"

    # 6. 再次 forward → 新 audit 重建；归档原样保留；合同函数重建且零 blocker
    _exec(pg.connect, url, forward)
    assert _exec(pg.connect, url, "SELECT COUNT(*) AS c FROM public.whitelabel_audit")[0]["c"] == 0
    rows = _exec(pg.connect, url, "SELECT COUNT(*) AS c FROM public.whitelabel_audit_archived_20260722")
    assert rows[0]["c"] == 1, "归档不得被覆盖"
    assert _contract_blockers(pg.connect, url, False) == [], "forward 后合同必须零 blocker"

    # [R5 · 复审 P2] 回滚已显式重命名归档索引释放 canonical 名 → forward 的
    # CREATE INDEX IF NOT EXISTS 不再静默跳过：新 audit 表必须持有定义级一致的
    # (user_id, created_at DESC) 查询索引；归档索引保留在归档表上。
    idx = _exec(
        pg.connect, url,
        "SELECT indexname, indexdef FROM pg_catalog.pg_indexes "
        "WHERE schemaname = 'public' AND tablename = 'whitelabel_audit'",
    )
    idx_map = {r["indexname"]: r["indexdef"] for r in idx}
    assert idx_map.get("idx_whitelabel_audit_user_created") == (
        "CREATE INDEX idx_whitelabel_audit_user_created ON public.whitelabel_audit "
        "USING btree (user_id, created_at DESC)"
    ), "回滚→前滚后新 audit 表必须重建 (user_id, created_at DESC) 查询索引"
    archived_idx = _exec(
        pg.connect, url,
        "SELECT indexname FROM pg_catalog.pg_indexes "
        "WHERE schemaname = 'public' AND tablename = 'whitelabel_audit_archived_20260722'",
    )
    assert "idx_whitelabel_audit_archived_20260722_user_created" in {
        r["indexname"] for r in archived_idx
    }, "归档索引必须随归档显式重命名"

    # 7. 再次 rollback → 归档名冲突，稳定 fail-closed；失败后两表与数据完整保留
    conn = pg.connect(url)
    try:
        with conn.cursor() as cursor:
            with pytest.raises(
                psycopg2.Error,
                match="WHITELABEL_BACKOFFICE_ROLLBACK_BLOCKED_ARCHIVE_CONFLICT",
            ):
                cursor.execute(rollback)
        conn.rollback()
    finally:
        conn.close()
    assert _exec(pg.connect, url, "SELECT COUNT(*) AS c FROM public.whitelabel_audit")[0]["c"] == 0
    assert _exec(pg.connect, url, "SELECT COUNT(*) AS c FROM public.whitelabel_audit_archived_20260722")[0]["c"] == 1
    # 触发器仍在（回滚整体中止，未执行 DROP）——BEFORE ... FOR EACH ROW 需有行才触发，先插一行
    _exec(pg.connect, url, "INSERT INTO public.whitelabel_audit (user_id, field) VALUES (2, 'probe2')")
    conn = pg.connect(url)
    try:
        with conn.cursor() as cursor:
            with pytest.raises(psycopg2.Error, match="append-only"):
                cursor.execute("DELETE FROM public.whitelabel_audit")
        conn.rollback()
    finally:
        conn.close()


# ============================================================
# DB-B · 既有 settings 升级 + D2 映射
# ============================================================

def test_existing_settings_upgrade_id132_oem_mapping_and_roundtrip(pg):
    import psycopg2

    url = pg.make_db("wl_upgrade_r4")
    forward = FORWARD_SQL.read_text(encoding="utf-8")
    rollback = ROLLBACK_SQL.read_text(encoding="utf-8")

    # 1. 运行时 20 列基表 + 三个账号：132(oem 全档但固定 customer-only)、
    #    456(合法 OEM 授权)、789(无法证明档位 → fail-closed)
    _exec(pg.connect, url, BASE_SETTINGS_DDL)
    _exec(pg.connect, url, "INSERT INTO public.users (id, username) VALUES (132, 'a'), (456, 'b'), (789, 'c')")
    _exec(
        pg.connect, url,
        """
        INSERT INTO public.whitelabel_settings
            (user_id, company_name, whitelabel_mode, whitelabel_status, unlocked_by_admin,
             approved_by, approved_at)
        VALUES (132, 'ID132', 'oem', 'active', TRUE, 1, NOW()),
               (456, 'OEM456', 'oem', 'active', TRUE, 1, NOW()),
               (789, 'EXT789', 'external_only', 'active', FALSE, NULL, NULL)
        """,
    )

    # 2. forward 2× → 新列补齐 + 映射幂等
    _exec(pg.connect, url, forward)
    _exec(pg.connect, url, forward)

    # 3. ID 132 固定 customer-only；合法 OEM 保留；无法证明档位 fail-closed
    rows = _exec(
        pg.connect, url,
        "SELECT user_id, backoffice_brand_unlocked, backoffice_brand_granted_by, brand_version "
        "FROM public.whitelabel_settings ORDER BY user_id",
    )
    assert [r["backoffice_brand_unlocked"] for r in rows] == [False, True, False]
    assert rows[0]["user_id"] == 132, "ID 132 必须固定 customer-only"
    assert rows[1]["backoffice_brand_granted_by"] == 1, "合法 OEM 授权痕迹保留"
    assert all(r["brand_version"] == 1 for r in rows)

    # 4. audit UPDATE/DELETE 继续被 DB 拒绝（append-only）
    _exec(pg.connect, url, "INSERT INTO public.whitelabel_audit (user_id, field) VALUES (456, 'backoffice_brand_unlocked')")
    for mutating in (
        "UPDATE public.whitelabel_audit SET field = 'x' WHERE user_id = 456",
        "DELETE FROM public.whitelabel_audit WHERE user_id = 456",
    ):
        conn = pg.connect(url)
        try:
            with conn.cursor() as cursor:
                with pytest.raises(psycopg2.Error, match="append-only"):
                    cursor.execute(mutating)
            conn.rollback()
        finally:
            conn.close()

    # 5. rollback → 4 新列移除；含行 audit 归档保留
    _exec(pg.connect, url, rollback)
    cols = _exec(
        pg.connect, url,
        """
        SELECT COUNT(*) AS c
          FROM pg_catalog.pg_attribute
         WHERE attrelid = 'public.whitelabel_settings'::pg_catalog.regclass
           AND attname IN ('backoffice_brand_unlocked', 'backoffice_brand_granted_by',
                           'backoffice_brand_granted_at', 'brand_version')
           AND NOT attisdropped
        """,
    )
    assert cols[0]["c"] == 0, "回滚后新列必须移除"
    assert _exec(pg.connect, url, "SELECT COUNT(*) AS c FROM public.whitelabel_audit_archived_20260722")[0]["c"] == 1
    # 既有 20 列基表不受回滚影响
    base_cols = _exec(
        pg.connect, url,
        "SELECT COUNT(*) AS c FROM pg_catalog.pg_attribute "
        "WHERE attrelid = 'public.whitelabel_settings'::pg_catalog.regclass "
        "AND attnum > 0 AND NOT attisdropped",
    )
    assert base_cols[0]["c"] == 20, "既有基表列不得受影响"

    # 6. 再次 forward → 同值重放映射；append-only 再次生效（触发器按行触发，先插一行）
    _exec(pg.connect, url, forward)
    rows = _exec(
        pg.connect, url,
        "SELECT backoffice_brand_unlocked FROM public.whitelabel_settings ORDER BY user_id",
    )
    assert [r["backoffice_brand_unlocked"] for r in rows] == [False, True, False]
    _exec(pg.connect, url, "INSERT INTO public.whitelabel_audit (user_id, field) VALUES (456, 'probe')")
    conn = pg.connect(url)
    try:
        with conn.cursor() as cursor:
            with pytest.raises(psycopg2.Error, match="append-only"):
                cursor.execute("UPDATE public.whitelabel_audit SET field = 'y'")
        conn.rollback()
    finally:
        conn.close()


# ============================================================
# DB-C · 诱饵 search_path
# ============================================================

def test_forward_migration_public_qualified_against_lure_search_path(pg):
    """[R6 · 复审 P2] 诱饵 search_path：同名诱饵表不得被迁移触碰，public 对象完整建立。"""
    url = pg.make_db("wl_lure_r6")
    forward = FORWARD_SQL.read_text(encoding="utf-8")

    # public 基表（运行时 20 列形状）+ lure schema 同名诱饵
    _exec(pg.connect, url, BASE_SETTINGS_DDL)
    _exec(pg.connect, url, "INSERT INTO public.users (id, username) VALUES (132, 'a')")
    _exec(pg.connect, url, "INSERT INTO public.whitelabel_settings (user_id, company_name) VALUES (132, 'LureCheck')")
    _exec(
        pg.connect, url,
        """
        CREATE SCHEMA lure;
        CREATE TABLE lure.whitelabel_settings (user_id INTEGER PRIMARY KEY, marker TEXT);
        CREATE TABLE lure.whitelabel_audit (id INTEGER PRIMARY KEY, marker TEXT);
        INSERT INTO lure.whitelabel_settings (user_id, marker) VALUES (132, 'decoy');
        """,
    )

    # 在诱饵 search_path 下执行 forward：SET LOCAL 钉死 + public.* 全限定双重防线
    conn = pg.connect(url)
    try:
        with conn, conn.cursor() as cursor:
            cursor.execute("SET LOCAL search_path = lure, pg_temp, public")
            cursor.execute(forward)
    finally:
        conn.close()

    # public 侧：新列/审计表/索引/触发器/映射全部落在 public
    cols = _exec(
        pg.connect, url,
        "SELECT COUNT(*) AS c FROM pg_catalog.pg_attribute "
        "WHERE attrelid = 'public.whitelabel_settings'::pg_catalog.regclass "
        "AND attname IN ('backoffice_brand_unlocked', 'backoffice_brand_granted_by', "
        "'backoffice_brand_granted_at', 'brand_version') AND NOT attisdropped",
    )
    assert cols[0]["c"] == 4, "public.whitelabel_settings 必须获得 4 个新列"
    assert _exec(pg.connect, url, "SELECT to_regclass('public.whitelabel_audit') AS t")[0]["t"] == "whitelabel_audit"
    idx = _exec(
        pg.connect, url,
        "SELECT indexdef FROM pg_catalog.pg_indexes WHERE schemaname = 'public' "
        "AND tablename = 'whitelabel_audit' AND indexname = 'idx_whitelabel_audit_user_created'",
    )
    assert idx and "(user_id, created_at DESC)" in idx[0]["indexdef"]
    # ID 132 映射写到了 public 行
    row = _exec(
        pg.connect, url,
        "SELECT backoffice_brand_unlocked FROM public.whitelabel_settings WHERE user_id = 132",
    )
    assert row == [{"backoffice_brand_unlocked": False}]

    # lure 诱饵：列形状零变化（不得被加列/改写），无新对象落进 lure schema
    lure_cols = _exec(
        pg.connect, url,
        "SELECT COUNT(*) AS c FROM pg_catalog.pg_attribute "
        "WHERE attrelid = 'lure.whitelabel_settings'::pg_catalog.regclass "
        "AND attnum > 0 AND NOT attisdropped",
    )
    assert lure_cols[0]["c"] == 2, "诱饵 whitelabel_settings 不得被迁移触碰"
    assert _exec(
        pg.connect, url,
        "SELECT marker FROM lure.whitelabel_settings WHERE user_id = 132",
    ) == [{"marker": "decoy"}]
    lure_audit_cols = _exec(
        pg.connect, url,
        "SELECT COUNT(*) AS c FROM pg_catalog.pg_attribute "
        "WHERE attrelid = 'lure.whitelabel_audit'::pg_catalog.regclass "
        "AND attnum > 0 AND NOT attisdropped",
    )
    assert lure_audit_cols[0]["c"] == 2, "诱饵 whitelabel_audit 不得被迁移触碰"
    lure_triggers = _exec(
        pg.connect, url,
        "SELECT COUNT(*) AS c FROM pg_catalog.pg_trigger t "
        "WHERE t.tgrelid = 'lure.whitelabel_audit'::pg_catalog.regclass AND NOT t.tgisinternal",
    )
    assert lure_triggers[0]["c"] == 0, "触发器不得挂到诱饵表"

    # [统一 R3 §七] 合同函数自身同样免疫诱饵 search_path（函数级 SET search_path 钉死）
    conn = pg.connect(url)
    try:
        with conn, conn.cursor() as cursor:
            cursor.execute("SET LOCAL search_path = lure, pg_temp, public")
            cursor.execute(
                "SELECT blocker FROM public.whitelabel_backoffice_schema_blockers(true) ORDER BY blocker"
            )
            assert cursor.fetchall() == [], "诱饵 search_path 下合同函数必须仍零 blocker"
    finally:
        conn.close()


# ============================================================
# DB-D · 索引契约与归属（forward/rollback fail-closed）
# ============================================================

def test_forward_index_name_conflict_fails_closed(pg):
    """[R7 · 复审 P2 → 统一 R3 §七] canonical 索引名被异物占用 → forward 不再假成功，
    单一合同函数自验稳定 fail-closed。"""
    import psycopg2

    url = pg.make_db("wl_idxfwd_r7")
    forward = FORWARD_SQL.read_text(encoding="utf-8")

    _exec(
        pg.connect, url,
        """
        CREATE TABLE public.decoy_owner (x INTEGER);
        CREATE INDEX idx_whitelabel_audit_user_created ON public.decoy_owner (x);
        """,
    )

    conn = pg.connect(url)
    try:
        with conn.cursor() as cursor:
            with pytest.raises(
                psycopg2.Error,
                match="WHITELABEL_BACKOFFICE_FORWARD_CONTRACT_DRIFT",
            ):
                cursor.execute(forward)
        conn.rollback()
    finally:
        conn.close()

    # 假成功封堵：迁移整体中止，audit 表未落库；异物索引原样保留
    assert _exec(pg.connect, url, "SELECT to_regclass('public.whitelabel_audit') AS t")[0]["t"] is None
    decoy_idx = _exec(
        pg.connect, url,
        "SELECT indexname, tablename FROM pg_catalog.pg_indexes "
        "WHERE schemaname = 'public' AND indexname = 'idx_whitelabel_audit_user_created'",
    )
    assert decoy_idx == [{"indexname": "idx_whitelabel_audit_user_created", "tablename": "decoy_owner"}]


def test_forward_index_half_built_state_fails_closed(pg):
    """[R8 · 复审 P2 → 统一 R3 §七] 半成品索引反例：定义完全正确但 indisvalid/
    indisready/indislive 全 false → forward 二次执行必须 fail-closed（单一合同函数
    判别）；状态恢复后正常通过。

    破坏性纪律：本用例 SET allow_system_table_mods 直改 pg_catalog.pg_index，
    必须满足 _require_destructive_pg 五项前提（显式 flag + loopback throwaway DSN
    + 库名含 test + rolsuper + 拒生产 DSN），否则 skip 不算绿。
    """
    import psycopg2

    url = pg.make_db("wl_idxhalf_r8")
    _require_destructive_pg(pg.connect, url)
    forward = FORWARD_SQL.read_text(encoding="utf-8")

    # 首次 forward 正常建立索引
    _exec(pg.connect, url, forward)

    # 目录级构造半成品索引（仅限一次性 throwaway 测试库 · 前提见 _require_destructive_pg）
    _exec(
        pg.connect, url,
        """
        SET allow_system_table_mods = on;
        UPDATE pg_catalog.pg_index
           SET indisvalid = false, indisready = false, indislive = false
         WHERE indexrelid = 'public.idx_whitelabel_audit_user_created'::pg_catalog.regclass;
        """,
    )
    flags = _exec(
        pg.connect, url,
        "SELECT indisvalid, indisready, indislive FROM pg_catalog.pg_index "
        "WHERE indexrelid = 'public.idx_whitelabel_audit_user_created'::pg_catalog.regclass",
    )
    assert flags == [{"indisvalid": False, "indisready": False, "indislive": False}]

    # 单一合同函数直接判别半成品（不必经迁移也可见）
    blockers = _contract_blockers(pg.connect, url, False)
    assert any("half_built_index:" in b["blocker"] for b in blockers), (
        f"合同函数必须报 half_built_index blocker: {blockers}"
    )

    # forward 二次执行：必须稳定 fail-closed（定义文字相同但状态不可用）
    conn = pg.connect(url)
    try:
        with conn.cursor() as cursor:
            with pytest.raises(
                psycopg2.Error,
                match="WHITELABEL_BACKOFFICE_FORWARD_CONTRACT_DRIFT",
            ):
                cursor.execute(forward)
        conn.rollback()
    finally:
        conn.close()

    # 半成品状态未被 migration 静默带过（仍 false，等待 dba 处置）
    flags = _exec(
        pg.connect, url,
        "SELECT indisvalid FROM pg_catalog.pg_index "
        "WHERE indexrelid = 'public.idx_whitelabel_audit_user_created'::pg_catalog.regclass",
    )
    assert flags == [{"indisvalid": False}]

    # 状态恢复后 forward 再次通过（契约满足）
    _exec(
        pg.connect, url,
        """
        SET allow_system_table_mods = on;
        UPDATE pg_catalog.pg_index
           SET indisvalid = true, indisready = true, indislive = true
         WHERE indexrelid = 'public.idx_whitelabel_audit_user_created'::pg_catalog.regclass;
        """,
    )
    _exec(pg.connect, url, forward)
    flags = _exec(
        pg.connect, url,
        "SELECT indisvalid, indisready, indislive FROM pg_catalog.pg_index "
        "WHERE indexrelid = 'public.idx_whitelabel_audit_user_created'::pg_catalog.regclass",
    )
    assert flags == [{"indisvalid": True, "indisready": True, "indislive": True}]
    assert _contract_blockers(pg.connect, url, False) == []


def test_rollback_index_ownership_conflict_fail_closed(pg):
    """[R6/R7 · 复审 P2] canonical 索引名被其他表占用 → rollback 稳定 fail-closed，不误改异物。"""
    import psycopg2

    url = pg.make_db("wl_idxown_r7")
    rollback = ROLLBACK_SQL.read_text(encoding="utf-8")

    # 手工构造状态：audit 表存在但无索引；canonical 索引名挂在异物表上
    _exec(
        pg.connect, url,
        """
        CREATE TABLE public.whitelabel_audit (
            id BIGSERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            actor_user_id INTEGER,
            actor_role TEXT NOT NULL DEFAULT 'agent',
            field TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            request_id TEXT,
            ip TEXT,
            reason TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE public.decoy_owner (x INTEGER);
        CREATE INDEX idx_whitelabel_audit_user_created ON public.decoy_owner (x);
        """,
    )

    # rollback：归档改名后遇到同名异物索引 → 稳定错误码，整体回滚
    conn = pg.connect(url)
    try:
        with conn.cursor() as cursor:
            with pytest.raises(
                psycopg2.Error,
                match="WHITELABEL_BACKOFFICE_ROLLBACK_BLOCKED_INDEX_OWNERSHIP_CONFLICT",
            ):
                cursor.execute(rollback)
        conn.rollback()
    finally:
        conn.close()

    # 失败后：audit 表未归档、异物索引原样保留在 decoy_owner 上
    assert _exec(pg.connect, url, "SELECT to_regclass('public.whitelabel_audit') AS t")[0]["t"] == "whitelabel_audit"
    assert _exec(pg.connect, url, "SELECT to_regclass('public.whitelabel_audit_archived_20260722') AS t")[0]["t"] is None
    decoy_idx = _exec(
        pg.connect, url,
        "SELECT indexname, tablename FROM pg_catalog.pg_indexes "
        "WHERE schemaname = 'public' AND indexname = 'idx_whitelabel_audit_user_created'",
    )
    assert decoy_idx == [{"indexname": "idx_whitelabel_audit_user_created", "tablename": "decoy_owner"}]


def test_rollback_concurrent_runs_serialized_by_advisory_lock(pg):
    """[R7 · 复审 P2] 并发回滚竞态：咨询锁序列化 → 两者干净完成（后者看到前者成果，
    走幂等跳过），无重复名/半态错误，终态一致。"""
    from concurrent.futures import ThreadPoolExecutor

    url = pg.make_db("wl_race_r7")
    forward = FORWARD_SQL.read_text(encoding="utf-8")
    rollback = ROLLBACK_SQL.read_text(encoding="utf-8")
    _exec(pg.connect, url, forward)

    def run_rollback():
        conn = pg.connect(url)
        try:
            cur = conn.cursor()
            cur.execute(rollback)
            conn.commit()
            return "ok"
        except Exception as exc:
            conn.rollback()
            return f"unexpected: {exc}"
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: run_rollback(), range(2)))

    # 序列化后：一个完成归档，另一个在锁后观察到归档已成、走 fresh/已回滚幂等跳过；
    # 两者都必须无错误干净结束（咨询锁消灭 check-then-rename 交错窗口）
    assert outcomes == ["ok", "ok"], f"并发回滚必须全部干净完成: {outcomes}"
    # 终态：归档表唯一存在且含重命名索引；原表已不在
    assert _exec(pg.connect, url, "SELECT to_regclass('public.whitelabel_audit') AS t")[0]["t"] is None
    archived_idx = _exec(
        pg.connect, url,
        "SELECT indexname FROM pg_catalog.pg_indexes "
        "WHERE schemaname = 'public' AND tablename = 'whitelabel_audit_archived_20260722'",
    )
    assert "idx_whitelabel_audit_archived_20260722_user_created" in {
        r["indexname"] for r in archived_idx
    }


# ============================================================
# 统一 R3 §七 · 单一 readiness 合同（同一函数 · 禁第二份口径）
# ============================================================

def test_single_contract_function_is_the_only_schema_verdict():
    """单一合同静态闸：migration 自验/prestart/runtime/统一 release 四处调同一
    DB 合同函数；薄调用方不得内嵌 schema 期望值（禁第二份口径）；旧内联索引
    自验块已移除；forward/rollback 同一咨询锁 key + 有界 lock_timeout。"""
    forward = FORWARD_SQL.read_text(encoding="utf-8")
    rollback = ROLLBACK_SQL.read_text(encoding="utf-8")

    # 合同函数只由 migration 创建；自验块调同一函数；旧内联索引自验块已收敛移除
    assert "CREATE OR REPLACE FUNCTION public.whitelabel_backoffice_schema_blockers(" in forward
    assert "FROM public.whitelabel_backoffice_schema_blockers(" in forward
    assert "WHITELABEL_BACKOFFICE_FORWARD_CONTRACT_DRIFT" in forward
    assert "WHITELABEL_BACKOFFICE_FORWARD_INDEX_CONTRACT_DRIFT" not in forward, (
        "旧内联索引自验块必须移除（合同口径只有 §6 一份）"
    )

    # forward/rollback 同一咨询锁 key + 有界 lock_timeout；rollback 同批移除合同函数
    for name, sql in (("forward", forward), ("rollback", rollback)):
        assert f"pg_advisory_xact_lock({ADVISORY_LOCK_KEY})" in sql, name
        assert "lock_timeout = '5000'" in sql, name
    assert "DROP FUNCTION IF EXISTS public.whitelabel_backoffice_schema_blockers(boolean);" in rollback

    # 四处调用点：prestart / runtime 启动自检 / 统一 release readiness 经同一薄调用方
    for rel in (
        "scripts/prestart.py",
        "scripts/verify_unified_release_readiness.py",
        "api/referral_api.py",
    ):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "assert_whitelabel_backoffice_schema_ready" in src, f"{rel} 未接同一合同"
    prestart = (ROOT / "scripts" / "prestart.py").read_text(encoding="utf-8")
    assert "require_settings=False" in prestart, "prestart 必须容忍 fresh 基表缺失"
    readiness = (ROOT / "scripts" / "verify_unified_release_readiness.py").read_text(encoding="utf-8")
    assert "require_settings=True" in readiness, "统一 release 必须全量核验"

    # 薄调用方只负责调用，不得内嵌 schema 期望值/系统目录查询（第二份口径）
    wrapper = (ROOT / "services" / "whitelabel_backoffice_schema_contract.py").read_text(encoding="utf-8")
    assert "whitelabel_backoffice_schema_blockers(boolean)" in wrapper
    for forbidden in ("format_type", "pg_get_indexdef", "CREATE OR REPLACE FUNCTION",
                      "missing_column:", "wrong_index_ownership:"):
        assert forbidden not in wrapper, f"薄调用方禁止内嵌口径: {forbidden}"


def test_destructive_guard_requires_explicit_flag(monkeypatch):
    """destructive 纪律①：无显式 ALLOW_DESTRUCTIVE_TEST_DB=1 → skip（不得算绿）。"""
    monkeypatch.delenv("ALLOW_DESTRUCTIVE_TEST_DB", raising=False)
    with pytest.raises(pytest.skip.Exception):
        _require_destructive_pg(None, None)


def test_destructive_guard_rejects_non_loopback_dsn(monkeypatch):
    """destructive 纪律②：flag 齐但 DSN 非 loopback（疑似生产）→ skip（拒生产 DSN）。"""
    monkeypatch.setenv("ALLOW_DESTRUCTIVE_TEST_DB", "1")
    with pytest.raises(pytest.skip.Exception):
        _require_destructive_pg(None, "postgresql://u:p@db.prod.internal:5432/test_x")
    with pytest.raises(pytest.skip.Exception):
        _require_destructive_pg(None, "postgresql://u:p@192.168.1.10:5432/test_x")


def test_contract_zero_blockers_and_fresh_settings_tolerance(pg):
    """合同函数：fresh 守卫（基表缺失）require=FALSE 零 blocker、require=TRUE 报
    missing_table；基表就位后 require=TRUE 零 blocker。"""
    url = pg.make_db("wl_contract_r3")
    forward = FORWARD_SQL.read_text(encoding="utf-8")

    _exec(pg.connect, url, forward)  # fresh：§1 守卫跳过 settings 列级迁移
    assert _contract_blockers(pg.connect, url, False) == []
    blockers = _contract_blockers(pg.connect, url, True)
    assert [b["blocker"] for b in blockers] == ["missing_table:public.whitelabel_settings"]

    # 基表就位后重跑 forward → 全量核验零 blocker
    _exec(pg.connect, url, BASE_SETTINGS_DDL)
    _exec(pg.connect, url, "INSERT INTO public.users (id, username) VALUES (132, 'a')")
    _exec(pg.connect, url, "INSERT INTO public.whitelabel_settings (user_id, company_name) VALUES (132, 'C')")
    _exec(pg.connect, url, forward)
    assert _contract_blockers(pg.connect, url, True) == []


def test_contract_detects_id132_violation(pg):
    """合同判别：ID 132 被置 backoffice_brand_unlocked=TRUE → id132 blocker（事务内构造，回滚还原）。"""
    url = pg.make_db("wl_id132_r3")
    forward = FORWARD_SQL.read_text(encoding="utf-8")

    _exec(pg.connect, url, BASE_SETTINGS_DDL)
    _exec(pg.connect, url, "INSERT INTO public.users (id, username) VALUES (132, 'a')")
    _exec(pg.connect, url, "INSERT INTO public.whitelabel_settings (user_id, company_name) VALUES (132, 'C')")
    _exec(pg.connect, url, forward)
    assert _contract_blockers(pg.connect, url, True) == []

    conn = pg.connect(url)
    try:
        with conn.cursor() as cursor:
            cursor.execute("BEGIN")
            cursor.execute(
                "UPDATE public.whitelabel_settings SET backoffice_brand_unlocked = TRUE WHERE user_id = 132"
            )
            cursor.execute(
                "SELECT blocker FROM public.whitelabel_backoffice_schema_blockers(true) ORDER BY blocker"
            )
            blockers = [r["blocker"] for r in cursor.fetchall()]
            assert any(b.startswith("id132_backoffice_unlocked:") for b in blockers), blockers
        conn.rollback()
    finally:
        conn.close()
    assert _contract_blockers(pg.connect, url, True) == []


def test_contract_detects_column_trigger_and_function_drift(pg):
    """合同判别（事务内构造 drift，回滚还原）：audit 缺列 / 触发器缺失 / 函数定义漂移
    逐一报 blocker —— 证明合同不是恒绿摆设。"""
    url = pg.make_db("wl_drift_r3")
    forward = FORWARD_SQL.read_text(encoding="utf-8")
    _exec(pg.connect, url, forward)
    assert _contract_blockers(pg.connect, url, False) == []

    def blockers_in_txn(statement):
        conn = pg.connect(url)
        try:
            with conn.cursor() as cursor:
                cursor.execute("BEGIN")
                cursor.execute(statement)
                cursor.execute(
                    "SELECT blocker FROM public.whitelabel_backoffice_schema_blockers(false) ORDER BY blocker"
                )
                return [r["blocker"] for r in cursor.fetchall()]
        finally:
            conn.rollback()
            conn.close()

    assert any("missing_column:public.whitelabel_audit.reason" in b
               for b in blockers_in_txn("ALTER TABLE public.whitelabel_audit DROP COLUMN reason"))
    assert any("missing_trigger:" in b
               for b in blockers_in_txn(
                   "DROP TRIGGER trg_whitelabel_audit_append_only ON public.whitelabel_audit"))
    assert any("wrong_function_definition:" in b
               for b in blockers_in_txn(
                   "CREATE OR REPLACE FUNCTION public.trg_whitelabel_audit_append_only() "
                   "RETURNS TRIGGER LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END; $$"))
    assert any("wrong_default:public.whitelabel_audit.actor_role" in b
               for b in blockers_in_txn(
                   "ALTER TABLE public.whitelabel_audit ALTER COLUMN actor_role SET DEFAULT 'system'"))
    # 每次事务回滚后合同恢复零 blocker
    assert _contract_blockers(pg.connect, url, False) == []


# ============================================================
# 统一 R3 §七 · forward/rollback 同一咨询锁 + 有界 lock_timeout
# ============================================================

def test_forward_blocks_on_rollback_advisory_lock_then_times_out_clean(pg):
    """forward×rollback 并发判别①：咨询锁被（模拟在飞 rollback）占用时，forward 必须
    阻塞并以 55P03 lock_not_available 有界超时整体中止（无任何对象落库）；锁释放后
    重跑成功且合同零 blocker。forward 若不取同一锁，本用例立即变红（不会被阻塞）。"""
    import psycopg2

    url = pg.make_db("wl_lockfwd_r3")
    forward = FORWARD_SQL.read_text(encoding="utf-8")

    holder = _hold_advisory_lock(pg.connect, url)
    try:
        started = time.monotonic()
        conn = pg.connect(url)
        try:
            with conn.cursor() as cursor:
                # backstop：即使 lock_timeout 被误删，30s statement_timeout 也让用例
                # 以 QueryCanceled 干净变红，而不是无限挂死
                cursor.execute("SET statement_timeout = '30s'")
                with pytest.raises(psycopg2.errors.LockNotAvailable):
                    cursor.execute(forward)
            conn.rollback()
        finally:
            conn.close()
        elapsed = time.monotonic() - started
        assert 3.0 < elapsed < 25, f"lock_timeout 必须有界（5s 量级），实际 {elapsed:.1f}s"
        # 超时后无任何对象落库（整体中止，无半态）
        assert _exec(pg.connect, url, "SELECT to_regclass('public.whitelabel_audit') AS t")[0]["t"] is None
    finally:
        holder.rollback()
        holder.close()

    # 锁释放后重跑成功；合同函数与薄调用方均零 blocker
    _exec(pg.connect, url, forward)
    assert _contract_blockers(pg.connect, url, False) == []
    from services.whitelabel_backoffice_schema_contract import (
        assert_whitelabel_backoffice_schema_ready,
        whitelabel_backoffice_schema_blockers,
    )

    conn = pg.connect(url)
    try:
        with conn.cursor() as cursor:
            assert whitelabel_backoffice_schema_blockers(cursor, require_settings=False) == []
            assert_whitelabel_backoffice_schema_ready(cursor, require_settings=False)
    finally:
        conn.close()


def test_rollback_blocks_on_forward_advisory_lock_then_times_out_clean(pg):
    """forward×rollback 并发判别②：咨询锁被（模拟在飞 forward）占用时，rollback 同样
    55P03 有界超时干净中止、现场零改动；锁释放后 rollback 成功（audit 归档、合同函数
    同批移除），薄调用方在函数缺失时 fail-closed 报 missing_contract_function。"""
    import psycopg2

    url = pg.make_db("wl_lockrb_r3")
    forward = FORWARD_SQL.read_text(encoding="utf-8")
    rollback = ROLLBACK_SQL.read_text(encoding="utf-8")
    _exec(pg.connect, url, forward)
    _exec(pg.connect, url, "INSERT INTO public.whitelabel_audit (user_id, field) VALUES (1, 'probe')")

    holder = _hold_advisory_lock(pg.connect, url)
    try:
        started = time.monotonic()
        conn = pg.connect(url)
        try:
            with conn.cursor() as cursor:
                cursor.execute("SET statement_timeout = '30s'")
                with pytest.raises(psycopg2.errors.LockNotAvailable):
                    cursor.execute(rollback)
            conn.rollback()
        finally:
            conn.close()
        elapsed = time.monotonic() - started
        assert 3.0 < elapsed < 25, f"lock_timeout 必须有界（5s 量级），实际 {elapsed:.1f}s"
        # 超时后现场零改动：audit 仍在（未归档）、合同函数仍在
        assert _exec(pg.connect, url, "SELECT to_regclass('public.whitelabel_audit') AS t")[0]["t"] == "whitelabel_audit"
        assert _exec(pg.connect, url, "SELECT COUNT(*) AS c FROM public.whitelabel_audit")[0]["c"] == 1
        assert _contract_blockers(pg.connect, url, False) == []
    finally:
        holder.rollback()
        holder.close()

    # 锁释放后 rollback 成功：audit 归档（含数据）、合同函数同批移除
    _exec(pg.connect, url, rollback)
    assert _exec(pg.connect, url, "SELECT to_regclass('public.whitelabel_audit') AS t")[0]["t"] is None
    assert _exec(pg.connect, url, "SELECT COUNT(*) AS c FROM public.whitelabel_audit_archived_20260722")[0]["c"] == 1

    from services.whitelabel_backoffice_schema_contract import (
        whitelabel_backoffice_schema_blockers,
    )

    conn = pg.connect(url)
    try:
        with conn.cursor() as cursor:
            blockers = whitelabel_backoffice_schema_blockers(cursor, require_settings=False)
            assert any(b.startswith("missing_contract_function:") for b in blockers), blockers
    finally:
        conn.close()


def test_forward_rollback_concurrent_serialized_consistent_end_state(pg):
    """forward×rollback 并发判别③：两方向真并发（表锁屏障强制重叠启动）→ 同一咨询锁
    序列化，双方干净完成（55P03 超时有界重试）；终态必为「完整 forward」或「完整
    rollback」两种一致状态之一，绝无半态交错。无同一锁时本用例交错报错变红。"""
    from concurrent.futures import ThreadPoolExecutor

    import psycopg2

    url = pg.make_db("wl_fxrb_r3")
    forward = FORWARD_SQL.read_text(encoding="utf-8")
    rollback = ROLLBACK_SQL.read_text(encoding="utf-8")

    # 起始为已 forward 状态（含 settings 行与 1 行审计）
    _exec(pg.connect, url, BASE_SETTINGS_DDL)
    _exec(pg.connect, url, "INSERT INTO public.users (id, username) VALUES (132, 'a'), (456, 'b')")
    _exec(
        pg.connect, url,
        """
        INSERT INTO public.whitelabel_settings
            (user_id, company_name, whitelabel_mode, whitelabel_status, unlocked_by_admin,
             approved_by, approved_at)
        VALUES (132, 'ID132', 'oem', 'active', TRUE, 1, NOW()),
               (456, 'OEM456', 'oem', 'active', TRUE, 1, NOW())
        """,
    )
    _exec(pg.connect, url, forward)
    _exec(pg.connect, url, "INSERT INTO public.whitelabel_audit (user_id, field) VALUES (456, 'probe')")

    def run_script(script):
        for _attempt in range(3):
            conn = pg.connect(url)
            try:
                cur = conn.cursor()
                cur.execute(script)
                conn.commit()
                return "ok"
            except psycopg2.errors.LockNotAvailable:
                conn.rollback()  # 有界超时 → 干净重试（序列化后幂等）
                continue
            except Exception as exc:
                conn.rollback()
                return f"unexpected: {type(exc).__name__}: {exc}"
            finally:
                conn.close()
        return "lock_timeout_exhausted"

    # 表锁屏障：强制两线程在 audit 表上重叠等待，消灭"先后起跑无竞争"的假并发
    barrier = pg.connect(url)
    try:
        with barrier.cursor() as cursor:
            cursor.execute("BEGIN")
            cursor.execute("LOCK TABLE public.whitelabel_audit IN ACCESS EXCLUSIVE MODE")
            with ThreadPoolExecutor(max_workers=2) as executor:
                fut_forward = executor.submit(run_script, forward)
                fut_rollback = executor.submit(run_script, rollback)
                time.sleep(0.6)  # 两线程均已进入临界区排队
                cursor.execute("COMMIT")
            outcomes = sorted([fut_forward.result(), fut_rollback.result()])
    finally:
        barrier.close()

    assert outcomes == ["ok", "ok"], f"并发 forward×rollback 必须全部干净完成: {outcomes}"

    # 终态一致性判别（二选一，绝不半态）
    audit_present = _exec(pg.connect, url, "SELECT to_regclass('public.whitelabel_audit') AS t")[0]["t"] is not None
    new_cols = _exec(
        pg.connect, url,
        "SELECT COUNT(*) AS c FROM pg_catalog.pg_attribute "
        "WHERE attrelid = 'public.whitelabel_settings'::pg_catalog.regclass "
        "AND attname IN ('backoffice_brand_unlocked','backoffice_brand_granted_by',"
        "'backoffice_brand_granted_at','brand_version') AND NOT attisdropped",
    )[0]["c"]
    if audit_present:
        # forward 收尾：全量合同零 blocker（含 ID132/OEM 映射与 append-only）
        assert new_cols == 4
        assert _contract_blockers(pg.connect, url, True) == []
        rows = _exec(
            pg.connect, url,
            "SELECT backoffice_brand_unlocked FROM public.whitelabel_settings ORDER BY user_id",
        )
        assert [r["backoffice_brand_unlocked"] for r in rows] == [False, True]
    else:
        # rollback 收尾：audit 归档（含 probe 行）、新列移除、合同函数同批移除
        assert new_cols == 0
        assert _exec(
            pg.connect, url,
            "SELECT COUNT(*) AS c FROM public.whitelabel_audit_archived_20260722",
        )[0]["c"] == 1
        assert _exec(
            pg.connect, url,
            "SELECT to_regprocedure('public.whitelabel_backoffice_schema_blockers(boolean)') AS f",
        )[0]["f"] is None


def test_rollback_twice_sequential_idempotent_state_unchanged(pg):
    """rollback×rollback 顺序幂等：第二次 rollback 是干净的 no-op（audit 已不存在 →
    守卫跳过；归档冲突检查不触发；DROP IF EXISTS 幂等），前后状态指纹完全一致。"""
    url = pg.make_db("wl_rb2_r3")
    forward = FORWARD_SQL.read_text(encoding="utf-8")
    rollback = ROLLBACK_SQL.read_text(encoding="utf-8")

    _exec(pg.connect, url, BASE_SETTINGS_DDL)
    _exec(pg.connect, url, "INSERT INTO public.users (id, username) VALUES (132, 'a'), (456, 'b')")
    _exec(
        pg.connect, url,
        """
        INSERT INTO public.whitelabel_settings
            (user_id, company_name, whitelabel_mode, whitelabel_status, unlocked_by_admin,
             approved_by, approved_at)
        VALUES (132, 'ID132', 'oem', 'active', TRUE, 1, NOW()),
               (456, 'OEM456', 'oem', 'active', TRUE, 1, NOW())
        """,
    )
    _exec(pg.connect, url, forward)
    _exec(pg.connect, url, "INSERT INTO public.whitelabel_audit (user_id, field) VALUES (456, 'probe')")

    def fingerprint():
        return _exec(
            pg.connect, url,
            """
            SELECT to_regclass('public.whitelabel_audit') IS NULL AS audit_gone,
                   to_regclass('public.whitelabel_audit_archived_20260722') IS NOT NULL AS archived,
                   to_regprocedure('public.whitelabel_backoffice_schema_blockers(boolean)') IS NULL AS contract_fn_gone,
                   to_regprocedure('public.trg_whitelabel_audit_append_only()') IS NULL AS appendonly_fn_gone,
                   (SELECT COUNT(*) FROM pg_catalog.pg_attribute a
                     WHERE a.attrelid = 'public.whitelabel_settings'::pg_catalog.regclass
                       AND a.attname IN ('backoffice_brand_unlocked','backoffice_brand_granted_by',
                                         'backoffice_brand_granted_at','brand_version')
                       AND NOT a.attisdropped) AS new_cols,
                   (SELECT COUNT(*) FROM pg_catalog.pg_attribute a
                     WHERE a.attrelid = 'public.whitelabel_settings'::pg_catalog.regclass
                       AND a.attnum > 0 AND NOT a.attisdropped) AS total_cols,
                   (SELECT COUNT(*) FROM public.whitelabel_audit_archived_20260722) AS archived_rows,
                   (SELECT COUNT(*) FROM public.whitelabel_settings) AS settings_rows,
                   (SELECT COUNT(*) FROM pg_catalog.pg_trigger t
                     WHERE t.tgrelid = 'public.whitelabel_audit_archived_20260722'::pg_catalog.regclass
                       AND NOT t.tgisinternal) AS archived_triggers
            """,
        )[0]

    _exec(pg.connect, url, rollback)
    fp1 = fingerprint()
    # 第二次 rollback：干净 no-op（不 RAISE、不改任何对象）
    _exec(pg.connect, url, rollback)
    fp2 = fingerprint()
    assert fp1 == fp2, f"rollback×rollback 必须幂等（状态指纹一致）: {fp1} vs {fp2}"
    assert fp1["audit_gone"] and fp1["archived"] and fp1["contract_fn_gone"]
    assert fp1["new_cols"] == 0 and fp1["total_cols"] == 20
    assert fp1["archived_rows"] == 1 and fp1["archived_triggers"] == 0
