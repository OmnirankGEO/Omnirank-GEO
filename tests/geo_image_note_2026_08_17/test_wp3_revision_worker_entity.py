"""WP3 · 成品版本 CAS / durable worker 恢复 / 冻结实体 / 租户作用域(03 §6)。

含 WP2 交验时如实标注为 ⬜ 的两项(本轮有载体了):
  · 删除 tenant predicate 必须转红
  · freeze 后各 kill point 恢复(pending / running / 已 external-start 三种)
"""
from __future__ import annotations

import os
import pathlib
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import RealDictCursor  # noqa: E402

from services.geo_douyin.durable_worker import (  # noqa: E402
    LeaseLost, claim_next_task, finish_task, mark_external_start,
    reconcile_stuck_external_tasks, renew_lease,
)
from services.geo_douyin.frozen_entity import (  # noqa: E402
    DISPLAY_NAME_MAX, SKELETON_PER_ENTITY, SKELETON_PER_FEATURE, SKELETON_PER_QUESTION,
    apply_frozen_entities, assert_card_entity_identity, choose_skeleton,
    freeze_entities, mark_stale_cards,
)
from services.geo_douyin.post_revisions import (  # noqa: E402
    GenerationSuperseded, RevisionConflict, activate_revision, begin_generation,
    compute_render_input_hash, etag_for, parse_etag,
    replace_card_in_active_revision, stage_revision, supersede_active_tasks,
)
from services.geo_douyin.tenant_scope import (  # noqa: E402
    ScopeUnavailable, assert_object_in_scope, build_post_scope,
)

REPO = pathlib.Path(__file__).resolve().parents[2]
MIGRATION = REPO / "db" / "migration_034_geo_image_note_contract_2026_08_17.sql"
PROD_SCHEMA = pathlib.Path(os.getenv(
    "GEOIMG_PROD_SCHEMA_SQL", r"C:/AI-Test/.deploy_toolkit/_geoimg_prodschema_20260817.sql"))
DSN = os.getenv("TEST_DATABASE_URL")


# ============================================================
# A. 冻结实体(不连库 · 纯行为)
# ============================================================

def test_frozen_entity_overrides_model_visible_name():
    """🔴 品牌占位 BUG 的根治判据。

    模型给了一个占位名,冻结 DTO 给的是真名 —— 卡上**三处**(引用/可见/生图 headline)
    都必须变成真名。删掉 apply_frozen_entities 里任一处赋值,这条转红。
    """
    frozen = freeze_entities([{"display_name": "深圳市恒通电梯有限公司"}], source="ranking")
    cards = [{"role": "entity", "entity": "XX品牌", "headline": "XX品牌", "entity_ref": ""}]
    apply_frozen_entities(cards, frozen)

    assert cards[0]["entity_ref"] == "深圳市恒通电梯有限公司", "引用字段没同源"
    assert cards[0]["entity"] == "深圳市恒通电梯有限公司"[:DISPLAY_NAME_MAX], "可见名没同源"
    assert cards[0]["headline"] == cards[0]["entity"], "生图 headline 没同源"
    assert "XX品牌" not in str(cards[0]), "模型给的占位名还留在卡上"


def test_non_entity_cards_are_untouched():
    """反向对照:封面/收尾卡没有实体主体,强写会把封面标题也覆盖掉。"""
    frozen = freeze_entities([{"display_name": "真实企业"}], source="ranking")
    cards = [{"role": "cover", "entity": "封面标题", "headline": "封面标题"}]
    apply_frozen_entities(cards, frozen)
    assert cards[0]["headline"] == "封面标题"


def test_no_candidates_never_produces_per_entity():
    """没有真实候选 → **不生成虚拟品牌实体**,改走问题卡/卖点卡(规格 §10.2)。"""
    assert choose_skeleton([]) == SKELETON_PER_QUESTION
    assert choose_skeleton([], requested=SKELETON_PER_ENTITY) == SKELETON_PER_QUESTION
    assert choose_skeleton([], requested=SKELETON_PER_FEATURE) == SKELETON_PER_FEATURE
    # 反向对照:有候选时才允许 per_entity,否则"永远不 per_entity"是恒真
    frozen = freeze_entities([{"display_name": "真实企业"}], source="kb")
    assert choose_skeleton(frozen) == SKELETON_PER_ENTITY


