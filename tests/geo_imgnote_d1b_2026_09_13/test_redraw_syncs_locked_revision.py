"""#184 d1b 判据 —— 单卡重抽必须同步进锁定版本。

为什么这条卡存在:d1 之后 ready 作品有了 active revision,而发布链读的是**版本**,
不是 `posts.oss_keys`。单卡重抽原来只定点改 post ⇒ **重抽完发出去的还是旧图**,
而页面上看是新的。这是 d1 带出来的不一致,不是重抽本来的毛病。
"""
import asyncio
import json

import pytest

from .conftest import USER_ID, make_post, post_row, revisions_of, tasks_of

_REF = [0]


def _new_generation(post_id):
    from db import geo_douyin_db as ddb
    _REF[0] += 1
    return ddb.create_task_with_generation(
        post_id=post_id, user_id=USER_ID, task_ref="d1b-%d-%d" % (post_id, _REF[0]),
        freeze_id=None, progress_total=3)


def _freeze(post_id, task_id, epoch):
    from services.geo_douyin.production_task import _freeze_ordinary_revision
    return _freeze_ordinary_revision(post_id, USER_ID, task_id, epoch)


def _ready_post_with_revision(cards=3):
    """做出一条 **ready 且有锁定版本** 的作品 —— 与生产落地后的状态一致。

    🔴 生成任务必须**收尾**:真实生产 `run_image_post_production` 落 ready 前
       会把任务置终态。夹具留着 pending 的话,后面任何一次建任务都会撞
       `uq_geo_douyin_task_active_generation` —— 那是夹具不像生产,不是被测对象的毛病。
    """
    from db import geo_douyin_db as ddb

    post_id = make_post(cards=cards)
    task_id, epoch = _new_generation(post_id)
    revision_id = _freeze(post_id, task_id, epoch)
    assert revision_id is not None
    ddb.update_task(task_id, status="succeeded", stage="done", mark_finished=True)
    ddb.set_post_status(post_id, "ready")
    return post_id, revision_id


def _stub_render(monkeypatch, new_key="geo/img/c184b/NEW.png", ok=True):
    """桩掉生图。

    🔴 打在**模块属性**上:`redraw_one_card` 是在函数体里
       `from ...image_pipeline import render_one_card` 的,查名发生在调用时,
       所以打模块属性有效(打在 redraw 模块上反而无效)。
    """
    from services.geo_douyin import image_pipeline

    class _Res:
        def __init__(self):
            self.ok = ok
            self.oss_key = new_key
            self.error = "" if ok else "stub-fail"

    async def _fake(*a, **kw):
        return _Res()

    monkeypatch.setattr(image_pipeline, "render_one_card", _fake)
    return new_key


def _manifest_of(post_id):
    rev = [r for r in revisions_of(post_id) if str(r["status"]) == "active"]
    assert len(rev) == 1, rev
    m = rev[0]["asset_manifest"]
    return (json.loads(m) if isinstance(m, str) else dict(m)), rev[0]


# ── 主线:重抽第 k 张 ⇒ 版本里的第 k 个资产变成新 key ──────────────────
@pytest.mark.parametrize("k", [0, 1, 2])
def test_d1b_redraw_updates_the_locked_revision(monkeypatch, k):
    from services.geo_douyin.redraw import redraw_one_card

    post_id, _rev = _ready_post_with_revision(cards=3)
    before, _ = _manifest_of(post_id)
    new_key = _stub_render(monkeypatch, "geo/img/c184b/new-%d.png" % k)

    res = asyncio.run(redraw_one_card(post_row(post_id), k))
    assert res.ok, res.error

    after, rev = _manifest_of(post_id)
    assert after["oss_keys"][k] == new_key, "版本里的第 %d 个资产没换 —— 发出去的会是旧图" % k
    # 其余资产一个字节都不许动
    for i in range(3):
        if i != k:
            assert after["oss_keys"][i] == before["oss_keys"][i]
    # hash 与 manifest 对得上(发布链按它核身份)
    from services.geo_douyin.post_revisions import compute_manifest_hash
    assert rev["manifest_hash"] == compute_manifest_hash(after)
    # post 侧也同步了(页面看到的与发出去的是同一张)
    assert post_row(post_id)["oss_keys"][k] == new_key


