# -*- coding: utf-8 -*-
"""WO_225-c1 §8.8 的锁。每一条都先注毒证过红(证据见交付单)。

对应关系(工单 §8.8 八条 + Review 追加的结构锁):
  L1 价格不变 ....................... test_price_does_not_move_when_bps_moves
                                      test_pricing_never_imports_the_conversion_table
  L2 老单读数逐字等于 COUNT(topics) ... test_legacy_quote_reading_equals_plain_count
  L3 客户端点无换算键 ................ test_customer_payload_carries_no_conversion_key
  L4 未付款不出写类动作 .............. test_unpaid_emits_no_write_action
  L5 已付款容量 0 仍出写类动作 ....... test_paid_over_capacity_still_writes
  L6 planned 随口径变 ................ test_planned_posts_default_follows_perspective
  L7 元锁仍绿 ........................ test_article_count_meta_lock_still_green
  L8 结构锁:状态词表 ................ test_status_vocabulary_is_complete
                                      test_status_scanner_shouts_when_it_cannot_parse
"""
from __future__ import annotations

import importlib
import io
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services import article_capacity_contract as cc            # noqa: E402
from services import media_slot_conversion as M                 # noqa: E402


# ════════════════════════════════════════════════════════════════
# L1 · 价格不变
# ════════════════════════════════════════════════════════════════

def _set_bps(cur, version: int, bps: int) -> None:
    for bucket in M.BUCKETS:
        cur.execute(
            "INSERT INTO media_slot_conversion_versions "
            "(version, bucket, posts_per_slot_bps, created_by, note) "
            "VALUES (%s,%s,%s,'lock','judge') "
            "ON CONFLICT (version, bucket) DO UPDATE SET posts_per_slot_bps=EXCLUDED.posts_per_slot_bps",
            (version, bucket, bps))


def test_price_does_not_move_when_bps_moves(cur):
    """🔴 方案甲:客户价一分不动。把换算系数推到荒谬值,任一售价变即红。

    Owner 2026-09-15 ② 原话「不变」。价 = 槽数 × 单篇成本 × 系数,
    与「一个槽按哪类媒体发几条」**无关**。
    """
    from tools.transparent_pricing import calculate_transparent_price, generate_tiered_quotes

    before = [calculate_transparent_price(n) for n in (3, 17, 41)]
    before_tiers = [generate_tiered_quotes(n) for n in (3, 17)]

    # 推一个新版本,系数荒谬到一眼能看出影响(9 条帖填一个槽)
    _set_bps(cur, 999, 90000)
    try:
        assert M.current_version() == 999, "新版本没生效,这条锁测的是空气"
        table = dict(M.resolve_for_quote(None)["bps"])
        assert set(table.values()) == {90000}, table
        after = [calculate_transparent_price(n) for n in (3, 17, 41)]
        after_tiers = [generate_tiered_quotes(n) for n in (3, 17)]
    finally:
        cur.execute("DELETE FROM media_slot_conversion_versions WHERE version=999")

    for b, a in zip(before, after):
        assert b["selling_price"] == a["selling_price"], (b, a)
        assert b["total_cost"] == a["total_cost"], (b, a)
        assert b["required_articles"] == a["required_articles"], (b, a)
    assert before_tiers == after_tiers, "分档报价随换算系数变了(方案甲被破坏)"


