"""【A-1 = Codex 三审 P1-2】040 的不可变触发器只按名字判存。

Codex 的真 PG16 反例(我复现过,见交付文 §A-1)
----------------------------------------------
040 的反查块写的是::

    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_defgeo_qplan_immutable')

—— **连表都没绑**(没有 `tgrelid` 谓词),更不看它执行的是哪个函数、是不是被
DISABLE 了。于是把同名触发器换成一个 `RETURN NEW` 的放行版本,040 二跑照样报通过,
而 `profile_revision_id` 可以随便 UPDATE:审计不可变性整个消失,一条判据都不红。

这是本仓「IF NOT EXISTS / 按名判存」那一族的**第三例**:
`CREATE INDEX IF NOT EXISTS` 按名判存不绑表 → `DROP INDEX` 语法上不绑表 →
现在是 `pg_trigger.tgname` 不绑表。而触发器这一例最贵:索引骗过去少了个约束,
触发器骗过去是「拦不住」而目录上一切正常。

修法(见 `scripts/defgeo_readiness_gen.py` 的触发器轴)
----------------------------------------------------
触发器进**机械 census**,期望值从真 PG16 现读,核四样:
  · `pg_get_triggerdef` —— 内含宿主表 / BEFORE|AFTER / 事件集 / FOR EACH ROW /
    `EXECUTE FUNCTION <fn>`,即 tgrelid + tgtype + tgfoid;
  · `tgenabled` —— DISABLE 掉的触发器目录定义一字不差,却一次都不执行;
  · 函数体 md5 —— 同名触发器指向同名函数、函数体被换成 `RETURN NEW` 那一手,
    只有比函数定义才看得见;
  · 查询本身带 `tgrelid = r.tname::regclass` —— 「不绑表」的病根。

🔴 我自己的判例(041 台账):**令牌锁判「在不在」,行为臂判「真不真」,缺一不可。**
   所以本文件除了 poison,还有一条真打一发 UPDATE / DELETE 看被不被拒的行为臂。
"""
from __future__ import annotations

import uuid

import psycopg2
import pytest

from tests.defgeo_v3a_2026_08_28 import conftest as CT

pytestmark = pytest.mark.integration

REL_040 = "db/migration_040_defgeo_question_plans_2026_08_21.sql"
TABLE = "defgeo_question_plans"
TRIGGER = "trg_defgeo_qplan_immutable"


def _connect(dsn):
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    conn.cursor().execute("SET search_path = public")
    return conn


def _bootstrap_040(dsn):
    """生产 dump + 只跑 040。poison 判据要的是「040 自己拦不拦得住」。"""
    conn = _connect(dsn)
    CT.load_prod_schema(conn)
    conn.cursor().execute("SET search_path = public")
    CT.run_migration(conn, REL_040)
    return conn


def _seed_plan(cur) -> str:
    """插一条真题单。行为臂必须打在**真行**上 —— 空表上 UPDATE 影响 0 行,
    触发器一次都不会被调用,判据会以「没报错」的形式绿(而那绿什么都不证明)。
    """
    plan_id = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO defgeo_question_plans "
        "(plan_id, plan_revision, tenant_owner_user_id, brand_id, profile_revision_id, "
        " mode, question_set_version, canonical_hash, frozen_payload, "
        " defensive_count, offensive_count, total_count, "
        " client_request_id, request_content_hash, expires_at, created_by_user_id) "
        "VALUES (%s,1,4242,7,'prof-1','defensive','qsv-1',%s,'{}'::jsonb,"
        "        1,0,1,%s,%s, NOW() + interval '1 day', 4242)",
        (plan_id, "a" * 64, "creq-" + uuid.uuid4().hex[:8], "b" * 64))
    return plan_id


