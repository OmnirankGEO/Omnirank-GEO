"""#184 d1 判据 —— 普通生产链加入代际机制。

全部是**行为臂**:真建库、真跑那三条 SQL、真比 CAS。不 grep 源码。
"""
import json

import psycopg2
import pytest

from .conftest import (
    USER_ID, make_post, post_row, revisions_of, tasks_of,
)


_REF_SEQ = [0]


def _new_task_with_generation(post_id, *, task_ref="t"):
    """🔴 `task_ref` 上有 UNIQUE(`uq_geo_douyin_post_tasks_ref`,生产 schema 里就有)——
    同一个串复用第二次就 UniqueViolation。手写夹具不会有这条约束,
    于是判据会在一个生产上不可能存在的世界里全绿。"""
    from db import geo_douyin_db as ddb
    _REF_SEQ[0] += 1
    return ddb.create_task_with_generation(
        post_id=post_id, user_id=USER_ID,
        task_ref="%s-%d-%d" % (task_ref, post_id, _REF_SEQ[0]),
        freeze_id=None, progress_total=3)


def _freeze(post_id, task_id, epoch):
    from services.geo_douyin.production_task import _freeze_ordinary_revision
    return _freeze_ordinary_revision(post_id, USER_ID, task_id, epoch)


# ── 改前实测:今天不接管就插任务,孤儿 pending 会把重绘永久焊死 ──────────
def test_before_plain_create_task_wedges_the_post_forever(monkeypatch):
    """这条**不测我的修复**,测的是修复前那条路今天的行为(Review 要的「改前一行」)。

    `create_task` 只插 `pending`,不接管在途任务。进程重启/崩在中途留下的
    孤儿 pending 于是永远在那儿,而 034 的唯一索引只允许一个 ——
    ⇒ **这条作品此后每一次重做都插不进去**,而且永远插不进去。
    """
    from db import geo_douyin_db as ddb

    post_id = make_post()
    first = ddb.create_task(post_id=post_id, user_id=USER_ID,
                            task_ref="orphan-%d" % post_id,
                            freeze_id=None, progress_total=3)
    assert first > 0
    # 模拟"进程没了,任务留在 pending"——什么都不做就是现场。
    # task_ref 换成新的,证明撞的是**代际那条**唯一索引,不是 task_ref 那条。
    with pytest.raises(psycopg2.errors.UniqueViolation) as err:
        ddb.create_task(post_id=post_id, user_id=USER_ID,
                        task_ref="retry-%d" % post_id,
                        freeze_id=None, progress_total=3)
    assert "uq_geo_douyin_task_active_generation" in str(err.value)


# ── d1 主线:建任务即开代际,落 ready 前冻出 active revision ───────────
def test_d1_task_creation_opens_a_generation(monkeypatch):
    post_id = make_post()
    task_id, epoch = _new_task_with_generation(post_id)
    row = post_row(post_id)
    assert row["active_generation_task_id"] == task_id
    assert int(row["generation_epoch"]) == epoch >= 1


def test_d1_freeze_makes_the_post_point_at_a_real_revision():
    """没有这一步,v2 素材准备对每一篇都 409 SOURCE_NOT_READY。"""
    post_id = make_post(cards=3)
    task_id, epoch = _new_task_with_generation(post_id)

    revision_id = _freeze(post_id, task_id, epoch)

    assert revision_id is not None
    row = post_row(post_id)
    assert row["active_revision_id"] == revision_id      # 指针指过去了
    revs = revisions_of(post_id)
    assert len(revs) == 1 and revs[0]["post_revision_id"] == revision_id
    assert str(revs[0]["status"]) == "active"


# ── J1 版本内容完整性:不是"有个 id"就算数 ────────────────────────────
@pytest.mark.parametrize("cards", [1, 3, 7])
def test_j1_revision_carries_every_asset_and_a_recomputable_hash(cards):
    post_id = make_post(cards=cards)
    task_id, epoch = _new_task_with_generation(post_id)
    revision_id = _freeze(post_id, task_id, epoch)
    assert revision_id is not None

    rev = revisions_of(post_id)[0]
    manifest = rev["asset_manifest"]
    if isinstance(manifest, str):
        manifest = json.loads(manifest)
    # 恰好 N 个资产 —— 少一个就是"有 id 的空壳",发出去会缺图
    assert len(manifest["oss_keys"]) == cards, manifest
    assert int(manifest["card_count"]) == cards
    assert manifest["cover_oss_key"] == post_row(post_id)["oss_keys"][0]

    # hash 能按同一份输入重算出来(不是随便存了个串)。
    # 🔴 用**模块自己那只**重算器 `compute_manifest_hash`,不是随手挑一个哈希函数:
    #    发布链核身份用的就是它;拿别的函数算出来"对不上",红的是判据不是代码。
    from services.geo_douyin.post_revisions import compute_manifest_hash
    assert rev["manifest_hash"] == compute_manifest_hash(manifest), (
        "manifest_hash 与 manifest 对不上 —— 发布链按 hash 核身份,对不上就发不出去")


