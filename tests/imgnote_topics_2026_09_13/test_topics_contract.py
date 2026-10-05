"""WO_204 c1 判据 · 选题表与四个端点(C1 / C2 / C4 / C5)。

Owner 09-13:「和写作一样:默认把这几个选题写出来,用户可以修改,
修改后就按照标题来进行创作。」——半自动,不是托管。

本文件跑**真库**(生产 schema + 059)。C3「标题以选题为准」在另一个文件里,
因为那条必须驱动 `run_image_post_production` 本体。
"""
import psycopg2
import pytest

from .conftest import (BRAND_ID, OTHER_BRAND_ID, USER_ID, conn, make_post,
                       seed_topic, topic_row)


# ═══════════════════════════════════════════════════════════════
# C1 · 蒸馏产物逐条落表,重放不重复
# ═══════════════════════════════════════════════════════════════
def _distilled(n, prefix="题"):
    return [{"title": "%s%d" % (prefix, i), "keyword": "装修公司",
             "angle": "角度%d" % i, "card_outline": ["c%d" % i]} for i in range(n)]


def test_distilled_topics_land_one_row_each():
    """C1 主臂:落表条数 == 回包 topics 数。"""
    from db.geo_douyin_db import insert_distilled_topics, list_topics

    topics = _distilled(5)
    n = insert_distilled_topics(brand_id=BRAND_ID, created_by=USER_ID,
                                distill_task_id=5001, topics=topics)
    assert n == len(topics)
    page = list_topics(brand_id=BRAND_ID)
    assert page["total"] == len(topics)
    assert {t["title"] for t in page["topics"]} == {t["title"] for t in topics}
    assert {t["status"] for t in page["topics"]} == {"pending"}
    assert {t["source"] for t in page["topics"]} == {"distilled"}


def test_replaying_the_same_task_inserts_nothing():
    """C1 反向臂:同 task 重放 ⇒ 0 条新增,总数不变。

    🔴 返回值是**本次真正插进去的条数**,不是 len(topics) ——
       返 len 的话重放看起来像又落了一批,而表里其实没变。
    """
    from db.geo_douyin_db import insert_distilled_topics, list_topics

    topics = _distilled(4)
    first = insert_distilled_topics(brand_id=BRAND_ID, created_by=USER_ID,
                                    distill_task_id=5002, topics=topics)
    again = insert_distilled_topics(brand_id=BRAND_ID, created_by=USER_ID,
                                    distill_task_id=5002, topics=topics)
    assert (first, again) == (4, 0)
    assert list_topics(brand_id=BRAND_ID)["total"] == 4


def test_two_topics_with_the_same_title_both_land():
    """反向对照:标题相同的两条**都要落**。

    🔴 这条是去重键选 `distill_index` 而不是 `title` 的理由。
       按 title 去重会静默吞掉第二条,而 C1 主臂("条数相等")就会红在
       一个根本没有缺陷的地方 —— 然后有人会去"修"主臂。
    """
    from db.geo_douyin_db import insert_distilled_topics, list_topics

    same = [{"title": "一模一样的标题"}, {"title": "一模一样的标题"}]
    assert insert_distilled_topics(brand_id=BRAND_ID, created_by=USER_ID,
                                   distill_task_id=5003, topics=same) == 2
    assert list_topics(brand_id=BRAND_ID)["total"] == 2


def test_a_different_task_is_not_deduplicated_against_the_first():
    """反向对照:**另一个** task 的同样内容照落 —— 用户又花了一次 130,
    拿到的就该是新的一批,不是"和上次一样所以没有"。"""
    from db.geo_douyin_db import insert_distilled_topics, list_topics

    topics = _distilled(3)
    insert_distilled_topics(brand_id=BRAND_ID, created_by=USER_ID,
                            distill_task_id=5004, topics=topics)
    assert insert_distilled_topics(brand_id=BRAND_ID, created_by=USER_ID,
                                   distill_task_id=5005, topics=topics) == 3
    assert list_topics(brand_id=BRAND_ID)["total"] == 6