def test_nameless_candidate_is_dropped_not_invented():
    """没有名字的候选**丢弃**,不许编一个 —— 编名字正是本模块要消灭的事。"""
    frozen = freeze_entities([{"display_name": ""}, {"name": "有名字的"}], source="kb")
    assert [e.display_name for e in frozen] == ["有名字的"]


def test_identity_check_catches_tampered_visible_name():
    """渲染前结构化校验:可见名被改成别的字符串 → 报不一致。"""
    frozen = freeze_entities([{"display_name": "真实企业名称"}], source="kb")
    cards = [{"role": "entity", "entity": "真实企业名称", "headline": "真实企业名称",
              "entity_ref": "真实企业名称", "frozen_entity": frozen[0].to_dto()}]
    assert assert_card_entity_identity(cards, frozen) == []      # 正向
    cards[0]["entity"] = "冒牌企业"
    cards[0]["headline"] = "冒牌企业"
    problems = assert_card_entity_identity(cards, frozen)
    assert len(problems) == 1 and problems[0].card_index == 0


def test_bad_card_is_marked_stale_without_killing_the_others():
    """规格 §10.2 末 + 01 §4.4:只标那一张,其他卡与整篇保存不受影响。"""
    frozen = freeze_entities(
        [{"display_name": "企业甲"}, {"display_name": "企业乙"}], source="kb")
    cards = [{"role": "entity"}, {"role": "entity"}]
    apply_frozen_entities(cards, frozen)
    cards[1]["entity"] = "被改坏了"
    problems = assert_card_entity_identity(cards, frozen)
    marked = mark_stale_cards(cards, problems)

    assert marked == [1]
    assert cards[1]["entity_identity_stale"] is True
    assert cards[1]["user_message"] == "这张图的品牌名称需要重做"
    assert "entity_identity_stale" not in cards[0], "好卡被连坐了"


def test_image_pipeline_wiring():
    """接线锁(源码形态):生图管线里 headline 的取值必须经过 frozen_entity 分支。"""
    import inspect

    from services.geo_douyin import image_pipeline

    src = inspect.getsource(image_pipeline)
    assert "frozen_entity" in src, "生图管线没接冻结实体 —— 冻结逻辑是死代码"
    assert "_headline" in src, "headline 仍是就地取模型字段"


# ============================================================
# B. 租户作用域(不连库 · 谓词与对象级校验)
# ============================================================

def test_scope_includes_colleague_and_excludes_revoked():
    """双向都要对:同 owner 合法同事可见 / 撤权后原创建者不可见。"""
    sql, params = build_post_scope(tenant_owner_user_id=7,
                                   authorized_brand_ids=[36, 40], actor_user_id=99)
    assert "tenant_owner_user_id = %s" in sql
    assert "brand_id IN (%s, %s)" in sql
    assert params[:3] == [7, 36, 40]
    # created_by 只作为**历史行**的兜底,不是主授权
    assert "created_by = %s" in sql and "tenant_owner_user_id IS NULL" in sql


def test_scope_never_filters_by_created_by_alone():
    """🔴 规格点名的 P0:`created_by` 不能代替当前授权。

    判据形态:`created_by` 只允许出现在**带 `tenant_owner_user_id IS NULL` 限定**的
    那一支里。谁把它提成独立条件,这条转红。
    """
    sql, _ = build_post_scope(tenant_owner_user_id=7,
                              authorized_brand_ids=[36], actor_user_id=99)
    for branch in sql.strip("()").split(" OR "):
        if "created_by" in branch:
            assert "tenant_owner_user_id IS NULL" in branch, (
                f"created_by 成了独立授权条件:{branch}"
            )


def test_empty_authorization_yields_false_predicate_not_open_scope():
    """fail-closed:授权集合为空时返回恒假,**绝不**放行全量。"""
    sql, params = build_post_scope(tenant_owner_user_id=7,
                                   authorized_brand_ids=[], actor_user_id=None)
    assert sql == "FALSE" and params == []


def test_no_identity_at_all_raises():
    with pytest.raises(ScopeUnavailable):
        build_post_scope(tenant_owner_user_id=None, authorized_brand_ids=[], actor_user_id=None)


