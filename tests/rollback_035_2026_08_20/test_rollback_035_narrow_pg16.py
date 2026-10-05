"""判据 · 035 回滚收窄 SQL(P1 · 回滚纵深 · 2026-08-20)。

## 这份要证明什么

36 班把 `db/migration_035_geo_image_note_slot_channel_2026_08_18.sql` 上了生产,
`ck_geo_article_slot_event_kind` 从 9 值扩到 14 值。要把版本退回 `c2ad8825` 时,
那一版的 `services/article_closed_loop_schema_contract.py:273` 把 **9 值定义逐字钉死**
⇒ 库里 14 值 ⇒ `assert_schema_ready()` 抛 `GEO_ARTICLE_CLOSED_LOOP_SCHEMA_NOT_READY`
⇒ **版本回退腿被我们自己的守卫挡住**。

`scripts/rollback_migration_035_narrow_slot_event_kind_2026_08_18.sql` 负责把它收窄回去。

四条验收判据(全部在**同构库**上实跑 —— 生产 schema 快照 + 真跑 035 + 真跑回滚 SQL):

1. `test_rollback_makes_the_old_guard_pass` —— 035 → 回滚 SQL → 旧契约守卫转 OK;
2. `test_without_rollback_the_old_guard_still_fails` —— 反向对照:不跑回滚,守卫必须仍红;
3. `test_rows_using_the_new_kinds_block_the_narrowing` —— 注毒:先插一行新枚举,
   回滚 SQL 必须被 DO 块拦下、报错**指名那几行**,且 CHECK 原样不动;
4. `test_rollback_is_idempotent` —— 跑两遍幂等。

外加三条自证(缺一条,上面四条都可能是假绿):

* `test_the_old_contract_module_is_really_the_old_one` —— 旧契约是从 `git show c2ad8825:`
  真取的,不是手抄的当前那份(手抄 = 两边一起错还一起绿);
* `test_the_guard_passes_on_the_untouched_prod_snapshot` —— 守卫在**没动过**的生产快照上
  必须是绿的。它要是本来就永远红,判据 1 的「转 OK」根本无从谈起;
* `test_the_rollback_artifact_is_not_in_the_migration_manifest` —— 🔴 工单红线:
  它是人工回滚工件,**不进 prestart 迁移清单**(prestart 每次部署无条件重放全部迁移,
  收窄进了清单就会和同一份清单里的 035 互相拆台)。
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PG_URL = os.environ.get("TEST_DATABASE_URL")
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

#: 生产 schema 快照(035 **之前**的形状:该 CHECK 是 9 值)。
SCHEMA_SQL = ROOT / "tests" / "article_self_report_2026_08_19" / "prod_schema_2026-08-19.sql"
MIGRATION_035 = ROOT / "db" / "migration_035_geo_image_note_slot_channel_2026_08_18.sql"
ROLLBACK_SQL = ROOT / "scripts" / "rollback_migration_035_narrow_slot_event_kind_2026_08_18.sql"

#: 要退回去的那个版本 —— 旧契约从这里取,不手抄。
OLD_SHA = "c2ad8825"

NEW_KINDS = ("slot_claimed", "slot_released", "generation_started", "post_linked", "ready")

CONSTRAINT = "ck_geo_article_slot_event_kind"


# ══════════════════════════════════════════════════════════════════════════
# 底座
# ══════════════════════════════════════════════════════════════════════════

def _skip_unless_throwaway_pg():
    if not PG_URL:
        pytest.skip("需要 TEST_DATABASE_URL(一次性 loopback 测试库)")
    parsed = urlsplit(PG_URL)
    if (parsed.hostname or "").lower() not in LOOPBACK_HOSTS:
        pytest.skip(f"只允许 loopback 一次性容器 DSN:{parsed.hostname}")
    base_db = (parsed.path or "").lstrip("/").lower()
    if "prod" in base_db or "test" not in base_db:
        pytest.skip(f"基础库名必须含 test 且不含 prod:{base_db}")


def _url_for(name: str) -> str:
    parsed = urlsplit(PG_URL)
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{name}", parsed.query, parsed.fragment))


def _snapshot_body() -> str:
    raw = SCHEMA_SQL.read_text(encoding="utf-8")
    return "\n".join(
        line for line in raw.splitlines()
        if not line.startswith("\\") and not line.startswith("CREATE SCHEMA public;")
    )


@pytest.fixture(scope="module")
def template_db():
    """生产形状模板库:装一次,后面每条用例从它 CREATE DATABASE ... TEMPLATE 复制。

    每条用例一把**独立**的库 —— 本文件的用例会改 schema、会插毒数据,
    共用一把库的话上一条的残留会决定下一条的颜色(本仓 8 月刚为此付过费)。
    """
    _skip_unless_throwaway_pg()
    assert SCHEMA_SQL.exists(), f"缺生产 schema 快照:{SCHEMA_SQL}"
    assert MIGRATION_035.exists(), f"缺 035 迁移:{MIGRATION_035}"
    assert ROLLBACK_SQL.exists(), f"缺回滚工件:{ROLLBACK_SQL}"
    import psycopg2
    from psycopg2 import sql

    admin = psycopg2.connect(PG_URL)
    admin.autocommit = True
    name = f"roll035_tmpl_test_{uuid.uuid4().hex[:8]}"
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))

    conn = psycopg2.connect(_url_for(name))
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for ext in ("vector", "pg_trgm"):
                cur.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")
            cur.execute(_snapshot_body())
            # 🔴 pg_dump 头部 set_config('search_path','',false) 对**本连接**生效且不还原,
            #    不复位的话后面全是 relation does not exist(本仓踩过)。
            cur.execute("SET search_path TO public")
    finally:
        conn.close()

    yield name

    with admin.cursor() as cur:
        cur.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
    admin.close()


@pytest.fixture()
def db(template_db):
    """每条用例一把新库(从模板复制 · 秒级)。"""
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extras import RealDictCursor

    admin = psycopg2.connect(PG_URL)
    admin.autocommit = True
    name = f"roll035_case_test_{uuid.uuid4().hex[:8]}"
    with admin.cursor() as cur:
        cur.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                sql.Identifier(name), sql.Identifier(template_db)))

    conn = psycopg2.connect(_url_for(name), cursor_factory=RealDictCursor)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO public")
        yield conn
    finally:
        conn.close()
        with admin.cursor() as cur:
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


def _run_sql_file(conn, path: Path) -> None:
    with conn.cursor() as cur:
        cur.execute("SET search_path TO public")
        cur.execute(path.read_text(encoding="utf-8"))


def _constraint_def(conn) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_get_constraintdef(oid, true) AS d FROM pg_constraint "
            "WHERE conname = %s AND connamespace = 'public'::regnamespace", (CONSTRAINT,))
        row = cur.fetchone()
        return (row or {}).get("d") or ""


def _old_contract():
    """从 ``git show c2ad8825:`` 取那一版契约,**不手抄**。

    手抄一份就是第二套真值:抄错了两边一起错、一起绿。
    """
    src = subprocess.check_output(
        ["git", "show", f"{OLD_SHA}:services/article_closed_loop_schema_contract.py"],
        cwd=str(ROOT), text=True, encoding="utf-8")
    spec = importlib.util.spec_from_loader("old_closed_loop_contract_c2ad8825", loader=None)
    module = importlib.util.module_from_spec(spec)
    exec(compile(src, f"<{OLD_SHA}:services/article_closed_loop_schema_contract.py>", "exec"),
         module.__dict__)
    return module


def _guard_blockers(conn) -> list[str]:
    contract = _old_contract()
    with conn.cursor() as cur:
        cur.execute("SET search_path TO public")
        return contract.schema_blockers(cur)


def _kind_blockers(blockers: list[str]) -> list[str]:
    return [b for b in blockers if CONSTRAINT in b]


# ══════════════════════════════════════════════════════════════════════════
# 自证:判据本身没塌
# ══════════════════════════════════════════════════════════════════════════

def test_the_old_contract_module_is_really_the_old_one():
    """旧契约必须真是**旧的**那份:钉的是 9 值,而当前工作树那份钉的是 14 值。"""
    old = _old_contract()
    pinned = old.CHECK_DEFINITIONS[CONSTRAINT]
    for kind in NEW_KINDS:
        assert kind not in pinned, f"取到的不是旧契约:它里面有 {kind}"
    assert "publication_locked" in pinned

    from services.article_closed_loop_schema_contract import CHECK_DEFINITIONS as CUR
    for kind in NEW_KINDS:
        assert kind in CUR[CONSTRAINT], "当前工作树那份反倒没有新枚举 —— 底选错了"


def test_the_guard_passes_on_the_untouched_prod_snapshot(db):
    """守卫在**没动过**的生产快照上必须绿。

    它要是本来就永远红,判据 1 的「转 OK」就无从谈起(而且会以一种很像成功的方式失败)。
    """
    blockers = _guard_blockers(db)
    assert blockers == [], blockers[:8]


def test_the_rollback_artifact_is_not_in_the_migration_manifest():
    """🔴 工单红线:回滚工件不进 prestart 迁移清单。

    prestart 每次部署**无条件重放**全部 manifest 迁移。收窄一旦进清单,
    就会和同一份清单里的 035(往回扩)在同一次 prestart 里互相拆台。
    """
    from db.migration_manifest import MIGRATIONS

    name = ROLLBACK_SQL.name
    assert not any(name in entry for entry in MIGRATIONS), (
        f"{name} 进了迁移清单 —— 它是人工回滚工件,不是迁移")
    # 正向对照:035 本身**必须**在清单里(否则上一行的绿可能只是清单空了)
    assert any("migration_035_geo_image_note_slot_channel" in e for e in MIGRATIONS), MIGRATIONS[:3]
    # 位置也对:工件放在 scripts/ 下,不在 db/ 迁移目录
    assert ROLLBACK_SQL.parent.name == "scripts"


# ══════════════════════════════════════════════════════════════════════════
# 判据 1 / 2:回滚 SQL 让旧守卫转 OK · 不跑就仍红
# ══════════════════════════════════════════════════════════════════════════

def test_without_rollback_the_old_guard_still_fails(db):
    """判据 2(反向对照):只跑 035、不跑回滚 ⇒ 旧守卫必须仍然红。

    没有这一条,判据 1 的绿可能只是「守卫压根不看这个 CHECK」。
    """
    _run_sql_file(db, MIGRATION_035)
    assert "slot_claimed" in _constraint_def(db), "035 没生效,后面比的就不是同一件事"

    blockers = _guard_blockers(db)
    assert _kind_blockers(blockers), ("035 之后旧守卫居然是绿的", blockers[:8])
    assert any(b.startswith("wrong_constraint_definition") for b in _kind_blockers(blockers)), \
        _kind_blockers(blockers)

    # 顺带钉死:035 的另外几项是纯 additive,旧守卫对它们**不报** ——
    # 回滚工件只需要动这一个 CHECK,不必(也不该)把三列删掉。
    assert _kind_blockers(blockers) == blockers, ("035 还带来了别的 blocker", blockers[:8])


def test_rollback_makes_the_old_guard_pass(db):
    """判据 1:035 → 回滚 SQL → 旧契约守卫转 OK。"""
    _run_sql_file(db, MIGRATION_035)
    assert _kind_blockers(_guard_blockers(db)), "前置不成立:035 之后守卫本该是红的"

    _run_sql_file(db, ROLLBACK_SQL)

    definition = _constraint_def(db)
    for kind in NEW_KINDS:
        assert kind not in definition, (kind, definition)
    assert _guard_blockers(db) == [], _guard_blockers(db)[:8]

    # 渲染口径也钉一下:旧契约是**逐字**比定义的,收窄了但渲染不同样等于没收窄。
    old = _old_contract()
    assert old._check_definition_matches(old.CHECK_DEFINITIONS[CONSTRAINT], definition), definition


# ══════════════════════════════════════════════════════════════════════════
# 判据 3:注毒 —— 有真行在用就必须拒收窄
# ══════════════════════════════════════════════════════════════════════════

def _seed_event(conn, *, kind: str) -> int:
    """插一条用指定枚举的**真事件行**(走真外键、填齐真 NOT NULL)。

    不 mock、不放宽:`geo_article_delivery_slot_events` 的 FK 指向
    `geo_article_contract_revisions` 与 `geo_article_plan_runs`(生产 schema 实证,
    它**不**外键到 slots),所以这里按那两张表的真列建父行。
    """
    with conn.cursor() as cur:
        cur.execute("SET search_path TO public")
        # 父行按真外键补齐(brands → quotes → contract_revisions → plan_runs → events)。
        # 不用 `session_replication_role=replica` 绕开外键:绕开之后这条判据就不再
        # 证明「真能插进去」,而毒数据插不进去的话,闸拦不拦得住根本无从谈起。
        cur.execute(
            "INSERT INTO brands (id, name) VALUES (9702, 'roll035 判据品牌') "
            "ON CONFLICT (id) DO NOTHING")
        cur.execute("INSERT INTO quotes (id) VALUES (9703) ON CONFLICT (id) DO NOTHING")
        cur.execute(
            """
            INSERT INTO geo_article_contract_revisions
                (revision_key, source_event_key, source_version, owner_user_id, brand_id,
                 quote_id, authority_snapshot, authority_snapshot_hash, delivery_count,
                 contract_version)
            VALUES (%s, %s, 'roll035-test', 9701, 9702, 9703, '{}'::jsonb, %s, 1, 'v1')
            RETURNING id
            """,
            (uuid.uuid4().hex * 2, uuid.uuid4().hex * 2, "h" * 64),
        )
        revision_id = cur.fetchone()["id"]
        cur.execute(
            """
            INSERT INTO geo_article_plan_runs
                (run_key, contract_revision_id, owner_user_id, brand_id, quote_id,
                 run_mode, compiler_version, input_snapshot, input_snapshot_hash)
            VALUES (%s, %s, 9701, 9702, 9703, 'shadow', 'v1', '{}'::jsonb, %s)
            RETURNING id
            """,
            (uuid.uuid4().hex * 2, revision_id, "i" * 64),
        )
        run_id = cur.fetchone()["id"]
        cur.execute(
            """
            INSERT INTO geo_article_delivery_slot_events
                (event_key, delivery_slot_key, slot_version, event_kind, target_state,
                 contract_revision_id, plan_run_id, owner_user_id, brand_id, quote_id,
                 source_version, occurred_at)
            VALUES (%s, %s::uuid, 1, %s, 'active', %s, %s, 9701, 9702, 9703,
                    'roll035-test', now())
            RETURNING id
            """,
            (uuid.uuid4().hex + uuid.uuid4().hex, str(uuid.uuid4()), kind, revision_id, run_id),
        )
        return cur.fetchone()["id"]


def test_rows_using_the_new_kinds_block_the_narrowing(db):
    """判据 3:先插一行 `generation_started`,再跑回滚 SQL ⇒ 必须被 DO 块拦下。

    报错要**指名**:多少行、哪几种、样例 id。只说「拒绝」的话,
    收到的人还得自己去写 SQL 找是谁 —— 而那正是回滚当天最没时间做的事。
    """
    import psycopg2

    _run_sql_file(db, MIGRATION_035)
    _seed_event(db, kind="generation_started")
    before = _constraint_def(db)

    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _run_sql_file(db, ROLLBACK_SQL)

    message = str(exc.value)
    assert "拒绝收窄" in message, message
    assert "generation_started=1" in message, message
    assert "id=" in message and "slot=" in message, message

    # 🔴 拒了就必须**什么都没改** —— 半路收窄比不收窄坏。
    assert _constraint_def(db) == before, (before, _constraint_def(db))
    assert "generation_started" in _constraint_def(db)


def test_the_gate_only_fires_on_the_new_kinds(db):
    """反向对照:旧 9 值的行(哪怕很多)不该拦住收窄。

    闸要是对任何数据都拒,判据 3 的红就跟「新枚举」没关系了。
    """
    _run_sql_file(db, MIGRATION_035)
    _seed_event(db, kind="created")
    _seed_event(db, kind="publication_locked")

    _run_sql_file(db, ROLLBACK_SQL)          # 不该抛
    assert "slot_claimed" not in _constraint_def(db)
    assert _guard_blockers(db) == []


# ══════════════════════════════════════════════════════════════════════════
# 判据 4:幂等
# ══════════════════════════════════════════════════════════════════════════

def test_rollback_is_idempotent(db):
    """判据 4:跑两遍。第二遍必须无害,且 CHECK 定义**逐字**不变、仍只有一条。"""
    _run_sql_file(db, MIGRATION_035)
    _run_sql_file(db, ROLLBACK_SQL)
    first = _constraint_def(db)

    _run_sql_file(db, ROLLBACK_SQL)
    second = _constraint_def(db)

    assert first == second, (first, second)
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM pg_constraint WHERE conname = %s "
            "AND connamespace = 'public'::regnamespace", (CONSTRAINT,))
        assert cur.fetchone()["n"] == 1, "重放把约束建重了"
    assert _guard_blockers(db) == []


def test_rollback_on_a_database_that_never_had_035(db):
    """边界:直接在 035 之前的形状上跑回滚 SQL —— 必须是安静的 no-op。

    回滚当天没人有把握「035 到底跑没跑过」,这一路必须无害。
    """
    before = _constraint_def(db)
    assert "slot_claimed" not in before

    _run_sql_file(db, ROLLBACK_SQL)

    assert _constraint_def(db) == before
    assert _guard_blockers(db) == []