def test_blank_titles_are_dropped_not_stored_as_empty_rows():
    """空标题不落 —— 列表里一行没有标题的题,用户既看不懂也点不动。"""
    from db.geo_douyin_db import insert_distilled_topics, list_topics

    assert insert_distilled_topics(
        brand_id=BRAND_ID, created_by=USER_ID, distill_task_id=5006,
        topics=[{"title": "  "}, {"title": "真标题"}, {"title": ""}]) == 1
    assert list_topics(brand_id=BRAND_ID)["total"] == 1


# ═══════════════════════════════════════════════════════════════
# C2 · 改标题:source 置 user;做中不许改
# ═══════════════════════════════════════════════════════════════
def test_editing_a_title_marks_it_as_the_users_own():
    """C2 主臂:改完 `source='user'`、`edited_at` 落时间、标题是新的。

    🔴 `source` 是"这条还是不是 AI 原话"的**唯一**依据。不置的话,
       人改过的题会被算进 AI 的成绩里,效果复盘就永远偏乐观。
    """
    from db.geo_douyin_db import TOPIC_OK, update_topic_title

    tid = seed_topic(title="AI 出的题")
    assert topic_row(tid)["source"] == "distilled"
    assert topic_row(tid)["edited_at"] is None

    assert update_topic_title(topic_id=tid, title="人改过的题") == TOPIC_OK
    row = topic_row(tid)
    assert row["title"] == "人改过的题"
    assert row["source"] == "user"
    assert row["edited_at"] is not None


@pytest.mark.parametrize("locked_status", ["making", "done", "failed"])
def test_editing_is_refused_once_it_left_pending(locked_status):
    """C2 反向臂:不是 pending 就改不了,而且**能分辨**是被锁不是不存在。

    🔴 两者都返 not_found 的话,用户会以为选题被删了,然后再点一次建单。
    """
    from db.geo_douyin_db import TOPIC_LOCKED, update_topic_title

    tid = seed_topic(status=locked_status,
                     post_id=make_post() if locked_status == "done" else None)
    assert update_topic_title(topic_id=tid, title="想改") == TOPIC_LOCKED
    assert topic_row(tid)["title"] == "原选题", "被锁的条目标题竟然被改了"


def test_missing_topic_is_not_found_not_locked():
    from db.geo_douyin_db import TOPIC_NOT_FOUND, delete_topic, update_topic_title
    assert update_topic_title(topic_id=99999999, title="x") == TOPIC_NOT_FOUND
    assert delete_topic(99999999) == TOPIC_NOT_FOUND


def test_empty_title_is_refused():
    from db.geo_douyin_db import TOPIC_EMPTY_TITLE, update_topic_title
    tid = seed_topic()
    assert update_topic_title(topic_id=tid, title="   ") == TOPIC_EMPTY_TITLE
    assert topic_row(tid)["title"] == "原选题"


def test_delete_only_when_pending():
    """删除与改标题同一条口径:做中/已做删掉会让成品成孤儿。"""
    from db.geo_douyin_db import TOPIC_LOCKED, TOPIC_OK, delete_topic

    assert delete_topic(seed_topic()) == TOPIC_OK
    assert delete_topic(seed_topic(status="making")) == TOPIC_LOCKED
    assert delete_topic(seed_topic(status="done", post_id=make_post())) == TOPIC_LOCKED


def test_manually_added_topic_is_user_sourced_from_the_start():
    from db.geo_douyin_db import create_topic

    tid = create_topic(brand_id=BRAND_ID, created_by=USER_ID, title="我自己想的题",
                       keyword="装修", city="杭州")
    row = topic_row(tid)
    assert row["source"] == "user" and row["status"] == "pending"
    assert row["edited_at"] is not None
    assert row["distill_task_id"] is None, "手加的题不该挂在某个蒸馏任务上"


# ═══════════════════════════════════════════════════════════════
# C4 · 同一条选题不许重复建单
# ═══════════════════════════════════════════════════════════════
def test_claiming_a_topic_twice_fails_the_second_time():
    """C4 主臂:抢单是"不许重复做"的唯一依据。

    🔴 守卫写在 UPDATE 的 WHERE 里、与状态变更**同一条语句**。
       「先查 status 再建单」在双击下两次都读到 pending、两次都建单、
       两次都扣算力,而两次都会"成功"。
    """
    from db.geo_douyin_db import claim_topic_for_production

    tid = seed_topic()
    first = claim_topic_for_production(tid)
    assert first is not None
    assert first["title"] == "原选题", "抢到的应当是**抢那一刻**的行"
    assert topic_row(tid)["status"] == "making"
    assert claim_topic_for_production(tid) is None


