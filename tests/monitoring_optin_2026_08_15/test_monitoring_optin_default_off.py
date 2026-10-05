"""手动监测词默认关 —— 真 PostgreSQL 行为锁(WO_MONITORING_OPTIN_DEFAULT_OFF_2026-08-15)。

病灶(生产实证 2026-08-15):
  `extra_keywords` 15 列**没有 is_monitored**,唯一闸是 `status`,而库默认就是 'active';
  `get_client_keywords` 的 extra 分支又把 `is_monitored` 写死成 `FALSE as is_monitored`。
  净效果:加一个自定义词 = 立刻进该品牌每一次监测批次、按 monitor_single 130 算力/词/次
  开始花钱,而界面上永远画不出开关、事后关不掉(只能整条归档)。违元指令 #2「按钮级确认扣费」。

判据一律打在**真库真 SQL** 上,schema 复用生产快照
`tests/orphanmon_2026_08_10/prod_schema_snapshot.sql`(pg_dump --schema-only)。
🔴 复用而非新建快照的依据(2026-08-15 亲自比对,不是想当然):
   该快照里 extra_keywords 15 列 / confirmed_keywords 31 列 / keyword_monitor_subscriptions 17 列,
   与当天生产 information_schema 实测**逐列逐序相同** → 对本包涉及的表结构未漂。
🔴 不手搓建表:本仓踩过"测试 schema 类型不同构照样全绿,在生产快照副本上才炸"。
🔴 本文件的 fixture **在快照之上真跑本包的迁移 SQL** —— 于是"迁移能不能在生产形状的库上跑通"
   本身也被锁住了,而不是测一个我手写的理想 schema。

锁表
  A1-A2  取词闸 · quote 级"跑全部":关着取不到 / 开着取得到(正反两态**给不同值**)
  A3     取词闸 · brand 级"跑全部"同样有闸(只修一条分支 = 漏 —— 本单专门防这个)
  A4     刻意例外 1:显式 keyword_keys 点名 → 关着也放行(Owner 2026-08-15 拍板 · 按钮级确认)
  A5     刻意例外 2:for_dispatch=False(报表)→ 关着也返回;且**默认值必须是 True**(fail-safe)
  B1     显示真值:get_client_keywords 的 extra 行跟随真列(开/关**给不同值**)
  B2     不破 2026-08-10 恒等式:confirmed 行仍派生自订阅
  C1     默认关:列存在 · NOT NULL · default false
  C2     默认关是**行为**不是常量:真调 add_keyword() 插一行 → 该行 is_monitored 为 FALSE
  D1-D2  开关写入 + 审计列;归档词不能被开启
  E1-E3  P0-A.6 回填:dry-run 不写 / apply 后正好 6 条 / 幂等重跑 diff=0
  F      P0-B 孤儿收口在 test_orphan_backfill_discriminating_power.py(含判别力反向对照)
"""

from __future__ import annotations

import os
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
SCHEMA_SQL = ROOT / "tests" / "orphanmon_2026_08_10" / "prod_schema_snapshot.sql"
MIGRATION_SQL = ROOT / "scripts" / "migration_monitoring_extra_optin_2026_08_15.sql"
# [parity 2026-08-16] 本包的 036 也要灌:parity 之后 get_client_keywords / 
#   list_active_subscriptions 都按 kms.keyword_source 取数,夹具缺这一列 
#   会以 UndefinedColumn 报红 —— 那是**夹具没跟上**,不是判据发现了缺陷。
#   (同族教训:包新增迁移必须同步进所有自建 schema 的夹具,否则干净库上全 ERROR。)
MIGRATION_SQL_PARITY = ROOT / "scripts" / "migration_kms_keyword_source_2026_08_16.sql"

OWNER_ID = 8811
BRAND_ID = 7711
QUOTE_ID = 6611
# 回填脚本只认这两个 brand(真客户),夹具照抄它们的 id,让 E 组锁真的走同一条判据
REAL_BRAND_A = 289
REAL_BRAND_B = 662


