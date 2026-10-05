"""#151 · 图文成品的发布终态收敛。

## 缺陷(实证 + 机理订正)

生产 post 24 自 2026-08-10 停在 `publishing` **29 天**(order 491)。

🔴 我第一版把机理写成「`publishing` 没有出口」,**那是错的**
(Deploy 只读取证 2026-09-08:同路 **502 条**已 published、中位 133 分钟、
最长 6.2 天 ⇒ 出口存在且平时在用;卡住的 22 条全落在 08-10~08-12 故障窗)。
真相是**那三天的回写没发生且无人重试**。
结论(要收敛)对、机理错 —— 这种错不会让判据变红,
只会让下一个人去修一个不存在的东西。

⇒ 本包只钉「item 到终态 ⇒ 作品收敛」。`submitted` 判**在途**(收敛不了那 22 条),
  这是**对的**:供应商接了单但结果未知不是终态。重探属 #151-B。

## 判据三面

  · **收敛**:整单 item 都终态 ⇒ 作品落 `published`(带 url)或 `failed`;
  · **不编造**:还有 item 在途 ⇒ **一个字都不写**(超时同理,只告警);
  · **接线**:任务真被注册,且 `@sched_claim` 没被偷。
"""

from __future__ import annotations

import ast
import io
import os
import pathlib
import uuid

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DSN = os.getenv("TEST_DATABASE_URL")
PROD_SCHEMA = pathlib.Path(os.getenv(
    "GDQ_PROD_SCHEMA_SQL", r"C:/AI-Test/.deploy_toolkit/prod_schema_2026-09-05.sql"))


# ══════════════════════════════════ 判定逻辑(纯函数)

def _items(*states):
    return [{"status": s, "publish_url": ("http://u/%d" % i if s in ("published", "success") else "")}
            for i, s in enumerate(states)]


def test_one_success_among_many_counts_as_published():
    """🔴 「有一条成就算成」,不是「全成才算成」。

    一条内容发到 N 个账号,成一个就是发出去了。
    毒:改成 all(...) ⇒ 三个账号成了一个会被判成失败,而那条内容明明在线上。
    """
    from services.geo_douyin.publish_convergence import classify_order_items

    verdict, url = classify_order_items(_items("failed", "published", "rejected"))
    assert verdict == "published"
    assert url.startswith("http"), "成了却没带回 publish_url(飞轮反查靠它)"


def test_all_failure_states_converge_to_failed():
    from services.geo_douyin.publish_convergence import classify_order_items
    for states in (("failed",), ("rejected", "cancelled"), ("withdrawn", "failed")):
        assert classify_order_items(_items(*states))[0] == "failed", states


@pytest.mark.parametrize("pending", ["pending", "submitting",
                                     "awaiting_confirmation", "awaiting_sync"])
def test_any_in_flight_item_blocks_convergence(pending):
    """🔴🔴 承重那一格:**还有在途的就不收敛**。

    毒:把在途也判成 failed ⇒ 本条红。
    「卡了很久」只说明我们**不知道**结果,不说明它失败了 ——
    钱已经花掉,把未知宣布成失败是业务判断,不该由 sweeper 替 Owner 做。
    """
    from services.geo_douyin.publish_convergence import classify_order_items
    assert classify_order_items(_items("published", pending))[0] is None
    assert classify_order_items(_items("failed", pending))[0] is None


def test_an_empty_order_is_not_a_verdict():
    """没有 item ⇒ 判不了,不是「失败」。"""
    from services.geo_douyin.publish_convergence import classify_order_items
    assert classify_order_items([])[0] is None
    assert classify_order_items(None)[0] is None


def test_an_unknown_item_state_blocks_convergence():
    """🔴 认不出的状态一律当在途 —— 白名单,不是黑名单。

    mhz 那边加一个新状态时,黑名单会把它当成终态草率收敛;
    白名单只会暂缓,代价是慢一点,而不是判错。
    """
    from services.geo_douyin.publish_convergence import classify_order_items
    assert classify_order_items(_items("published", "some_future_state"))[0] is None


# ══════════════════════════════════ 真库行为臂

