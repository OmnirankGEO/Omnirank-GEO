# -*- coding: utf-8 -*-
"""WO_225-c1'' —— Review 2026-09-15 复审 4 发存活毒,各配一把锁 + 两条新增。

存活毒与对应的锁:
  (a) coverage 50000 → 30000 仍绿 ... test_seed_v1_pins_the_owner_ruling_five_times
  (b) DEFAULT_PERSPECTIVE → portal  ... test_default_perspective_is_self_media_end_to_end
  (c) POST_COUNTS_AS 1 → 5 仍绿 ..... test_image_note_done_is_a_post_count_not_a_converted_one
  (d) NULL 桶 bps 毒 ................ 冗余目标,不另立锁;短路点写进 `bps_for` 抬头,
                                      折算那条腿由 test_legacy_quote_reading_equals_plain_count
                                      的反向对照(先盖真桶再读)覆盖。
新增:
  · 项目级合计 ..................... test_project_totals_come_from_the_same_rounding_path
  · SAVEPOINT 探针 ................. test_no_savepoint_is_issued_on_an_autocommit_connection

🔴 这四发能活下来的共同根因:**作者的毒来自作者的判据**。
   我 8 发毒打的全是自己已经钉过的面,所以全红;复审从「抬头承诺的每一样」出发,
   打到了三处「承诺了但没人钉」的:Owner 的数值、默认值那条腿、done 的单位。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services import media_slot_conversion as M            # noqa: E402


# ════════════════════════════════════════════════════════════════
# (a) Owner 的「5 倍」必须被字面量钉死 + ensure_seeded 真写进库那三个数
# ════════════════════════════════════════════════════════════════

OWNER_RULED = {
    M.BUCKET_ANCHOR: 10000,      # 重点媒体:一槽一条
    M.BUCKET_COVERAGE: 50000,    # 行业与平台覆盖:一槽约 5 条(Owner「以 5 倍来计算」)
    M.BUCKET_DOUYIN: 50000,      # 抖音图文:Owner 与自媒体同列「低价媒体」,同取 5
}


def _dsn() -> str:
    raw = os.environ.get("TEST_DATABASE_URL") or os.environ["DATABASE_URL"]
    return raw.split("?", 1)[0]


def test_seed_v1_pins_the_owner_ruling_five_times(cur):
    """🔴 (a) 存活毒:`coverage 50000 → 30000` 全绿。

    根因:没有一条判据说过那个数**是多少** —— 所有判据都从被测常量推导,
    于是常量改了,判据跟着改,数值等于没钉(本仓记过的同形)。

    两条腿分开测,缺一不可:
      · 字面量腿 —— `SEED_V1` 就是 Owner 2026-09-15 ① 拍的那三个数;
      · 落库腿   —— `ensure_seeded()` 写进 DB 的确实是这三个数。
        只有字面量腿的话,把播种函数改成写死 10000 不会红。
    """
    import psycopg2
    import psycopg2.extras

    assert M.SEED_V1 == OWNER_RULED, (
        "SEED_V1 不等于 Owner 2026-09-15 ① 拍的数。改这三个数 = 改所有单的默认交付条数,"
        "属商业口径、要 Owner 点头,不是改完把这条判据跟着改。实得:%s" % M.SEED_V1)
    assert M.ONE_SLOT_BPS == 10000, M.ONE_SLOT_BPS

    # 落库腿:在一个**会回滚**的事务里清掉 v1、重播、读回 —— 不污染库。
    # `ensure_seeded(conn=...)` 传了连接就不 commit(own=False),正好给回滚用。
    conn = psycopg2.connect(_dsn(), cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    try:
        c = conn.cursor()
        c.execute("DELETE FROM media_slot_conversion_versions WHERE version = 1")
        c.execute("SELECT COUNT(*) AS n FROM media_slot_conversion_versions WHERE version = 1")
        assert int(dict(c.fetchone())["n"]) == 0, "没清干净,下面读到的是旧行,这条腿是空的"
        M.ensure_seeded(conn)
        c.execute("SELECT bucket, posts_per_slot_bps FROM media_slot_conversion_versions "
                  " WHERE version = 1")
        got = {r["bucket"]: int(r["posts_per_slot_bps"]) for r in (c.fetchall() or [])}
        assert got == OWNER_RULED, "ensure_seeded 写进库的不是 Owner 拍的那三个数:%s" % got
    finally:
        conn.rollback()          # 🔴 一定回滚:v1 是别的判据与开发库共用的
        conn.close()

    # 回滚真的生效:库里 v1 还在,且还是那三个数
    cur.execute("SELECT bucket, posts_per_slot_bps FROM media_slot_conversion_versions "
                " WHERE version = 1")
    still = {r["bucket"]: int(r["posts_per_slot_bps"]) for r in (cur.fetchall() or [])}
    assert still == OWNER_RULED, "回滚没生效,判据把库改了:%s" % still


# ════════════════════════════════════════════════════════════════
# (b) 不传口径走的默认,必须端到端等于自媒体换算
# ════════════════════════════════════════════════════════════════

def test_default_perspective_is_self_media_end_to_end(cur, make_quote):
    """🔴 (b) 存活毒:`DEFAULT_PERSPECTIVE → portal` 全绿。

    根因:原判据只在**显式传口径**时钉数,默认那条腿没人走。
    而 Owner 2026-09-15 ③ 定的正是「写作中心默认出现换算后的条数」——
    默认值本身就是那条裁定,不是实现细节。

    三段都钉:常量 → 不传参的 `plan_media_mix` → 不传参录进快照后的 `planned_posts_default`。
    """
    from db.diagnosis_db import get_writing_project_detail
    from services.quote_media_mix import plan_media_mix, record_media_mix_snapshot

    assert M.DEFAULT_PERSPECTIVE == M.PERSPECTIVE_SELF_MEDIA, (
        "默认口径不是自媒体 —— Owner ③「写作中心默认出现换算后的条数」落不了地:%s"
        % M.DEFAULT_PERSPECTIVE)

    payload = plan_media_mix(7, target_engines=["deepseek"])      # 不传 perspective
    assert payload["delivery_perspective"] == M.PERSPECTIVE_SELF_MEDIA, payload
    assert payload["posts_estimate_total"] == 35, payload

    # 端到端:不传口径录快照 → 每词派生读数必须是 35
    qid = make_quote(80, perspective=None, required=7)
    cur.execute("INSERT INTO quote_pricing_snapshots (quote_id, brand_id, "
                "selection_session_id, version, reason, calculation_version, "
                "pricing_snapshot, snapshot_hash) VALUES (%s,%s,%s,1,'base','t',%s,%s)",
                (qid, qid, qid, json.dumps({"keywords": []}), "0" * 64))
    rec = record_media_mix_snapshot(qid, payload, actor_user_id=1)
    assert rec.get("recorded") is True, rec

    kw = (get_writing_project_detail(qid).get("keywords") or [{}])[0]
    assert kw.get("planned_posts_default") == 35, (
        "不传口径时每词默认条数不是 35 —— 默认口径那条腿断了:%s"
        % kw.get("planned_posts_default"))


# ════════════════════════════════════════════════════════════════
# (c) 图文的 done 是帖数,不许被换算系数乘一遍
# ════════════════════════════════════════════════════════════════

def test_image_note_done_is_a_post_count_not_a_converted_one():
    """🔴 (c) 存活毒:`POST_COUNTS_AS 1 → 5` 全绿。

    根因:原来 12 条用例的 `done` 全是 0,乘任何系数都还是 0 ——
    `0 × k == 0` 让这条腿在数据上不可见(同形:夹具不真实,毒该抓的缺陷被藏起来)。

    换算只作用在 **quota**(槽 → 条);`done` 来自 `geo_douyin_posts` 的行数,本来就是条。
    乘在 done 上等于把方向倒过来:7 槽的单做 2 条帖就会显示「做完了」。
    """
    from services.geo_douyin.content_plan import (
        PLAN_MAX_PER_KEYWORD, POST_COUNTS_AS, _plan_rows)

    assert POST_COUNTS_AS == 1, (
        "POST_COUNTS_AS 不是 1 —— 一条图文帖就不再等于一条内容,done 的单位被改了。"
        "换算要乘在 quota 侧,理由见该常量抬头(工单 §8.7 原文方向写反,已订正)")

    rows = [{"keyword": "判据词", "required_articles": 7,
             "confirmed_keyword_id": 11, "quote_id": 700}]
    POSTS = 10
    p = _plan_rows(rows, {("ck", 11): POSTS}, {700: 50000})[0]
    assert p.quota == 35, "quota 必须是换算后的条数(7 槽 × 5):%s" % p.quota
    assert p.done == POSTS, "done 必须逐字等于帖数,不许乘系数:%s != %s" % (p.done, POSTS)
    assert p.gap == p.quota - POSTS == 25, (p.quota, p.done, p.gap)
    assert p.suggested == min(p.gap, PLAN_MAX_PER_KEYWORD), p.suggested

    # 反向对照:done=0 那档必须仍成立(否则上面几条可能只是换了个恒等式)
    zero = _plan_rows(rows, {}, {700: 50000})[0]
    assert zero.done == 0 and zero.gap == 35, (zero.done, zero.gap)


# ════════════════════════════════════════════════════════════════
# 新增 · 项目级合计只有一条取整路径
# ════════════════════════════════════════════════════════════════

def test_project_totals_come_from_the_same_rounding_path(cur, make_quote):
    """🔴 项目级合计 = **逐词值之和**,不是「先求和再取整」。

    两者在 bps 非整数倍时会差:bps=45000、两个 7 槽的词 ⇒
    逐词 `round_half_up(31.5)` = 32,和 = 64;先求和再取整 = `round_half_up(63.0)` = 63。
    前端若自己 `Σ round(...)` 而顶栏显示后者,**同一屏上两个数差 1**,
    而没有任何东西会报错。所以只有一条取整路径,合计由服务端给。
    """
    from db.diagnosis_db import get_writing_project_detail

    # ① 整数倍档(coverage 50000):7 槽 ⇒ 35 条,两个词 ⇒ 70
    q_int = make_quote(81, perspective=M.PERSPECTIVE_SELF_MEDIA, required=7, keywords=2)
    d = get_writing_project_detail(q_int)
    per = [int(k["planned_posts_default"]) for k in d["keywords"]]
    assert per == [35, 35], per
    assert d["total_planned_posts_default"] == sum(per) == 70, d["total_planned_posts_default"]
    assert d["total_required_articles"] == 14, d["total_required_articles"]

    # ② 非整数倍档(coverage 45000):逐词 32,和 64;先求和再取整会得 63
    V = 998
    for bucket, bps in ((M.BUCKET_ANCHOR, 10000), (M.BUCKET_COVERAGE, 45000),
                        (M.BUCKET_DOUYIN, 45000)):
        cur.execute(
            "INSERT INTO media_slot_conversion_versions "
            "(version, bucket, posts_per_slot_bps, created_by, note) "
            "VALUES (%s,%s,%s,'lock','judge') ON CONFLICT (version, bucket) "
            "DO UPDATE SET posts_per_slot_bps = EXCLUDED.posts_per_slot_bps",
            (V, bucket, bps))
    try:
        q_frac = make_quote(82, perspective=M.PERSPECTIVE_SELF_MEDIA, required=7,
                            keywords=2, conversion_version=V)
        d2 = get_writing_project_detail(q_frac)
        per2 = [int(k["planned_posts_default"]) for k in d2["keywords"]]
        assert per2 == [32, 32], "7 槽 × 4.5 = 31.5,四舍五入必须是 32:%s" % per2
        assert d2["total_planned_posts_default"] == sum(per2) == 64, (
            "合计不等于逐词之和 —— 说明合计走了另一条取整路径"
            "(先求和再取整会是 63):%s" % d2["total_planned_posts_default"])
        assert d2["total_planned_posts_default"] != 63, (
            "合计正好是「先求和再取整」的那个数,两条路径已经分叉")
    finally:
        cur.execute("DELETE FROM media_slot_conversion_versions WHERE version = %s", (V,))


# ════════════════════════════════════════════════════════════════
# 新增 · autocommit 连接上一条 SAVEPOINT 都不许发
# ════════════════════════════════════════════════════════════════

class _SpyCursor:
    """把执行过的 SQL 全收下来。读那一句必失败 —— 本锁测的是**失败前后发了什么**。"""

    def __init__(self, autocommit: bool):
        self.sql: list = []

        class _Conn:
            pass

        self.connection = _Conn()
        self.connection.autocommit = autocommit

    def execute(self, sql, params=None):
        self.sql.append(str(sql))
        if "SAVEPOINT" in str(sql).upper():
            return None
        raise RuntimeError("表不存在(模拟读失败)")

    def fetchone(self):
        return None


def test_no_savepoint_is_issued_on_an_autocommit_connection():
    """🔴 autocommit 连接上**一条 SAVEPOINT 都不许发**(Review 2026-09-15 复审指出)。

    改之前是 try SAVEPOINT / except 回落 —— 接得住,但每次调用多一个往返、
    多一行 PG ERROR(`SAVEPOINT can only be used in transaction blocks`)。
    一堆良性 ERROR 会盖掉真 ERROR,这本身就是本仓记过的病。

    用 spy 游标把**所有**执行过的 SQL 收下来比对,不看日志
    ——「日志里没有」不是证据,发没发才是。
    """
    auto = _SpyCursor(autocommit=True)
    try:
        M._read_with_cursor(auto, 1)
    except Exception:
        pass
    assert auto.sql, "一条 SQL 都没发 —— 这条锁测的是空气"
    assert not [s for s in auto.sql if "SAVEPOINT" in s.upper()], (
        "autocommit 连接上仍然发了 SAVEPOINT:%s" % auto.sql)

    # 反向对照:非 autocommit 必须**发** SAVEPOINT,否则失败会溢出到调用方事务
    txn = _SpyCursor(autocommit=False)
    try:
        M._read_with_cursor(txn, 1)
    except Exception:
        pass
    ups = [s.upper() for s in txn.sql]
    assert any(u.startswith("SAVEPOINT") for u in ups), txn.sql
    assert any("ROLLBACK TO SAVEPOINT" in u for u in ups), (
        "读失败了却没有 ROLLBACK TO SAVEPOINT —— 隔离没兑现:%s" % txn.sql)


def test_null_required_articles_counts_as_one_on_both_sides(make_quote):
    """🔴 `required_articles` 为 NULL 时,**槽与条都按 1**,两个合计不许对不上。

    这处不一致是本窗口自查出来的,不在复审清单里:
    我原来把 `total_required_articles` 写成 `int(... or 0)` ⇒ NULL 计 0,
    而 **同名字段在列表端点**(`get_writing_projects`)是
    `COALESCE(SUM(COALESCE(required_articles, 1)), 0)` ⇒ NULL 计 **1**;
    同时本函数自己的 `planned_posts_default` 已按 NULL⇒1(复刻
    `writing.keyword_topic_generator._required_article_count` 的老语义)。
    结果是同一个词「槽 0 条 1」,而且同一个字段名在两个端点上给两个数。

    NULL 与显式 0 是**两种语义**,所以两档都钉:
      · NULL      ⇒ 槽 1 / 条 1(老行为,不是「没买」)
      · 显式 0    ⇒ 槽 0 / 条 0(覆盖词:客户没买这个词的内容量,不许抬成 1)
    """
    from db.diagnosis_db import get_writing_project_detail

    q_null = make_quote(83, perspective=None, required=None, keywords=2)
    d = get_writing_project_detail(q_null)
    assert all(k.get("required_articles") is None for k in d["keywords"]), (
        "夹具没写成 NULL,这条锁测的是别的东西:%s"
        % [k.get("required_articles") for k in d["keywords"]])
    assert [int(k["planned_posts_default"]) for k in d["keywords"]] == [1, 1], d["keywords"]
    assert d["total_required_articles"] == 2, (
        "NULL 的槽没按 1 算 —— 与列表端点同名字段对不上:%s"
        % d["total_required_articles"])
    assert d["total_planned_posts_default"] == 2, d["total_planned_posts_default"]

    # 反向对照:**显式 0** 必须保持 0,不许被上面那条 NULL 规则一起抬成 1
    q_zero = make_quote(84, perspective=None, required=0, keywords=2)
    d0 = get_writing_project_detail(q_zero)
    assert [int(k["planned_posts_default"]) for k in d0["keywords"]] == [0, 0], d0["keywords"]
    assert d0["total_required_articles"] == 0, (
        "显式 0 被抬成了 1 —— 会给客户没买的词凭空造出缺口:%s"
        % d0["total_required_articles"])
    assert d0["total_planned_posts_default"] == 0, d0["total_planned_posts_default"]


class _NoConnCursor:
    """拿不到 `connection` 的游标(包装层 / 桩 / 将来换驱动都可能这样)。"""

    def __init__(self, raising: bool = False):
        self.sql: list = []
        self._raising = raising
        if not raising:
            return

    @property
    def connection(self):
        if self._raising:
            raise RuntimeError("读 connection 就炸")
        raise AttributeError("connection")

    def execute(self, sql, params=None):
        self.sql.append(str(sql))
        if "SAVEPOINT" in str(sql).upper():
            return None
        raise RuntimeError("表不存在(模拟读失败)")

    def fetchone(self):
        return None


def test_probe_falls_back_to_sending_the_savepoint_when_it_cannot_tell():
    """🔴 探不到连接时,**回落方向必须是「发」**,不是「不发」。

    这条腿原来没人走 —— 另外两条 spy 游标都带 `.connection`。
    Review 2026-09-15 的注毒清单里正有「探不到连接时不发」这一发,
    按当时的判据它会**存活**;本锁就是把那个方向钉死。

    两种后果不对称,所以回落只能朝一边倒:
      · 多发一条 SAVEPOINT —— 最坏是一行 PG ERROR(autocommit 时);
      · 该发没发 —— 最坏是**把调用方整条事务打废**,端点全线 503。
    「拿不到就当不需要」正是那种读起来最省事、代价最大的回落。
    """
    for label, cursor in (("attribute 缺失", _NoConnCursor()),
                          ("读属性抛异常", _NoConnCursor(raising=True))):
        assert M._needs_savepoint(cursor) is True, (
            "探不到连接(%s)时判成「不需要 SAVEPOINT」—— 回落方向反了" % label)
        try:
            M._read_with_cursor(cursor, 1)
        except Exception:
            pass
        ups = [s.upper() for s in cursor.sql]
        assert any(u.startswith("SAVEPOINT") for u in ups), (
            "探不到连接(%s)时没发 SAVEPOINT:%s" % (label, cursor.sql))
        assert any("ROLLBACK TO SAVEPOINT" in u for u in ups), (
            "发了 SAVEPOINT 却没在失败后 ROLLBACK TO —— 隔离没兑现(%s):%s"
            % (label, cursor.sql))
