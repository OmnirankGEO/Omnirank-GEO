"""Codex 终签 P1:迁移约束守卫必须**绑定作用域** —— 真 PG16 判别测试 + 机械分母锁。

═══════════════════════════════════════════════════════════════════════
被测的是什么
═══════════════════════════════════════════════════════════════════════
``IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='X')`` 这种守卫
**没有绑定表**。而 ``pg_constraint.conname`` 在 PostgreSQL 里**不是 schema 唯一**的
(只在同一张表上唯一)。所以库里任意一张**别的**表上只要存在同名约束,
这个守卫就会判"已存在"→ 目标表上的约束**被静默跳过**。

后果按约束类型分两种,后一种才是真正阴的:
  · UNIQUE:它要建同名索引,而**索引名是 schema 唯一**的,所以会当场报错 ——
    响亮,能被发现;
  · CHECK / FK:没有索引名冲突,于是**一声不响地不建**。租户隔离的 FK、
    状态机的 CHECK 就这么消失了,而迁移返回成功。

所以本文件的诱饵用 **CHECK**(``chk_defgeo_qplan_mode``)—— 那是会静默失败的那一种。

═══════════════════════════════════════════════════════════════════════
🔴 分母是机械扫出来的
═══════════════════════════════════════════════════════════════════════
"哪些守卫没绑 conrelid"如果由我手写一份名单,漏掉的那一条不会让任何判据变红
(本仓 2026-08 记过多次)。所以 :func:`test_no_unscoped_constraint_guard_remains`
现扫 ``db/migration_04*.sql`` 的**全部** ``FROM pg_constraint`` 守卫。
"""

from __future__ import annotations

import re
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

from tests.defensive_geo_w3_2026_08_21.conftest import EXACT_THROWAWAY_URL

ROOT = Path(__file__).resolve().parents[2]
MIGRATION_GLOB = "migration_04*.sql"

#: 本次绑定作用域的四个文件(042/043 本来就是对的,当模板)。
SCOPED_FILES = (
    "db/migration_040_defgeo_question_plans_2026_08_21.sql",
    "db/migration_041_defgeo_run_previews_2026_08_21.sql",
    "db/migration_044_defgeo_publish_decision_2026_08_21.sql",
    "db/migration_046_defgeo_monitoring_lineage_2026_08_22.sql",
)
TEMPLATE_FILES = (
    "db/migration_042_defgeo_customer_accepted_snapshot_2026_08_21.sql",
    "db/migration_043_defgeo_activation_outbox_2026_08_21.sql",
)

_PROBE_DB = "geo_defgeo_conrelid_probe_test"