def _skip_unless_throwaway_pg():
    if not PG_URL:
        pytest.skip("需要 TEST_DATABASE_URL(一次性 loopback 测试库)")
    parsed = urlsplit(PG_URL)
    if (parsed.hostname or "").lower() not in LOOPBACK_HOSTS:
        pytest.skip(f"只允许 loopback 一次性容器 DSN:{parsed.hostname}")
    base_db = (parsed.path or "").lstrip("/").lower()
    if "prod" in base_db or "test" not in base_db:
        pytest.skip(f"基础库名必须含 test 且不含 prod:{base_db}")


@pytest.fixture(scope="module")
def db():
    """一次性 throwaway 库 + 生产同构 schema + **本包迁移**。"""
    _skip_unless_throwaway_pg()
    assert SCHEMA_SQL.exists(), f"缺 schema 快照:{SCHEMA_SQL}"
    assert MIGRATION_SQL.exists(), f"缺本包迁移:{MIGRATION_SQL}"
    assert MIGRATION_SQL_PARITY.exists(), f"缺 parity 迁移:{MIGRATION_SQL_PARITY}"
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extras import RealDictCursor

    admin = psycopg2.connect(PG_URL)
    admin.autocommit = True
    name = f"monoptin_test_{uuid.uuid4().hex[:10]}"
    assert "test" in name and "prod" not in name
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    parsed = urlsplit(PG_URL)
    url = urlunsplit((parsed.scheme, parsed.netloc, f"/{name}", parsed.query, parsed.fragment))

    raw = SCHEMA_SQL.read_text(encoding="utf-8")
    body = "\n".join(
        l for l in raw.splitlines()
        if not l.startswith("\\") and not l.startswith("CREATE SCHEMA public;")
    )
    conn = psycopg2.connect(url, cursor_factory=RealDictCursor)
    conn.autocommit = True
    with conn.cursor() as cur:
        for _ext in ("vector", "pg_trgm"):
            cur.execute(f"CREATE EXTENSION IF NOT EXISTS {_ext}")
        cur.execute(body)
        # 🔴 pg_dump 写的 set_config('search_path','',false) 对**本连接**生效,不还原后面全 "relation does not exist"
        cur.execute("SET search_path TO public")
        # 真跑本包迁移(不是我手写一句 ALTER —— 那样锁的是我的想象不是要上线的那份文件)
        cur.execute(MIGRATION_SQL.read_text(encoding="utf-8"))
        cur.execute(MIGRATION_SQL_PARITY.read_text(encoding="utf-8"))
        # [platform-covered 2026-08-16] 本包 037(billing_mode)同理必须灌 —— 同族第四次
        cur.execute((ROOT / "scripts" / "migration_kms_billing_mode_2026_08_16.sql").read_text(encoding="utf-8"))
    try:
        yield type("DB", (), {
            "url": url,
            "connect": staticmethod(lambda: psycopg2.connect(url, cursor_factory=RealDictCursor)),
            "conn": conn,
        })
    finally:
        conn.close()
        with admin.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
        admin.close()


def _patch_conn(monkeypatch, db):
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "get_connection", db.connect)
    import db.monitoring_db as mdb
    if hasattr(mdb, "get_connection"):
        monkeypatch.setattr(mdb, "get_connection", db.connect)


