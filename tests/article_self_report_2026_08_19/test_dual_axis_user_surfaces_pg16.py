"""判据④ · 用户面**双轴**(R2 §②③)—— raw success 不再念成「已发布」。

Review §② 点名四个面必须区分「操作回执」与「已核实发布」:

  1. ~~发布结果轮询端点~~(WO_273:随插件后端删除,见「面 1 / 面 2」节)
  2. 历史列表(~~插件历史列表端点~~,WO_273 删除;统一记录列表 `db.meijiehezi_db` 照旧在守)
  3. 聚合 `/api/meijiehezi/published-articles`
  4. 聚合 `/api/meijiehezi/article-publish-stats`

判据同样是**成对**的:同一行数据,未核实时四个面都不许说已发布;人工核实之后
四个面都必须翻面。只测一半的话,"都不说已发布"可能只是因为夹具压根没数据。

🔴 §③ 的两条硬约束单独锁:
   · 回执与**防重复发布占位**必须保留(口径收紧不该让用户白发第二遍);
   · 文案必须是「浏览器曾回报成功 · 尚未核实」——不是「未发布」(抹掉了用户
     确实操作过),也不是「已发布(待核实)」(那还是在说已发布)。
"""

from __future__ import annotations

import os
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PG_URL = os.environ.get("TEST_DATABASE_URL")
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
SCHEMA_SQL = Path(__file__).resolve().parent / "prod_schema_2026-08-19.sql"
MIGRATION_SQL = ROOT / "scripts" / "migration_publish_records_url_verification_2026_08_19.sql"

ARTICLE_ID = 991201
TOPIC_ID = 991202
QUOTE_ID = 991203
BRAND_ID = 991204
OWNER_UID = 991205
UID = "991205"
TITLE = "双轴投影验证专用标题一二三四"
BODY = "正文。" * 20
PUBLIC_URL = "https://www.toutiao.com/article/991201/"


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
    name = f"artaxis_test_{uuid.uuid4().hex[:10]}"
    assert "test" in name and "prod" not in name
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    parsed = urlsplit(PG_URL)
    url = urlunsplit((parsed.scheme, parsed.netloc, f"/{name}", parsed.query, parsed.fragment))

    raw = SCHEMA_SQL.read_text(encoding="utf-8")
    body = "\n".join(
        line for line in raw.splitlines()
        if not line.startswith("\\") and not line.startswith("CREATE SCHEMA public;")
    )
    conn = psycopg2.connect(url, cursor_factory=RealDictCursor)
    conn.autocommit = True
    with conn.cursor() as cur:
        for _ext in ("vector", "pg_trgm"):
            cur.execute(f"CREATE EXTENSION IF NOT EXISTS {_ext}")
        cur.execute(body)
        cur.execute("SET search_path TO public")
        cur.execute(MIGRATION_SQL.read_text(encoding="utf-8"))
    try:
        yield type("DB", (), {"url": url, "conn": conn})
    finally:
        conn.close()
        with admin.cursor() as cur:
            cur.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


@pytest.fixture(autouse=True)
def seed(db):
    with db.conn.cursor() as cur:
        cur.execute("SET search_path TO public")
        cur.execute("DELETE FROM publish_records WHERE user_id=%s", (UID,))
        cur.execute("DELETE FROM articles WHERE id=%s", (ARTICLE_ID,))
        cur.execute("DELETE FROM quotes WHERE id=%s", (QUOTE_ID,))
        cur.execute("DELETE FROM brands WHERE id=%s", (BRAND_ID,))
        cur.execute("INSERT INTO brands (id,name,owner_user_id) VALUES (%s,%s,%s)",
                    (BRAND_ID, "双轴验证品牌", OWNER_UID))
        cur.execute("INSERT INTO quotes (id,brand_id,service_days,owner_user_id) "
                    "VALUES (%s,%s,365,%s)", (QUOTE_ID, BRAND_ID, OWNER_UID))
        cur.execute("INSERT INTO articles (id,topic_id,quote_id,title,content,"
                    "publication_profile) VALUES (%s,%s,%s,%s,%s,'standard')",
                    (ARTICLE_ID, TOPIC_ID, QUOTE_ID, TITLE, BODY))
        cur.execute(
            """
            INSERT INTO publish_records
            (user_id, article_title, platform, account_name, status, brand_id, article_id,
             request_id, submitted_title_snapshot, public_url,
             public_url_reported_explicitly, public_url_verification_state, created_at)
            VALUES (%s,%s,'今日头条','acct-1','success',%s,%s,'req-axis',%s,%s,TRUE,'pending',
                    CURRENT_TIMESTAMP)
            """,
            (UID, TITLE, BRAND_ID, ARTICLE_ID, TITLE, PUBLIC_URL),
        )
    yield