# ══════════════════════════════════════════════════════════════════════════
# 机械分母:扫全部 pg_constraint 守卫
# ══════════════════════════════════════════════════════════════════════════
def _guard_spans(sql: str) -> list[tuple[int, int]]:
    """每个 ``FROM pg_constraint`` 守卫的字符区间(到闭合它的 ``)`` 为止)。"""
    spans: list[tuple[int, int]] = []
    for m in re.finditer(r"FROM\s+pg_constraint", sql, re.IGNORECASE):
        depth = 0
        i = m.end()
        while i < len(sql):
            ch = sql[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    break
                depth -= 1
            i += 1
        spans.append((m.start(), i))
    return spans


def _unscoped_guards(sql: str) -> list[str]:
    out = []
    for start, end in _guard_spans(sql):
        seg = sql[start:end]
        if "conrelid" in seg:
            continue
        name = re.search(r"conname\s*=\s*'([A-Za-z0-9_]+)'", seg)
        out.append(name.group(1) if name else seg[:60])
    return out


def _migration_files() -> list[Path]:
    return sorted((ROOT / "db").glob(MIGRATION_GLOB))


def test_no_unscoped_constraint_guard_remains() -> None:
    """机械分母锁:``db/migration_04*.sql`` 里不许再有不绑 conrelid 的守卫。

    这条是**防第 5 次同型复发**的那一道:同一形态在本仓已经反复出现,
    每次都靠人读代码发现。现在改成扫出来。
    """
    files = _migration_files()
    assert len(files) >= 7, f"只扫到 {len(files)} 个迁移文件 —— glob 走空了(零分母)"

    total_guards = 0
    offenders: dict[str, list[str]] = {}
    for path in files:
        sql = path.read_text(encoding="utf-8", errors="replace")
        total_guards += len(_guard_spans(sql))
        bad = _unscoped_guards(sql)
        if bad:
            offenders[path.name] = bad

    # 🔴 分母非空自证:全家族至少有几十条守卫。扫不到守卫时"零违规"是假绿。
    assert total_guards >= 40, f"全家族只扫到 {total_guards} 条守卫 —— 扫描器瞎了"
    assert not offenders, (
        "这些 pg_constraint 存在性守卫没有绑定 conrelid(同名约束长在别的表上时会被"
        f"误判成『已存在』,目标表上的约束被静默跳过):{offenders}"
    )


def test_scanner_has_discriminating_power() -> None:
    """反向对照:扫描器必须认得出**没绑**的那一种,否则上面那条恒绿。"""
    unscoped = """
    DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'zz_probe_fake') THEN
            ALTER TABLE public.zz_probe ADD CONSTRAINT zz_probe_fake CHECK (true);
        END IF;
    END $$;
    """
    scoped = """
    DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'zz_probe_fake'
                         AND conrelid = 'public.zz_probe'::regclass) THEN
            ALTER TABLE public.zz_probe ADD CONSTRAINT zz_probe_fake CHECK (true);
        END IF;
    END $$;
    """
    assert _unscoped_guards(unscoped) == ["zz_probe_fake"], "认不出未绑定的守卫"
    assert _unscoped_guards(scoped) == [], "把已绑定的守卫误报成违规"


def test_045_contributes_zero_guards() -> None:
    """终签单点名:045 本来就没有这类守卫,零命中即绿(不是漏扫)。"""
    p = ROOT / "db" / "migration_045_defgeo_run_dispatch_2026_08_22.sql"
    assert p.exists(), "045 不在 —— 这条断言会变成对空气说话"
    assert _guard_spans(p.read_text(encoding="utf-8", errors="replace")) == []


def test_template_files_stay_scoped() -> None:
    """042/043 是正确模板 —— 它们退化了同样要红。"""
    for rel in TEMPLATE_FILES:
        sql = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
        assert _guard_spans(sql), f"{rel} 里一条守卫都没有 —— 模板锚点没了"
        assert _unscoped_guards(sql) == [], f"{rel}(模板)退化成了不绑定作用域"


def test_every_scoped_file_actually_has_guards() -> None:
    """活性:四个被改文件里确实存在守卫,否则『全部已绑定』是对空气说的。"""
    for rel in SCOPED_FILES:
        sql = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
        assert len(_guard_spans(sql)) >= 4, f"{rel} 守卫数异常少 —— 分母塌了"


# ══════════════════════════════════════════════════════════════════════════
# 判别测试:诱饵表上先放同名约束,迁移仍必须在目标表上建成
# ══════════════════════════════════════════════════════════════════════════
def _admin_conn():
    base, _, _db = EXACT_THROWAWAY_URL.rpartition("/")
    conn = psycopg2.connect(base + "/postgres")
    conn.autocommit = True
    return conn


def _fresh_probe_db() -> str:
    """每次判别测试都从**空库**起 —— 迁移是幂等的,残留会让诱饵失去意义。"""
    assert "defgeo" in _PROBE_DB and "test" in _PROBE_DB, "安全栓:库名必须含 defgeo 与 test"
    admin = _admin_conn()
    try:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{_PROBE_DB}" WITH (FORCE)')
            cur.execute(f'CREATE DATABASE "{_PROBE_DB}"')
    finally:
        admin.close()
    base, _, _ = EXACT_THROWAWAY_URL.rpartition("/")
    return base + "/" + _PROBE_DB


def _run_migration(url: str, rel: str) -> None:
    sql = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
    conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO public")
            cur.execute(sql)
    finally:
        conn.close()


def _constraintdef(url: str, cname: str, table: str) -> str | None:
    conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint "
                " WHERE conname=%s AND conrelid=%s::regclass",
                (cname, f"public.{table}"))
            row = cur.fetchone()
        conn.rollback()
    finally:
        conn.close()
    return row["d"] if row else None