@pytest.fixture(scope="module")
def live():
    if not DSN or not PROD_SCHEMA.is_file():
        pytest.skip("需要 TEST_DATABASE_URL 与生产 schema dump(缺则本组未验证)")
    psycopg2 = pytest.importorskip("psycopg2")
    from psycopg2.extras import RealDictCursor

    name = "gdq_conv_%s" % uuid.uuid4().hex[:8]
    root = DSN.rsplit("/", 1)[0]
    admin = psycopg2.connect(root + "/postgres")
    admin.autocommit = True
    admin.cursor().execute('CREATE DATABASE "%s"' % name)
    admin.close()
    dsn = root + "/" + name
    conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    conn.autocommit = True
    cur = conn.cursor()
    for role in ("ai_ops_runner", "geo_readonly"):
        cur.execute("DO $$BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=%s)"
                    " THEN EXECUTE format('CREATE ROLE %%I', %s); END IF; END$$",
                    (role, role))
    cur.execute("\n".join(
        l for l in PROD_SCHEMA.read_text(encoding="utf-8", errors="ignore").splitlines()
        if not l.startswith("\\restrict") and not l.startswith("\\unrestrict")))
    conn.close()
    try:
        yield dsn
    finally:
        admin = psycopg2.connect(root + "/postgres")
        admin.autocommit = True
        admin.cursor().execute('DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % name)
        admin.close()


@pytest.fixture
def wired(live, monkeypatch):
    psycopg2 = pytest.importorskip("psycopg2")
    from psycopg2.extras import RealDictCursor

    import db.connection as dbc
    import db.geo_douyin_db as gddb

    def _conn(*a, **k):
        return psycopg2.connect(live, cursor_factory=RealDictCursor)

    # 两处都打(§3.2 教训:geo_douyin_db 顶层绑了自己的引用)
    monkeypatch.setattr(dbc, "get_connection", _conn)
    monkeypatch.setattr(gddb, "get_connection", _conn, raising=False)
    return _conn


def _seed(conn_factory, *item_states):
    conn = conn_factory()
    conn.autocommit = True
    cur = conn.cursor()
    # 真 schema 有真外键(items.order_id → mhz_publish_orders)——
    # 订单行必须先造。夹具库上没有这条 FK,造不出这个约束。
    cur.execute("INSERT INTO mhz_publish_orders (user_id, article_title)"
                " VALUES (1,'夹具订单') RETURNING id")
    order_id = dict(cur.fetchone())["id"]
    cur.execute("INSERT INTO geo_douyin_posts (created_by, keyword, publish_status,"
                " publish_order_id) VALUES (1,'k','publishing', %s) RETURNING id",
                (order_id,))
    post_id = dict(cur.fetchone())["id"]
    for st in item_states:
        # 真 schema 的必填:user_id / media_name(夹具库上没有这些约束,
        # 只有打真 dump 才会撞见 —— 这正是打真库的价值)。
        cur.execute("INSERT INTO mhz_publish_order_items"
                    " (order_id, user_id, media_name, status, publish_url)"
                    " VALUES (%s,%s,%s,%s,%s)",
                    (order_id, 1, "测试账号", st,
                     "http://u" if st in ("published", "success") else ""))
    conn.close()
    return post_id


def _status(conn_factory, post_id):
    conn = conn_factory()
    cur = conn.cursor()
    cur.execute("SELECT publish_status, published_url FROM geo_douyin_posts WHERE id=%s",
                (post_id,))
    row = dict(cur.fetchone())
    conn.close()
    return row


def test_a_finished_order_really_converges_in_the_database(wired):
    """🔴 打真库:整单终态 ⇒ 作品真的落 `published` 且带 url。"""
    from services.geo_douyin.publish_convergence import converge_publishing_posts

    pid = _seed(wired, "published", "failed")
    stats = converge_publishing_posts()
    assert stats["published"] >= 1, stats
    row = _status(wired, pid)
    assert row["publish_status"] == "published", row
    assert (row["published_url"] or "").startswith("http"), row


def test_an_in_flight_order_is_left_untouched_in_the_database(wired):
    """🔴🔴 真库反向臂:还有在途 ⇒ 作品**一个字都不改**。

    毒:让在途也收敛 ⇒ 本条红。这条守的是「不把未知宣布成结论」。
    """
    from services.geo_douyin.publish_convergence import converge_publishing_posts

    pid = _seed(wired, "published", "awaiting_sync")
    before = _status(wired, pid)
    converge_publishing_posts()
    assert _status(wired, pid) == before, "在途的作品被改了状态"
    assert before["publish_status"] == "publishing"


# ══════════════════════════════════ 超时:只告警 + 展示态(Review 裁定 ③)

def _post(hours_ago, status="publishing"):
    import datetime as dt
    return {"publish_status": status,
            "updated_at": dt.datetime.now() - dt.timedelta(hours=hours_ago)}


def test_a_long_stuck_post_is_shown_as_pending_confirmation():
    """🔴 超过 24h 仍 publishing ⇒ 列表显示「发布结果待确认」。

    它是**展示态**,由现有字段现场派生 —— 不落库、不进 `publish_status` 取值域。
    前端对 publish_status 没有白名单,新加一个裸串会直接上屏(本仓红线)。
    """
    from services.geo_douyin.publish_convergence import pending_confirm_display
    assert pending_confirm_display(_post(25)) is True
    assert pending_confirm_display(_post(23)) is False, "不到 24h 就喊,会变成噪音"


def test_only_publishing_rows_get_the_pending_badge():
    """反向臂:已终态的行**不显示**待确认。

    没有这条,一个「只看时间不看状态」的实现也能让上一条变绿 ——
    那会让三个月前发成功的作品全都挂上「待确认」。
    """
    from services.geo_douyin.publish_convergence import pending_confirm_display
    for st in ("published", "failed", "self_reported_unverified", ""):
        assert pending_confirm_display(_post(999, st)) is False, st


def test_the_list_endpoint_derives_the_badge_and_stores_nothing():
    """结构臂:列表端点用派生函数,且**没有**把它写回库。"""
    src = io.open(ROOT / "api" / "geo_douyin_api.py", encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "api_list_posts")
    # 🔴 按 **AST 取真实 Call 节点**,不数名字出现次数:
    #    `from ... import pending_confirm_display` 让名字始终在场,
    #    毒把赋值删了、判据照样绿(#139 那条教训,我在这条上又栽了一次)。
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "pending_confirm_display"]
    assert calls, "列表没**调用**展示态派生(只 import 不算)"
    body = ast.unparse(fn)
    for writer in ("bind_publish_result", "UPDATE geo_douyin_posts", "set_post_status"):
        assert writer not in body, "列表端点写库了(展示态必须只读):%s" % writer