@contextmanager
def _app_db(db, monkeypatch):
    import psycopg2
    from psycopg2.extras import RealDictCursor
    import db.connection as dbconn

    opened = []

    def _get():
        c = psycopg2.connect(db.url, cursor_factory=RealDictCursor)
        with c.cursor() as cur:
            cur.execute("SET search_path TO public")
        opened.append(c)
        return c

    monkeypatch.setattr(dbconn, "get_connection", _get)
    try:
        yield
    finally:
        for c in opened:
            try:
                c.close()
            except Exception:
                pass


def _mark_verified(db):
    """把这一行升成 verified(走库允许的权威来源) —— 阳性臂用。"""
    with db.conn.cursor() as cur:
        cur.execute(
            "UPDATE publish_records SET public_url_verification_state='verified',"
            " public_url_verification_source='human_attestation',"
            " public_url_verified_at=CURRENT_TIMESTAMP WHERE user_id=%s", (UID,))


class _Req:
    """最小请求替身:只带 request.state.user(这几个接口只用它)。"""

    def __init__(self, user):
        self.state = type("S", (), {"user": user})()


def _user():
    return {"user_id": int(UID), "sub": int(UID), "is_admin": False}


# ===========================================================================
# 面 1 / 面 2(插件端点)—— [WO_273 · 2026-09-23 肯定式退役]
# ===========================================================================
# 原有两格:`test_publish_result_separates_receipt_from_publication`(发布结果轮询端点)与
# `test_history_list_marks_unverified_and_offers_three_actions`(插件历史列表 + 重核 / 补证据 / 查看三动作)。
# 两个端点随插件后端**整体删除**,被守的面不存在了。
# 接替:
#   · 「这两个面不许悄悄回来」→ tests/extension_retirement_2026_09_23 的路由缺席锁;
#   · 历史自助记录**仍然可见**的那个面 —— 统一记录列表(面 2b)与已分发聚合(面 3 / 面 4)——
#     本文件下方各格照旧在守「回执成功 ≠ 已发布」,一格未动。
# 对应变异 M12 / M13 一并从 run_mutations.py 退役。


# ===========================================================================
# 面 3 · /api/meijiehezi/published-articles(聚合 · 防重占位必须保留)
# ===========================================================================

@pytest.mark.asyncio
async def test_published_articles_keeps_dedupe_but_splits_verified(db, monkeypatch):
    import api.meijiehezi_api as mhz

    monkeypatch.setattr(mhz, "_get_user", lambda req: {"user_id": int(UID)})
    with _app_db(db, monkeypatch):
        out = await mhz.api_published_articles(_Req(_user()))

    # §③ 防重复发布的占位**保留** —— 否则用户会把同一篇再发一遍
    assert ARTICLE_ID in out["article_ids"]
    assert ARTICLE_ID in out["dedupe_placeholder_article_ids"]
    # 但"已核实发布"这一栏里没有它
    assert ARTICLE_ID not in out["verified_published_article_ids"]
    assert ARTICLE_ID in out["reported_success_unverified_article_ids"]

    _mark_verified(db)
    with _app_db(db, monkeypatch):
        out2 = await mhz.api_published_articles(_Req(_user()))
    assert ARTICLE_ID in out2["verified_published_article_ids"]
    assert ARTICLE_ID not in out2["reported_success_unverified_article_ids"]


# ===========================================================================
# 面 4 · /api/meijiehezi/article-publish-stats(聚合)
# ===========================================================================

