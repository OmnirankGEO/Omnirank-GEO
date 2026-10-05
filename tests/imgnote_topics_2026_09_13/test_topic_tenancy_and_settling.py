"""WO_204 c1b 判据 · Review 两发全绿毒指出的缺口(只加判据,产品零改动)。

复审 5ffaff96b 时两发毒全绿,都是**我的判据没覆盖到**:

  · P6 `_topic_or_404` 去掉 `require_brand_access` ⇒ 42/42 绿
    —— 跨租户改/删别人的选题没有任何东西钉。
       我的 R 组判据全在直接调 helper,**端点那一跳裸奔**。
       (同一个病我当天在 WO_211 里主动补过一条接线臂,却没有回头扫这里。)

  · P7 dispatch **异常路径**去掉 `_settle_topic(False)` ⇒ 42/42 绿
    —— 我以为"失败路径"只有一条,实际有两条:
       `run_image_post_production` **正常返回 ok=False** / **抛异常**。
       原来的判据只走了前者。
"""
import asyncio

import pytest
from fastapi import HTTPException

from .conftest import (BRAND_ID, OTHER_BRAND_ID, USER_ID, conn, fake_request,
                       make_post, seed_brand, seed_topic, topic_row)

OTHER_USER_ID = 920409


# ═══════════════════════════════════════════════════════════════
# P6 缺口 · 跨租户改 / 删 / 列表
# ═══════════════════════════════════════════════════════════════
def _patch(topic_id, title, request):
    from api.geo_douyin_api import PatchTopicRequest, api_patch_topic
    return asyncio.run(api_patch_topic(topic_id, PatchTopicRequest(title=title), request))


def _delete(topic_id, request):
    from api.geo_douyin_api import api_delete_topic
    return asyncio.run(api_delete_topic(topic_id, request))


def _list(brand_id, request):
    from api.geo_douyin_api import api_list_topics
    return asyncio.run(api_list_topics(brand_id, request))


def test_the_owner_can_patch_their_own_topic():
    """正样本臂:自己的选题改得动 —— 否则下面几条"改不动"可能只是"永远改不动"。"""
    seed_brand(BRAND_ID, USER_ID)
    tid = seed_topic(title="原选题")
    out = _patch(tid, "我改的标题", fake_request(USER_ID))
    assert out["status"] == "success"
    assert topic_row(tid)["title"] == "我改的标题"


def test_patching_another_brands_topic_is_refused():
    """🔴 P6 正打:拿**别人品牌**的 topic_id 调 PATCH 必须被拒。

    判读看的是**库里那一行有没有被改**,不只看抛没抛 ——
    「抛了但也改了」在只断异常的判据下是绿的。
    """
    seed_brand(OTHER_BRAND_ID, OTHER_USER_ID)
    tid = seed_topic(title="别人的选题", brand_id=OTHER_BRAND_ID)

    with pytest.raises(HTTPException) as e:
        _patch(tid, "我要改别人的", fake_request(USER_ID))
    # `require_brand_access` 的拒绝出口是 404「资源不存在」——
    # 与 403 分开会告诉调用方"存在但不归你",那是跨租户探测存在性的旁路。
    assert e.value.status_code == 404
    assert topic_row(tid)["title"] == "别人的选题", "拒了,但那一行其实被改了"


def test_deleting_another_brands_topic_is_refused():
    seed_brand(OTHER_BRAND_ID, OTHER_USER_ID)
    tid = seed_topic(title="别人的选题", brand_id=OTHER_BRAND_ID)

    with pytest.raises(HTTPException) as e:
        _delete(tid, fake_request(USER_ID))
    assert e.value.status_code == 404
    assert topic_row(tid) is not None, "拒了,但那一行其实被删了"


def test_listing_another_brands_topics_is_refused():
    seed_brand(OTHER_BRAND_ID, OTHER_USER_ID)
    seed_topic(title="别人的选题", brand_id=OTHER_BRAND_ID)
    with pytest.raises(HTTPException) as e:
        _list(OTHER_BRAND_ID, fake_request(USER_ID))
    assert e.value.status_code == 404