def test_object_level_check_rejects_revoked_and_cross_tenant():
    row = {"tenant_owner_user_id": 7, "brand_id": 36, "created_by": 99}
    assert_object_in_scope(row, tenant_owner_user_id=7,
                           authorized_brand_ids=[36], actor_user_id=99)   # 正向
    # 撤权:brand 不再在授权集合里 → 连创建者也读不到
    with pytest.raises(ScopeUnavailable, match="无法操作"):
        assert_object_in_scope(row, tenant_owner_user_id=7,
                               authorized_brand_ids=[], actor_user_id=99)
    # 跨租户
    with pytest.raises(ScopeUnavailable):
        assert_object_in_scope(row, tenant_owner_user_id=8,
                               authorized_brand_ids=[36], actor_user_id=99)


def test_legacy_row_visible_only_to_creator_never_guessed():
    """历史行(owner 为 NULL)只对创建者可见;不拿 brand 反推 owner(那是猜绑)。"""
    legacy = {"tenant_owner_user_id": None, "brand_id": 36, "created_by": 99}
    assert_object_in_scope(legacy, tenant_owner_user_id=7,
                           authorized_brand_ids=[36], actor_user_id=99)
    with pytest.raises(ScopeUnavailable, match="不猜绑"):
        assert_object_in_scope(legacy, tenant_owner_user_id=7,
                               authorized_brand_ids=[36], actor_user_id=1234)


# ============================================================
# C. ETag / render hash(不连库)
# ============================================================

def test_etag_roundtrip_and_rejects_garbage():
    assert parse_etag(etag_for(101, 7)) == (101, 7)
    for bad in ("", "post-101", '"rev-7"', "post-x-rev-y", "not-an-etag"):
        assert parse_etag(bad) is None, f"{bad!r} 被解析成了合法 ETag —— 不许猜"


@pytest.mark.parametrize("field,value", [
    ("topic_snapshot_hash", "other"), ("style_key", "photo_overlay"),
    ("ranking_template", "tpl_b"), ("style_catalog_version", "styles-v2"),
    ("card_count", 6), ("aspect_ratio", "9:16"),
    ("content_form", "ranking"), ("contact_enabled", True),
])
def test_render_input_hash_covers_every_axis(field, value):
    base = dict(topic_snapshot_hash="h", style_key="table_review", ranking_template=None,
                style_catalog_version="styles-v1", card_count=4, aspect_ratio="3:4",
                content_form="selection_cards", contact_enabled=False)
    assert compute_render_input_hash(**{**base, field: value}) != compute_render_input_hash(**base), \
        f"{field} 没进 render_input_hash"


# ============================================================
# D. PG16 真库:revision CAS / durable worker 三种崩溃恢复
# ============================================================

pytestmark_db = pytest.mark.skipif(
    not DSN or not PROD_SCHEMA.is_file(), reason="需要 TEST_DATABASE_URL 与生产 schema 夹具")


def _admin_dsn() -> str:
    return DSN.rsplit("/", 1)[0] + "/postgres"


@pytest.fixture(scope="module")
def db():
    if not DSN or not PROD_SCHEMA.is_file():
        pytest.skip("需要 TEST_DATABASE_URL 与生产 schema 夹具")
    name = f"geoimg_test_wp3_{uuid.uuid4().hex[:8]}"
    admin = psycopg2.connect(_admin_dsn())
    admin.autocommit = True
    admin.cursor().execute(f'CREATE DATABASE "{name}"')
    admin.close()

    dsn = DSN.rsplit("/", 1)[0] + "/" + name
    conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("\n".join(
        line for line in PROD_SCHEMA.read_text(encoding="utf-8", errors="ignore").splitlines()
        if not line.startswith("\\restrict") and not line.startswith("\\unrestrict")))
    cur.execute("SET search_path = public")
    cur.execute(MIGRATION.read_text(encoding="utf-8"))
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
    c.execute("DELETE FROM geo_douyin_post_revisions")
    c.execute("DELETE FROM geo_douyin_post_tasks")
    c.execute("DELETE FROM geo_douyin_posts")
    try:
        yield c
    finally:
        conn.close()


def _new_post(cur, *, brand_id=36, created_by=99) -> int:
    cur.execute(
        "INSERT INTO geo_douyin_posts (brand_id, created_by, keyword, content_type, status) "
        "VALUES (%s,%s,'测试词','image_post','draft') RETURNING id", (brand_id, created_by))
    return int(cur.fetchone()["id"])