def test_pricing_never_imports_the_conversion_table():
    """🔴 结构臂:价链**不许**依赖换算表 —— 连传递依赖都不许。

    只有行为臂的话,今天不读表所以恒绿;哪天有人把表读进价链,
    行为臂只有在那个取值恰好改变售价时才红。结构臂把「有没有这条路」钉死。
    """
    import ast as _ast

    seen, stack = set(), ["tools.transparent_pricing", "tools.pricing_bands"]
    while stack:
        mod = stack.pop()
        if mod in seen:
            continue
        seen.add(mod)
        path = ROOT / (mod.replace(".", "/") + ".py")
        if not path.exists():
            continue
        tree = _ast.parse(io.open(path, encoding="utf-8").read())
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                for a in node.names:
                    stack.append(a.name)
            elif isinstance(node, _ast.ImportFrom) and node.module and node.level == 0:
                stack.append(node.module)
                for a in node.names:
                    stack.append("%s.%s" % (node.module, a.name))
    assert "services.media_slot_conversion" not in seen, (
        "价链已经能够到换算表 —— 方案甲(价不随口径变)从此只靠自觉。"
        "可达路径起点:tools.transparent_pricing / tools.pricing_bands")
    # 反向对照:这个遍历真的走出去了,不是空集
    assert "tools.pricing_bands" in seen and len(seen) > 5, sorted(seen)


# ════════════════════════════════════════════════════════════════
# L2 · 老单读数逐字等于 COUNT(topics)
# ════════════════════════════════════════════════════════════════

def test_legacy_quote_reading_equals_plain_count(cur, make_quote, add_topics):
    """🔴 桶全 NULL 的老单,新读数必须**逐字**等于原口径的 COUNT。

    「原口径」= 同一状态谓词下的 `COUNT(*)`(工单 §8.4「在同一状态谓词下逐字相等」)。
    拿本模块自己留的 `_LEGACY_CONSUMED_SQL` 去算,不另抄一份 SQL ——
    抄一份就是第二套口径,它俩一起错时判据照样绿。
    """
    qid = make_quote(1, perspective=None, required=40)
    add_topics(qid, "completed", None, 9)
    add_topics(qid, "published", None, 2)
    add_topics(qid, "writing", None, 5)
    add_topics(qid, "draft", None, 13)
    add_topics(qid, "pending", None, 7)

    cur.execute(cc._LEGACY_CONSUMED_SQL, (qid, list(cc.CONSUMED_STATUSES)))
    legacy = int(dict(cur.fetchone())["consumed"])
    view = cc.resolve_quote_capacity(cur, qid)
    assert view["consumed_articles"] == legacy == 11, (view, legacy)

    # 反向对照:给同一批行盖上 coverage 桶,读数**必须**变 —— 否则折算根本没接上
    cur.execute("UPDATE topics SET media_bucket=%s WHERE quote_id=%s",
                (M.BUCKET_COVERAGE, qid))
    moved = cc.resolve_quote_capacity(cur, qid)
    assert moved["consumed_articles"] != legacy, (
        "盖了 coverage 桶读数却没变 —— 折算没接上,上面那条等式是恒真")
    assert moved["consumed_articles"] == 2, moved   # 11 条 / 5 = 2.2 -> 2


# ════════════════════════════════════════════════════════════════
# L3 · 客户端点无换算键
# ════════════════════════════════════════════════════════════════

def test_customer_payload_carries_no_conversion_key():
    """🔴 换算键一个都不许出客户面(Owner 2026-09-15 ③「不要看到,这是运营的事」)。

    键名**从生产者身上取**,不是我手打一份清单 ——
    手打的清单对「以后新加的键」是瞎的,而这正是本仓记过的黑名单锁通病。
    """
    sel = importlib.import_module("api.selection_api")
    from services.quote_media_mix import plan_media_mix

    produced = plan_media_mix(7, target_engines=["deepseek"])
    conversion_keys = {"conversion_version", "posts_per_slot_bps",
                       "posts_estimate", "posts_estimate_total", "delivery_perspective"}
    assert conversion_keys <= set(produced), (
        "生产者已经不再吐这些键了,这条锁的键名清单该跟着改:%s" % sorted(produced))

    payload = {
        "keywords": [{"keyword": "k", "standard": {"price": 9}}],
        "media_mix": {k: produced[k] for k in conversion_keys},
    }
    sel._strip_internal_pricing_fields(payload)

    blob = repr(payload)
    for key in conversion_keys:
        assert key not in blob, "客户出参里还有换算键 %s:%s" % (key, blob)
    assert "media_mix" not in payload, "整块 media_mix 必须剥掉(内部比例/样本数也在里面)"
    # 反向对照:客户该看到的东西没被一起剥走
    assert payload["keywords"][0]["standard"]["price"] == 9