@pytest.mark.asyncio
async def test_article_publish_stats_does_not_call_it_published(db, monkeypatch):
    import api.meijiehezi_api as mhz

    monkeypatch.setattr(mhz, "_get_user", lambda req: {"user_id": int(UID)})
    with _app_db(db, monkeypatch):
        out = await mhz.api_article_publish_stats(_Req(_user()))
    st = out["stats"][str(ARTICLE_ID)]
    assert st["status"] == "reported_success_unverified", st
    assert st["published"] == 0, "未核实的自报不许进 published 计数"
    assert st["reported_success_unverified"] == 1
    assert st["reported_success_unverified_label"] == "浏览器曾回报成功 · 尚未核实"
    assert "今日头条" in st["reported_success_unverified_media"]
    assert st["published_media"] == []

    _mark_verified(db)
    with _app_db(db, monkeypatch):
        out2 = await mhz.api_article_publish_stats(_Req(_user()))
    st2 = out2["stats"][str(ARTICLE_ID)]
    assert st2["status"] == "published"
    assert st2["published"] == 1
    assert st2["reported_success_unverified"] == 0
    assert "今日头条" in st2["published_media"]


# ===========================================================================
# 面 2b · 统一记录列表(db.meijiehezi_db)
# ===========================================================================

def test_unified_record_list_labels_and_family(db, monkeypatch):
    from db import meijiehezi_db as mdb

    with _app_db(db, monkeypatch):
        out = mdb.list_user_publish_history(user_id=int(UID), source="self")
    rows = [r for r in out["records"] if r.get("source") == "self"]
    assert rows, out
    row = rows[0]
    assert row["status_family"] == "reported_unverified", row
    assert row["status_label"] == "浏览器曾回报成功 · 尚未核实"
    assert row["publication_axis"] == "reported_success_unverified"
    # 🔴 [WO_273 · 改断言] 原来这里断言 can_reverify=True、普通用户 can_attest=False、管理员 can_attest=True,
    #    文案按身份分叉。两个动作(重新核实 / 补人工证据)的后端端点只存在于插件后端,随它整体删除,
    #    前端按钮由 A 同单去掉 —— 规则仍是 R3 §③ 那一句:「点不动的动作不该出现在用户面上,
    #    提示要么帮得上忙要么就别出现」。于是**任何身份**的行上都不许再有这两个动作位,
    #    文案只说状态、不再提任何一个动作。
    #    对照臂:状态说明本身必须还在(不是整段 SQL 没跑到 / 文案被一起删空),「查看」动作也还在。
    assert "can_reverify" not in row and "can_attest" not in row, sorted(row)
    assert row["status_detail"] == "这条只有浏览器回报，服务端还没核实过", row.get("status_detail")
    assert "can_view" in row, "对照臂:仍在役的「查看」动作位不许被一起删掉"

    with _app_db(db, monkeypatch):
        admin_out = mdb.list_user_publish_history(user_id=int(UID), source="self",
                                                  is_admin=True)
    admin_row = [r for r in admin_out["records"] if r.get("source") == "self"][0]
    # 管理员那一臂:原来它是「must be True」的正对照;现在身份不再影响输出 —— 同样不许有这两个动作位,
    # 文案与普通用户逐字相同(曾经按身份分叉的那句「补人工证据」也不许回来)。
    assert "can_reverify" not in admin_row and "can_attest" not in admin_row, sorted(admin_row)
    assert admin_row["status_detail"] == row["status_detail"]
    for r in (row, admin_row):
        assert "重新核实" not in r["status_detail"] and "补人工证据" not in r["status_detail"]
    # 🔴 published_at 是"发布时间"不是"点击时间" —— 没核实过就不该有
    assert row.get("published_at") in (None, "")
    assert int(out["stats"]["completed"]) == 0, out["stats"]
    assert int(out["stats"]["reported_unverified"]) == 1, out["stats"]

    _mark_verified(db)
    with _app_db(db, monkeypatch):
        out2 = mdb.list_user_publish_history(user_id=int(UID), source="self")
    row2 = [r for r in out2["records"] if r.get("source") == "self"][0]
    assert row2["status_family"] == "completed"
    assert row2["status_label"] == "已核实发布"
    assert row2["publication_axis"] == "verified_published"
    assert int(out2["stats"]["completed"]) == 1


# ===========================================================================
# 口径 SSOT:SQL 侧与 Python 侧必须同源
# ===========================================================================