def _new_task(cur, post_id: int, *, status="pending", external=False,
              lease_expired=False, worker=None) -> int:
    cur.execute(
        "INSERT INTO geo_douyin_post_tasks (post_id, user_id, task_ref, status) "
        "VALUES (%s, 99, %s, %s) RETURNING id",
        (post_id, f"ref_{uuid.uuid4().hex[:12]}", status))
    task_id = int(cur.fetchone()["id"])
    if lease_expired:
        cur.execute("UPDATE geo_douyin_post_tasks SET lease_expires_at = now() - interval '1 hour', "
                    "lease_owner = %s WHERE id = %s", (worker or "dead_worker", task_id))
    if external:
        cur.execute("UPDATE geo_douyin_post_tasks SET external_started_at = now() - interval '2 hours' "
                    "WHERE id = %s", (task_id,))
    return task_id


def test_late_generation_cannot_overwrite_active(cur):
    """🔴 03 §6 头条:两个并发 regenerate,B 快成功后 A 迟到只能 superseded。"""
    post_id = _new_post(cur)
    # 协调器真实次序:接管 → 建 task → begin_generation
    supersede_active_tasks(cur, geo_post_id=post_id)
    task_a = _new_task(cur, post_id)
    epoch_a = int(begin_generation(cur, geo_post_id=post_id, task_id=task_a)["generation_epoch"])

    # B 要开新一代:先接管 A(034 的部分唯一约束不允许两个在途任务并存),再建 B
    taken_over = supersede_active_tasks(cur, geo_post_id=post_id)
    assert task_a in taken_over, "开新一代前没有接管上一代"
    task_b = _new_task(cur, post_id)
    epoch_b = int(begin_generation(cur, geo_post_id=post_id, task_id=task_b)["generation_epoch"])
    assert epoch_b > epoch_a

    rev_b = stage_revision(cur, geo_post_id=post_id, created_by=99,
                           operation_kind="regenerate", title="B 版")
    activate_revision(cur, geo_post_id=post_id,
                      post_revision_id=int(rev_b["post_revision_id"]),
                      task_id=task_b, epoch=epoch_b)

    rev_a = stage_revision(cur, geo_post_id=post_id, created_by=99,
                           operation_kind="regenerate", title="A 版(迟到)")
    with pytest.raises(GenerationSuperseded):
        activate_revision(cur, geo_post_id=post_id,
                          post_revision_id=int(rev_a["post_revision_id"]),
                          task_id=task_a, epoch=epoch_a)

    cur.execute("SELECT active_revision_id FROM geo_douyin_posts WHERE id=%s", (post_id,))
    assert int(cur.fetchone()["active_revision_id"]) == int(rev_b["post_revision_id"]), \
        "迟到任务把 active 覆盖了"


def test_redraw_on_superseded_revision_is_rejected(cur):
    """03 §6:新版成功后旧版 card redraw 完成 → replace CAS 拒绝(资金由调用方收敛)。"""
    post_id = _new_post(cur)
    supersede_active_tasks(cur, geo_post_id=post_id)
    t1 = _new_task(cur, post_id)
    e1 = int(begin_generation(cur, geo_post_id=post_id, task_id=t1)["generation_epoch"])
    rev1 = stage_revision(cur, geo_post_id=post_id, created_by=99,
                          operation_kind="create",
                          cards_snapshot=[{"role": "entity"}, {"role": "entity"}])
    activate_revision(cur, geo_post_id=post_id,
                      post_revision_id=int(rev1["post_revision_id"]), task_id=t1, epoch=e1)

    supersede_active_tasks(cur, geo_post_id=post_id)
    t2 = _new_task(cur, post_id)
    e2 = int(begin_generation(cur, geo_post_id=post_id, task_id=t2)["generation_epoch"])
    rev2 = stage_revision(cur, geo_post_id=post_id, created_by=99,
                          operation_kind="regenerate",
                          cards_snapshot=[{"role": "entity"}, {"role": "entity"}])
    activate_revision(cur, geo_post_id=post_id,
                      post_revision_id=int(rev2["post_revision_id"]), task_id=t2, epoch=e2)

    # 旧版的单卡重绘现在才回来
    ok = replace_card_in_active_revision(
        cur, post_revision_id=int(rev1["post_revision_id"]), card_index=0,
        card={"role": "entity", "entity": "迟到的图"}, asset_manifest={})
    assert ok is False, "旧 revision 上的重绘被写进去了 —— 旧图会插回新版"
    # 反向对照:对当前 active 版重绘必须成功
    assert replace_card_in_active_revision(
        cur, post_revision_id=int(rev2["post_revision_id"]), card_index=0,
        card={"role": "entity", "entity": "新图"}, asset_manifest={}) is True


