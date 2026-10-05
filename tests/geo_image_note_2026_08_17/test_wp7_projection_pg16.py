"""WP7 · 六阶段投影的 PG16 行为测试(真库、真 SQL、真 quote 隔离)。

纯函数那层已经在 `test_wp7_stage_projection.py` 打过。这里打的是**取数层**:
四条发布链的 SQL 真的把 quote 隔开了吗?列名真的存在吗?

🔴 为什么这层不能省:纯函数测试喂的是我自己构造的 dict,它**验证不了**
   `_ATTEMPTS_SQL` 里写错一个列名、或某一段忘了 quote predicate。
   本仓 2026-08-17 刚吃过一次"夹具自造句柄恒绿、读真实签名才撞见"的亏。
"""
from __future__ import annotations

import os
import pathlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import RealDictCursor  # noqa: E402

from services.publication_stage_sources import (  # noqa: E402
    load_allocated_capacity, load_quote_projection,
)

REPO = pathlib.Path(__file__).resolve().parents[2]
MIG_034 = REPO / "db" / "migration_034_geo_image_note_contract_2026_08_17.sql"
MIG_035 = REPO / "db" / "migration_035_geo_image_note_slot_channel_2026_08_18.sql"
MIG_036 = REPO / "db" / "migration_036_publication_stage_strict_source_2026_08_18.sql"
#: [并车 2026-08-20 · 36 班] 文章包的核实位迁移。并车后投影谓词读
#: public_url_verification_state,夹具库不放它=整片 UndefinedColumn。
MIG_039 = REPO / "scripts" / "migration_publish_records_url_verification_2026_08_19.sql"
PROD_SCHEMA = pathlib.Path(os.getenv(
    "GEOIMG_PROD_SCHEMA_SQL", r"C:/AI-Test/.deploy_toolkit/_geoimg_prodschema_20260817.sql"))
DSN = os.getenv("TEST_DATABASE_URL")

T0 = datetime(2026, 8, 1, tzinfo=timezone.utc)

#: 🔴 [2026-08-20 判据自伤修复] CUTOFF 原来写死成 `2026-08-20T00:00Z`,而好几条夹具行的
#:    `created_at` 吃的是列默认 `NOW()` —— UTC 一过 2026-08-20 00:00,那些行就落到
#:    cutoff **之后**,三条判据(mhz 链 / publish_records 链 / article.quote_id 回落)
#:    **永久红**,而且红因与被测代码毫无关系。实测 2026-08-20 03:24Z 当场爆。
#:
#:    修法两件一起做,缺一不可:
#:      ① CUTOFF **相对 T0**,不再是会过期的绝对日期;
#:      ② 夹具行**显式写 created_at = T0**(见下面各 INSERT)。
#:    只改 ① 是把爆炸时间往后推,不是拆引信 —— 任何吃 `NOW()` 的行都还会随挂钟漂移。
#:
#:    🔴 **判据里不许有"今天"**:一条依赖挂钟的判据,红起来的时间由日历决定,
#:       不由代码决定。下面 `test_no_fixture_row_depends_on_the_wall_clock` 盯着这件事。
CUTOFF = T0 + timedelta(days=19)
URL_A = "https://example.com/a"
URL_B = "https://example.com/b"


def _admin_dsn() -> str:
    return DSN.rsplit("/", 1)[0] + "/postgres"


@pytest.fixture(scope="module")
def db():
    if not DSN or not PROD_SCHEMA.is_file():
        pytest.skip("需要 TEST_DATABASE_URL 与生产 schema 夹具")
    name = f"geoimg_test_wp7_{uuid.uuid4().hex[:8]}"
    admin = psycopg2.connect(_admin_dsn())
    admin.autocommit = True
    admin.cursor().execute(f'CREATE DATABASE "{name}"')
    admin.close()
    dsn = DSN.rsplit("/", 1)[0] + "/" + name
    conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    conn.autocommit = True
    c = conn.cursor()
    c.execute("\n".join(
        line for line in PROD_SCHEMA.read_text(encoding="utf-8", errors="ignore").splitlines()
        if not line.startswith("\\restrict") and not line.startswith("\\unrestrict")))
    # 🔴 生产 pg_dump 里有 set_config('search_path','',false),会毒死同连接后续迁移。
    c.execute("SET search_path = public")
    for mig in (MIG_034, MIG_035, MIG_036, MIG_039):
        c.execute(mig.read_text(encoding="utf-8"))
    # confirmed_keywords.monitoring_product_version 有 FK 指向这张矩阵表,
    # 而 pg_dump 只带 schema 不带数据 —— 不种这一行,任何词都插不进去。
    c.execute("INSERT INTO monitoring_product_platform_matrices (version, platforms) "
              "VALUES ('monitoring-unified5-v1', 'dashscope,deepseek,kimi,doubao') "
              "ON CONFLICT DO NOTHING")
    conn.close()
    try:
        yield dsn
    finally:
        admin = psycopg2.connect(_admin_dsn())
        admin.autocommit = True
        admin.cursor().execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