def test_an_assigned_brand_is_still_reachable():
    """反向对照的反面:被**分配**的品牌照样能改。

    少了它,「把校验收紧成只认 owner」也能让上面三条绿,
    而那会把"被分配客户"的服务商挡在外面。
    """
    seed_brand(OTHER_BRAND_ID, OTHER_USER_ID)
    tid = seed_topic(title="被分配客户的选题", brand_id=OTHER_BRAND_ID)
    out = _patch(tid, "分配来的也能改",
                 fake_request(USER_ID, client_brand_ids=[OTHER_BRAND_ID]))
    assert out["status"] == "success"


def test_admin_can_reach_any_brands_topic():
    seed_brand(OTHER_BRAND_ID, OTHER_USER_ID)
    tid = seed_topic(title="别人的选题", brand_id=OTHER_BRAND_ID)
    out = _patch(tid, "admin 改的", fake_request(USER_ID, is_admin=True))
    assert out["status"] == "success"


# ═══════════════════════════════════════════════════════════════
# P7 缺口 · 失败有**两条**路,都要收尾
# ═══════════════════════════════════════════════════════════════
def _dispatch_and_wait(**kwargs):
    from services.geo_douyin.production_task import dispatch_production

    async def _go():
        return await dispatch_production(**kwargs)

    return asyncio.run(_go())


def test_an_exception_inside_production_still_settles_the_topic(monkeypatch):
    """🔴 P7 正打:`run_image_post_production` **抛异常**那条路也要收尾。

    原来的失败判据走的是"正常返回 ok=False";抛异常是**另一条出口**,
    它由 `dispatch_production` 自己的 except 兜。两条各有一条判据,
    因为删掉其中任一条,另一条都照样绿。

    不收尾的后果:选题永远卡在「制作中」——用户点不动,列表也不解释为什么。
    """
    from services.geo_douyin import production_task

    async def _boom(**kw):
        raise RuntimeError("兜不住的异常(比如落库时连不上 DB)")

    monkeypatch.setattr(production_task, "run_image_post_production", _boom)

    seed_brand(BRAND_ID, USER_ID)
    tid = seed_topic(title="要做的题", status="making")
    post_id = make_post()

    _dispatch_and_wait(post_id=post_id, user_id=USER_ID, keyword="装修公司",
                       brand_id=BRAND_ID, brand_name="某装修公司", city="杭州",
                       card_count=3, topic_id=tid, topic_title="要做的题")

    row = topic_row(tid)
    assert row["status"] == "failed", (
        "抛异常那条路没给选题收尾 —— 它会永远卡在 %r" % row["status"])
    assert row["post_id"] is None


def test_finish_only_moves_a_making_topic_to_done():
    """`finish_topic` 只接受 `making → done`。

    没有这条,"把一条 pending 的选题直接标成已做"会是合法操作 ——
    而那意味着一条没人做过的选题挂上了别人的成品。
    """
    from db.geo_douyin_db import finish_topic

    seed_brand(BRAND_ID, USER_ID)
    pid = make_post()

    pending_id = seed_topic(status="pending")
    finish_topic(topic_id=pending_id, post_id=pid)
    row = topic_row(pending_id)
    assert (row["status"], row["post_id"]) == ("pending", None), (
        "pending 的选题被直接标成了 %r" % row["status"])

    failed_id = seed_topic(status="failed")
    finish_topic(topic_id=failed_id, post_id=pid)
    assert topic_row(failed_id)["status"] == "failed"

    making_id = seed_topic(status="making")
    finish_topic(topic_id=making_id, post_id=pid)
    row = topic_row(making_id)
    assert (row["status"], row["post_id"]) == ("done", pid)


def test_release_only_moves_a_making_topic():
    """反向:`release_topic` 同样只动 `making` —— 不许把已做的打回失败。"""
    from db.geo_douyin_db import release_topic

    seed_brand(BRAND_ID, USER_ID)
    pid = make_post()
    done_id = seed_topic(status="done", post_id=pid)
    release_topic(topic_id=done_id)
    row = topic_row(done_id)
    assert (row["status"], row["post_id"]) == ("done", pid), (
        "已做的选题被打回了 %r,成品就此失联" % row["status"])