# ══════════════════════════════════════════════════════════════════════════
# 行为臂 —— 「它在」证明不了「它拦得住」
# ══════════════════════════════════════════════════════════════════════════
def test_a1_the_immutability_trigger_actually_rejects_update_and_delete(fresh_db):
    """🔴 行为臂:装完 040 之后,真打一发 UPDATE、一发 DELETE,必须**真的被拒**。

    不是查目录。目录查询回答的是「有没有一个叫这个名字的东西」,
    而这条判据回答的是「它拦不拦得住」—— Codex 反例里两个答案是相反的。
    """
    conn = _bootstrap_040(fresh_db("a1beh"))
    try:
        cur = conn.cursor()
        plan_id = _seed_plan(cur)

        with pytest.raises(psycopg2.Error) as upd:
            cur.execute("UPDATE defgeo_question_plans SET profile_revision_id='prof-2' "
                        "WHERE plan_id=%s", (plan_id,))
        assert "REV-01" in str(upd.value), (
            "UPDATE 被拒了,但不是不可变触发器拒的:%s" % upd.value)

        cur.execute("SELECT profile_revision_id FROM defgeo_question_plans WHERE plan_id=%s",
                    (plan_id,))
        assert cur.fetchone()[0] == "prof-1", "UPDATE 报了错,值却还是被改掉了"

        with pytest.raises(psycopg2.Error) as dele:
            cur.execute("DELETE FROM defgeo_question_plans WHERE plan_id=%s", (plan_id,))
        assert "REV-01" in str(dele.value), str(dele.value)

        cur.execute("SELECT count(*) FROM defgeo_question_plans WHERE plan_id=%s", (plan_id,))
        assert cur.fetchone()[0] == 1, "DELETE 报了错,行却没了"
    finally:
        conn.close()


def test_a1_the_permitted_supersede_update_still_goes_through(fresh_db):
    """配对的必须不命中:唯一允许的原地变更(NULL → 非 NULL 的 superseded 标记)
    必须仍然放行。

    少了它,一个「什么 UPDATE 都拒」的触发器也能让上面那条绿 ——
    而那会把「改题生成 superseding revision」这条正路堵死。
    """
    conn = _bootstrap_040(fresh_db("a1pair"))
    try:
        cur = conn.cursor()
        plan_id = _seed_plan(cur)
        cur.execute("UPDATE defgeo_question_plans SET superseded_by_revision=2 "
                    "WHERE plan_id=%s", (plan_id,))
        cur.execute("SELECT superseded_by_revision FROM defgeo_question_plans "
                    "WHERE plan_id=%s", (plan_id,))
        assert cur.fetchone()[0] == 2, "允许的 supersede 标记没写进去"
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
# poison —— 冷库预置同名放行触发器,迁移必须 RAISE
# ══════════════════════════════════════════════════════════════════════════
def _replay_expecting_raise(conn, needle="trg_defgeo_qplan_immutable"):
    with pytest.raises(psycopg2.Error) as err:
        CT.run_migration(conn, REL_040)
    assert needle in str(err.value), "RAISE 了,但不是触发器那条:%s" % err.value
    return str(err.value)


def test_a1_poison_same_name_permissive_trigger_makes_the_migration_raise(fresh_db):
    """🔴 Codex 那一发,原样:同名触发器指向一个 `RETURN NEW` 的放行函数。

    ⚠️ 关键在于**旧的建触发器块会放过它**:它按 (tgname, tgrelid) 判存,
       看见"有了"就不再 CREATE —— 于是毒活了下来。旧反查块又只按名字判存,
       于是 040 二跑报通过。现在 readiness 比的是定义 + 函数体,当场 RAISE。
    """
    conn = _bootstrap_040(fresh_db("a1p1"))
    try:
        cur = conn.cursor()
        cur.execute("CREATE OR REPLACE FUNCTION defgeo_qplan_allow_all() RETURNS TRIGGER "
                    "AS $f$ BEGIN RETURN NEW; END $f$ LANGUAGE plpgsql")
        cur.execute("DROP TRIGGER %s ON %s" % (TRIGGER, TABLE))
        cur.execute("CREATE TRIGGER %s BEFORE UPDATE OR DELETE ON %s "
                    "FOR EACH ROW EXECUTE FUNCTION defgeo_qplan_allow_all()" % (TRIGGER, TABLE))
        msg = _replay_expecting_raise(conn)
        assert "allow_all" in msg, (
            "报错里没把**实得**的函数名摊开,运维看不出是被换了函数:%s" % msg)
    finally:
        conn.close()


def test_a1_poison_disabled_trigger_makes_the_migration_raise(fresh_db):
    """🔴 第二种毒:定义**一字不差**,只是被 `DISABLE TRIGGER` 了。

    这一发专打「比定义就够了」的错觉:`pg_get_triggerdef` 对 DISABLE 的触发器
    输出与正品完全相同,只有 `tgenabled` 那一位是 'D'。
    一个只比 triggerdef 的实现在这里会全绿,而库上那个触发器一次都不会执行。
    """
    conn = _bootstrap_040(fresh_db("a1p2"))
    try:
        conn.cursor().execute("ALTER TABLE %s DISABLE TRIGGER %s" % (TABLE, TRIGGER))
        msg = _replay_expecting_raise(conn)
        assert "enabled=" in msg, "报错里没体现启用状态这一位:%s" % msg
    finally:
        conn.close()