def _seed(db, *, brand_id=BRAND_ID, quote_id=QUOTE_ID, extra_words=("手动词甲",)):
    """一个付费且在服务期内的 quote(否则服务锚会把词全过滤掉,测出来的空是假空)。"""
    with db.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO monitoring_product_platform_matrices (version, platforms) VALUES"
            " ('monitoring-classic4-v1','dashscope,deepseek,kimi,doubao'),"
            " ('monitoring-unified5-v1','dashscope,deepseek,doubao,kimi,yuanbao')"
            " ON CONFLICT (version) DO NOTHING")
        cur.execute("DELETE FROM keyword_monitor_subscriptions")
        cur.execute("DELETE FROM extra_keywords")
        cur.execute("DELETE FROM confirmed_keywords")
        cur.execute("DELETE FROM quotes")
        cur.execute("DELETE FROM brands")
        cur.execute("DELETE FROM users")
        cur.execute(
            "INSERT INTO users (id, username, password_hash, display_name)"
            " VALUES (%s,'owner','x','服务商')", (OWNER_ID,))
        cur.execute(
            "INSERT INTO brands (id, name, owner_user_id, is_deleted) VALUES (%s,'测试品牌',%s,FALSE)",
            (brand_id, OWNER_ID))
        cur.execute(
            "INSERT INTO quotes (id, brand_id, brand_name, status, service_status,"
            " service_start_date, service_days, paid_at)"
            " VALUES (%s,%s,'测试品牌','paid','active',CURRENT_DATE - 10, 180, NOW())",
            (quote_id, brand_id))
        ids = []
        for i, kw in enumerate(extra_words, start=1):
            cur.execute(
                "INSERT INTO extra_keywords (id, quote_id, brand_id, client_id, keyword,"
                " target_brand, status) VALUES (%s,%s,%s,%s,%s,'测试品牌','active') RETURNING id",
                (4400 + i, quote_id, brand_id, str(quote_id), kw))
            ids.append(cur.fetchone()["id"])
    return ids


def _set_on(db, keyword_id, on: bool):
    with db.conn.cursor() as cur:
        cur.execute("UPDATE extra_keywords SET is_monitored=%s WHERE id=%s", (on, keyword_id))


# ─────────────────────────── C 组:默认关 ───────────────────────────

def test_C1_column_exists_notnull_default_false(db):
    """列在 · NOT NULL · default false。默认值是本单的核心要求,不许是 TRUE。"""
    with db.conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type, is_nullable, column_default"
            "  FROM information_schema.columns"
            " WHERE table_schema='public' AND table_name='extra_keywords'"
            "   AND column_name IN ('is_monitored','monitoring_enabled_at','monitoring_enabled_by')"
            " ORDER BY column_name")
        rows = {r["column_name"]: r for r in cur.fetchall()}
    assert set(rows) == {"is_monitored", "monitoring_enabled_at", "monitoring_enabled_by"}
    assert rows["is_monitored"]["data_type"] == "boolean"
    assert rows["is_monitored"]["is_nullable"] == "NO"
    assert "false" in (rows["is_monitored"]["column_default"] or "").lower()
    # 审计列必须可空:存量行没有开启动作,不该编造时间/人
    assert rows["monitoring_enabled_at"]["is_nullable"] == "YES"


def test_C2_add_keyword_lands_unmonitored(db, monkeypatch):
    """默认关是**行为**不是常量:真走 add_keyword() 插一行,读回来必须是关。"""
    _patch_conn(monkeypatch, db)
    _seed(db, extra_words=())
    from db.monitoring_db import add_keyword
    kid = add_keyword(brand_id=BRAND_ID, client_id=str(QUOTE_ID), keyword="新加的词",
                      target_brand="测试品牌", difficulty="中等", target_rate=60)
    with db.conn.cursor() as cur:
        cur.execute("SELECT is_monitored, status FROM extra_keywords WHERE id=%s", (kid,))
        row = cur.fetchone()
    assert row["status"] == "active", "词本身照常入库(不是不让加词,是加了不自动跑)"
    assert row["is_monitored"] is False, "加词就自动开启监测 = 本单要消灭的形态"


# ─────────────────────── A 组:取词闸(P0-A.2) ───────────────────────

def test_A1_A2_quote_scope_gate_both_states(db, monkeypatch):
    """quote 级"跑全部":关着取不到、开着取得到 —— 正反两态**给不同值**才算判据。"""
    _patch_conn(monkeypatch, db)
    (kid,) = _seed(db, extra_words=("手动词甲",))
    from db.monitoring_db import get_keywords_for_monitoring

    off = get_keywords_for_monitoring(quote_id=QUOTE_ID)
    assert [k["id"] for k in off if k["source"] == "extra"] == [], "默认关时不该进监测取词结果"

    _set_on(db, kid, True)
    on = get_keywords_for_monitoring(quote_id=QUOTE_ID)
    assert [k["id"] for k in on if k["source"] == "extra"] == [kid], "开启后必须进"