@pytest.mark.parametrize("status,state,expected", [
    ("success", "verified", "verified_published"),
    ("success", "content_matched", "reported_success_unverified"),
    ("success", "pending", "reported_success_unverified"),
    ("success", "needs_action", "reported_success_unverified"),
    ("success", "unverified", "reported_success_unverified"),
    ("failed", "verified", "not_published"),
    ("pending", "unverified", "not_published"),
    # 🔴 R3 §⑤ NULL 档 —— 这四格原来两侧答案是**相反**的。
    #    SQL 里 `NULL <> 'success'` 求值为 NULL(不是 TRUE),CASE 这一格不命中,
    #    于是 status IS NULL 的行掉进下一格;若它恰好带 verified,SQL 会答
    #    「已核实发布」,而 Python 侧 receipt_axis(None) 归 pending → 「未发布」。
    #    一个接口说已发布、另一个说没有,正是这份 SSOT 存在要防的那件事。
    (None, "verified", "not_published"),
    (None, None, "not_published"),
    ("success", None, "reported_success_unverified"),
    ("failed", None, "not_published"),
])
def test_sql_axis_matches_python_axis(db, status, state, expected):
    """同一份口径的两个实现(Python `publication_axis` / SQL `PUBLICATION_AXIS_SQL`)
    必须逐格一致 —— 各写一份 CASE 必然漂移,漂移出来的就是「一个接口说已发布、
    另一个说没有」这种最难查的 bug。"""
    from services.publication_receipt_projection import (
        PUBLICATION_AXIS_SQL, publication_axis,
    )

    assert publication_axis(status, state) == expected
    with db.conn.cursor() as cur:
        cur.execute(
            f"SELECT {PUBLICATION_AXIS_SQL.format(r='t')} AS axis "
            "FROM (SELECT %s::text AS status, %s::text AS public_url_verification_state) t",
            (status, state))
        assert dict(cur.fetchone())["axis"] == expected


def test_content_matched_is_not_published_anywhere():
    """`content_matched` 是探针线索 —— 任何一面都不许把它算成已发布。"""
    from services.publication_receipt_projection import (
        PUBLICATION_COUNTS_AS_PUBLISHED, publication_axis,
    )

    # (原来这里还有一句 `X not in (A & B) or True` —— `or True` 让它恒成立,
    #  是句没有判别力的废断言,R3 顺手删掉。)
    assert publication_axis("success", "content_matched") == "reported_success_unverified"
    assert PUBLICATION_COUNTS_AS_PUBLISHED == {"verified_published"}


# ===========================================================================
# R3 §⑤ · 两条布尔谓词的 NULL 档(比七格那条更贵的一个洞)
# ===========================================================================

@pytest.mark.parametrize("status,state,verified,unverified", [
    ("success", "verified", True, False),
    ("success", "pending", False, True),
    ("failed", "verified", False, False),
    # 🔴 这一格是那个洞:`state IS NULL` 时 `state <> 'verified'` 求值为 NULL,
    #    整条谓词是 NULL 而不是 TRUE → 该行**从"未核实"这一档里整个消失**。
    #    既不算已核实也不算未核实 —— 而防重复发布的占位面正走这条谓词,
    #    行消失 = 用户会被允许把同一篇再发一遍。
    ("success", None, False, True),
    (None, "verified", False, False),
])
def test_boolean_predicates_have_no_null_hole(db, status, state, verified, unverified):
    from services.publication_receipt_projection import (
        PUBLICATION_REPORTED_UNVERIFIED_SQL, PUBLICATION_VERIFIED_SQL,
    )

    with db.conn.cursor() as cur:
        cur.execute(
            f"SELECT {PUBLICATION_VERIFIED_SQL.format(r='t')} AS v, "
            f"       {PUBLICATION_REPORTED_UNVERIFIED_SQL.format(r='t')} AS u "
            "FROM (SELECT %s::text AS status, "
            "             %s::text AS public_url_verification_state) t",
            (status, state))
        row = dict(cur.fetchone())
    # `is` 不是 `==`:NULL 会回 None,而 `None == False` 是 False —— 用 `==` 的话
    # "谓词求值成 NULL" 这个洞照样会被断言成不通过…… 但错的理由会看不出来。
    assert row["v"] is verified, f"已核实谓词在 ({status},{state}) 上给了 {row['v']}"
    assert row["u"] is unverified, f"未核实谓词在 ({status},{state}) 上给了 {row['u']}"


