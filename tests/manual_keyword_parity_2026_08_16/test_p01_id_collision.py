# -*- coding: utf-8 -*-
"""P0-1 串词反例 —— 两表 id 各自独立,订阅必须带 source 才认得出是哪一个词。

引爆条件:`extra_keywords.id` 与 `confirmed_keywords.id` 号段一旦重叠,
只按 keyword_id 查订阅就会把**另一种词**的订阅认成自己的 —— 取词去跑时
拿到的是错客户的问法(串词),扣费也记到错的订阅上。
今天两表 id 零重叠 = 哑弹;号段涨到重叠就引爆。
"""
from __future__ import annotations
import psycopg2


def _mk(cur, kid: int):
    """在**同一个 id** 上各造一条 confirmed 词和一条 extra 词。"""
    cur.execute("INSERT INTO users (id, username, password_hash, display_name) VALUES "
                "(990001,'p01_owner','x','P01 Owner') ON CONFLICT (id) DO NOTHING")
    cur.execute("INSERT INTO brands (id, name, owner_user_id) VALUES "
                "(990001,'P01品牌',990001) ON CONFLICT (id) DO NOTHING")
    cur.execute("INSERT INTO quotes (id, brand_name, brand_id, status) VALUES "
                "(990001,'P01品牌',990001,'paid') ON CONFLICT (id) DO NOTHING")
    cur.execute(
        "INSERT INTO confirmed_keywords (id, quote_id, keyword, monitoring_query, is_monitored) "
        "VALUES (%s,990001,'合同词_P01','合同词的问法', TRUE)", (kid,))
    cur.execute(
        "INSERT INTO extra_keywords (id, client_id, quote_id, brand_id, keyword, target_brand, "
        "monitoring_query, status, is_monitored) VALUES "
        "(%s,990001,990001,990001,'手动词_P01','P01品牌','手动词的问法','active',TRUE)",
        (kid,))
    for src in ("confirmed", "extra"):
        cur.execute(
            "INSERT INTO keyword_monitor_subscriptions "
            "(user_id, keyword_id, quote_id, brand_id, status, daily_points, "
            " feature_code, keyword_source) VALUES "
            "(990001,%s,990001,990001,'active',130,'monitoring_keyword_daily',%s)",
            (kid, src))


def test_P01_id_collision_does_not_swap_keywords(conn):
    cur = conn.cursor()
    _mk(cur, 990001)

    from db.monitoring_db import list_active_subscriptions  # noqa
    cur.execute("""
        SELECT s.keyword_source, ek.keyword AS extra_kw, ck.keyword AS conf_kw
          FROM keyword_monitor_subscriptions s
          LEFT JOIN extra_keywords ek
                 ON ek.id = s.keyword_id AND s.keyword_source = 'extra'
          LEFT JOIN confirmed_keywords ck
                 ON ck.id = s.keyword_id AND s.keyword_source = 'confirmed'
         WHERE s.keyword_id = 990001 AND s.status = 'active'
         ORDER BY s.keyword_source
    """)
    rows = cur.fetchall()
    assert len(rows) == 2, f"夹具没造齐(判据会恒真):{rows}"
    got = {r["keyword_source"]: (r["conf_kw"], r["extra_kw"]) for r in rows}
    assert got["confirmed"] == ("合同词_P01", None), f"合同侧串词:{got['confirmed']}"
    assert got["extra"] == (None, "手动词_P01"), f"手动侧串词:{got['extra']}"
    conn.rollback()


def test_P01_unique_index_is_per_source(conn):
    """同一个 id 上两种 source 各一条 active 订阅必须**都能存在**。

    反向:旧唯一索引 uniq_kms_keyword_active 只按 keyword_id → 第二条会被拒。
    """
    cur = conn.cursor()
    _mk(cur, 990002)   # 不抛 UniqueViolation 就是通过
    cur.execute("SELECT count(*) c FROM keyword_monitor_subscriptions "
                "WHERE keyword_id=990002 AND status='active'")
    assert cur.fetchone()["c"] == 2
    conn.rollback()


def test_P01_same_source_still_blocked(conn):
    """🔴 反向对照:唯一性没有被放松 —— 同 source 重复插仍必须被唯一索引拒掉。

    只证明「两种 source 能共存」是不够的:那也可能是唯一索引整个失效了。
    """
    cur = conn.cursor()
    _mk(cur, 990003)
    try:
        cur.execute(
            "INSERT INTO keyword_monitor_subscriptions "
            "(user_id, keyword_id, quote_id, brand_id, status, daily_points, "
            " feature_code, keyword_source) VALUES "
            "(990001,990003,990001,990001,'active',130,'monitoring_keyword_daily','extra')")
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return
    raise AssertionError("同 source 重复订阅被放行 = 唯一索引失效,并发兜底(UniqueViolation)也随之失效")


def test_P01b_selfhealing_ddl_does_not_undo_the_migration(committed):
    """🔴 自愈 DDL 不许把迁移撤销回去。

    病史:036 迁移 DROP 掉只按 keyword_id 的 uniq_kms_keyword_active,
    而 init_monitoring_tables() 每次应用启动都无条件重建它 —— 迁移被静默撤销,
    下一次部署后"给手动词建订阅"就会撞上同 id 合同词的订阅报 UniqueViolation。
    这条是被 P0-1 串词用例**实测**抓到的,不是看代码看出来的。
    """
    import db.monitoring_db as MDB
    MDB.init_monitoring_tables()

    cur = committed.cursor()
    cur.execute("""
        SELECT indexname FROM pg_indexes
         WHERE tablename = 'keyword_monitor_subscriptions'
           AND indexname IN ('uniq_kms_keyword_active','uniq_kms_keyword_source_active')
    """)
    names = {r["indexname"] for r in cur.fetchall()}
    assert "uniq_kms_keyword_source_active" in names, \
        "带 source 维度的唯一索引不在 —— 建订阅的并发兜底失效"
    assert "uniq_kms_keyword_active" not in names, \
        "init_monitoring_tables 把只按 keyword_id 的旧唯一索引重建回来了 = 自愈 DDL 撤销了迁移"