def test_A3_brand_scope_gate(db, monkeypatch):
    """brand 级"跑全部"(无 quote 的自助订阅场景)同样有闸。

    专门单列一条:本单的教训就是"只修一处 = 漏"。这条分支和 A1 是两段独立 SQL,
    只测 A1 的话,把闸只加在其中一段照样全绿。
    """
    _patch_conn(monkeypatch, db)
    _seed(db, extra_words=("brand 级手动词",))
    with db.conn.cursor() as cur:
        # brand 级 fallback 的前提是**完全没有 quote**;把词的 quote 关联摘掉并删 quote
        cur.execute("UPDATE extra_keywords SET quote_id=NULL WHERE brand_id=%s", (BRAND_ID,))
        cur.execute("DELETE FROM quotes WHERE id=%s", (QUOTE_ID,))
        cur.execute("SELECT id FROM extra_keywords WHERE brand_id=%s", (BRAND_ID,))
        kid = cur.fetchone()["id"]
    from db.monitoring_db import get_keywords_for_monitoring

    assert get_keywords_for_monitoring(brand_id=BRAND_ID) == [], "brand 级分支默认关时不该取到"
    _set_on(db, kid, True)
    got = get_keywords_for_monitoring(brand_id=BRAND_ID)
    assert [k["id"] for k in got] == [kid], "brand 级分支开启后必须取到"


def test_A4_explicit_keyword_keys_bypass_is_intentional(db, monkeypatch):
    """刻意例外 1:显式点名该词 = 代理在界面勾选并确认扣费,按元指令 #2 视为已确认。

    这条锁的存在是为了让"例外"变成**被写下来的决定**,而不是某天被人当 bug 顺手堵掉。
    """
    _patch_conn(monkeypatch, db)
    (kid,) = _seed(db, extra_words=("手动词甲",))
    from db.monitoring_db import get_keywords_for_monitoring

    got = get_keywords_for_monitoring(quote_id=QUOTE_ID, keyword_keys=[f"extra-{kid}"])
    assert [k["id"] for k in got] == [kid], "显式点名必须放行(Owner 2026-08-15 拍板)"

    # 反向对照:同一个关着的词,不点名就取不到 —— 证明放行来自"点名"而不是闸压根没生效
    assert [k["id"] for k in get_keywords_for_monitoring(quote_id=QUOTE_ID)] == []


def test_A5_for_dispatch_default_true_and_report_optout(db, monkeypatch):
    """刻意例外 2:报表路径显式退出闸;**默认值必须是 True**(新调用方自动被闸住)。"""
    import inspect
    from db.monitoring_db import get_keywords_for_monitoring
    sig = inspect.signature(get_keywords_for_monitoring)
    assert sig.parameters["for_dispatch"].default is True, \
        "默认必须是加闸;默认 False = 新写的调用方默认绕过闸"

    _patch_conn(monkeypatch, db)
    (kid,) = _seed(db, extra_words=("手动词甲",))
    off_gated = get_keywords_for_monitoring(quote_id=QUOTE_ID)
    off_report = get_keywords_for_monitoring(quote_id=QUOTE_ID, for_dispatch=False)
    assert [k["id"] for k in off_gated if k["source"] == "extra"] == []
    assert [k["id"] for k in off_report if k["source"] == "extra"] == [kid], \
        "关掉的词仍要出现在报表/导出里 —— 抹掉它 = 抹掉客户已付费跑出来的历史"


# ─────────────────────── B 组:显示真值(P0-A.3) ───────────────────────