def test_claim_returns_the_row_as_it_was_at_claim_time():
    """抢单返回的是 RETURNING 的那一行,不是事后再 SELECT 一次。

    🔴 事后再读可能读到已被别人改过的值 —— 那样制作用的标题
       就不是用户按下按钮时看到的那一个(#184 d1b 同族缺陷)。
    """
    from db.geo_douyin_db import claim_topic_for_production
    tid = seed_topic(title="用户看到的标题")
    row = claim_topic_for_production(tid)
    assert row["title"] == "用户看到的标题"
    assert row["status"] == "making"


@pytest.mark.parametrize("taken", ["making", "done"])
def test_an_already_taken_topic_cannot_be_claimed(taken):
    from db.geo_douyin_db import claim_topic_for_production
    tid = seed_topic(status=taken, post_id=make_post() if taken == "done" else None)
    assert claim_topic_for_production(tid) is None


def test_finish_and_release_move_the_topic_out_of_making():
    from db.geo_douyin_db import (claim_topic_for_production, finish_topic,
                                  release_topic)

    done_id = seed_topic()
    pid = make_post()
    claim_topic_for_production(done_id)
    finish_topic(topic_id=done_id, post_id=pid)
    row = topic_row(done_id)
    assert (row["status"], row["post_id"]) == ("done", pid)

    failed_id = seed_topic()
    claim_topic_for_production(failed_id)
    release_topic(topic_id=failed_id)
    row = topic_row(failed_id)
    assert row["status"] == "failed"
    assert row["post_id"] is None, "失败的行留着 post_id ⇒ 点进去是别人的成品"


# ═══════════════════════════════════════════════════════════════
# 约束本身(手写夹具漏掉的就是这些)
# ═══════════════════════════════════════════════════════════════
def test_done_without_a_post_is_rejected_by_the_database():
    """`done` 必须带 post_id —— 由建表的 CHECK 保证,不靠调用方自觉。"""
    c = conn()
    try:
        with pytest.raises(psycopg2.errors.CheckViolation):
            c.cursor().execute(
                "INSERT INTO geo_douyin_topics (brand_id,title,status,created_by)"
                " VALUES (%s,'x','done',%s)", (BRAND_ID, USER_ID))
    finally:
        c.close()


def test_non_done_with_a_post_is_rejected_by_the_database():
    """反向:没做完却挂着 post_id 也不许 —— 那正是"失败了还留着上一次"的形状。

    🔴 post_id 必须是**真存在**的成品:编一个 id 会先撞外键,
       于是这条判据"通过"的原因与它要验的 CHECK **毫无关系** ——
       把 CHECK 删掉它照样绿。
    """
    real_post = make_post()
    c = conn()
    try:
        with pytest.raises(psycopg2.errors.CheckViolation):
            c.cursor().execute(
                "INSERT INTO geo_douyin_topics (brand_id,title,status,post_id,created_by)"
                " VALUES (%s,'x','failed',%s,%s)", (BRAND_ID, real_post, USER_ID))
    finally:
        c.close()


def test_a_topic_cannot_point_at_a_nonexistent_post():
    """外键:post_id 必须指向真的成品(与同族三张表同一种关系)。"""
    c = conn()
    try:
        with pytest.raises(psycopg2.errors.ForeignKeyViolation):
            c.cursor().execute(
                "INSERT INTO geo_douyin_topics (brand_id,title,status,post_id,created_by)"
                " VALUES (%s,'x','done',%s,%s)", (BRAND_ID, 987654321, USER_ID))
    finally:
        c.close()


@pytest.mark.parametrize("bad_col, bad_val", [("status", "whatever"),
                                              ("source", "robot")])
def test_unknown_status_or_source_is_rejected(bad_col, bad_val):
    c = conn()
    try:
        with pytest.raises(psycopg2.errors.CheckViolation):
            c.cursor().execute(
                "INSERT INTO geo_douyin_topics (brand_id,title,%s,created_by)"
                " VALUES (%%s,'x',%%s,%%s)" % bad_col, (BRAND_ID, bad_val, USER_ID))
    finally:
        c.close()