@pytest.fixture()
def cur(db):
    conn = psycopg2.connect(db, cursor_factory=RealDictCursor)
    conn.autocommit = True
    c = conn.cursor()
    c.execute("SET search_path = public")
    for table in ("media_publications", "mhz_publish_order_items", "mhz_publish_orders",
                  "publish_order_items", "publish_orders", "publish_records",
                  "monitoring_results", "monitoring_tasks", "confirmed_keywords",
                  "articles", "topics", "geo_douyin_posts", "quotes", "brands"):
        c.execute(f"DELETE FROM {table}")
    try:
        yield c
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 夹具构造:一个品牌两张报价
# ---------------------------------------------------------------------------

def _brand(c, brand_id):
    # brands.name 有 (name, owner_user_id) 唯一约束 —— 每次调用给不同名字。
    c.execute("INSERT INTO brands (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
              (brand_id, f"夹具品牌-{brand_id}"))


def _brand_two_quotes(c, brand_id=7001):
    _brand(c, brand_id)
    c.execute("INSERT INTO quotes (id, brand_id, brand_name) VALUES (%s,%s,%s)",
              (501, brand_id, "夹具品牌"))
    c.execute("INSERT INTO quotes (id, brand_id, brand_name) VALUES (%s,%s,%s)",
              (502, brand_id, "夹具品牌"))
    for qid, kw_id, articles in ((501, 9101, 3), (502, 9102, 2)):
        c.execute(
            "INSERT INTO confirmed_keywords (id, quote_id, keyword, required_articles, brand_id) "
            "VALUES (%s,%s,%s,%s,%s)", (kw_id, qid, f"词{qid}", articles, brand_id))
    return 501, 502