def test_B1_display_follows_real_column(db, monkeypatch):
    """get_client_keywords 的 extra 行必须读真值(旧代码写死 FALSE → 前端永远画不出开关)。

    🔴 [parity 2026-08-16 P0-3] 「真值」的定义在本包变了,这条锁跟着改 ——
      optin 时期 extra 没有逐词订阅,真值 = `extra_keywords.is_monitored` 那一列;
      parity 之后手动词与合同词同权、有了逐词日订阅,真值 =
      **列开着 且 有在跑的订阅**(与 confirmed 侧同口径,也与取数的 extra 臂同口径)。
      只看列会重新造出「界面说开着、cron 不跑」——正是 2026-08-10 那条恒等式要消灭的形态。
      所以下面把两半分开验:只置列 → 仍显示关;再补上订阅 → 才显示开。
    """
    _patch_conn(monkeypatch, db)
    (kid,) = _seed(db, extra_words=("手动词甲",))
    from db.monitoring_db import get_client_keywords

    def _extra_rows():
        return {r["id"]: r for r in get_client_keywords(QUOTE_ID) if r["source"] == "extra"}

    assert _extra_rows()[kid]["is_monitored"] is False

    # 只置列、不建订阅 —— 这正是 P0-9 在清的那 146 条「假开关」的形态
    _set_on(db, kid, True)
    assert _extra_rows()[kid]["is_monitored"] is False, \
        "列开着但没有在跑的订阅却显示『开』= 假开关(界面说开着、cron 不跑)"

    # 补上订阅 —— 两半齐了才算真的开着
    with db.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO keyword_monitor_subscriptions (user_id, keyword_id, quote_id, brand_id,"
            " status, daily_points, feature_code, keyword_source)"
            " VALUES (%s,%s,%s,%s,'active',130,'monitoring_keyword_daily','extra')",
            (OWNER_ID, kid, QUOTE_ID, BRAND_ID),
        )
    assert _extra_rows()[kid]["is_monitored"] is True, \
        "开着却显示关 = 2026-08-10 立的「看着开 = 真的跑」恒等式被破"


def test_B2_confirmed_side_still_derives_from_subscription(db, monkeypatch):
    """不破 2026-08-10:confirmed 行的开关仍派生自订阅,不是读 ck.is_monitored 那一列。"""
    _patch_conn(monkeypatch, db)
    _seed(db, extra_words=())
    with db.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO confirmed_keywords (id, quote_id, brand_id, keyword, is_core,"
            " is_monitored, monitoring_status) VALUES (9901,%s,%s,'合同词',TRUE,TRUE,'active')",
            (QUOTE_ID, BRAND_ID))
    from db.monitoring_db import get_client_keywords
    row = [r for r in get_client_keywords(QUOTE_ID) if r["source"] == "confirmed"][0]
    assert row["is_monitored"] is False, \
        "ck.is_monitored=TRUE 但无订阅 = 孤儿态,界面必须显示关(否则又在说谎)"

    with db.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO keyword_monitor_subscriptions (user_id, keyword_id, quote_id, brand_id,"
            " status, daily_points, feature_code) VALUES (%s,9901,%s,%s,'active',130,"
            "'monitoring_keyword_daily')", (OWNER_ID, QUOTE_ID, BRAND_ID))
    row = [r for r in get_client_keywords(QUOTE_ID) if r["source"] == "confirmed"][0]
    assert row["is_monitored"] is True


# ─────────────────────── D 组:开关写入 + 审计 ───────────────────────

def test_D1_toggle_writes_value_and_audit(db, monkeypatch):
    _patch_conn(monkeypatch, db)
    (kid,) = _seed(db, extra_words=("手动词甲",))
    from db.monitoring_db import set_extra_keyword_monitored

    row = set_extra_keyword_monitored(keyword_id=kid, is_monitored=True, operator_user_id=OWNER_ID)
    assert row and row["is_monitored"] is True
    with db.conn.cursor() as cur:
        cur.execute("SELECT monitoring_enabled_at, monitoring_enabled_by FROM extra_keywords WHERE id=%s", (kid,))
        a = cur.fetchone()
    assert a["monitoring_enabled_at"] is not None, "开启动作必须可审计(工单红线 4)"
    assert a["monitoring_enabled_by"] == OWNER_ID

    set_extra_keyword_monitored(keyword_id=kid, is_monitored=False, operator_user_id=OWNER_ID)
    with db.conn.cursor() as cur:
        cur.execute("SELECT is_monitored, monitoring_enabled_at FROM extra_keywords WHERE id=%s", (kid,))
        b = cur.fetchone()
    assert b["is_monitored"] is False
    assert b["monitoring_enabled_at"] is not None, "关闭保留历史痕迹(与 confirmed 侧同规矩)"

    # 开关**不得**给 extra 建订阅:extra 没有订阅模型,建了就是又一种"看着开永远不跑"的孤儿
    with db.conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM keyword_monitor_subscriptions WHERE keyword_id=%s", (kid,))
        assert cur.fetchone()["n"] == 0