def test_manual_topics_are_not_covered_by_the_dedupe_index():
    """partial 唯一索引只管有 task 的行:手加两条一模一样的是用户的自由。"""
    from db.geo_douyin_db import create_topic, list_topics
    create_topic(brand_id=BRAND_ID, created_by=USER_ID, title="同一句")
    create_topic(brand_id=BRAND_ID, created_by=USER_ID, title="同一句")
    assert list_topics(brand_id=BRAND_ID)["total"] == 2


# ═══════════════════════════════════════════════════════════════
# 列表:按品牌、按状态,且**不串客户**
# ═══════════════════════════════════════════════════════════════
def test_list_is_scoped_to_one_brand_and_can_filter_by_status():
    from db.geo_douyin_db import list_topics

    seed_topic(title="A1")
    seed_topic(title="A2", status="done", post_id=make_post())
    seed_topic(title="别人的", brand_id=OTHER_BRAND_ID)

    allp = list_topics(brand_id=BRAND_ID)
    assert allp["total"] == 2
    assert "别人的" not in {t["title"] for t in allp["topics"]}

    pend = list_topics(brand_id=BRAND_ID, status="pending")
    assert pend["total"] == 1 and pend["topics"][0]["title"] == "A1"


def test_list_reports_total_independently_of_the_page():
    """分页必须带 total —— 否则前端只能靠"这页少于 limit"猜,边界上必猜错一次。"""
    from db.geo_douyin_db import insert_distilled_topics, list_topics

    insert_distilled_topics(brand_id=BRAND_ID, created_by=USER_ID,
                            distill_task_id=5007, topics=_distilled(7))
    page = list_topics(brand_id=BRAND_ID, limit=3)
    assert len(page["topics"]) == 3 and page["total"] == 7


# ═══════════════════════════════════════════════════════════════
# C5 · 分母:geo_douyin_* 表清单,新表在列
# ═══════════════════════════════════════════════════════════════
def test_new_table_is_in_the_geo_douyin_family(capsys):
    """C5:打印这一族的表清单,并断言新表在里面。

    🔴 打印是给人看的(工单要求),断言是给机器看的 ——
       只打印不断言的话,表没建出来时输出照样"看起来正常"。
    """
    c = conn()
    try:
        cur = c.cursor()
        cur.execute("SELECT tablename FROM pg_tables "
                    " WHERE schemaname='public' AND tablename LIKE 'geo\\_douyin\\_%' "
                    " ORDER BY tablename")
        names = [r["tablename"] for r in cur.fetchall()]
    finally:
        c.close()
    print("geo_douyin_* 表清单(%d 张):" % len(names))
    for n in names:
        print("   " + n)
    assert "geo_douyin_topics" in names
    # 既有六张一张都不能少 —— 新建表不该顺手改掉别的。
    for existing in ("geo_douyin_posts", "geo_douyin_post_tasks",
                     "geo_douyin_post_revisions", "geo_douyin_distill_tasks",
                     "geo_douyin_production_batches", "geo_douyin_publish_artifacts"):
        assert existing in names, "既有表 %s 不见了" % existing


def test_migration_is_registered_in_the_manifest():
    """迁移必须进清单 —— 不进清单的迁移**永远不会被 prestart 执行**,
    而本地判据照样全绿(本包的 conftest 是自己 psql 跑的)。"""
    from db.migration_manifest import MIGRATIONS
    assert "db/migration_059_geo_douyin_topics_2026_09_13.sql" in MIGRATIONS


def test_migration_body_has_no_dml():
    """迁移体内禁 DML(本仓红线)。"""
    import io as _io
    import re

    text = _io.open("db/migration_059_geo_douyin_topics_2026_09_13.sql",
                    encoding="utf-8").read()
    body = "\n".join(l for l in text.splitlines() if not l.strip().startswith("--"))
    for verb in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "DROP TABLE"):
        assert not re.search(r"\b%s\b" % verb, body, re.IGNORECASE), (
            "迁移体里出现了 %s —— 本仓迁移禁 DML" % verb)
