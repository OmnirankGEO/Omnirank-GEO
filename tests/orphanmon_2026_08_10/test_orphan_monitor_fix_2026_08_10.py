"""P0 孤儿监测词双侧根因修复 —— 真 PostgreSQL 行为锁(WO_ORPHAN_MONITOR_FIX_2026-08-10)。

病灶(取证已定):
  C 端确认报价 `_mark_keywords_monitored()` **只**置 `is_monitored=TRUE`,从不建订阅;
  而每日 cron 只认 `list_active_subscriptions()`(读 `keyword_monitor_subscriptions`),
  前端开关又读 `is_monitored` —— 于是当场产出"UI 开着、cron 永不跑"的孤儿词。

判据一律打在**真库真 SQL** 上,schema 来自生产快照 `prod_schema_snapshot.sql`
(pg_dump --schema-only)。🔴 不手搓建表:本仓踩过"测试 schema 类型不同构照样 40/40 全绿,
在生产快照副本上才炸"。

  锁1-3  ① 端到端:确认报价 → 订阅行真落 / billing_user = 品牌 owner / 调度器捞得到
  锁4    ① fail-closed:owner 不可确认 → 一个订阅不建,**且 is_monitored 也不落**(不产孤儿)
  锁5    ① 幂等:重复确认不产生第二行订阅
  锁6-7  ② 派生显示:孤儿态必须"关",有订阅必须"开"(正反两态**给不同值**)
  锁8    ② paused_low_balance 也算"开"(余额不足是暂停不是关闭)
  锁9-10 ③ 归档 → 订阅 cancelled;恢复 → 不自动复开
  锁11   夹具元判据:孤儿夹具真的是"is_monitored=TRUE 且订阅 0 行"
  锁12   计费主体口径一致性:共享模块与 server.py 那份的真值表逐格相同
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
SCHEMA_SQL = Path(__file__).with_name("prod_schema_snapshot.sql")

OWNER_ID = 8801            # 品牌 owner = 服务商 = 该收钱的人
OPERATOR_ID = 8802         # 不该被计费的人(C 端客户/别的操作者)
BRAND_ID = 7701
QUOTE_ID = 6601


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
    """一次性 throwaway 库 + 生产同构 schema。"""
    _skip_unless_throwaway_pg()
    assert SCHEMA_SQL.exists(), f"缺 schema 快照:{SCHEMA_SQL}"
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extras import RealDictCursor

    admin = psycopg2.connect(PG_URL)
    admin.autocommit = True
    name = f"orphanmon_test_{uuid.uuid4().hex[:10]}"
    assert "test" in name and "prod" not in name
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    parsed = urlsplit(PG_URL)
    url = urlunsplit((parsed.scheme, parsed.netloc, f"/{name}", parsed.query, parsed.fragment))

    raw = SCHEMA_SQL.read_text(encoding="utf-8")
    # pg_dump 16 会写 \restrict 这类 psql 元命令(psycopg2 执行不了),
    # 还会写 `CREATE SCHEMA public;` —— 而新建的库天生就有 public。两样都剔掉。
    body = "\n".join(
        l for l in raw.splitlines()
        if not l.startswith("\\") and not l.startswith("CREATE SCHEMA public;")
    )
    conn = psycopg2.connect(url, cursor_factory=RealDictCursor)
    conn.autocommit = True
    with conn.cursor() as cur:
        # 生产装了这些扩展(replica 实测 `SELECT extname FROM pg_extension` = pg_trgm/plpgsql/vector),
        # schema 里有 vector(1024) 列与 gin_trgm_ops 索引会用到。
        # 装扩展而不是把那些表/索引删掉 —— 删了就又是"测试库跟生产不同构"。
        for _ext in ("vector", "pg_trgm"):
            cur.execute(f"CREATE EXTENSION IF NOT EXISTS {_ext}")
        cur.execute(body)
        # 🔴 pg_dump 会在文件里写 `SELECT pg_catalog.set_config('search_path', '', false);`,
        #   它对**当前连接**生效 —— 不还原的话,后面所有不带 schema 前缀的 SQL 都会
        #   "relation does not exist"(而表其实建好了)。这坑第一次跑就撞上了。
        cur.execute("SET search_path TO public")
        # [WO_MONITORING_OPTIN_DEFAULT_OFF 2026-08-15] 本快照是 08-10 拍的,那时 extra_keywords
        #   还没有 is_monitored 列;而 get_client_keywords 的 extra 分支现在会引用它
        #   (手动词默认关 · 见 scripts/migration_monitoring_extra_optin_2026_08_15.sql)。
        #   不补这一步,本文件在干净库上会以 UndefinedColumn 全红 —— 那是夹具落后于 schema,
        #   不是 08-10 那批判据失效。生产由 prestart 按 manifest 跑同一份迁移。
        cur.execute(
            (Path(__file__).resolve().parents[2] / "scripts"
             / "migration_monitoring_extra_optin_2026_08_15.sql").read_text(encoding="utf-8")
        )
        # [WO_MANUAL_KEYWORD_PARITY 2026-08-16] 同一条理由,再来一次:parity 给
        #   keyword_monitor_subscriptions 加了 keyword_source(两表 id 各自独立,
        #   订阅必须带来源维度才认得出指向哪张表)。取消订阅 / 展示 / 每日取词
        #   现在都按这一列走,夹具缺它会以 UndefinedColumn 全红 ——
        #   **那是夹具落后于 schema,不是 08-10 那批判据失效**。
        #   🔴 这是本会话第三个自建 schema 的夹具因同一原因要补迁移。
        #      包新增迁移 = 必须同步进**所有**自建 schema 的夹具,不能靠"跑到红了再说"。
        cur.execute(
            (Path(__file__).resolve().parents[2] / "scripts"
             / "migration_kms_keyword_source_2026_08_16.sql").read_text(encoding="utf-8")
        )
        # [WO_MONITORING_PLATFORM_COVERED 2026-08-16 §2] 同一条理由,**第四次**:
        #   本包给 keyword_monitor_subscriptions 加了 billing_mode,建订阅的 INSERT 现在带这一列。
        #   夹具缺它 → INSERT 失败 → _subs() 返空 → 用例以"没建订阅"的形态红,
        #   看起来像"孤儿修复失效",实际是**夹具落后于 schema**。
        #   ⇒ 包新增迁移必须同步进**所有**自建 schema 的夹具,这条已经付过四次学费。
        cur.execute(
            (Path(__file__).resolve().parents[2] / "scripts"
             / "migration_kms_billing_mode_2026_08_16.sql").read_text(encoding="utf-8")
        )
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
    """把全站的 get_connection 指到测试库 —— 被测函数在各处按不同别名 import 它。"""
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "get_connection", db.connect)
    import api.selection_api as sel
    if hasattr(sel, "get_connection"):
        monkeypatch.setattr(sel, "get_connection", db.connect)
    import db.monitoring_db as mdb
    if hasattr(mdb, "get_connection"):
        monkeypatch.setattr(mdb, "get_connection", db.connect)


def _seed(db, *, owner=OWNER_ID, keywords=("孤儿词甲", "孤儿词乙")):
    with db.conn.cursor() as cur:
        # 生产 confirmed_keywords.monitoring_product_version 有外键指向这张矩阵表,
        # 且默认值就是 'monitoring-unified5-v1' —— 不先把矩阵行喂进来,插词就 FK 违反。
        # 值逐字取自 replica(两行:classic4 / unified5)。
        cur.execute(
            "INSERT INTO monitoring_product_platform_matrices (version, platforms) VALUES"
            " ('monitoring-classic4-v1','dashscope,deepseek,kimi,doubao'),"
            " ('monitoring-unified5-v1','dashscope,deepseek,doubao,kimi,yuanbao')"
            " ON CONFLICT (version) DO NOTHING")
        cur.execute("DELETE FROM keyword_monitor_subscriptions")
        cur.execute("DELETE FROM confirmed_keywords")
        cur.execute("DELETE FROM quotes")
        cur.execute("DELETE FROM brands")
        cur.execute("DELETE FROM users")
        # 生产 users 表 password_hash 是 NOT NULL —— 同构 schema 会当场咬住夹具,
        # 这正是不手搓建表的价值(手搓表这一列多半会被写成可空,测试全绿、生产才炸)。
        cur.execute(
            "INSERT INTO users (id, username, password_hash, display_name)"
            " VALUES (%s,'owner','x','服务商'),(%s,'operator','x','操作者')",
            (OWNER_ID, OPERATOR_ID))
        cur.execute(
            "INSERT INTO brands (id, name, owner_user_id, is_deleted) VALUES (%s,'测试品牌',%s,FALSE)",
            (BRAND_ID, owner) if owner is not None else (BRAND_ID, None))
        # `get_client_keywords` 的 brand_quotes CTE 走服务锚 SSOT
        # (quote_service_anchor_condition_sql):status='paid' + service_status 非
        # cancelled/inactive/expired。不满足这个,该报价的词一条都不会被返回 ——
        # 第一版夹具只插了 id/brand_id,于是"真跑 get_client_keywords"直接拿到空列表。
        cur.execute(
            "INSERT INTO quotes (id, brand_id, brand_name, status, service_status,"
            " service_start_date, service_days, paid_at)"
            " VALUES (%s,%s,'测试品牌','paid','active',CURRENT_DATE - 10, 180, NOW())",
            (QUOTE_ID, BRAND_ID))
        ids = []
        for i, kw in enumerate(keywords, start=1):
            cur.execute(
                "INSERT INTO confirmed_keywords (id, quote_id, brand_id, keyword, is_core,"
                " is_monitored, monitoring_status)"
                " VALUES (%s,%s,%s,%s,TRUE,FALSE,'active') RETURNING id",
                (5500 + i, QUOTE_ID, BRAND_ID, kw))
            ids.append(cur.fetchone()["id"])
    return ids


def _subs(db, keyword_id=None):
    with db.conn.cursor() as cur:
        if keyword_id is None:
            cur.execute("SELECT * FROM keyword_monitor_subscriptions ORDER BY id")
        else:
            cur.execute("SELECT * FROM keyword_monitor_subscriptions WHERE keyword_id=%s ORDER BY id",
                        (keyword_id,))
        return [dict(r) for r in cur.fetchall()]


def _confirm_quote(db, monkeypatch, kw_texts):
    """真跑 `_mark_keywords_monitored` —— C 端确认报价那条路径的落库动作。"""
    _patch_conn(monkeypatch, db)
    import api.selection_api as sel
    # 该函数用 resolve_selected_keyword_texts 把 id 解析成文本;这里直接喂快照,口径不变。
    monkeypatch.setattr(sel, "resolve_selected_keyword_texts",
                        lambda ids, **kw: list(kw_texts))
    sel._mark_keywords_monitored(QUOTE_ID, [1, 2], keywords_snapshot=None)


# ── 锁1-3 · ① 端到端 ────────────────────────────────────────────────────
def test_confirm_quote_really_creates_subscription(db, monkeypatch):
    ids = _seed(db)
    _confirm_quote(db, monkeypatch, ("孤儿词甲", "孤儿词乙"))
    rows = _subs(db)
    assert len(rows) == 2, f"确认报价没建订阅(这正是孤儿词的成因):{rows}"
    assert {r["keyword_id"] for r in rows} == set(ids)
    assert all(r["status"] == "active" for r in rows)
    assert all(r["quote_id"] == QUOTE_ID and r["brand_id"] == BRAND_ID for r in rows)


def test_billing_user_is_brand_owner(db, monkeypatch):
    """🔴 资金:订阅每天扣费,计费主体必须是**品牌 owner**(服务商),不是别人。"""
    _seed(db)
    _confirm_quote(db, monkeypatch, ("孤儿词甲",))
    rows = _subs(db)
    assert rows and all(r["user_id"] == OWNER_ID for r in rows), \
        f"计费主体错了(应为品牌 owner {OWNER_ID}):{[r['user_id'] for r in rows]}"


def test_scheduler_can_pick_it_up(db, monkeypatch):
    """端到端最后一跳:真实调度器的取数条件能捞到这个词 —— 否则前面全是自娱自乐。"""
    _seed(db)
    _confirm_quote(db, monkeypatch, ("孤儿词甲",))
    with db.conn.cursor() as cur:
        # 与 list_active_subscriptions 同形的核心条件(订阅 active ∧ 逐词开关开)
        cur.execute("""
            SELECT count(*) AS n
              FROM keyword_monitor_subscriptions s
              JOIN confirmed_keywords ck ON ck.id = s.keyword_id
             WHERE s.status = 'active'
               AND ck.is_monitored = TRUE
               AND COALESCE(ck.monitoring_status,'active') = 'active'
        """)
        assert cur.fetchone()["n"] == 1, "调度器条件捞不到 → 还是孤儿"


# ── 锁4 · ① fail-closed 不产孤儿 ────────────────────────────────────────
def test_fail_closed_when_owner_unknown_leaves_no_orphan(db, monkeypatch, caplog):
    """owner 为 NULL(孤儿 brand)→ 一个订阅不建,**且 is_monitored 也不许落**。

    🔴 这条是本工单的灵魂:旧实现"标了不建"就是孤儿;
      新实现如果"建不了还照标",那就是换了个入口继续产孤儿。两样都不许。
    """
    _seed(db)
    with db.conn.cursor() as cur:
        cur.execute("UPDATE brands SET owner_user_id = NULL WHERE id = %s", (BRAND_ID,))
    import logging
    with caplog.at_level(logging.ERROR):
        _confirm_quote(db, monkeypatch, ("孤儿词甲",))
    assert _subs(db) == [], "owner 不可确认还建了订阅 —— 钱会落到不该落的人头上"
    with db.conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM confirmed_keywords WHERE is_monitored = TRUE")
        assert cur.fetchone()["n"] == 0, "订阅没建成,is_monitored 却落了 = 新造的孤儿"
    # 🔴 还要证明**走的是显式 fail-closed 那条路**,而不是"照建结果撞了库约束偶然没落"。
    #   只断末态时,把 fail-closed 拆掉的变异照样绿(实测 M3 SURVIVED)——
    #   因为 user_id NOT NULL 恰好把它兜住了。那是运气,不是设计。
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "监测计费主体无法确认" in text, f"没走显式 fail-closed(日志里没有那句):{text[:400]}"


def test_normal_path_not_broken_by_fail_closed(db, monkeypatch):
    """反向对照:owner 正常时必须照建(否则上一条可能只是恒不建 = 零判别力)。"""
    _seed(db)
    _confirm_quote(db, monkeypatch, ("孤儿词甲",))
    assert len(_subs(db)) == 1


# ── 锁5 · ① 幂等 ────────────────────────────────────────────────────────
def test_repeat_confirm_is_idempotent(db, monkeypatch):
    _seed(db)
    _confirm_quote(db, monkeypatch, ("孤儿词甲",))
    _confirm_quote(db, monkeypatch, ("孤儿词甲",))
    rows = _subs(db)
    assert len(rows) == 1, f"重复确认产生了第二行订阅(会重复扣费):{rows}"


# ── 锁6-8 · ② 展示派生 ──────────────────────────────────────────────────
def _derived_is_monitored(db, monkeypatch, keyword_id):
    """跑**真的** `get_client_keywords(quote_id)`,取该词的 is_monitored。

    🔴 第一版这里自带了一份"与生产同形"的 EXISTS SQL —— 那是在测我抄的副本,
      改坏 `db/monitoring_db.py` 里那段真 SQL 它照样绿(变异 M6 当场 SURVIVED 证明了)。
      判据必须打在真链路上:直接调被测函数。
    """
    _patch_conn(monkeypatch, db)
    from db.monitoring_db import get_client_keywords
    rows = get_client_keywords(QUOTE_ID)
    hit = [r for r in rows if r.get("id") == keyword_id and r.get("source") == "confirmed"]
    assert hit, f"get_client_keywords 没返回这个词(keyword_id={keyword_id})· 夹具或口径不对"
    return bool(hit[0].get("is_monitored"))


def test_orphan_shows_off_and_subscribed_shows_on(db, monkeypatch):
    """正反两态**给不同值** —— 只验一态的锁证明不了它不是恒真/恒假。"""
    ids = _seed(db)
    orphan, healthy = ids[0], ids[1]
    with db.conn.cursor() as cur:
        # 孤儿态:is_monitored=TRUE 但订阅 0 行(生产 147 条就是这个形状)
        cur.execute("UPDATE confirmed_keywords SET is_monitored=TRUE WHERE id=%s", (orphan,))
        # 健康态:有 active 订阅
        cur.execute(
            "INSERT INTO keyword_monitor_subscriptions (user_id,keyword_id,quote_id,brand_id,status)"
            " VALUES (%s,%s,%s,%s,'active')", (OWNER_ID, healthy, QUOTE_ID, BRAND_ID))
    assert _derived_is_monitored(db, monkeypatch, orphan) is False, "孤儿词仍显示'开' → 界面继续说谎"
    assert _derived_is_monitored(db, monkeypatch, healthy) is True, "有订阅却显示'关' → 正常路径被误伤"


def test_paused_low_balance_still_shows_on(db, monkeypatch):
    """余额不足是**暂停**不是关闭(充值后自己恢复),UI 不该显示成关。"""
    ids = _seed(db)
    kid = ids[0]
    with db.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO keyword_monitor_subscriptions (user_id,keyword_id,quote_id,brand_id,status)"
            " VALUES (%s,%s,%s,%s,'paused_low_balance')", (OWNER_ID, kid, QUOTE_ID, BRAND_ID))
    assert _derived_is_monitored(db, monkeypatch, kid) is True


def test_cancelled_subscription_shows_off(db, monkeypatch):
    """反向对照:cancelled 必须显示关(否则 EXISTS 的 status 过滤形同虚设)。"""
    ids = _seed(db)
    kid = ids[0]
    with db.conn.cursor() as cur:
        cur.execute("UPDATE confirmed_keywords SET is_monitored=TRUE WHERE id=%s", (kid,))
        cur.execute(
            "INSERT INTO keyword_monitor_subscriptions (user_id,keyword_id,quote_id,brand_id,status)"
            " VALUES (%s,%s,%s,%s,'cancelled')", (OWNER_ID, kid, QUOTE_ID, BRAND_ID))
    assert _derived_is_monitored(db, monkeypatch, kid) is False


def test_derivation_is_wired_into_get_client_keywords():
    """接线锁:上面那段 EXISTS 必须真的长在 `get_client_keywords` 的 SQL 里。

    🔴 判据打在接线上:派生口径写对了但没接进真查询 = 界面照样说谎
      (本仓有过"新增字段被上层丢掉、后端锁全绿"的前科)。
    """
    src = (ROOT / "db" / "monitoring_db.py").read_text(encoding="utf-8")
    i = src.index("def get_client_keywords")
    j = src.index("\ndef ", i + 10)
    body = src[i:j]
    assert "FROM keyword_monitor_subscriptions kms" in body, "派生没接进 get_client_keywords"
    assert "COALESCE(is_monitored, FALSE) as is_monitored" not in body, "还在读旧字段"


# ── 锁9-10 · ③ 归档联动 ─────────────────────────────────────────────────
def test_archive_cancels_subscription(db):
    """行为锁:归档动作真的把订阅打成 cancelled(跑 db 层真函数 + 真库)。"""
    ids = _seed(db)
    kid = ids[0]
    with db.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO keyword_monitor_subscriptions (user_id,keyword_id,quote_id,brand_id,status)"
            " VALUES (%s,%s,%s,%s,'active')", (OWNER_ID, kid, QUOTE_ID, BRAND_ID))
    from db.monitoring_db import cancel_keyword_monitor_subscriptions_for_keyword_with_cursor
    conn = db.connect()
    try:
        cur = conn.cursor()
        n = cancel_keyword_monitor_subscriptions_for_keyword_with_cursor(cur, kid)
        conn.commit()
    finally:
        conn.close()
    assert n == 1
    assert [r["status"] for r in _subs(db, kid)] == ["cancelled"], "归档后订阅仍是僵尸 active"


def test_archive_endpoint_is_actually_wired_to_the_cancel(db):
    """接线锁(AST):归档端点**自己**必须调那个 cancel,而不是"我写的函数能用"就算数。

    🔴 上一条只证明 db 层函数能用;把 server.py 里的联动删掉它照样绿
      (变异 M7 实测 SURVIVED)。这条补上"接线"这一半。
    🔴 为什么不是跑真端点:`import server` 会触发启动自检(geo_observation_policy 默认行、
      cron 任务注册),而本包用的是 **schema-only** 生产快照、没有种子数据 —— 端到端挂载
      要额外喂一串启动种子,属本单范围外。这条如实标注为接线锁,不冒充端到端。
    🔴 用 AST 不用 grep:函数名出现在注释里也能骗过字符串搜索,只有真实调用节点算数。
    """
    import ast
    tree = ast.parse((ROOT / "server.py").read_text(encoding="utf-8"))
    target = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))                 and node.name == "archive_monitoring_keyword":
            target = node
    assert target is not None, "找不到归档端点(改名了?先看代码再改锁)"
    called = {
        getattr(n.func, "id", getattr(n.func, "attr", ""))
        for n in ast.walk(target) if isinstance(n, ast.Call)
    }
    assert "cancel_keyword_monitor_subscriptions_for_keyword_with_cursor" in called,         "归档端点没接联动取消 → 订阅表会留僵尸 active"

    # 🔴 [Review 2026-08-10 补] 上面那条只断言"调用存在",有个盲区:
    #   谁把这行挪到 `conn.commit()` **之后**,它照样绿 —— 而那时归档 UPDATE 已经提交,
    #   取消订阅落进了另一个事务:中间崩一下就是"词归档了、订阅还 active"的僵尸态,
    #   正是本条修复要消灭的东西。所以再断一次**源码顺序**:cancel 必须在 commit 之前。
    #   判据用 AST 节点位置(lineno, col_offset)而不是裸文本 offset —— 同一个语义,
    #   但不会被注释/字符串里出现的 "conn.commit()" 字样骗到;也不 import server
    #   (它的启动自检在 schema-only 夹具上跑不起来)。
    cancel_at = [
        (n.lineno, n.col_offset) for n in ast.walk(target)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", getattr(n.func, "attr", ""))
        == "cancel_keyword_monitor_subscriptions_for_keyword_with_cursor"
    ]
    commit_at = [
        (n.lineno, n.col_offset) for n in ast.walk(target)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "commit"
    ]
    # 元判据:两边都得真找到节点 —— 有一边为空,下面那句 max<min 就是空对空的恒真。
    assert cancel_at, "没找到 cancel 调用节点(判据失效,不是通过)"
    assert commit_at, "没找到 commit 调用节点(判据失效,不是通过)"
    # 取最晚的 cancel 与最早的 commit 比:多个 commit 时按最保守的那个算。
    assert max(cancel_at) < min(commit_at), (
        f"取消订阅排在 commit 之后(cancel@{max(cancel_at)} vs commit@{min(commit_at)})· "
        "归档与取消就不再是同一个事务,中间崩一下会留僵尸 active"
    )


@pytest.mark.parametrize("running_status", ["active", "paused_low_balance"])
def test_archive_cancels_either_running_status_and_only_that_keyword(db, running_status):
    """两种"在跑"状态都要能被取消,且**只动目标词**。

    🔴 这条用例原本写的是"同一个词插两行在跑,验证两行都被 cancel" —— 生产 schema 当场
      把它证伪了:`uniq_kms_keyword_active` 是
      `UNIQUE (keyword_id) WHERE status IN ('active','paused_low_balance')`,
      同一个词根本插不进第二行在跑的。手搓建表就不会有这个索引,那条用例会"通过"
      并让我以为自己验了一件**生产上不可能发生**的事。留档:这就是不手搓 schema 的价值。
      改成验真正有判别力的两件:两种在跑状态都吃得掉 + 不误伤同库别的词。
    """
    ids = _seed(db)
    target, bystander = ids[0], ids[1]
    with db.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO keyword_monitor_subscriptions (user_id,keyword_id,quote_id,brand_id,status)"
            " VALUES (%s,%s,%s,%s,%s)", (OWNER_ID, target, QUOTE_ID, BRAND_ID, running_status))
        cur.execute(
            "INSERT INTO keyword_monitor_subscriptions (user_id,keyword_id,quote_id,brand_id,status)"
            " VALUES (%s,%s,%s,%s,'active')", (OWNER_ID, bystander, QUOTE_ID, BRAND_ID))
    from db.monitoring_db import cancel_keyword_monitor_subscriptions_for_keyword_with_cursor
    conn = db.connect()
    try:
        cur = conn.cursor()
        n = cancel_keyword_monitor_subscriptions_for_keyword_with_cursor(cur, target)
        conn.commit()
    finally:
        conn.close()
    assert n == 1
    assert [r["status"] for r in _subs(db, target)] == ["cancelled"]
    assert [r["status"] for r in _subs(db, bystander)] == ["active"], "误伤了别的词的订阅"


def test_restore_does_not_auto_reenable():
    """③ 恢复 = 回列表,不自动复开 —— 源码锁(端点在 server.py,行为锁见变异 M9)。

    🔴 双向:旧形状必须消失 + 新形状必须在。只删不加或只加不删都会转红。
    """
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    i = src.index("async def restore_archived_monitoring_keyword")
    j = src.index("\n@app.", i)
    body = src[i:j]
    assert "is_monitored = FALSE" in body, "恢复仍会自动把监测打开(静默复扣费)"
    assert "is_monitored = TRUE" not in body
    assert "自动监测未自动开启" in body, "没把'不自动开'这件事告诉用户"


# ── 锁11 · 夹具元判据 ───────────────────────────────────────────────────
def test_orphan_fixture_is_really_an_orphan(db):
    """🔴 元判据:上面"孤儿态显示关"的断言,全建立在夹具真是孤儿这个前提上。

    夹具要是压根没设 is_monitored=TRUE(或悄悄带了订阅),那条锁会以"没人反对"的方式全绿。
    这里逐字证明:is_monitored=TRUE 且该词订阅 0 行 —— 与生产 q7_orphans 同形。
    """
    ids = _seed(db)
    kid = ids[0]
    with db.conn.cursor() as cur:
        cur.execute("UPDATE confirmed_keywords SET is_monitored=TRUE WHERE id=%s", (kid,))
        cur.execute("SELECT is_monitored FROM confirmed_keywords WHERE id=%s", (kid,))
        assert cur.fetchone()["is_monitored"] is True
    assert _subs(db, kid) == [], "夹具带了订阅,那就不是孤儿"


# ── 锁12 · 计费主体口径一致性 ───────────────────────────────────────────
def test_billing_resolver_matches_server_copy(db):
    """共享模块与 server.py 那份 `_resolve_monitor_billing_user` 必须逐格同值。

    🔴 为什么是两份实现:server.py 那份被
      `tests/test_p04_monitor_billing_owner_2026_06_10.py` 用**源码文本 exec()** 跑真值表,
      改成转调共享模块会让那条已上线的资金 P0 回归测试当场 NameError。
      所以 server.py 一个字没动,改由这条锁钉死两份不许漂。
    """
    import textwrap
    import types

    from services.monitor_billing import resolve_monitor_billing_user_with_cursor as shared

    server_src = (ROOT / "server.py").read_text(encoding="utf-8")
    i = server_src.find("def _resolve_monitor_billing_user")
    j = server_src.find('@app.post("/api/monitoring/keyword/{keyword_id}/enable")', i)
    assert i >= 0 and j > i
    snippet = textwrap.dedent(server_src[i:j])

    class _Logger:
        def warning(self, *a, **k): pass
        def info(self, *a, **k): pass

    class _Cur:
        def __init__(self, row): self._row = row
        def execute(self, *a): pass
        def fetchone(self): return self._row

    def _call_server_copy(row, operator, brand, boom=False):
        """在 stub 装着的**整个调用期间**跑 server.py 那份实现。

        🔴 第一版把 `sys.modules` 还原写在 return 之前的 finally 里 —— 函数拿到手时
          stub 已经被撤掉,真跑时连的是真库、必然抛错、恒返 None,于是"两份一致"
          变成了"两份都返 None"的假绿。这一版把还原放到**调用之后**。
        """
        fake = types.ModuleType("db.connection")
        if boom:
            fake.get_connection = lambda: (_ for _ in ()).throw(RuntimeError("db down"))
        else:
            class _Conn:
                def cursor(self): return _Cur(row)
                def close(self): pass
            fake.get_connection = lambda: _Conn()
        old = sys.modules.get("db.connection")
        sys.modules["db.connection"] = fake
        try:
            ns = {"logger": _Logger()}
            exec(snippet, ns)
            return ns["_resolve_monitor_billing_user"](operator, brand)
        finally:
            if old is not None:
                sys.modules["db.connection"] = old
            else:
                sys.modules.pop("db.connection", None)

    class _BoomCur:
        def execute(self, *a): raise RuntimeError("db down")
        def fetchone(self): raise AssertionError("不该走到")

    cases = [
        ("owner 正常", {"owner_user_id": 46}, 99, 46),
        ("操作者本就是 owner", {"owner_user_id": 46}, 99, 46),
        ("brand 为空", {"owner_user_id": 46}, None, None),
        ("owner 为 NULL", {"owner_user_id": None}, 99, None),
        ("brand 查不到", None, 99, None),
    ]
    for label, row, brand, expect in cases:
        srv = _call_server_copy(row, 1, brand)
        shr = shared(_Cur(row), brand)
        assert srv == expect, f"{label}: server.py 那份给了 {srv},期望 {expect}"
        assert shr == expect, f"{label}: 共享模块给了 {shr},期望 {expect}"

    # 查询失败这一格
    assert _call_server_copy(None, 7, 99, boom=True) is None
    assert shared(_BoomCur(), 99) is None

    # 元判据:上面用例里 True/False 两侧都有,不是一串恒 None
    assert any(c[3] is not None for c in cases) and any(c[3] is None for c in cases)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