#: 诱饵用 **CHECK**:UNIQUE 会因索引名冲突当场报错(响亮),
#: CHECK 才是"静默不建"的那一种 —— 也就是真正会溜过去的那一种。
_DECOY_CONSTRAINT = "chk_defgeo_qplan_mode"
_TARGET_TABLE = "defgeo_question_plans"
_MIGRATION = "db/migration_040_defgeo_question_plans_2026_08_21.sql"


def test_decoy_same_named_constraint_does_not_block_the_real_one() -> None:
    """🔴 Codex 指定的反例:别的表上先有同名约束,迁移仍必须在目标表上建成。

    绑定 conrelid 之前,这里会拿到 ``None``(守卫被诱饵骗了,静默跳过)。
    """
    url = _fresh_probe_db()
    conn = psycopg2.connect(url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE public.zz_decoy (mode TEXT)")
            cur.execute(
                f"ALTER TABLE public.zz_decoy "
                f"ADD CONSTRAINT {_DECOY_CONSTRAINT} CHECK (mode IS NOT NULL)")
    finally:
        conn.close()

    # 诱饵确实到位(否则这条判据在证明一个不存在的场景)
    assert _constraintdef(url, _DECOY_CONSTRAINT, "zz_decoy") is not None, "诱饵没建上"

    _run_migration(url, _MIGRATION)

    actual = _constraintdef(url, _DECOY_CONSTRAINT, _TARGET_TABLE)
    assert actual is not None, (
        f"诱饵表上有同名约束时,{_TARGET_TABLE}.{_DECOY_CONSTRAINT} **没有被创建** —— "
        "守卫被同名约束骗了,约束静默消失而迁移返回成功")
    # exact:不只验存在
    assert "mode" in actual and "defensive" in actual, f"定义不像目标那条:{actual}"


def test_without_decoy_behaviour_is_unchanged() -> None:
    """反向:没有诱饵时,结果必须与有诱饵时**逐字相同**。

    没有这条,上面那条无法区分"绑定生效"与"迁移碰巧总能建成"。
    """
    url = _fresh_probe_db()
    _run_migration(url, _MIGRATION)
    plain = _constraintdef(url, _DECOY_CONSTRAINT, _TARGET_TABLE)
    assert plain is not None, "无诱饵时都建不成 —— 判据底座坏了"

    url2 = _fresh_probe_db()
    conn = psycopg2.connect(url2)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE public.zz_decoy (mode TEXT)")
            cur.execute(
                f"ALTER TABLE public.zz_decoy "
                f"ADD CONSTRAINT {_DECOY_CONSTRAINT} CHECK (mode IS NOT NULL)")
    finally:
        conn.close()
    _run_migration(url2, _MIGRATION)
    decoyed = _constraintdef(url2, _DECOY_CONSTRAINT, _TARGET_TABLE)

    assert decoyed == plain, (
        f"有无诱饵拿到的定义不一样:\n  无诱饵 {plain}\n  有诱饵 {decoyed}")


# ══════════════════════════════════════════════════════════════════════
# 041 的另一种"同名对象跨作用域"—— information_schema 是**跨 schema** 的
#
# 040 那两条诱饵判的是 `pg_constraint` 不绑表;这一对判的是
# `information_schema.columns` 不绑 schema。同一件事的第三种长法
# (第二种是 CREATE INDEX IF NOT EXISTS 按名判存)。
# ══════════════════════════════════════════════════════════════════════

_MIGRATION_041 = "db/migration_041_defgeo_run_previews_2026_08_21.sql"
_PREVIEWS_TABLE = "defgeo_diagnosis_run_previews"
#: 041 的"禁第二套 settlement enum"守卫要挡的那几列之一。
_FORBIDDEN_COLUMN = "run_status"


def _add_column(url: str, qualified_table: str, column: str) -> None:
    conn = psycopg2.connect(url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f"ALTER TABLE {qualified_table} ADD COLUMN {column} TEXT")
    finally:
        conn.close()


def test_decoy_previews_table_in_another_schema_does_not_trip_041() -> None:
    """🔴 别的 schema 里一张同名表,不许替真表回答"这里有没有 settlement 列"。

    041 的反查块问的是「``defgeo_diagnosis_run_previews`` 上有没有
    run_status/funding_state/… 」。``information_schema`` 是**跨 schema** 的:
    不带 ``table_schema = 'public'`` 时,这句话问的其实是
    「**任何** schema 里有没有」—— 于是 ``zz_other`` 里一张同名表就能让
    真表干净的库**发不了车**(迁移 RAISE),而真正的目标表一个字没错。

    拆红:把 041 里那句 ``AND table_schema = 'public'`` 删掉。
    """
    url = _fresh_probe_db()
    conn = psycopg2.connect(url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA zz_other")
            cur.execute(
                f"CREATE TABLE zz_other.{_PREVIEWS_TABLE} "
                f"({_FORBIDDEN_COLUMN} TEXT)")
    finally:
        conn.close()

    # 诱饵确实到位(否则这条判据在证明一个不存在的场景)
    conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM information_schema.columns "
                " WHERE table_name=%s AND column_name=%s",
                (_PREVIEWS_TABLE, _FORBIDDEN_COLUMN))
            assert int(cur.fetchone()["n"]) == 1, "诱饵没建上 —— 场景不成立"
    finally:
        conn.close()

    _run_migration(url, _MIGRATION_041)          # 不抛 = 通过

    # 真表建成了,而且**没有**那一列 —— 反查块判的确实是它。
    conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM information_schema.columns "
                " WHERE table_schema='public' AND table_name=%s", (_PREVIEWS_TABLE,))
            assert int(cur.fetchone()["n"]) > 0, "public 上的真表没建出来"
            cur.execute(
                "SELECT count(*) AS n FROM information_schema.columns "
                " WHERE table_schema='public' AND table_name=%s AND column_name=%s",
                (_PREVIEWS_TABLE, _FORBIDDEN_COLUMN))
            assert int(cur.fetchone()["n"]) == 0, "真表上出现了被禁的 settlement 列"
    finally:
        conn.close()