def _gated_client():
    """把 `geo_douyin` 路由器挂进一个最小 app,**走真实路由**拿 client。

    🔴 [0913e 集成] 这是本条判据这次改写的全部理由:
       原来它直接 `asyncio.run(api_patch_topic(...))` 调 handler 函数 ——
       那条路**绕开了依赖链**,`Depends(pipeline_gate)` 根本不会执行。
       于是它量到的其实是端点体内那两行 `if not is_pipeline_enabled(): return
       _COMING_SOON`(213 已把这套逐端点判定废掉,常量也删了)。
       后果有两层:
         · 213 合车后这条判据报 `NameError: _COMING_SOON`,
           读起来像"运行时崩了",而生产 HTTP 路径上那两行**根本不可达**
           (Deploy 2026-09-15 实查合成树确认);
         · 更要命的是反过来:直接调 handler 的判据**永远量不到闸**。
           闸即使被整个摘掉,它也照样绿。
       所以改走 TestClient:依赖链跑起来,闸才在被测范围内。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.geo_douyin_api import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


# ═══════════════════════════════════════════════════════════════
# 总闸本身(c1b 顺带:它是"半成品不许扣费"的那道,原来没人钉)
# ═══════════════════════════════════════════════════════════════
def test_endpoints_are_refused_by_the_gate_when_the_switch_is_off(monkeypatch):
    """总闸关着 ⇒ **503 + PIPELINE_DISABLED**,并且**一行都不改**。

    🔴 这条是本轮顺带发现的:原来 42 条判据全是直接调数据层的,
       从没经过总闸 —— 闸关着却照样落库/扣费,是这类总闸唯一的失败方式。
    🔴 [0913e 集成] 断言从「返回 coming_soon 字典」改成「HTTP 503 +
       机器可读 code」:213 把逐端点早退换成了路由器级依赖,
       闸关时是**抛 503**,不是返 200 带个 status 字段。
       返 200 的老形态正是 213 要修的病 —— 网关/埋点/前端错误分支都看不见它。
    """
    monkeypatch.setenv("GEO_DOUYIN_PIPELINE_ENABLED", "0")

    seed_brand(BRAND_ID, USER_ID)
    tid = seed_topic(title="闸关着不许动")

    client = _gated_client()
    for resp in (client.patch("/api/geo-douyin/topics/%d" % tid,
                              json={"title": "偷偷改"}),
                 client.delete("/api/geo-douyin/topics/%d" % tid)):
        assert resp.status_code == 503, (
            "闸关着却不是 503(实得 %d)—— 返 200 的话,"
            "「功能关了」与「功能跑通了」在状态码上分不出来" % resp.status_code)
        detail = (resp.json() or {}).get("detail") or {}
        assert detail.get("code") == "PIPELINE_DISABLED", detail
        # 与老 `_COMING_SOON` 形状对齐:前端既有分支认 status 这一键
        assert detail.get("status") == "coming_soon", detail
        assert detail.get("message"), "闸关了却没有一句给人看的话"

    row = topic_row(tid)
    assert row is not None, "总闸关着却把行删了"
    assert row["title"] == "闸关着不许动", "总闸关着却把库改了"


def test_the_gate_is_what_produces_that_503_not_something_permanent(monkeypatch):
    """反向对照:闸**开**着时,同一个请求不再是 503。

    🔴 少了它,上一条可以靠「这两个端点永远 503」满足 —— 而那等于把
       图文选题永久关掉,且看起来完全合规。
       这里不断言 200:没带登录态,端点会在 `_user()` 上 401 ——
       401 恰恰证明**已经越过闸、进到了 handler**,这正是要证的那一步。
    """
    monkeypatch.setenv("GEO_DOUYIN_PIPELINE_ENABLED", "1")

    seed_brand(BRAND_ID, USER_ID)
    tid = seed_topic(title="闸开着")

    client = _gated_client()
    resp = client.delete("/api/geo-douyin/topics/%d" % tid)
    assert resp.status_code != 503, "闸开着还 503 —— 那不是闸,是永久关停"
    assert resp.status_code == 401, (
        "闸开着应当走进 handler 再被登录校验拦下(401),实得 %d" % resp.status_code)
