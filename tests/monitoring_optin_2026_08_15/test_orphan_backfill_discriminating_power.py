"""146 孤儿监测词收口 + **判据判别力**证明(WO_MONITORING_OPTIN_DEFAULT_OFF_2026-08-15 · P0-B)。

工单 §四.1 第 4 条原文:
  「孤儿查询回填后为 0,**且拆掉回填后必须非 0**(证明这条判据有判别力)」

为什么必须专门证:
  "回填后孤儿数 = 0" 这句话,在一个本来就没有孤儿的库上**恒真**。
  本仓踩过"锁全绿是因为夹具是空的"、"归并用『或』并特征→恒真→全绿像无差异"。
  所以这里三条一起打:
    F1 夹具元判据 —— 造完孤儿后 orphan_before **非 0**(否则后面两条都没意义)
    F2 跑回填 → orphan_after == 0
    F3 **不跑回填**(同一夹具、同一查询)→ 孤儿数**仍非 0**
  F3 就是"拆掉回填"的那一臂:它红了,F2 才有意义。

  另外还锁二选一的**分流**本身(F4):有付费凭证的补订阅、无凭证的关掉且不建订阅 ——
  只看总数归零的话,"把 146 条全部关掉"也能让总数归零,却把该继续监测的真客户一起停了。
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

OWNER_ID = 8821
BRAND_ID = 7721
PAID_QUOTE = 6621      # status='paid' + paid_at 有值 → 有付费凭证 → 该补订阅
UNPAID_QUOTE = 6622    # status='draft' → 无付费凭证 → 该关掉且不建订阅


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
    _skip_unless_throwaway_pg()
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extras import RealDictCursor

    admin = psycopg2.connect(PG_URL)
    admin.autocommit = True
    name = f"monoptin_orphan_test_{uuid.uuid4().hex[:8]}"
    assert "test" in name and "prod" not in name
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    parsed = urlsplit(PG_URL)
    url = urlunsplit((parsed.scheme, parsed.netloc, f"/{name}", parsed.query, parsed.fragment))

    body = "\n".join(
        l for l in SCHEMA_SQL.read_text(encoding="utf-8").splitlines()
        if not l.startswith("\\") and not l.startswith("CREATE SCHEMA public;")
    )
    conn = psycopg2.connect(url, cursor_factory=RealDictCursor)
    conn.autocommit = True
    with conn.cursor() as cur:
        for _ext in ("vector", "pg_trgm"):
            cur.execute(f"CREATE EXTENSION IF NOT EXISTS {_ext}")
        cur.execute(body)
        cur.execute("SET search_path TO public")
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
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
        admin.close()


def _seed_orphans(db, paid_n=3, unpaid_n=4):
    """造孤儿:is_monitored=TRUE 且无 active 订阅。两种归属各造一批。"""
    with db.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO monitoring_product_platform_matrices (version, platforms) VALUES"
            " ('monitoring-unified5-v1','dashscope,deepseek,doubao,kimi,yuanbao')"
            " ON CONFLICT (version) DO NOTHING")
        cur.execute("DELETE FROM keyword_monitor_subscriptions")
        cur.execute("DELETE FROM confirmed_keywords")
        cur.execute("DELETE FROM quotes")
        cur.execute("DELETE FROM brands")
        cur.execute("DELETE FROM users")
        cur.execute("INSERT INTO users (id, username, password_hash, display_name) VALUES (%s,'o','x','服务商')", (OWNER_ID,))
        cur.execute("INSERT INTO brands (id,name,owner_user_id,is_deleted) VALUES (%s,'品牌',%s,FALSE)",
                    (BRAND_ID, OWNER_ID))
        cur.execute("INSERT INTO quotes (id,brand_id,brand_name,status,service_status,"
                    "service_start_date,service_days,paid_at,owner_user_id)"
                    " VALUES (%s,%s,'品牌','paid','active',CURRENT_DATE-10,180,NOW(),%s)",
                    (PAID_QUOTE, BRAND_ID, OWNER_ID))
        cur.execute("INSERT INTO quotes (id,brand_id,brand_name,status,service_status,service_days,owner_user_id)"
                    " VALUES (%s,%s,'品牌','draft','pending',180,%s)",
                    (UNPAID_QUOTE, BRAND_ID, OWNER_ID))
        n = 0
        for q, cnt in ((PAID_QUOTE, paid_n), (UNPAID_QUOTE, unpaid_n)):
            for i in range(cnt):
                n += 1
                cur.execute(
                    "INSERT INTO confirmed_keywords (id,quote_id,brand_id,keyword,is_core,"
                    "is_monitored,monitoring_status) VALUES (%s,%s,%s,%s,TRUE,TRUE,'active')",
                    (3300 + n, q, BRAND_ID, f"孤儿词{n}"))


def _orphans(db) -> int:
    with db.conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM confirmed_keywords ck WHERE ck.is_monitored = TRUE"
            " AND NOT EXISTS (SELECT 1 FROM keyword_monitor_subscriptions kms"
            "                  WHERE kms.keyword_id = ck.id AND kms.status='active')")
        return int(cur.fetchone()["n"])


def test_F1_fixture_really_contains_orphans(db):
    """夹具元判据:不先证 before 非 0,后面"回填后为 0"就可能只是因为库里本来就没有。"""
    _seed_orphans(db)
    assert _orphans(db) == 7, "夹具必须真的是『is_monitored=TRUE 且 0 条 active 订阅』"


def test_F3_without_backfill_orphans_stay_nonzero(db):
    """🔴 拆掉回填这一臂:同一夹具、同一查询,不跑回填 → 孤儿数**仍非 0**。

    这条红了,F2 的『== 0』才是被回填做到的,而不是查询恒为 0。
    """
    _seed_orphans(db)
    # 刻意不调用 backfill —— 这就是"拆掉"
    assert _orphans(db) > 0


def test_F5_production_shape_confirmed_paid_at_is_reported_not_silently_dropped(db, monkeypatch):
    """🔴 生产真形态:孤儿的 quote 是 `confirmed` + `paid_at` 有值(不是 draft)。

    2026-08-15 真数据实测:146/146 全是这个形态,**一个 draft 都没有** ——
    也就是说本文件原来那套 draft 夹具**测不到生产上真正会发生的那条分支**。
    (同族教训:夹具与被测实现同构盲区,互相验不出。)

    这一条锁住三件事:
      ① 这类行仍会被关掉(库不再说谎 · 行为零变化 —— 它们本来就没有订阅、cron 不跑)
      ② **但绝不会被静默抹掉**:必须出现在「付费未监测」清单里,交 Owner 定夺
      ③ **绝不给它们建订阅**(建了 = 凭空产生 130/词/天 的日扣费,没有任何人点过开关)
    """
    with db.conn.cursor() as cur:
        # 🔴 自足:不靠别的用例先跑过。confirmed_keywords.monitoring_product_version 有 FK
        #    指向这张矩阵表,少了它这条用例**单独跑必红** —— 顺序依赖的锁 = 不稳定的锁,
        #    2026-08-15 变异 runner 就是用"复位后仍红"把这个缺陷抓出来的。
        cur.execute(
            "INSERT INTO monitoring_product_platform_matrices (version, platforms) VALUES"
            " ('monitoring-unified5-v1','dashscope,deepseek,doubao,kimi,yuanbao')"
            " ON CONFLICT (version) DO NOTHING")
        cur.execute("DELETE FROM keyword_monitor_subscriptions")
        cur.execute("DELETE FROM confirmed_keywords")
        cur.execute("DELETE FROM quotes")
        cur.execute("DELETE FROM brands")
        cur.execute("DELETE FROM users")
        cur.execute("INSERT INTO users (id, username, password_hash, display_name)"
                    " VALUES (%s,'o','x','服务商')", (OWNER_ID,))
        cur.execute("INSERT INTO brands (id,name,owner_user_id,is_deleted,is_test)"
                    " VALUES (%s,'真客户品牌',%s,FALSE,FALSE)", (BRAND_ID, OWNER_ID))
        # 生产真形态:线下收款 → status='confirmed' 但 paid_at / service_start_date 有值
        cur.execute("INSERT INTO quotes (id,brand_id,brand_name,status,service_status,"
                    "service_start_date,service_days,paid_at,owner_user_id)"
                    " VALUES (%s,%s,'真客户品牌','confirmed','active',CURRENT_DATE-10,180,NOW(),%s)",
                    (PAID_QUOTE, BRAND_ID, OWNER_ID))
        for i in range(3):
            cur.execute("INSERT INTO confirmed_keywords (id,quote_id,brand_id,keyword,is_core,"
                        "is_monitored,monitoring_status) VALUES (%s,%s,%s,%s,TRUE,TRUE,'active')",
                        (3400 + i, PAID_QUOTE, BRAND_ID, f"付费未监测词{i}"))

    import db.connection as conn_mod
    import db.monitoring_db as mdb
    monkeypatch.setattr(conn_mod, "get_connection", db.connect)
    monkeypatch.setattr(mdb, "get_connection", db.connect)
    import scripts.kms_backfill_2026_05_29 as kms
    monkeypatch.setattr(kms, "_connect", db.connect)
    import scripts.backfill_orphan_monitor_2026_08_15 as ob
    monkeypatch.setattr(ob, "_connect", db.connect)

    assert _orphans(db) == 3
    report = ob.run(apply=True)

    # ① 关掉
    assert report["orphan_after"] == 0
    assert report["closed_no_active_subscription"] == 3
    # ② 出现在清单里(不是静默抹掉)
    assert report["paid_but_unmonitored_total"] == 3, \
        "有付款锚却没人监测的词必须进清单 —— 数据清洗不能把服务缺口一起洗掉"
    assert report["paid_but_unmonitored_by_brand"][0]["brand_id"] == BRAND_ID
    assert report["paid_but_unmonitored_csv"], "清单必须落盘,不能只活在 stdout"
    # ③ 一条订阅都不许建(建了就是凭空扣钱)
    with db.conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM keyword_monitor_subscriptions")
        assert cur.fetchone()["n"] == 0, "绝不给它们建订阅 —— 那是没人点过开关的 130/词/天"

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