def test_041_still_raises_on_a_real_forbidden_column_in_public() -> None:
    """判别力自证:加了 schema 谓词之后,守卫**仍然咬得动**真表。

    没有这一条,上一条可以靠"把守卫改成永远查不到东西"通过 ——
    那是把一条承重的反查块悄悄变成装饰。
    """
    url = _fresh_probe_db()
    _run_migration(url, _MIGRATION_041)                       # 先建好真表
    _add_column(url, f"public.{_PREVIEWS_TABLE}", _FORBIDDEN_COLUMN)

    with pytest.raises(psycopg2.Error) as caught:
        _run_migration(url, _MIGRATION_041)                   # 重放 ⇒ 必须 RAISE
    assert "settlement" in str(caught.value), (
        f"抛了,但不是「禁第二套 settlement enum」那条:{caught.value}")


def test_exact_readiness_block_is_present_and_load_bearing() -> None:
    """exact schema readiness 自证块必须真的在四个文件里,且比的是定义不是存在。"""
    for rel in SCOPED_FILES:
        sql = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
        assert "exact schema readiness" in sql, f"{rel} 没有 readiness 块"
        assert "defgeo_ident_constraint(" in sql, (
        f"{rel} 的 readiness 只验存在没比定义"
        "(锚已随取值口更新:比的是语义身份,不是 pg_get_constraintdef 的渲染文本)")
        assert "定义漂移" in sql, f"{rel} 的 readiness 没有漂移分支"


@pytest.mark.parametrize("rel", SCOPED_FILES)
def test_migration_body_stays_zero_dml(rel: str) -> None:
    """迁移体仍零 DML、纯 additive(readiness 块只 SELECT + RAISE)。"""
    sql = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
    body = "\n".join(ln for ln in sql.splitlines() if not ln.lstrip().startswith("--"))
    for verb in (r"^\s*INSERT\s+INTO\s", r"^\s*UPDATE\s+\w", r"^\s*DELETE\s+FROM\s"):
        hit = re.search(verb, body, re.IGNORECASE | re.MULTILINE)
        assert hit is None, f"{rel} 出现 DML:{hit.group(0)!r}"
    for verb in ("DROP TABLE", "DROP COLUMN", "TRUNCATE"):
        assert verb not in body.upper(), f"{rel} 出现破坏性语句 {verb}"