def test_the_stale_warning_is_actionable():
    """🔴 告警必须带 post_id / order_id / 卡了几小时。

    只说「有 N 条卡住了」的告警,收到的人不知道下一步做什么 ——
    那种告警会被训练成忽略。
    """
    src = io.open(ROOT / "services" / "geo_douyin" / "publish_convergence.py",
                  encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "converge_publishing_posts")
    body = ast.unparse(fn)
    assert "post_id=%s" in body and "publish_order_id=%s" in body, body[:400]
    assert "小时" in body, "告警没给卡了多久"


# ══════════════════════════════════ 接线

def test_the_job_is_registered_and_keeps_its_claim_decorator():
    """🔴 任务真被注册,且 `@sched_claim` 没被偷。

    #120 的教训:把函数定义插进装饰器与被修饰函数之间,
    decorator 会套到**别的**函数头上,而 `ast.parse` 与 import 都不会报错。
    """
    src = io.open(ROOT / "scheduler.py", encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "job_geo_douyin_publish_converge"), None)
    assert fn is not None, "收敛任务不在 scheduler 里"
    decos = [ast.unparse(d) for d in fn.decorator_list]
    assert any("sched_claim" in d for d in decos), (
        "收敛任务没有 sched_claim —— 多实例会并发跑同一批:%r" % decos)
    assert "job_id=\"geo_douyin_publish_converge\"" in src, "任务没被 add_job 注册"


def test_the_sweeper_never_writes_a_status_for_in_flight_posts():
    """结构臂:在途分支里不许出现写库调用。

    行为臂打的是两种夹具;这条挡的是「在别的分支里顺手也写一次」。
    """
    src = io.open(ROOT / "services" / "geo_douyin" / "publish_convergence.py",
                  encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "converge_publishing_posts")
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        if "verdict is None" not in ast.unparse(node.test):
            continue
        body = ast.unparse(ast.Module(body=node.body, type_ignores=[]))
        assert "bind_publish_result" not in body, (
            "在途分支里写了状态 —— 那就是把未知宣布成结论:\n%s" % body[:300])