def test_a1_poison_disabled_trigger_really_stops_rejecting(fresh_db):
    """把上一条的「为什么这算毒」用行为证一遍:DISABLE 之后 UPDATE 真的过了。

    没有这一条,「DISABLE 也要拦」只是一个看起来严格的主张 ——
    这一条把它变成一个**可观测的资损面**:审计不可变的行可以被就地改写。
    """
    conn = _bootstrap_040(fresh_db("a1p2b"))
    try:
        cur = conn.cursor()
        plan_id = _seed_plan(cur)
        cur.execute("ALTER TABLE %s DISABLE TRIGGER %s" % (TABLE, TRIGGER))
        cur.execute("UPDATE defgeo_question_plans SET profile_revision_id='prof-tampered' "
                    "WHERE plan_id=%s", (plan_id,))
        cur.execute("SELECT profile_revision_id FROM defgeo_question_plans WHERE plan_id=%s",
                    (plan_id,))
        assert cur.fetchone()[0] == "prof-tampered", (
            "DISABLE 之后 UPDATE 竟然还是被拦住了 —— 那这条毒不成立,"
            "上一条判据守的东西要重新想")
    finally:
        conn.close()


def test_a1_a_repairable_state_is_repaired_not_rejected(fresh_db):
    """配对的必须不命中:迁移**能自己修好**的两种状态不许变成硬红。

    · 触发器被整个删掉(或挪到别的表上)⇒ 建触发器块会重新建;
    · 函数体被换掉 ⇒ 文件顶部的 `CREATE OR REPLACE FUNCTION` 会覆盖回来。
    这两种重放之后必须**通过**。少了这一条,一个「见到任何异常就 RAISE」的
    实现也能让上面两条 poison 绿 —— 而那会让每一次自愈式部署都卡在迁移这一步。
    """
    conn = _bootstrap_040(fresh_db("a1pair2"))
    try:
        cur = conn.cursor()
        cur.execute("DROP TRIGGER %s ON %s" % (TRIGGER, TABLE))
        cur.execute("CREATE OR REPLACE FUNCTION defgeo_qplan_forbid_mutation() RETURNS TRIGGER "
                    "AS $f$ BEGIN RETURN NEW; END $f$ LANGUAGE plpgsql")
        CT.run_migration(conn, REL_040)          # 不抛 = 自愈成功
        plan_id = _seed_plan(cur)
        with pytest.raises(psycopg2.Error):      # 且自愈之后真的又拦得住了
            cur.execute("DELETE FROM defgeo_question_plans WHERE plan_id=%s", (plan_id,))
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
# 机械 census —— 轴的作用域 = 声明了触发器的**全部** readiness 文件
# ══════════════════════════════════════════════════════════════════════════
def test_a1_every_declared_trigger_is_in_its_readiness_block():
    """本轴不是只为 040 修的:041 / 044 / 046 的触发器长着同一个病。

    分母从文件**机械扫**(`declared_triggers`),不是手抄的清单 ——
    手抄漏掉的那一条不会让任何判据变红。声明了几个,readiness 里就得有几个。
    """
    from scripts.defgeo_readiness_gen import READINESS_FILES, declared_triggers

    total = 0
    for tag, rel in sorted(READINESS_FILES.items()):
        sql = (CT.REPO / rel).read_text(encoding="utf-8", errors="replace")
        declared = declared_triggers(sql)
        total += len(declared)
        block = sql.split("-- @readiness-begin %s" % tag)[1].split(
            "-- @readiness-end %s" % tag)[0]
        for name, tbl in declared:
            assert "('%s', 'public.%s'," % (name, tbl) in block, (
                "[%s] 声明了触发器 %s on %s,readiness 里却没有它 —— "
                "「按名判存」的病根就是它没进分母" % (tag, name, tbl))
        if declared:
            assert "pg_get_triggerdef" in block, "[%s] 声明了触发器却没验" % tag
            assert "g.tgrelid = r.tname::regclass" in block, (
                "[%s] 验触发器时**没绑表** —— 这正是 P1-2 的病根" % tag)
            assert "g.tgenabled" in block, (
                "[%s] 没验 tgenabled —— DISABLE 掉的触发器目录定义一字不差" % tag)
            assert "pg_get_functiondef" in block, (
                "[%s] 没验函数体 —— 同名函数被换成 RETURN NEW 时目录看不出来" % tag)
    assert total >= 5, "整条轴只扫到 %d 个触发器 —— 分母塌了,这些判据在守空气" % total