# ── J2 次序:并发重做不许把 UniqueViolation 冒到用户面前 ────────────────
def test_j2_second_generation_supersedes_the_first_without_unique_violation():
    post_id = make_post()
    first_task, first_epoch = _new_task_with_generation(post_id, task_ref="gen1")

    # 第一代还在 pending 就发起第二代(= 用户在制作中点了重做)
    second_task, second_epoch = _new_task_with_generation(post_id, task_ref="gen2")

    assert second_epoch == first_epoch + 1
    rows = {int(t["id"]): t for t in tasks_of(post_id)}
    assert rows[first_task]["superseded_at"] is not None
    assert str(rows[first_task]["status"]) == "superseded"
    # 在途的只剩一个 —— 唯一索引的那一格
    live = [t for t in rows.values()
            if str(t["status"]) in ("pending", "running") and t["superseded_at"] is None]
    assert len(live) == 1 and int(live[0]["id"]) == second_task


# ── J3 输家:CAS 输了不许写 active,也不许把 ready 写成自己的 ────────────
def test_j3_loser_gets_none_and_is_marked_superseded_without_activating():
    post_id = make_post()
    loser_task, loser_epoch = _new_task_with_generation(post_id, task_ref="loser")
    # 新一代接管(用户又点了一次重做)
    winner_task, _ = _new_task_with_generation(post_id, task_ref="winner")

    # 输家现在才做完,拿着**过期的** epoch 来冻
    result = _freeze(post_id, loser_task, loser_epoch)

    assert result is None, "CAS 输了必须返回 None,不能当成功继续"
    row = post_row(post_id)
    assert row["active_revision_id"] is None, "输家把指针抢过去了 —— 库里会是另一版"
    assert row["active_generation_task_id"] == winner_task
    rows = {int(t["id"]): t for t in tasks_of(post_id)}
    assert str(rows[loser_task]["status"]) == "superseded"
    # 输家 staged 的那一版(若有)绝不能是 active
    assert all(str(r["status"]) != "active" for r in revisions_of(post_id))


def test_j3b_winner_can_still_freeze_after_the_loser_lost():
    """反向对照:输家出局之后,赢家照样冻得成。

    少了这条,把 `freeze_active_revision` 改成"永远返回 None"也能让 J3 绿。
    """
    post_id = make_post()
    loser_task, loser_epoch = _new_task_with_generation(post_id, task_ref="loser")
    winner_task, winner_epoch = _new_task_with_generation(post_id, task_ref="winner")

    assert _freeze(post_id, loser_task, loser_epoch) is None
    revision_id = _freeze(post_id, winner_task, winner_epoch)

    assert revision_id is not None
    assert post_row(post_id)["active_revision_id"] == revision_id


# ── 读面:A 的 #186 面板靠这个字段筛「可发布」,必须有人钉住 ────────────
def test_posts_projection_exposes_active_revision_id():
    """`_POST_FIELDS` 是列表与详情共用的那份列清单。

    034 加了 `active_revision_id`,而这份清单**没跟上**(仓里的注释原话),
    于是详情侧只好再直读一次兜底、列表侧干脆没有。A 的面板要靠它区分
    「ready 但没版本(发不出去)」与「ready 且有版本(能发)」——
    字段不在响应里,那个筛选就只能永远为空。
    """
    from db import geo_douyin_db as ddb

    post_id = make_post()
    task_id, epoch = _new_task_with_generation(post_id)
    revision_id = _freeze(post_id, task_id, epoch)

    rows = ddb.list_posts(brand_id=None, tenant_owner_user_id=USER_ID,
                          status="", limit=50, offset=0)
    hit = [r for r in rows if int(r["id"]) == post_id]
    assert hit, "作品不在列表里 —— 夹具或作用域不对,后面的断言无意义"
    assert "active_revision_id" in hit[0], (
        "列表响应里没有 active_revision_id —— A 的「可发布」筛选会恒空")
    assert hit[0]["active_revision_id"] == revision_id

    detail = ddb.get_post(post_id)
    assert detail.get("active_revision_id") == revision_id