def test_d1b_redrawing_the_cover_moves_the_cover_in_the_revision_too(monkeypatch):
    """`replace_one_card` 在 index==0 时连 `cover_oss_key` 一起换,版本必须同口径。

    对不齐的话,版本里的封面会指向一张已经被替换掉的图。
    """
    from services.geo_douyin.redraw import redraw_one_card

    post_id, _ = _ready_post_with_revision(cards=3)
    new_key = _stub_render(monkeypatch, "geo/img/c184b/cover-new.png")

    assert asyncio.run(redraw_one_card(post_row(post_id), 0)).ok
    after, _ = _manifest_of(post_id)
    assert after["cover_oss_key"] == new_key
    assert post_row(post_id)["cover_oss_key"] == new_key


# ── 版本被新一代顶掉:作废 + 退额度 + **post 一个字节都不动** ────────────
def test_d1b_superseded_revision_aborts_without_touching_the_post(monkeypatch):
    from services.geo_douyin.redraw import redraw_one_card

    post_id, _ = _ready_post_with_revision(cards=3)
    post_before = post_row(post_id)
    used_before = int(post_before["redraw_count"] or 0)

    # 新一代生成接管并冻出新版本 ⇒ 旧版本被 supersede
    t2, e2 = _new_generation(post_id)
    assert _freeze(post_id, t2, e2) is not None

    # 旧版本上的重抽现在才做完
    _stub_render(monkeypatch, "geo/img/c184b/stale.png")
    res = asyncio.run(redraw_one_card(post_before, 1))

    assert res.ok is False and res.superseded is True
    after = post_row(post_id)
    # 🔴 旧图不许插回新版:post 的 oss_keys 与封面一个字节都不该动
    assert after["oss_keys"] == post_row(post_id)["oss_keys"]
    assert "geo/img/c184b/stale.png" not in after["oss_keys"]
    # 额度退回(没消耗)
    assert int(after["redraw_count"] or 0) == used_before


# ── 存量作品(没有 active revision)⇒ 与改前逐字同行为 ─────────────────
def test_d1b_post_without_revision_behaves_exactly_as_before(monkeypatch):
    """d1 之前的 36 条作品没有版本。它们不能因为本卡而报错或被跳过。

    这条是**反向对照**:少了它,把同步写成"没有版本就返回 False"
    也能让上面那些绿 —— 而那会让所有存量作品的重抽全部作废。
    """
    from services.geo_douyin.redraw import redraw_one_card

    post_id = make_post(cards=3)               # 只建作品,不开代际、不冻版本
    assert post_row(post_id)["active_revision_id"] is None
    new_key = _stub_render(monkeypatch, "geo/img/c184b/legacy.png")

    res = asyncio.run(redraw_one_card(post_row(post_id), 2))

    assert res.ok is True and res.superseded is False
    assert post_row(post_id)["oss_keys"][2] == new_key
    assert revisions_of(post_id) == []          # 也没有凭空造出版本来


# ── 任务行走同一道门:孤儿 pending 不再把重抽永久焊死 ────────────────
def test_d1b_redraw_task_goes_through_the_same_door(monkeypatch):
    from db import geo_douyin_db as ddb
    from services.geo_douyin.redraw import dispatch_redraw

    post_id, _ = _ready_post_with_revision(cards=3)
    # 留一个孤儿 pending(进程重启的现场)
    orphan = ddb.create_task(post_id=post_id, user_id=USER_ID,
                             task_ref="orphan-d1b-%d" % post_id,
                             freeze_id=None, progress_total=1)
    _stub_render(monkeypatch, "geo/img/c184b/after-orphan.png")

    async def _go():
        task_id, task = await dispatch_redraw(post_row(post_id), 1, user_id=USER_ID)
        await task
        return task_id

    task_id = asyncio.run(_go())           # 改前:这里 UniqueViolation

    rows = {int(t["id"]): t for t in tasks_of(post_id)}
    assert rows[orphan]["superseded_at"] is not None, "孤儿没被接管 ⇒ 下次还会撞"
    assert int(task_id) in rows