# ════════════════════════════════════════════════════════════════
# L4 / L5 · 写类动作的闸
# ════════════════════════════════════════════════════════════════

def test_unpaid_emits_no_write_action():
    """🔴 未付款:一条写类动作都不许签发(08_billing 未付款不执行)。"""
    from services.gap_operation_plan import Capacity, _actions_for_item, can_emit_write_actions

    unpaid = Capacity(cc.build_capacity_view(
        authorized_articles=15, consumed_articles=0, quote_is_payable=False))
    assert can_emit_write_actions(unpaid) is False
    ids = {a["action_id"] for a in
           _actions_for_item(allocation_code="ready_to_execute", capacity=unpaid)}
    assert "open_writing_task" not in ids and "submit_publication_link" not in ids, ids


def test_paid_over_capacity_still_writes():
    """🔴 已付款 + 槽用满:照发,并带 over_capacity(Owner ④「不用阻断不用提示」)。"""
    from services.gap_operation_plan import Capacity, _actions_for_item, can_emit_write_actions

    full = Capacity(cc.build_capacity_view(
        authorized_articles=15, consumed_articles=15, quote_is_payable=True))
    assert full.executable is False and can_emit_write_actions(full) is True
    acts = _actions_for_item(allocation_code="ready_to_execute", capacity=full)
    writes = [a for a in acts if a["action_id"] == "open_writing_task"]
    assert writes, [a["action_id"] for a in acts]
    assert all(a.get("over_capacity") is True for a in writes), writes
    assert all(a.get("enabled") is not False for a in writes), (
        "over_capacity 变成了第二道软闸,与 Owner ④ 相违")

    # 反向对照:还有额度时不许带这个标(否则它是个恒真装饰)
    ok = Capacity(cc.build_capacity_view(
        authorized_articles=15, consumed_articles=1, quote_is_payable=True))
    assert all(a.get("over_capacity") is None for a in
               _actions_for_item(allocation_code="ready_to_execute", capacity=ok))


# ════════════════════════════════════════════════════════════════
# L6 · planned_posts_default 随口径变
# ════════════════════════════════════════════════════════════════

def test_planned_posts_default_follows_perspective(make_quote):
    """🔴 7 槽:门户 7 条 / 自媒体 35 条 / 没口径的老单 7 条(工单 §8.8 第 6 条)。"""
    from db.diagnosis_db import get_writing_project_detail

    portal = make_quote(2, perspective=M.PERSPECTIVE_PORTAL, required=7)
    selfm = make_quote(3, perspective=M.PERSPECTIVE_SELF_MEDIA, required=7)
    legacy = make_quote(4, perspective=None, required=7)

    def planned(qid):
        d = get_writing_project_detail(qid) or {}
        return (d.get("keywords") or [{}])[0].get("planned_posts_default")

    assert planned(portal) == 7, planned(portal)
    assert planned(selfm) == 35, planned(selfm)
    assert planned(legacy) == 7, planned(legacy)
    # 三者必须真的不同 —— 全等于 7 也能让前两条里的一条过
    assert planned(selfm) != planned(portal)


# ════════════════════════════════════════════════════════════════
# L7 · 元锁仍绿
# ════════════════════════════════════════════════════════════════

def test_article_count_meta_lock_still_green():
    """🔴 `test_article_count_meta_lock` 的核心条款在本单之后仍成立。

    直接调那个模块里的判据函数,而不是「相信它在别的地方跑过」——
    「必跑」只写在注释里是不会传递的(本仓累犯)。
    """
    meta = importlib.import_module("tests.test_article_count_meta_lock_2026_08_08")
    meta.test_l1_tier_article_ladder_only_in_ssot()
    meta.test_l1_ssot_really_contains_a_ladder()
    meta.test_l1_criterion_actually_fires()


