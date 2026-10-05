"""WP1 · 槽位渠道化 + 事件 CHECK 行为变更 + caller census(附加约束 B / C)。

三块:
  B. 事件 CHECK **原子替换**是 schema 行为变更 —— 重放 ×2 后定义唯一且为新版,
     且有绕应用直接写非法值的变异测试。
  C. caller census **全量分母自证** —— 结构锚取全集,不点名式列举。
  R1/R2/R3. 四前置 / ordinal 守恒 / 唯一写入者。
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import RealDictCursor  # noqa: E402

from services.geo_douyin.delivery_slots import (  # noqa: E402
    ARTICLE_CHANNEL_PREDICATE, CHANNEL_ARTICLE, CHANNEL_IMAGE_NOTE,
    FULFILLMENT_CLAIMED, FULFILLMENT_GENERATING, FULFILLMENT_OPEN, FULFILLMENT_READY,
    IMAGE_NOTE_CHANNEL_PREDICATE, IMAGE_NOTE_EVENT_KINDS,
    SlotClaimConflict, SlotWriteUnsafe, activation_blockers, claim, release, transition,
)

REPO = pathlib.Path(__file__).resolve().parents[2]
MIG_034 = REPO / "db" / "migration_034_geo_image_note_contract_2026_08_17.sql"
MIG_035 = REPO / "db" / "migration_035_geo_image_note_slot_channel_2026_08_18.sql"
PROD_SCHEMA = pathlib.Path(os.getenv(
    "GEOIMG_PROD_SCHEMA_SQL", r"C:/AI-Test/.deploy_toolkit/_geoimg_prodschema_20260817.sql"))
DSN = os.getenv("TEST_DATABASE_URL")

CHECK_NAME = "ck_geo_article_slot_event_kind"
NEW_KINDS = ("slot_claimed", "slot_released", "generation_started", "post_linked", "ready")
OLD_KINDS = ("created", "blocked", "unblocked", "cancelled", "superseded",
             "reassigned", "topic_linked", "article_linked", "publication_locked")

IDENTITY = {"tenant_owner_user_id": 42, "actor_user_id": 99}


def _admin_dsn() -> str:
    return DSN.rsplit("/", 1)[0] + "/postgres"


@pytest.fixture(scope="module")
def db():
    if not DSN or not PROD_SCHEMA.is_file():
        pytest.skip("需要 TEST_DATABASE_URL 与生产 schema 夹具")
    name = f"geoimg_test_wp1_{uuid.uuid4().hex[:8]}"
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
    c.execute("SET search_path = public")
    c.execute(MIG_034.read_text(encoding="utf-8"))
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
    # 🔴 删除顺序必须与 FK 反向:slots.current_event_id → events(id) 是 FK,
    #    先删 events 会 ForeignKeyViolation。slots 先走,events 后走。
    for t in ("geo_article_delivery_slots", "geo_article_delivery_slot_events",
              "geo_article_plan_runs", "geo_article_contract_revisions"):
        c.execute(f"DELETE FROM {t}")
    try:
        yield c
    finally:
        conn.close()


@pytest.fixture()
def txn(db):
    """显式事务连接 —— 槽位写入必须在事务内(见 SlotWriteUnsafe)。"""
    conn = psycopg2.connect(db, cursor_factory=RealDictCursor)
    c = conn.cursor()
    c.execute("SET search_path = public")
    for t in ("geo_article_delivery_slots", "geo_article_delivery_slot_events",
              "geo_article_plan_runs", "geo_article_contract_revisions"):
        c.execute(f"DELETE FROM {t}")
    conn.commit()
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _check_defs(c) -> list[str]:
    c.execute("SELECT pg_get_constraintdef(oid, true) AS d FROM pg_constraint "
              "WHERE conname = %s", (CHECK_NAME,))
    return [str(dict(r)["d"]) for r in (c.fetchall() or [])]


def _run_035(c) -> None:
    c.execute(MIG_035.read_text(encoding="utf-8"))


# ============================================================
# B. 事件 CHECK 原子替换 = schema 行为变更
# ============================================================

def test_before_035_new_kinds_are_rejected(cur):
    """分母先立住:035 **之前**旧 CHECK 确实挡住新种类。

    没有这条,后面的"035 之后能插"可能只是因为压根没有 CHECK。
    """
    defs = _check_defs(cur)
    assert len(defs) == 1, f"旧 CHECK 不唯一:{defs}"
    for kind in NEW_KINDS:
        assert f"'{kind}'" not in defs[0], f"旧 CHECK 里已经有 {kind} —— 前提不成立"


def test_035_replaces_check_atomically_and_is_replay_idempotent(cur):
    """🔴 附加约束 B:重放 ×2 后该 CHECK **定义唯一且为新版**。"""
    _run_035(cur)
    after_first = _check_defs(cur)
    assert len(after_first) == 1, f"替换后 CHECK 不唯一:{after_first}"
    for kind in NEW_KINDS + OLD_KINDS:
        assert f"'{kind}'" in after_first[0], f"新定义缺 {kind}"

    _run_035(cur)          # 第二遍
    _run_035(cur)          # 第三遍
    after_replay = _check_defs(cur)
    assert len(after_replay) == 1, f"重放后出现多条同名 CHECK:{after_replay}"
    assert after_replay[0] == after_first[0], "重放改变了 CHECK 定义"


def test_second_replay_does_not_drop_and_readd(cur):
    """形态判据:第二遍重放**什么都不做**。

    无条件 DROP+ADD 每次重放都会出现一个"约束不存在"的窗口;
    prestart 无条件重放时若恰好并发写入,那一瞬间的写不受约束。
    这里用 constraint 的 oid 是否变化来判 —— 没变 = 没被 DROP 过。
    """
    _run_035(cur)
    cur.execute("SELECT oid FROM pg_constraint WHERE conname=%s", (CHECK_NAME,))
    oid_first = int(dict(cur.fetchone())["oid"])
    _run_035(cur)
    cur.execute("SELECT oid FROM pg_constraint WHERE conname=%s", (CHECK_NAME,))
    oid_second = int(dict(cur.fetchone())["oid"])
    assert oid_first == oid_second, "第二遍重放把约束 DROP 重建了 —— 存在无约束窗口"


def _seed_slot(c, *, channel=CHANNEL_IMAGE_NOTE, ordinal=1) -> tuple[str, int, int]:
    # 品牌名必须每次唯一:brands(name, owner_user_id) 有唯一约束,
    # 而 cur fixture 只清 slot 族四张表、不清 brands/quotes(那两张不是本判据的对象)。
    c.execute("INSERT INTO brands(name, owner_user_id) VALUES (%s, 42) RETURNING id",
              (f"槽位测试品牌_{uuid.uuid4().hex[:8]}",))
    brand_id = int(c.fetchone()["id"])
    c.execute("INSERT INTO quotes(brand_id, owner_user_id, status) "
              "VALUES (%s, 42, 'confirmed') RETURNING id", (brand_id,))
    quote_id = int(c.fetchone()["id"])
    c.execute(
        "INSERT INTO geo_article_contract_revisions(revision_key, source_event_key, "
        " source_version, owner_user_id, brand_id, quote_id, authority_snapshot, "
        " authority_snapshot_hash, delivery_count, contract_version) "
        "VALUES (%s,%s,'v1',42,%s,%s,'{}'::jsonb,%s,1,'c1') RETURNING id",
        (uuid.uuid4().hex + uuid.uuid4().hex[:32], uuid.uuid4().hex + uuid.uuid4().hex[:32],
         brand_id, quote_id, uuid.uuid4().hex + uuid.uuid4().hex[:32]))
    revision_id = int(c.fetchone()["id"])
    c.execute(
        "INSERT INTO geo_article_plan_runs(run_key, contract_revision_id, owner_user_id, "
        " brand_id, quote_id, run_mode, compiler_version, input_snapshot, input_snapshot_hash) "
        "VALUES (%s,%s,42,%s,%s,'shadow','c1','{}'::jsonb,%s) RETURNING id",
        (uuid.uuid4().hex + uuid.uuid4().hex[:32], revision_id, brand_id, quote_id,
         uuid.uuid4().hex + uuid.uuid4().hex[:32]))
    run_id = int(c.fetchone()["id"])
    slot_key = str(uuid.uuid4())
    c.execute(
        "INSERT INTO geo_article_delivery_slot_events(event_key, delivery_slot_key, "
        " slot_version, event_kind, target_state, contract_revision_id, plan_run_id, "
        " owner_user_id, brand_id, quote_id, source_version, occurred_at) "
        "VALUES (%s,%s,0,'created','active',%s,%s,42,%s,%s,'v1',now()) RETURNING id",
        (uuid.uuid4().hex + uuid.uuid4().hex[:32], slot_key, revision_id, run_id,
         brand_id, quote_id))
    event_id = int(c.fetchone()["id"])
    c.execute(
        "INSERT INTO geo_article_delivery_slots(delivery_slot_key, contract_revision_id, "
        " contract_ordinal, owner_user_id, brand_id, quote_id, current_state, "
        " projection_version, current_event_id, last_event_key, delivery_channel, "
        " fulfillment_state) "
        "VALUES (%s,%s,%s,42,%s,%s,'active',0,%s,%s,%s,'open')",
        (slot_key, revision_id, ordinal, brand_id, quote_id, event_id,
         uuid.uuid4().hex + uuid.uuid4().hex[:32], channel))
    return slot_key, revision_id, run_id, quote_id, brand_id


def test_new_event_kinds_are_insertable_after_035(cur):
    """PG16 逐种 insert dry-run(规格 §13 要求「每种签发 event 在 PG16 可插」)。"""
    _run_035(cur)
    slot_key, rev, run, quote_id, brand_id = _seed_slot(cur)
    for i, kind in enumerate(NEW_KINDS, start=10):
        cur.execute(
            "INSERT INTO geo_article_delivery_slot_events(event_key, delivery_slot_key, "
            " slot_version, event_kind, target_state, contract_revision_id, plan_run_id, "
            " owner_user_id, brand_id, quote_id, source_version, occurred_at) "
            "VALUES (%s,%s,%s,%s,'active',%s,%s,42,%s,%s,'v1',now())",
            (uuid.uuid4().hex + uuid.uuid4().hex[:32], slot_key, i, kind, rev, run,
             brand_id, quote_id))


def test_invalid_event_kind_is_still_rejected(cur):
    """🔴 变异:绕应用直接 INSERT 一个不在名单里的 event_kind 必须被拒。

    这条证明 035 是"**替换**成更大的封闭集合",不是"删了约束"。
    """
    _run_035(cur)
    slot_key, rev, run, quote_id, brand_id = _seed_slot(cur)
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(
            "INSERT INTO geo_article_delivery_slot_events(event_key, delivery_slot_key, "
            " slot_version, event_kind, target_state, contract_revision_id, plan_run_id, "
            " owner_user_id, brand_id, quote_id, source_version, occurred_at) "
            "VALUES (%s,%s,99,'totally_made_up','active',%s,%s,42,%s,%s,'v1',now())",
            (uuid.uuid4().hex + uuid.uuid4().hex[:32], slot_key, rev, run, brand_id, quote_id))


# ============================================================
# R2. ordinal 守恒 · channel 不进唯一键
# ============================================================

def test_channel_is_not_in_the_unique_key(cur):
    """🔴 RFC R2:`(contract_revision_id, contract_ordinal)` 仍是唯一键,
    `delivery_channel` **不进**去 —— 进了就能让同一 ordinal 在两个渠道各出现一次。"""
    _run_035(cur)
    cur.execute(
        "SELECT a.attname FROM pg_constraint c "
        "  JOIN unnest(c.conkey) k(attnum) ON TRUE "
        "  JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum "
        " WHERE c.conname = 'uq_geo_article_slot_revision_ordinal'")
    cols = {str(dict(r)["attname"]) for r in cur.fetchall()}
    assert cols == {"contract_revision_id", "contract_ordinal"}, cols
    assert "delivery_channel" not in cols


def test_same_ordinal_cannot_exist_in_both_channels(cur):
    """守恒的行为形式:同一 revision+ordinal 想在两个渠道各建一次 → 唯一约束拒绝。"""
    _run_035(cur)
    slot_key, rev, run, quote_id, brand_id = _seed_slot(cur, ordinal=1)
    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute(
            "INSERT INTO geo_article_delivery_slots(delivery_slot_key, contract_revision_id, "
            " contract_ordinal, owner_user_id, brand_id, quote_id, current_state, "
            " projection_version, current_event_id, last_event_key, delivery_channel) "
            "VALUES (%s,%s,1,42,%s,%s,'active',0,"
            " (SELECT id FROM geo_article_delivery_slot_events LIMIT 1),%s,'article')",
            (str(uuid.uuid4()), rev, brand_id, quote_id,
             uuid.uuid4().hex + uuid.uuid4().hex[:32]))


def test_article_slot_cannot_bind_a_geo_post(cur):
    """渠道隔离的 schema 层保证:文章槽位绑图文成品 = 渠道串了。"""
    _run_035(cur)
    slot_key, rev, run, quote_id, brand_id = _seed_slot(cur, channel=CHANNEL_ARTICLE, ordinal=2)
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("UPDATE geo_article_delivery_slots SET geo_post_id = 1 "
                    " WHERE delivery_slot_key = %s", (slot_key,))


# ============================================================
# R3. 唯一写入者 · event + projection CAS
# ============================================================

def test_claim_appends_event_and_cas_projection(txn):
    cur = txn.cursor()
    _run_035(cur)
    slot_key, rev, run, quote_id, brand_id = _seed_slot(cur)
    result = claim(cur, slot_key=slot_key, expected_version=0, identity=IDENTITY,
                   contract_revision_id=rev, plan_run_id=run, quote_id=quote_id,
                   brand_id=brand_id)
    assert result["fulfillment_state"] == FULFILLMENT_CLAIMED
    assert int(result["projection_version"]) == 1
    cur.execute("SELECT event_kind FROM geo_article_delivery_slot_events "
                " WHERE delivery_slot_key=%s AND slot_version=1", (slot_key,))
    assert str(dict(cur.fetchone())["event_kind"]) == "slot_claimed"


def test_slot_write_refuses_autocommit(cur):
    """🔴 "锁了等于没锁" 同族第三例:autocommit 下 CAS 被拒时**事件已落库**。

    后果不是多一行:它占掉 `(delivery_slot_key, slot_version)` 唯一键,
    下一次合法迁移到同一版本直接 UniqueViolation;更严重的是事件溯源的前提
    ——「事件是权威真值」——被破坏:一个被拒绝的操作留下了事件。
    是本包自己的迁移矩阵判据把它顶出来的。
    """
    _run_035(cur)
    slot_key, rev, run, quote_id, brand_id = _seed_slot(cur)
    with pytest.raises(SlotWriteUnsafe, match="显式事务"):
        claim(cur, slot_key=slot_key, expected_version=0, identity=IDENTITY,
              contract_revision_id=rev, plan_run_id=run, quote_id=quote_id,
              brand_id=brand_id)
    # 反向对照:被拒之后**一个事件都没多**(证明闸挡在 append 之前)
    cur.execute("SELECT count(*) AS n FROM geo_article_delivery_slot_events "
                " WHERE delivery_slot_key=%s", (slot_key,))
    assert int(dict(cur.fetchone())["n"]) == 1, "闸没挡住 append —— 孤儿事件仍会产生"


def test_rejected_transition_leaves_no_orphan_event(txn):
    """事务内被拒的迁移,回滚后**零残留** —— 事件日志不说谎。"""
    cur = txn.cursor()
    _run_035(cur)
    slot_key, rev, run, quote_id, brand_id = _seed_slot(cur)
    kw = dict(slot_key=slot_key, identity=IDENTITY, contract_revision_id=rev,
              plan_run_id=run, quote_id=quote_id, brand_id=brand_id)
    txn.commit()
    with pytest.raises(SlotClaimConflict):
        transition(cur, expected_version=0, next_state=FULFILLMENT_READY, **kw)
    txn.rollback()
    cur.execute("SELECT count(*) AS n FROM geo_article_delivery_slot_events "
                " WHERE delivery_slot_key=%s AND slot_version=1", (slot_key,))
    assert int(dict(cur.fetchone())["n"]) == 0, "被拒的迁移留下了孤儿事件"
    txn.rollback()


def test_stale_version_loses_the_cas(txn):
    """🔴 两批并发 claim 同一 slot:一批成功,一批 SLOT_CLAIM_CONFLICT。"""
    cur = txn.cursor()
    _run_035(cur)
    slot_key, rev, run, quote_id, brand_id = _seed_slot(cur)
    claim(cur, slot_key=slot_key, expected_version=0, identity=IDENTITY,
          contract_revision_id=rev, plan_run_id=run, quote_id=quote_id, brand_id=brand_id)
    with pytest.raises(SlotClaimConflict):
        claim(cur, slot_key=slot_key, expected_version=0, identity=IDENTITY,
              contract_revision_id=rev, plan_run_id=run, quote_id=quote_id, brand_id=brand_id)


def test_article_slot_cannot_be_claimed_by_image_note_writer(txn):
    """🔴 RFC R3 渠道隔离:图文写入者碰不到文章槽位,即使 slot_key 被猜到。"""
    cur = txn.cursor()
    _run_035(cur)
    slot_key, rev, run, quote_id, brand_id = _seed_slot(cur, channel=CHANNEL_ARTICLE, ordinal=3)
    with pytest.raises(SlotClaimConflict):
        claim(cur, slot_key=slot_key, expected_version=0, identity=IDENTITY,
              contract_revision_id=rev, plan_run_id=run, quote_id=quote_id, brand_id=brand_id)


def test_transition_matrix_rejects_illegal_predecessor(txn):
    """迁移矩阵列全部允许前驱,不用"不等于终态"的反向写法。"""
    cur = txn.cursor()
    _run_035(cur)
    slot_key, rev, run, quote_id, brand_id = _seed_slot(cur)
    kw = dict(slot_key=slot_key, identity=IDENTITY, contract_revision_id=rev,
              plan_run_id=run, quote_id=quote_id, brand_id=brand_id)
    # open → ready 非法(必须先 claimed/generating)
    with pytest.raises(SlotClaimConflict):
        transition(cur, expected_version=0, next_state=FULFILLMENT_READY, **kw)
    # 正向链路必须走得通,否则上面那条"拒绝"可能是"什么都拒"
    claim(cur, expected_version=0, **kw)
    transition(cur, expected_version=1, next_state=FULFILLMENT_GENERATING, **kw)
    out = transition(cur, expected_version=2, next_state=FULFILLMENT_READY, **kw)
    assert out["fulfillment_state"] == FULFILLMENT_READY


def test_release_returns_slot_to_open(txn):
    cur = txn.cursor()
    _run_035(cur)
    slot_key, rev, run, quote_id, brand_id = _seed_slot(cur)
    kw = dict(slot_key=slot_key, identity=IDENTITY, contract_revision_id=rev,
              plan_run_id=run, quote_id=quote_id, brand_id=brand_id)
    claim(cur, expected_version=0, **kw)
    out = release(cur, expected_version=1, **kw)
    assert out["fulfillment_state"] == FULFILLMENT_OPEN


# ============================================================
# R1. 四前置 + 存量零 enroll
# ============================================================

def test_activation_blocked_when_flag_off(cur):
    _run_035(cur)
    _, _, _, quote_id, _ = _seed_slot(cur)
    blockers = activation_blockers(cur, quote_id=quote_id, contract_lane_enabled=False,
                                   sidecar_schema_blockers=[], image_note_schema_blockers=[])
    assert any(b.startswith("flag_disabled") for b in blockers)


def test_activation_blocked_when_quote_not_enrolled(cur):
    """🔴 存量 quote **零自动 enroll**。"""
    _run_035(cur)
    _, _, _, quote_id, _ = _seed_slot(cur)
    blockers = activation_blockers(cur, quote_id=quote_id, contract_lane_enabled=True,
                                   sidecar_schema_blockers=[], image_note_schema_blockers=[])
    assert "quote_not_enrolled" in blockers


def test_activation_passes_only_when_all_four_hold(cur):
    """反向对照:四条全满足才放行,否则"总是被挡"是恒真。"""
    _run_035(cur)
    _, _, _, quote_id, _ = _seed_slot(cur)
    cur.execute("UPDATE quotes SET article_plan_writing_mode='image_note_contract', "
                " article_plan_enrolled_at=now() WHERE id=%s", (quote_id,))
    assert activation_blockers(cur, quote_id=quote_id, contract_lane_enabled=True,
                               sidecar_schema_blockers=[],
                               image_note_schema_blockers=[]) == []


def test_activation_never_auto_enrolls(cur):
    """求 blockers **不许**顺手把 quote enroll 了 —— 那就是 RFC 禁止的批量补造。"""
    _run_035(cur)
    _, _, _, quote_id, _ = _seed_slot(cur)
    activation_blockers(cur, quote_id=quote_id, contract_lane_enabled=True,
                        sidecar_schema_blockers=[], image_note_schema_blockers=[])
    cur.execute("SELECT article_plan_enrolled_at FROM quotes WHERE id=%s", (quote_id,))
    assert dict(cur.fetchone())["article_plan_enrolled_at"] is None, "求值过程写了 quotes"


# ============================================================
# C. caller census · 全量分母自证
# ============================================================

def test_caller_census_covers_the_full_denominator():
    """🔴 附加约束 C:用**结构锚**取全集,不点名式列举。

    锚 = 表名 `geo_article_delivery_slots` 在全仓 .py/.sql 的出现。
    每个文件必须落进四类之一,**没有第五类**;分母对不上就红。
    """
    out = subprocess.run(
        ["git", "grep", "-l", "geo_article_delivery_slots", "--", "*.py", "*.sql"],
        cwd=REPO, capture_output=True, text=True)
    files = {line.strip() for line in out.stdout.splitlines() if line.strip()}
    assert files, "结构锚零命中 —— census 没有分母,后面全是恒真"

    census = {
        # ① 图文侧唯一写入者(本包新建)
        "writer_image_note": {"services/geo_douyin/delivery_slots.py"},
        # ② 只读消费方(WP7 投影)—— 它读 slot 只为算「已分配容量」,零写。
        #    🔴 它是被本 census **抓出来**才补进来的:WP7 新建 `_SLOT_CAPACITY_SQL`
        #       时没登记,本条当场红。这正是结构锚 census 的用处 ——
        #       靠人记得去登记是靠不住的,靠"读了这张表就必须归类"才靠得住。
        "read_only_projection": {
            "services/publication_stage_sources.py",
            # 🔴 第二次被本 census 抓出来:2026-08-18 给 build_delivery_plan 接上
            #    active 读侧分支时,它成了新的 slot 读者却没登记。
            #    两次都不是"我忘了",而是"新增读者的受影响面不在自己的文件里" ——
            #    这正是结构锚 census 存在的意义:不靠人记得,靠读了这张表就必须归类。
            "services/geo_douyin/delivery_plan.py",
            # 🔴 第三次(2026-08-18 返工链 3):worker 生成成功后要把槽位推进到
            #    `ready`。它**经 delivery_slots.transition 写**,不自己发 SQL ——
            #    归到读侧是因为它对这张表的直接接触只是 `_load_production_context`
            #    里那条 LEFT JOIN(读 projection_version)。写仍然只有一个入口。
            "services/geo_douyin/contract_worker.py",
        },
        # ③ 文章侧既有读写方 —— cutover 由 ARTICLE_CHANNEL_PREDICATE 统一口径
        "article_lane": {
            "services/article_delivery_plan.py",
            "services/article_closed_loop_metadata.py",
            "services/article_closed_loop_queries.py",
            "db/diagnosis_db.py",
        },
        # ④ schema 契约与迁移(不产生运行时读写)
        "schema": {
            "services/article_closed_loop_schema_contract.py",
            "scripts/migration_geo_article_closed_loop_v1_2026_07_20.sql",
            "db/migration_029_gap_operation_plan_2026_08_08.sql",
            "db/migration_035_geo_image_note_slot_channel_2026_08_18.sql",
        },
    }
    classified = set().union(*census.values())
    tests = {f for f in files if f.startswith("tests/")}
    unclassified = files - classified - tests
    assert not unclassified, (
        "census 漏了这些 slot 消费方(每个都必须显式归类,不许默认放过):\n%s"
        % sorted(unclassified)
    )
    # 反向对照:分类表里不许有**已不存在**的文件(census 比现实活得久 = 骗人)
    stale = {f for f in classified if not (REPO / f).is_file()}
    assert not stale, f"census 里的文件已不存在:{sorted(stale)}"


def test_article_sidecar_readiness_still_green_after_035(cur):
    """🔴 035 改了 `ck_geo_article_slot_event_kind`,而
    `article_closed_loop_schema_contract` 把该定义**逐字钉死**
    (它自己的注释写着「substring 检查会接受被削弱的谓词」,所以用全定义比对)。

    不同步改那份契约 ⇒ 文章 sidecar 的 readiness 立刻报 wrong_constraint_definition ——
    一个由我们自己制造的假红。这条锁死"035 与契约同步"这件事。
    """
    from services.article_closed_loop_schema_contract import schema_blockers as article_blockers

    before = [b for b in article_blockers(cur) if "slot_event_kind" in b]
    assert before == [], f"035 之前文章契约就不绿:{before}"
    _run_035(cur)
    after = [b for b in article_blockers(cur) if "slot_event_kind" in b]
    assert after == [], f"035 之后文章 sidecar readiness 报假红:{after}"


def test_channel_predicates_have_a_single_source():
    """两个渠道谓词**只有一份**。四处各写一遍必然漂移。"""
    assert "IS NULL" in ARTICLE_CHANNEL_PREDICATE, "文章侧没兼容历史 NULL 行"
    assert "'article'" in ARTICLE_CHANNEL_PREDICATE
    assert "IS NULL" not in IMAGE_NOTE_CHANNEL_PREDICATE, (
        "图文侧把 NULL 历史行也收进来了 —— 会把文章槽位当成自己的"
    )
    assert "'douyin_image_note'" in IMAGE_NOTE_CHANNEL_PREDICATE


def test_image_note_writer_only_emits_the_five_new_kinds():
    """图文写入者不发文章侧那 9 种事件 —— 越界会让两条链互相踩。"""
    assert IMAGE_NOTE_EVENT_KINDS == set(NEW_KINDS)
    assert not (IMAGE_NOTE_EVENT_KINDS & set(OLD_KINDS))