def _manual_publication(c, *, pub_id, quote_id, url, published_at, slot=None,
                        retracted_at=None, body_proof=True, article_id=None):
    c.execute(
        """
        INSERT INTO media_publications
            (id, quote_id, article_id, platform_name, platform_url, created_at,
             publish_timestamp, delivery_slot_key, normalized_url, published_at_tz,
             retracted_at, body_proof)
        VALUES (%s,%s,%s,'小红书',%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (pub_id, quote_id, article_id, url, published_at, published_at,
         slot, url, published_at, retracted_at, body_proof))


def _monitoring(c, *, task_id, result_id, quote_id, brand_id, kw_id, tested_at,
                citations=None):
    c.execute(
        "INSERT INTO monitoring_tasks (id, quote_id, client_id, brand_id) "
        "VALUES (%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
        (task_id, quote_id, str(brand_id), brand_id))
    import json
    c.execute(
        """
        INSERT INTO monitoring_results
            (id, task_id, confirmed_keyword_id, keyword, platform, tested_at,
             search_citations, lineage_status, identity_review_state, response_status,
             mention_type)
        VALUES (%s,%s,%s,'词','dashscope',%s,%s,'complete','not_required','ok','none')
        """,
        (result_id, task_id, kw_id, tested_at,
         json.dumps([{"url": u} for u in (citations or [])])))


# ---------------------------------------------------------------------------
# 判据
# ---------------------------------------------------------------------------

def test_two_quotes_of_one_brand_do_not_bleed(cur):
    """规格:「同品牌两 quote:门户/周报/月报/运营/监测在同一 cutoff 读取
    相同阶段元组且**不串**」。"""
    q1, q2 = _brand_two_quotes(cur)
    _manual_publication(cur, pub_id=1, quote_id=q1, url=URL_A, published_at=T0, slot=None,
                        article_id=None)
    _monitoring(cur, task_id=1, result_id=1, quote_id=q1, brand_id=7001, kw_id=9101,
                tested_at=T0 + timedelta(days=1), citations=[URL_A])

    p1 = load_quote_projection(cur, quote_id=q1, cutoff=CUTOFF)
    p2 = load_quote_projection(cur, quote_id=q2, cutoff=CUTOFF)

    assert p1["stages"]["published_active"]["count"] == 1
    assert p1["stages"]["strictly_attributed"]["count"] == 1
    # Q2 完全干净 —— 品牌相同不构成任何继承
    assert p2["stages"]["published_active"]["count"] == 0
    assert p2["stages"]["strictly_attributed"]["count"] == 0


def test_capacity_reads_required_articles_per_quote(cur):
    q1, q2 = _brand_two_quotes(cur)
    assert load_allocated_capacity(cur, q1) == (3, "confirmed_keywords")
    assert load_allocated_capacity(cur, q2) == (2, "confirmed_keywords")


def test_capacity_none_when_quote_has_no_confirmed_keywords(cur):
    _brand(cur, 7002)
    cur.execute("INSERT INTO quotes (id, brand_id, brand_name) VALUES (599, 7002, '空报价')")
    capacity, source = load_allocated_capacity(cur, 599)
    assert capacity is None and source == "not_issued"


def test_capacity_zero_is_preserved_not_bumped_to_one(cur):
    """P0-5 的同一坑:显式 0 篇(覆盖词)必须保留,不许被 `or 1` 吃掉。"""
    _brand(cur, 7003)
    cur.execute("INSERT INTO quotes (id, brand_id, brand_name) VALUES (598, 7003, '零篇报价')")
    cur.execute("INSERT INTO confirmed_keywords (id, quote_id, keyword, required_articles) "
                "VALUES (9199, 598, '覆盖词', 0)")
    capacity, source = load_allocated_capacity(cur, 598)
    assert capacity == 0 and source == "confirmed_keywords"


def test_retracted_publication_leaves_active_but_keeps_occurrence(cur):
    from services.publication_stage_projection import has_published_occurrence

    q1, _ = _brand_two_quotes(cur)
    _manual_publication(cur, pub_id=2, quote_id=q1, url=URL_A, published_at=T0,
                        retracted_at=T0 + timedelta(days=2))
    proj = load_quote_projection(cur, quote_id=q1, cutoff=CUTOFF)
    assert proj["stages"]["published_active"]["count"] == 0
    assert has_published_occurrence(proj) is True


def test_replacement_chain_counts_once_and_does_not_inherit_coverage(cur):
    """A 发布→被监测→撤稿;B 替换刚发布。B 不许继承 A 的 coverage/strict。"""
    q1, _ = _brand_two_quotes(cur)
    slot = str(uuid.uuid4())
    _manual_publication(cur, pub_id=3, quote_id=q1, url=URL_A, published_at=T0, slot=slot,
                        retracted_at=T0 + timedelta(days=5))
    _manual_publication(cur, pub_id=4, quote_id=q1, url=URL_B,
                        published_at=T0 + timedelta(days=6), slot=slot)
    _monitoring(cur, task_id=2, result_id=2, quote_id=q1, brand_id=7001, kw_id=9101,
                tested_at=T0 + timedelta(days=1), citations=[URL_A])
    proj = load_quote_projection(cur, quote_id=q1, cutoff=CUTOFF)
    assert proj["stages"]["published_active"]["count"] == 1     # 同一 slot 只计 1
    assert proj["stages"]["monitored_covered"]["count"] == 0    # 监测早于 B 发布
    assert proj["stages"]["strictly_attributed"]["count"] == 0  # A 的命中不算 B 头上


def test_mhz_chain_is_quote_scoped_through_geo_post(cur):
    """代发链靠 `source_geo_post_id → geo_douyin_posts.quote_id` 定位 quote。"""
    q1, q2 = _brand_two_quotes(cur)
    cur.execute(
        "INSERT INTO geo_douyin_posts (id, brand_id, created_by, status, quote_id, "
        "counts_toward_contract, created_at, updated_at)"
        " VALUES (801, 7001, 1, 'ready', %s, TRUE, %s, %s)", (q1, T0, T0))
    cur.execute("INSERT INTO mhz_publish_orders (id, user_id, article_title) "
                "VALUES (901, 1, '标题')")
    cur.execute(
        "INSERT INTO mhz_publish_order_items "
        "(id, order_id, user_id, media_name, status, publish_url, submitted_at, "
        " published_at, source_geo_post_id, submitted_content_snapshot_hash, "
        " submitted_content_snapshot_at) "
        "VALUES (911, 901, 1, '媒体', 'published', %s, %s, %s, 801, %s, %s)",
        (URL_A, T0, T0, "a" * 64, T0))
    p1 = load_quote_projection(cur, quote_id=q1, cutoff=CUTOFF)
    p2 = load_quote_projection(cur, quote_id=q2, cutoff=CUTOFF)
    assert p1["stages"]["published_active"]["count"] == 1
    assert p1["stages"]["produced_ready"]["count"] == 1
    assert p2["stages"]["published_active"]["count"] == 0


def test_mhz_rejected_and_failed_do_not_count_published(cur):
    q1, _ = _brand_two_quotes(cur)
    cur.execute(
        "INSERT INTO geo_douyin_posts (id, brand_id, created_by, status, quote_id,"
        " created_at, updated_at) VALUES (802, 7001, 1, 'failed', %s, %s, %s)",
        (q1, T0, T0))
    cur.execute("INSERT INTO mhz_publish_orders (id, user_id, article_title) "
                "VALUES (902, 1, '标题')")
    for item_id, state in ((921, "rejected"), (922, "failed"), (923, "awaiting_action")):
        cur.execute(
            "INSERT INTO mhz_publish_order_items "
            "(id, order_id, user_id, media_name, status, publish_url, submitted_at, "
            " source_geo_post_id) VALUES (%s, 902, 1, '媒体', %s, %s, %s, 802)",
            (item_id, state, URL_A, T0))
    proj = load_quote_projection(cur, quote_id=q1, cutoff=CUTOFF)
    assert proj["stages"]["published_active"]["count"] == 0
    assert proj["stages"]["submitted"]["count"] == 1     # 同一 post 三次尝试,去重后 1
    assert proj["stages"]["produced_ready"]["count"] == 0  # status='failed' 不算已做好


def test_publish_records_chain_is_quote_scoped_through_article(cur):
    q1, q2 = _brand_two_quotes(cur)
    cur.execute("INSERT INTO topics (id, quote_id, original_keyword) "
                "VALUES (701, %s, '词')", (q1,))
    cur.execute("INSERT INTO articles (id, topic_id, quote_id, title, content,"
                " created_at, updated_at)"
                " VALUES (601, 701, %s, '标题', '正文', %s, %s)", (q1, T0, T0))
    # [并车 2026-08-20 · 36 班] 核实位翻转后,自报行要**核实过**才算发布事实。
    # 想计数的行显式带 verified(来源=provider_receipt,过库级 CHECK);
    # 再放一行未核实的(302)钉住并车语义:回执成功≠发布事实。
    cur.execute(
        "INSERT INTO publish_records (id, user_id, status, article_id, public_url, "
        " public_url_reported_explicitly, submitted_content_snapshot_hash, created_at, "
        " public_url_verification_state, public_url_verification_source) "
        "VALUES (301, '1', 'success', 601, %s, TRUE, %s, %s, 'verified', 'provider_receipt')",
        (URL_A, "b" * 64, T0))
    cur.execute(
        "INSERT INTO publish_records (id, user_id, status, article_id, public_url, "
        " public_url_reported_explicitly, submitted_content_snapshot_hash, created_at) "
        "VALUES (302, '1', 'success', 601, %s, TRUE, %s, %s)", (URL_A, "c" * 64, T0))
    p1 = load_quote_projection(cur, quote_id=q1, cutoff=CUTOFF)
    p2 = load_quote_projection(cur, quote_id=q2, cutoff=CUTOFF)
    assert p1["stages"]["published_active"]["count"] == 1   # 只有 verified 那一行
    assert p1["stages"]["produced_ready"]["count"] == 1   # 正文非空 = 已做好
    assert p2["stages"]["published_active"]["count"] == 0


def test_article_quote_id_null_falls_back_to_topic_quote(cur):
    """历史行 `articles.quote_id` 为 NULL 时按 topic 的 quote 归属,不猜。"""
    q1, _ = _brand_two_quotes(cur)
    cur.execute("INSERT INTO topics (id, quote_id, original_keyword) "
                "VALUES (702, %s, '词')", (q1,))
    cur.execute("INSERT INTO articles (id, topic_id, quote_id, title, content,"
                " created_at, updated_at)"
                " VALUES (602, 702, NULL, '标题', '正文', %s, %s)", (T0, T0))
    proj = load_quote_projection(cur, quote_id=q1, cutoff=CUTOFF)
    assert proj["stages"]["produced_ready"]["count"] == 1


def test_empty_article_body_is_not_produced(cur):
    """反向对照:上一条不是"有行就算已做好"。"""
    q1, _ = _brand_two_quotes(cur)
    cur.execute("INSERT INTO topics (id, quote_id, original_keyword) "
                "VALUES (703, %s, '词')", (q1,))
    cur.execute("INSERT INTO articles (id, topic_id, quote_id, title, content,"
                " created_at, updated_at)"
                " VALUES (603, 703, %s, '标题', '   ', %s, %s)", (q1, T0, T0))
    proj = load_quote_projection(cur, quote_id=q1, cutoff=CUTOFF)
    assert proj["stages"]["produced_ready"]["count"] == 0


def test_watermark_records_real_row_counts(cur):
    q1, _ = _brand_two_quotes(cur)
    _manual_publication(cur, pub_id=5, quote_id=q1, url=URL_A, published_at=T0)
    proj = load_quote_projection(cur, quote_id=q1, cutoff=CUTOFF)
    assert proj["watermark"]["attempt_rows"] == 1
    assert proj["watermark"]["quote_keyword_count"] == 1


def test_cutoff_is_honoured_by_the_sql_layer(cur):
    q1, _ = _brand_two_quotes(cur)
    _manual_publication(cur, pub_id=6, quote_id=q1, url=URL_A,
                        published_at=T0 + timedelta(days=40))
    early = load_quote_projection(cur, quote_id=q1, cutoff=CUTOFF)
    late = load_quote_projection(cur, quote_id=q1, cutoff=T0 + timedelta(days=60))
    assert early["stages"]["published_active"]["count"] == 0
    assert late["stages"]["published_active"]["count"] == 1


def test_monitoring_pending_identity_is_excluded_by_the_eligibility_predicate(cur):
    """资格谓词真的挂在 SQL 上 —— 而不是只写在文档里。"""
    q1, _ = _brand_two_quotes(cur)
    _manual_publication(cur, pub_id=7, quote_id=q1, url=URL_A, published_at=T0)
    cur.execute("INSERT INTO monitoring_tasks (id, quote_id, client_id, brand_id) "
                "VALUES (5, %s, '7001', 7001)", (q1,))
    import json
    # 🔴 pending 身份行的形态由两条 CHECK 共同约束:必须同时带
    #    response_status='brand_identity_unresolved' + mention_type='pending_identity'
    #    + identity_brand_id + identity_evidence_hash + identity_decision_version=0。
    #    只写 identity_review_state='pending' 插不进去 —— 夹具必须造**合法的**待审行,
    #    否则测的是"插不进去",不是"资格谓词把它排除了"。
    cur.execute(
        "INSERT INTO monitoring_results (id, task_id, confirmed_keyword_id, keyword, "
        " platform, tested_at, search_citations, lineage_status, identity_review_state, "
        " response_status, mention_type, identity_brand_id, identity_evidence_hash, "
        " identity_decision_version) "
        "VALUES (51, 5, 9101, '词', 'dashscope', %s, %s, 'complete', 'pending', "
        " 'brand_identity_unresolved', 'pending_identity', 7001, %s, 0)",
        (T0 + timedelta(days=1), json.dumps([{"url": URL_A}]), "c" * 64))
    proj = load_quote_projection(cur, quote_id=q1, cutoff=CUTOFF)
    assert proj["stages"]["monitored_covered"]["count"] == 0
    assert proj["stages"]["strictly_attributed"]["count"] == 0


def test_migration_036_replay_twice_is_idempotent(db):
    conn = psycopg2.connect(db, cursor_factory=RealDictCursor)
    conn.autocommit = True
    c = conn.cursor()
    c.execute("SET search_path = public")
    try:
        sql = MIG_036.read_text(encoding="utf-8")
        c.execute(sql)
        c.execute(sql)
        c.execute("SELECT COUNT(*) AS n FROM pg_constraint WHERE conname = 'ck_por_strict_scope'")
        assert dict(c.fetchone())["n"] == 1
        c.execute("SELECT column_name FROM information_schema.columns "
                  "WHERE table_name='publish_outcome_records' "
                  "AND column_name LIKE 'strict%%'")
        assert {dict(r)["column_name"] for r in c.fetchall()} == {
            "strict_same_source_citations", "strict_scope", "strict_metric_version"}
    finally:
        conn.close()


def test_strict_scope_check_rejects_brand_form(db):
    conn = psycopg2.connect(db, cursor_factory=RealDictCursor)
    conn.autocommit = True
    c = conn.cursor()
    c.execute("SET search_path = public")
    try:
        c.execute("INSERT INTO publish_outcome_records (strict_scope) VALUES ('quote:77')")
        with pytest.raises(psycopg2.errors.CheckViolation):
            c.execute("INSERT INTO publish_outcome_records (strict_scope) VALUES ('brand:77')")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 监测调度的"已发文"守卫:四条链都算,不只人工登记链
# ---------------------------------------------------------------------------

def _paid_quote_with_monitoring(c, *, quote_id, brand_id, kw_id):
    _brand(c, brand_id)
    c.execute(
        "INSERT INTO quotes (id, brand_id, brand_name, status, monitoring_enabled, "
        " service_start_date, service_end_date, service_status) "
        "VALUES (%s,%s,'夹具','paid',TRUE, CURRENT_DATE - 10, CURRENT_DATE + 30, 'active')",
        (quote_id, brand_id))
    c.execute("INSERT INTO confirmed_keywords (id, quote_id, keyword, is_core, "
              " super_red_ocean, required_articles) VALUES (%s,%s,'词',TRUE,FALSE,1)",
              (kw_id, quote_id))


def _enabled_quote_ids(dsn, monkeypatch) -> set[int]:
    """在**该测试库**上跑真正的 `get_monitoring_enabled_clients` SQL。

    🔴 不用 `importlib.reload`:reload `db.connection` 会把连接池换掉,
       污染同一 pytest 进程里后面所有测试(实测会把整个目录跑挂)。
       改成只把该模块命名空间里的 `get_connection` 换成指向测试库的工厂 ——
       作用域精确、可回滚、不碰别人。
    """
    import db.monitoring_db as mdb

    def _factory():
        conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
        conn.autocommit = True
        return conn

    monkeypatch.setattr(mdb, "get_connection", _factory)
    return {int(r["quote_id"]) for r in mdb.get_monitoring_enabled_clients()}


def test_agency_published_quote_enters_monitoring_schedule(db, cur, monkeypatch):
    """🔴 真缺陷:旧守卫只认 `articles.first_published_at`(仅人工登记链回写)。
    只走代发链发布的客户会被判成"从没发过文",监测**静默不跑**。"""
    _paid_quote_with_monitoring(cur, quote_id=581, brand_id=7101, kw_id=9301)
    cur.execute("INSERT INTO geo_douyin_posts (id, brand_id, created_by, status,"
                " quote_id, created_at, updated_at)"
                " VALUES (881, 7101, 1, 'ready', 581, %s, %s)", (T0, T0))
    cur.execute("INSERT INTO mhz_publish_orders (id, user_id, article_title) "
                "VALUES (981, 1, '标题')")
    cur.execute(
        "INSERT INTO mhz_publish_order_items (id, order_id, user_id, media_name, status, "
        " publish_url, submitted_at, published_at, source_geo_post_id) "
        "VALUES (991, 981, 1, '媒体', 'published', %s, %s, %s, 881)", (URL_A, T0, T0))
    assert 581 in _enabled_quote_ids(db, monkeypatch)


def test_quote_with_no_publication_at_all_stays_out_of_schedule(db, cur, monkeypatch):
    """反向对照:守卫不是被改成恒真了 —— 零 occurrence 的报价必须仍然不进调度。

    规格:「零 occurrence 的 quote 零 monitoring claim/freeze」。
    """
    _paid_quote_with_monitoring(cur, quote_id=582, brand_id=7102, kw_id=9302)
    assert 582 not in _enabled_quote_ids(db, monkeypatch)


def test_manual_publication_also_enters_schedule(db, cur, monkeypatch):
    """人工登记链(老口径唯一认的那条)当然仍要算 —— cutover 不许把老形态弄丢。"""
    _paid_quote_with_monitoring(cur, quote_id=583, brand_id=7103, kw_id=9303)
    _manual_publication(cur, pub_id=61, quote_id=583, url=URL_B, published_at=T0)
    assert 583 in _enabled_quote_ids(db, monkeypatch)


def test_retracted_publication_keeps_the_quote_in_schedule(db, cur, monkeypatch):
    """规格:「撤稿仍保留 occurrence,是否停止监测不由本功能擅改」。

    用 active 做守卫就会让一次撤稿把在跑的监测悄悄停掉 —— 那是业务决定,
    不该由投影替谁拍板。
    """
    _paid_quote_with_monitoring(cur, quote_id=584, brand_id=7104, kw_id=9304)
    _manual_publication(cur, pub_id=62, quote_id=584, url=URL_A, published_at=T0,
                        retracted_at=T0 + timedelta(days=1))
    assert 584 in _enabled_quote_ids(db, monkeypatch)


# ---------------------------------------------------------------------------
# 时区:naive 列必须在 SQL 里按库时区转成 timestamptz
# ---------------------------------------------------------------------------

def test_naive_timestamp_columns_are_cast_in_sql_not_assumed_utc(db, cur):
    """🔴 `monitoring_results.tested_at` / `articles.created_at` 是
    `timestamp WITHOUT time zone`,存的是**库时区的本地时间**;
    `published_at_tz` 已经是 timestamptz。

    两把尺子放在一起比,生产(Asia/Shanghai)会凭空差 8 小时:发布当天的监测
    被判成"发布之前",coverage 与严格归因整片丢,而且**不报错**。
    本判据把库时区改成 Asia/Shanghai 后重跑,结果必须与 UTC 下一致。
    """
    conn = psycopg2.connect(db, cursor_factory=RealDictCursor)
    conn.autocommit = True
    c = conn.cursor()
    c.execute("SET search_path = public")
    c.execute("SET TIME ZONE 'Asia/Shanghai'")
    try:
        for table in ("media_publications", "monitoring_results", "monitoring_tasks",
                      "confirmed_keywords", "quotes", "brands"):
            c.execute(f"DELETE FROM {table}")
        q1, _ = _brand_two_quotes(c)
        # 发布时间落在"UTC 当天早上"—— 若把 naive tested_at 误当 UTC,
        # 这条监测会被判成早于发布 8 小时。
        published = datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc)
        tested = datetime(2026, 8, 10, 3, 0, tzinfo=timezone.utc)
        _manual_publication(c, pub_id=71, quote_id=q1, url=URL_A, published_at=published)
        _monitoring(c, task_id=71, result_id=71, quote_id=q1, brand_id=7001, kw_id=9101,
                    tested_at=tested, citations=[URL_A])
        proj = load_quote_projection(c, quote_id=q1, cutoff=CUTOFF)
        assert proj["stages"]["monitored_covered"]["count"] == 1
        assert proj["stages"]["strictly_attributed"]["count"] == 1
    finally:
        conn.close()


def test_timezone_probe_actually_discriminates(db, cur):
    """反向对照:证明上一条不是"时区怎么设都一样"。

    把监测时间挪到发布**之前** 2 小时 —— 无论库时区如何,它都不该命中。
    两条合起来才能说明尺子既没错位、也不是恒真。
    """
    conn = psycopg2.connect(db, cursor_factory=RealDictCursor)
    conn.autocommit = True
    c = conn.cursor()
    c.execute("SET search_path = public")
    c.execute("SET TIME ZONE 'Asia/Shanghai'")
    try:
        for table in ("media_publications", "monitoring_results", "monitoring_tasks",
                      "confirmed_keywords", "quotes", "brands"):
            c.execute(f"DELETE FROM {table}")
        q1, _ = _brand_two_quotes(c)
        published = datetime(2026, 8, 10, 5, 0, tzinfo=timezone.utc)
        tested = datetime(2026, 8, 10, 3, 0, tzinfo=timezone.utc)
        _manual_publication(c, pub_id=72, quote_id=q1, url=URL_A, published_at=published)
        _monitoring(c, task_id=72, result_id=72, quote_id=q1, brand_id=7001, kw_id=9101,
                    tested_at=tested, citations=[URL_A])
        proj = load_quote_projection(c, quote_id=q1, cutoff=CUTOFF)
        assert proj["stages"]["monitored_covered"]["count"] == 0
        assert proj["stages"]["strictly_attributed"]["count"] == 0
    finally:
        conn.close()


def _timestamp_columns_of(table: str) -> set:
    """从**生产 schema dump** 里取这张表真实有哪些时间列。

    🔴 不写死清单:`publish_records` / `media_publications` **没有** `updated_at`,
       一刀切要求它就是假红(第一版正是这样把两条合规的夹具判红了)。
       "必需什么"必须由**表的实有列**决定,而不是由我记得什么决定。
    """
    body = PROD_SCHEMA.read_text(encoding="utf-8", errors="ignore")
    marker = "CREATE TABLE public." + table + " ("
    if marker not in body:
        return set()
    chunk = body[body.index(marker):]
    chunk = chunk[:chunk.index(");")]
    return {c for c in ("created_at", "updated_at") if (" " + c + " ") in chunk}


def _missing_timestamp_columns(table: str, cols_text: str) -> list:
    """这条 INSERT 漏了该表哪些时间列。**扫描循环与毒探针共用这一处**。

    🔴 单一出处不是洁癖:第一版毒探针自己算 `_timestamp_columns_of(...)`,
       没走扫描循环那一行 —— 于是把那一行弱化成"只要求 created_at"(变异 T3)
       照样全绿。**判据必须驱动被改的那一行,而不是自己构造它的输出。**
    """
    flat = cols_text.replace('"', " ").replace("'", " ")
    return sorted(c for c in _timestamp_columns_of(table) if c not in flat)


def test_no_fixture_row_depends_on_the_wall_clock():
    """🔴 **判据自伤锁**:夹具行不许把时间列交给列默认 `NOW()`。

    2026-08-20 03:24Z 实测:三条判据因为挂钟越过写死的 CUTOFF 而**永久红**,
    红因与被测代码毫无关系 —— 那种红比绿更糟,它会让下一棒去查一个不存在的缺陷。

    本锁扫本文件里所有插进**受 cutoff 过滤**的表的 INSERT,要求它显式列出
    该表**实有的**时间列(`created_at` / `updated_at`)。

    🔴 为什么 `updated_at` 也算:投影取的是 `ready_at or created_at`,
       而 `ready_at` 就是 `updated_at`(`_PRODUCED_SQL`)—— 第一版只钉 created_at,
       三条判据**仍然红**,是实跑出来才发现的。
    """
    import re

    src = pathlib.Path(__file__).read_text(encoding="utf-8", errors="ignore")
    src = chr(10).join(l for l in src.splitlines() if not l.strip().startswith("#"))
    cutoff_tables = ("geo_douyin_posts", "articles", "publish_records",
                     "media_publications")
    offenders, scanned = [], 0
    for m in re.finditer(r"INSERT INTO\s+(\w+)\s*\(([^)]*)\)", src, re.S):
        table, cols = m.group(1), m.group(2)
        if table not in cutoff_tables:
            continue
        scanned += 1
        missing = _missing_timestamp_columns(table, cols)
        if missing:
            line = src[:m.start()].count(chr(10)) + 1
            offenders.append("%s(缺 %s)@ 第 %d 行附近" % (table, "/".join(missing), line))
    assert not offenders, (
        "这些夹具行把时间列交给了列默认 NOW() —— 挂钟一过 CUTOFF 判据就永久红:"
        + str(offenders))
    assert scanned >= 6, (
        "锁只扫到 " + str(scanned) + " 条 INSERT,正则可能写坏了 —— 零分母的锁和恒绿一样废")
    # 毒化自证:锁真的抓得住一条"忘了写时间列"的 INSERT。
    # 🔴 探针**拼出来**,不写成整串字面量 —— 写成字面量的话它自己就会被上面那轮扫描
    #    判成 offender(第一版正是这样:锁把自己的毒判红了,而不是把被测代码判红)。
    probe = "INSERT " + "INTO articles (id, topic_id, title, content) VALUES (1,2,'a','b')"
    m = re.search(r"INSERT INTO\s+(\w+)\s*\(([^)]*)\)", probe)
    assert m and _missing_timestamp_columns("articles", m.group(2)), (
        "锁是死的 —— 连一条明显没写时间列的 INSERT 都抓不住")
    # 🔴 第二发毒:**只缺 `updated_at`**。没有这一发,把上面 `missing` 那句弱化成
    #    "只要求 created_at" 是杀不掉的(变异 T3 实测存活)——
    #    弱化一把锁,只有拿一个**恰好违反被弱化那一半**的样本才验得出来。
    probe2 = "INSERT " + ("INTO articles (id, topic_id, title, content, created_at)"
                          " VALUES (1,2,'a','b',now())")
    m2 = re.search(r"INSERT INTO\s+(\w+)\s*\(([^)]*)\)", probe2)
    assert m2 and _missing_timestamp_columns("articles", m2.group(2)) == ["updated_at"], (
        "锁放过了「写了 created_at 但漏了 updated_at」那一格 —— "
        "而投影取的正是 `ready_at(=updated_at) or created_at`,漏它照样随挂钟红")


def test_the_cutoff_is_defined_relative_to_t0():
    """CUTOFF 必须**相对 T0 定义** —— 绝对日期只是把爆炸时间往后推。

    🔴 这一条只能打**源码形态**,不能打值。第一版写的是
       `assert CUTOFF == T0 + timedelta(days=19)` —— 而 `2026-08-01 + 19 天`
       **就是** `2026-08-20`,把它改回写死的绝对日期,这个断言照样成立
       (变异 T1 实测存活)。任何相对表达式都会求值成某个绝对时刻,
       所以"是不是相对写的"这件事,值比较**永远**证明不了。
    """
    import re

    src = pathlib.Path(__file__).read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"^CUTOFF\s*=\s*(.+)$", src, re.M)
    assert m, "找不到 CUTOFF 的定义行"
    expr = m.group(1).strip()
    assert expr.startswith("T0 +") or expr.startswith("T0+"), (
        "CUTOFF 又变回不依赖 T0 的写法了(挂钟一过它就永久红):" + expr)
    assert "datetime(" not in expr, ("CUTOFF 里出现了绝对日期字面量:" + expr)
    assert CUTOFF > T0