# ════════════════════════════════════════════════════════════════
# L8 · 结构锁:状态词表
# ════════════════════════════════════════════════════════════════

#: 🔴 扫描器解析不出状态字面量的位置。每一条都要写明为什么允许。
#:   `update_topic_status(topic_id, status, ...)` 的状态是**函数参数**;
#:   该函数**全仓零调用**(`git grep 'update_topic_status('` 只有它自己的 def),
#:   所以它写不出任何状态。留着不删是因为删生产函数不在本单范围。
#:   🔴 哪天有人开始调它,状态就变成调用方决定的 —— 那时这份清单必须重写,
#:      而不是继续放行。这条注释本身不是门,门是下面那条断言。
FROZEN_UNRESOLVED = (
    "db/diagnosis_db.py:5373 · UPDATE status=%s 取不到字面量(第 0 个占位)",
    "db/diagnosis_db.py:5378 · UPDATE status=%s 取不到字面量(第 0 个占位)",
    "db/diagnosis_db.py:5383 · UPDATE status=%s 取不到字面量(第 0 个占位)",
)


def test_status_vocabulary_is_complete():
    """🔴 Review 追加的结构锁:**扫写入点得出的集合 == 谓词声明集合**,多一少一都红。

    为什么需要它:三套状态词表互相对不上(DDL 注释 / 生产实测 / 代码实际写入),
    照任何一套写谓词都会漏。漏掉的状态会被静默归进「不占槽」,
    而没有任何东西会报错 —— 直到某张单的额度算错。
    """
    from tests.media_slot_conversion_2026_09_15.status_scan import scan

    result = scan()
    scanned = set(result["statuses"])
    declared = set(cc.ALL_TOPIC_STATUSES)
    no_write = set(cc.DECLARED_WITHOUT_WRITE_SITE)

    assert no_write <= declared, (
        "DECLARED_WITHOUT_WRITE_SITE 里有不在声明集合里的值:%s" % sorted(no_write - declared))
    assert scanned == declared - no_write, (
        "状态词表对不上。扫到 %s;声明(扣掉具名无写入点例外)%s;"
        "多出来的 %s;少掉的 %s"
        % (sorted(scanned), sorted(declared - no_write),
           sorted(scanned - (declared - no_write)), sorted((declared - no_write) - scanned)))
    # 三组互不重叠且恰好拼成声明集合
    groups = [set(cc.CONSUMED_STATUSES), set(cc.RESERVED_STATUSES),
              set(cc.NON_OCCUPYING_STATUSES)]
    assert sum(len(g) for g in groups) == len(declared), "三组之间有重复"
    assert set().union(*groups) == declared, "三组之和不等于声明集合"
    # 扫描器真的扫到了东西 —— 空集会让上面的相等在「什么都没扫到」时也成立
    assert int(result["sites"]) >= 30, result["sites"]
    assert len(scanned) >= 7, sorted(scanned)


def test_status_scanner_shouts_when_it_cannot_parse():
    """🔴 仪器坏了要出声:解析不出的位置必须**恰好**是那份冻结清单。

    少一条 = 有人改了那段死代码(要重新判断);多一条 = 新出现一个解析不了的写入点,
    而那种写入点的状态值是扫不到的 —— 上一条锁会因此少算而**看起来更干净**。
    """
    from tests.media_slot_conversion_2026_09_15.status_scan import scan

    got = tuple(scan()["unresolved"])
    assert got == FROZEN_UNRESOLVED, (
        "解析不了的写入点变了。现在:%s;冻结的:%s" % (list(got), list(FROZEN_UNRESOLVED)))


def test_scanner_does_not_pick_up_other_tables():
    """🔴 反向对照:扫描器必须**扫不到** social_topics / geo_douyin_topics 的状态。

    第一版枚举正是因为跨表捞,把 archived/making/done/used/skipped 也算了进来。
    """
    from tests.media_slot_conversion_2026_09_15.status_scan import scan

    scanned = set(scan()["statuses"])
    foreign = {"archived", "making", "done", "used", "skipped", "success",
               "not_charged", "not_required", "titles_ready"}
    assert not (scanned & foreign), "扫到了别的表/别的列的状态:%s" % sorted(scanned & foreign)