# ---- durable worker · 三种崩溃点 ----

def test_pending_task_is_claimable(cur):
    post_id = _new_post(cur)
    _new_task(cur, post_id, status="pending")
    claimed = claim_next_task(cur, worker="w1")
    assert claimed is not None and claimed["lease_owner"] == "w1"


def test_running_with_expired_lease_and_no_external_start_is_reclaimable(cur):
    """崩溃点②:租约过期但**没有**外部副作用 → 重跑安全,可重领。"""
    post_id = _new_post(cur)
    _new_task(cur, post_id, status="running", lease_expired=True, worker="dead")
    claimed = claim_next_task(cur, worker="w2")
    assert claimed is not None and claimed["lease_owner"] == "w2"


def test_running_after_external_start_is_never_reclaimed(cur):
    """🔴 崩溃点③:已 external-start 的任务**永远不进可领集合**。

    删掉 claim SQL 里的 `external_started_at IS NULL`,这条转红 ——
    而那正是"崩溃恢复变成重复外调 + 重复扣费"的入口。
    """
    post_id = _new_post(cur)
    _new_task(cur, post_id, status="running", lease_expired=True,
              worker="dead", external=True)
    assert claim_next_task(cur, worker="w3") is None, "已外调的任务被重领了"


def test_stuck_external_task_converges_to_needs_action_not_release(cur):
    """结果未知 → `needs_action` + settlement `manual`,**不 release**。

    release 意味着"确定没发生",而我们恰恰不确定(规格 §8.2 末)。
    """
    post_id = _new_post(cur)
    task_id = _new_task(cur, post_id, status="running", lease_expired=True,
                        worker="dead", external=True)
    reclaimed = reconcile_stuck_external_tasks(cur, grace_seconds=0)
    assert task_id in reclaimed
    cur.execute("SELECT status, settlement_status FROM geo_douyin_post_tasks WHERE id=%s", (task_id,))
    row = cur.fetchone()
    assert row["status"] == "needs_action"
    assert row["settlement_status"] == "manual", "结果未知却被 release 了"


def test_lease_lost_blocks_stale_worker_from_writing(cur):
    """被接管的老 worker 续不上租约,也写不了终态和 external-start。"""
    post_id = _new_post(cur)
    _new_task(cur, post_id, status="pending")
    claimed = claim_next_task(cur, worker="w1")
    task_id = int(claimed["id"])

    # 模拟接管
    cur.execute("UPDATE geo_douyin_post_tasks SET lease_owner='w2' WHERE id=%s", (task_id,))
    for call in (
        lambda: renew_lease(cur, task_id=task_id, worker="w1"),
        lambda: mark_external_start(cur, task_id=task_id, worker="w1"),
        lambda: finish_task(cur, task_id=task_id, worker="w1", status="succeeded"),
    ):
        with pytest.raises(LeaseLost):
            call()
    # 反向对照:当前持有者可以正常做这三件事
    renew_lease(cur, task_id=task_id, worker="w2")
    mark_external_start(cur, task_id=task_id, worker="w2")
    finish_task(cur, task_id=task_id, worker="w2", status="succeeded")


def test_external_start_timestamp_is_not_overwritten_on_replay(cur):
    """恢复路径重放时不能把"什么时候开始外调的"改新。"""
    post_id = _new_post(cur)
    _new_task(cur, post_id, status="pending")
    claimed = claim_next_task(cur, worker="w1")
    task_id = int(claimed["id"])
    first = mark_external_start(cur, task_id=task_id, worker="w1")["external_started_at"]
    again = mark_external_start(cur, task_id=task_id, worker="w1")["external_started_at"]
    assert first == again, "external_started_at 被重放覆盖了 —— 时间事实漂移"


def test_two_workers_never_claim_the_same_task(cur):
    """反向对照:并发领取不重复。分母先立住 —— 只有一个任务时,第二个 worker 必须领空。"""
    post_id = _new_post(cur)
    _new_task(cur, post_id, status="pending")
    first = claim_next_task(cur, worker="w1")
    second = claim_next_task(cur, worker="w2")
    assert first is not None and second is None