def test_D2_archived_keyword_cannot_be_enabled(db, monkeypatch):
    _patch_conn(monkeypatch, db)
    (kid,) = _seed(db, extra_words=("手动词甲",))
    with db.conn.cursor() as cur:
        cur.execute("UPDATE extra_keywords SET status='archived' WHERE id=%s", (kid,))
    from db.monitoring_db import set_extra_keyword_monitored
    assert set_extra_keyword_monitored(keyword_id=kid, is_monitored=True) is None, \
        "归档词不能直接开监测(先恢复 —— 与 confirmed 侧 restore 不自动复开同规矩)"


# ─────────────────────── E 组:存量回填(P0-A.6) ───────────────────────

def _seed_real_customers(db):
    """照抄生产形状:289 五个 active + 一个 archived,662 一个 active。"""
    with db.conn.cursor() as cur:
        cur.execute("DELETE FROM extra_keywords")
        cur.execute("DELETE FROM quotes")
        cur.execute("DELETE FROM brands")
        cur.execute("DELETE FROM users")
        cur.execute("INSERT INTO users (id, username, password_hash, display_name) VALUES (%s,'o','x','服务商')", (OWNER_ID,))
        for b in (REAL_BRAND_A, REAL_BRAND_B, 387):
            cur.execute("INSERT INTO brands (id,name,owner_user_id,is_deleted) VALUES (%s,%s,%s,FALSE)",
                        (b, f"品牌{b}", OWNER_ID))
        n = 0
        for b, active_cnt, archived_cnt in ((REAL_BRAND_A, 5, 1), (REAL_BRAND_B, 1, 0), (387, 2, 0)):
            for i in range(active_cnt):
                n += 1
                cur.execute("INSERT INTO extra_keywords (id,brand_id,client_id,keyword,target_brand,status)"
                            " VALUES (%s,%s,'x',%s,'t','active')", (5000 + n, b, f"kw{b}_{i}"))
            for i in range(archived_cnt):
                n += 1
                cur.execute("INSERT INTO extra_keywords (id,brand_id,client_id,keyword,target_brand,status)"
                            " VALUES (%s,%s,'x',%s,'t','archived')", (5000 + n, b, f"arch{b}_{i}"))

# 🪦 [WO_MONITORING_PLATFORM_COVERED v1.2 · P0-0 · 2026-08-16] 以下用例随被测脚本一起删除。
#
# 被测对象 `scripts/backfill_monitoring_extra_optin_2026_08_15.py` 已删:
#   它的守卫校验「289/662 的 active 手动词**恰好 6 条**」—— 校验的是**目标集合**不是
#   **目标状态**。Owner 2026-08-16 已裁定把这 6 条关列对齐(Deploy 已执行,含审计痕迹)。
#   一旦有人再跑 `--apply`,这 6 条会被重新打开、次日起按 130/词/天 从服务商钱包扣费,
#   而守卫**不会拦**(集合没变,变的是状态)。
#
# 🔴 为什么删而不是留一个 skip:留着 skip 的用例会让后来者以为"这个能力还在,只是暂时没测",
#   于是去 git 历史里把脚本捞回来 —— 那正是本次要防的动作。
#   它们守的那件事现在由 **tests/platform_covered_2026_08_16/test_p0_0_dead_script_removed.py**
#   接管(文件系统不许有 + git 不许跟踪 + 全仓无可执行引用 + 判别力自证 + 复活即红变异)。