# ════════════════════════════════════════════════════════════════
# 接线 · 「加关键词」那条路真的把换算后的条数喂给了生成器
# ════════════════════════════════════════════════════════════════

def test_add_keyword_path_feeds_converted_count_to_the_generator(make_quote, monkeypatch):
    """🔴 值算对了、和它有没有被接上,是两件事。

    `_auto_generate_topics_for_new_keyword` 把 `detail["keywords"]` 里的那一行
    原样塞进 `KeywordTopicGenerator(keywords=[new_kw])`;生成器用
    `_required_article_count` 取每词条数。这条锁把**那一行**截下来,
    断言它带着换算后的条数,并且生成器读出来的就是 35。

    不是「我读了代码觉得会」——把 A 调 B 那一行真的跑一遍。
    """
    import server as S
    from writing.keyword_topic_generator import _required_article_count

    qid = make_quote(90, perspective=M.PERSPECTIVE_SELF_MEDIA, required=7)
    # 拿到这单唯一那个词的 id
    from db.diagnosis_db import get_writing_project_detail
    detail = get_writing_project_detail(qid)
    kw_row = (detail.get("keywords") or [None])[0]
    assert kw_row and kw_row.get("id"), detail

    captured = {}

    class _FakeGen:
        def __init__(self, **kwargs):
            captured["keywords"] = kwargs.get("keywords")

        async def generate(self):
            return []

    monkeypatch.setattr("writing.keyword_topic_generator.KeywordTopicGenerator", _FakeGen)
    S._auto_generate_topics_for_new_keyword(qid, int(kw_row["id"]), "判据词", 7)

    assert captured.get("keywords"), "生成器根本没被调用 —— 这条锁没测到接线"
    fed = captured["keywords"][0]
    assert fed.get("planned_posts_default") == 35, fed
    assert _required_article_count(fed) == 35, (
        "喂进生成器的那一行算出来不是 35 条 —— 换算没接到出题数上:%s" % fed)


def test_reading_with_the_callers_cursor_never_aborts_their_transaction():
    """🔴 用调用方的游标读快照,**失败不许溢出到它的事务**。

    fail-soft 的前提是失败不外溢。另开连接时靠 `_release` 先 rollback;
    复用游标时只能靠 SAVEPOINT —— 没有第三种写法。
    实测踩过:把 SAVEPOINT 去掉,`services/media_slot_conversion` 读一张
    **不存在的**表(gap_plan 判据库的 schema 来自 `init_db()`,没有
    `quote_pricing_snapshots`),调用方整条事务 aborted,
    之后它自己每一条查询都 `InFailedSqlTransaction` ⇒ 端点全线 503,
    gap_plan 包从 98 绿变成 35 红 12 error。
    """
    import psycopg2
    import psycopg2.extras

    dsn = (os.environ.get("TEST_DATABASE_URL") or os.environ["DATABASE_URL"]).split("?", 1)[0]
    conn = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False          # 🔴 必须是事务模式,autocommit 下这条锁测不到东西
    try:
        c = conn.cursor()
        c.execute("SELECT 1 AS ok")
        assert dict(c.fetchone())["ok"] == 1, "事务还没开始就坏了,后面读数无效"
        # 把快照表藏起来:search_path 只留 pg_temp ⇒ 那张表解析不到
        c.execute("SET LOCAL search_path TO pg_temp")
        try:
            M._read_with_cursor(c, 1)
        except Exception:
            pass                      # 读失败是预期的,本锁测的是**失败之后**
        c.execute("RESET search_path")
        c.execute("SELECT 2 AS ok")
        assert dict(c.fetchone())["ok"] == 2, "调用方的事务被读快照打废了"
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()