# ===========================================================================
# R3 §③ · 「待核实」筛选是条通路(前端有这个值,后端两处白名单也得有)
# ===========================================================================

def test_reported_unverified_status_filter_round_trips(db, monkeypatch):
    """R2 只加了前端那半:筛选器发 `reported_unverified`,后端 ValueError → 一点就报错。"""
    from db import meijiehezi_db as mdb

    with _app_db(db, monkeypatch):
        out = mdb.list_user_publish_history(user_id=int(UID), source="self",
                                            status_filter="reported_unverified")
    keys = [r["record_key"] for r in out["records"]]
    assert keys, "「待核实」筛选把那条自报记录漏掉了"

    # 正对照:换个筛选值必须把它筛掉 —— 否则"筛出来了"可能是筛选整个没生效。
    with _app_db(db, monkeypatch):
        other = mdb.list_user_publish_history(user_id=int(UID), source="self",
                                              status_filter="completed")
    assert [r["record_key"] for r in other["records"]] == []


def test_api_status_literal_accepts_reported_unverified():
    """FastAPI 那层的 Literal 少一个值 = 前端一点就 422(库里放行了也没用)。"""
    import typing

    import api.meijiehezi_api as mhz

    hints = typing.get_type_hints(mhz.api_publish_history, include_extras=True)
    allowed = set(typing.get_args(hints["status"]))
    assert "reported_unverified" in allowed, allowed
    assert "completed" in allowed, "分母自证:这个提取器确实取到了真的 Literal 值集"


# ===========================================================================
# R3 §③ · 前端把两个动作真渲染出来、真调后端 —— [WO_273 · 2026-09-23 肯定式退役]
# ===========================================================================
# 原格 `test_frontend_renders_and_calls_both_actions` 锁的是:发布历史页的「重新核实 / 人工证据」两个按钮
# 有渲染分支,且真的打到后端 `…/verify-url` 与 `…/attest`。这两个后端端点**只存在于插件后端**
# (全仓再无第二处提供),随插件后端整体删除;前端的这两处调用由 A(WO_273-A)同单去掉。
# 接替:
#   · 后端端点不许回来 → tests/extension_retirement_2026_09_23 的路由缺席锁;
#   · 前端不许再调插件端点 → A 的前端锁(frontend/src 里 `/api/extension` 计数必为 0)。
# 对应变异 M41 一并从 run_mutations.py 退役。


def test_unified_list_null_status_row_is_not_called_published(db, monkeypatch):
    """R3 §⑤ 打在**统一列表那份手写 CASE** 上:`publish_records.status` 可空
    (建表就是 `status TEXT DEFAULT 'pending'`,没有 NOT NULL)。

    不 COALESCE 的话 `pr.status <> 'success'` 求值为 NULL、这一格不命中,
    带 verified 的空 status 行会被答成「已核实发布」——
    而 SSOT 那份 Python 投影对同一行答「未发布」。两个接口对同一行说反话。
    """
    from db import meijiehezi_db as mdb
    from services.publication_receipt_projection import publication_axis

    with db.conn.cursor() as cur:
        cur.execute("SET search_path TO public")
        cur.execute(
            """
            INSERT INTO publish_records
            (user_id, article_title, platform, account_name, status, brand_id,
             article_id, public_url, public_url_verification_state,
             public_url_verification_source, created_at)
            VALUES (%s,'空状态行','今日头条','acct-null',NULL,%s,%s,
                    'https://www.toutiao.com/article/990199/','verified',
                    'human_attestation', CURRENT_TIMESTAMP)
            RETURNING id
            """,
            (UID, BRAND_ID, ARTICLE_ID))
        null_id = int(dict(cur.fetchone())["id"])

    assert publication_axis(None, "verified") == "not_published", "Python 侧的口径"
    with _app_db(db, monkeypatch):
        out = mdb.list_user_publish_history(user_id=int(UID), source="self")
    row = [r for r in out["records"] if r["action_id"] == str(null_id)][0]
    assert row["publication_axis"] == "not_published", row
    assert row["status_family"] != "completed"
