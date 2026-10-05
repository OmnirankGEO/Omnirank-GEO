"""#184 d2 判据 —— 下单成功后把观测列写回作品。

今天这三列在生产上全空:内容被发出去了,而它真正的主人在创作中心看还是「未发布」。

🔴 本包**必须**有接线臂:上一轮 d1 的教训是「我验了 A 也验了 B,
   但 A 调 B 那行没人验」。所以 D2-1 真跑 `materialize_command`,
   而不是只调 `bind_publish_submission`。
"""
import uuid

import pytest

from services.geo_douyin.artifact_prepare import STATE_READY
from services.geo_douyin.contract_funding import AUTHORITY_DIRECT
from services.geo_douyin.contract_pricing import publish_fingerprint
from services.geo_douyin.publish_coordinator import materialize_command

from .conftest import USER_ID, _conn, make_post, post_row, revisions_of

IDENTITY = {"tenant_owner_user_id": USER_ID, "payer_user_id": USER_ID,
            "actor_user_id": USER_ID, "payer_policy_snapshot": {}}


def _fake_freeze(counter):
    def _freeze(*, payer_user_id, amount, task_ref):
        counter.append(amount)
        return {"payer_user_id": payer_user_id, "freeze_id": 1000 + len(counter),
                "freeze_table": "legacy", "task_ref": task_ref,
                "reserved_amount": amount, "physical_split_snapshot": {"paid": amount}}
    return _freeze


def _mk(post_id, media_id, revision=None):
    # 🔴 revision 必须每篇不同:`publish_command` 强制「一篇作品恰好对应一个账号」
    #    (OneToOneViolation),而它是按 **revision** 判重的 —— 两篇共用一个
    #    revision id 会被整批拒绝,红的是夹具不是被测对象。
    return {"item_request_id": str(uuid.uuid4()), "geo_post_id": post_id,
            "post_revision_id": int(revision if revision is not None else post_id),
            "prepared_artifact_id": "x",
            "manifest_hash": "m1", "media_id": media_id,
            "expected_price_fingerprint": publish_fingerprint(
                feature_code="media_proxy_publish", media_id=media_id,
                final_price_points=9360, markup_version="m1",
                resolver_version="r1", catalog_version="c1")}


def _run_command(items):
    """真跑 `materialize_command`,返回 (result, 提交用的连接已 commit)。"""
    conn = _conn()
    conn.autocommit = False
    try:
        cur = conn.cursor()
        result = materialize_command(
            cur, command_request_id=str(uuid.uuid4()), identity=IDENTITY, items=items,
            resolved_prices={i["item_request_id"]: {
                "final_price_points": 9360,
                "publish_price_fingerprint": i["expected_price_fingerprint"],
                "price_snapshot": {"final_price_points": 9360}} for i in items},
            artifacts={i["item_request_id"]: {
                "prepared_artifact_id": 1,
                "post_revision_id": int(i["post_revision_id"]),
                "manifest_hash": "m1", "state": STATE_READY} for i in items},
            account_names={i["media_id"]: "号%d" % i["media_id"] for i in items},
            daily_limit=50,
            settlement={"authority": AUTHORITY_DIRECT, "payer_user_id": USER_ID},
            freeze_fn=_fake_freeze([]))
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── D2-1 接线臂:真跑下单命令 ⇒ 作品三列非空 ─────────────────────────
def test_d2_submission_writes_the_observation_columns(monkeypatch):
    post_id = make_post(cards=3, status="ready")
    before = post_row(post_id)
    assert before["publish_order_id"] is None and not (before["publish_item_ids"] or [])

    result = _run_command([_mk(post_id, 9001)])

    row = post_row(post_id)
    assert row["publish_order_id"] is not None, (
        "下单成功但作品上什么都没有 —— 服务商在创作中心看还是「未发布」")
    assert str(row["publish_status"]) == "publishing"
    item_ids = row["publish_item_ids"] or []
    assert len(item_ids) == 1
    # 绑的是**这一篇自己的**订单
    mine = [i for i in result["items"] if int(i["geo_post_id"]) == post_id]
    assert int(row["publish_order_id"]) == int(mine[0]["order_id"])
    assert int(item_ids[0]) == int(mine[0]["order_item_id"])


# ── D2-2 每篇绑自己的单(本链一项一单)────────────────────────────────
def test_d2_each_post_binds_its_own_order_not_the_last_one():
    """🔴 `order_id` 是**循环内**变量。在循环外拿它 = 每一篇都绑到最后一单。

    少了这条,那个 bug 会一路活到生产:两篇都指向同一个订单,
    而列表页据它展示 —— 第一篇的入口点进去是第二篇的单。
    """
    p1 = make_post(cards=3, status="ready")
    p2 = make_post(cards=3, status="ready")

    result = _run_command([_mk(p1, 9001), _mk(p2, 9002)])

    got = {int(r["geo_post_id"]): int(r["order_id"]) for r in result["items"]}
    assert got[p1] != got[p2], "本链应当每项一单 —— 夹具前提不成立,后面的断言无意义"
    assert int(post_row(p1)["publish_order_id"]) == got[p1]
    assert int(post_row(p2)["publish_order_id"]) == got[p2]


# ── D2-3 守卫:已发布的作品不许被重放倒回 publishing ────────────────────
def test_d2_replay_cannot_push_a_published_post_back_to_publishing():
    """顺利路径看不见这件事(提交在前、回执在后),只有重放臂看得见。

    `bind_publish_result` 用 `COALESCE(NULLIF(...))` —— 传非空就覆盖,
    所以守卫必须在 `WHERE` 里。
    """
    from db.geo_douyin_db import bind_publish_submission

    post_id = make_post(cards=3, status="ready")
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("""UPDATE geo_douyin_posts
                          SET publish_status='published',
                              published_url='https://x.example/p/1',
                              publish_order_id=555
                        WHERE id=%s""", (post_id,))
        ok = bind_publish_submission(cur, post_id=post_id, order_id=999,
                                     item_ids=[1, 2, 3])
    finally:
        conn.close()

    assert ok is False, "守卫没拦住 —— 已发布的作品被一笔重放倒回了 publishing"
    row = post_row(post_id)
    assert str(row["publish_status"]) == "published"
    assert row["published_url"] == "https://x.example/p/1"
    assert int(row["publish_order_id"]) == 555


def test_d2_guard_lets_a_normal_post_through():
    """反向对照:守卫只挡终态,不是把所有写都挡掉。

    少了这条,把 `bind_publish_submission` 写成"永远返回 False"也能让上面那条绿。
    """
    from db.geo_douyin_db import bind_publish_submission

    post_id = make_post(cards=3, status="ready")
    conn = _conn()
    try:
        cur = conn.cursor()
        ok = bind_publish_submission(cur, post_id=post_id, order_id=888, item_ids=[7])
    finally:
        conn.close()
    assert ok is True
    assert int(post_row(post_id)["publish_order_id"]) == 888


# ══════════════════════════════════════════════════════════════════════
# c1 · 列表投影(A 的 #188 区 B/E 依赖)
# ══════════════════════════════════════════════════════════════════════
def test_c1_list_projection_carries_cover_url_and_card_count(monkeypatch):
    """列表要能直接渲染卡片:封面签名 URL + 卡片数 + 版式三件套。

    没有判据钉着的话,这几个字段被谁顺手删掉都不会红,而 A 的面板会空着 ——
    「我给 A 加的回显没有仪器钉着」这件事上一轮已经发生过一次。
    """
    import asyncio

    from services.geo_douyin import image_pipeline
    import api.geo_douyin_api as gapi

    post_id = make_post(cards=3, status="ready")

    async def _fake_sign(keys, expires_seconds=900):
        # 桩掉真签名(要连 OSS);形状照它的契约:空 key 给空串,逐位对齐
        return ["https://signed.example/%s" % k if k else "" for k in (keys or [])]

    monkeypatch.setattr(image_pipeline, "signed_card_urls", _fake_sign)

    class _Req:
        class state:
            user = {"user_id": USER_ID, "is_admin": False}
            organization_identity = None

    out = asyncio.run(gapi.api_list_posts(_Req(), brand_id=None, status="",
                                          limit=50, offset=0))
    mine = [r for r in out["posts"] if int(r["id"]) == post_id]
    assert mine, "作品不在列表里 —— 作用域或夹具不对,后面的断言无意义"
    row = mine[0]

    assert row["card_count"] == 3, "卡片数不对 —— 列表会显示错的「N 张」"
    assert row["cover_url"].endswith(row["cover_oss_key"]), (
        "封面没签出来 —— 私有 bucket 下前端拿裸 key 是渲染不出图的")
    # A 要的版式三件套(它们随 _POST_FIELDS 走,但同样得有人钉着)
    for field in ("style_key", "aspect_ratio", "active_revision_id"):
        assert field in row, "列表缺字段 %s —— A 的筛选/展示会空着" % field


# ══════════════════════════════════════════════════════════════════════
# d3 / d3a · 一链一列一写入方
# ══════════════════════════════════════════════════════════════════════
def _seed_order_item(post_id, *, billing_mode, status="queued",
                     publish_url="", order_sn=None, synced_status=None):
    """造一笔订单 + 一项,可选造一条供应商回执镜像。返回 (order_id, item_id)。

    🔴 列名与约束全部按**生产 schema** 来,不是按记忆:
       · `mhz_publish_orders` 没有 `cost_points`,是 `total_cost_points`;
       · `ck_mhz_item_freeze_handle_complete`:`billing_mode='freeze_per_item'`
         的项**必须**带 task_ref / reserved_amount / physical_split_snapshot
         (除非 organization_charge)—— 手写夹具漏掉这条,就能插进一条生产上
         根本不可能存在的行,判据于是证明一件生产上不成立的事;
       · `mhz_synced_orders.id` 是 **text 且非自增**,要自己给。
    """
    import json as _json

    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO mhz_publish_orders (user_id, article_id, article_title,
                   total_cost_points, status)
               VALUES (%s, -1, 't', 0, 'pending') RETURNING id""", (USER_ID,))
        order_id = int(cur.fetchone()["id"])
        sn = order_sn or ("sn-%d" % order_id)
        # 🔴 `freeze_per_item` 不是一个孤立的标签,生产 schema 上它**强制配套**:
        #    ck_mhz_item_freeze_mode_pairing ⇒ settlement_authority 必须是
        #      organization_charge / direct_freeze / admin_exempt 之一;
        #    ck_mhz_item_authority_shape(direct_freeze)⇒ freeze_id + freeze_table
        #      + payer_user_id + reserved_amount 全非空,且 org link 为空;
        #    ck_mhz_item_freeze_handle_complete ⇒ 还要 task_ref + physical_split_snapshot。
        #    手写夹具漏掉任一条,就能造出一条**生产上不可能存在的**行。
        if billing_mode == "freeze_per_item":
            authority, freeze_id, freeze_table = "direct_freeze", 4242, "legacy"
        else:
            authority, freeze_id, freeze_table = None, None, None
        cur.execute(
            """INSERT INTO mhz_publish_order_items
                   (order_id, user_id, media_id, media_name, media_type, status,
                    cost_points, source_geo_post_id, billing_mode, settlement_status,
                    settlement_authority, freeze_id, freeze_table, payer_user_id,
                    publish_url, mhz_order_id, task_ref, reserved_amount,
                    physical_split_snapshot)
               VALUES (%s,%s,9001,'号A','mhz',%s,0,%s,%s,'committed',
                       %s,%s,%s,%s,%s,%s,%s,0,%s::jsonb)
               RETURNING id""",
            (order_id, USER_ID, status, int(post_id), billing_mode,
             authority, freeze_id, freeze_table, USER_ID,
             publish_url, sn, "tr-%d" % order_id, _json.dumps({"paid": 0})))
        item_id = int(cur.fetchone()["id"])
        if synced_status is not None:
            cur.execute(
                """INSERT INTO mhz_synced_orders (id, order_sn, title, media_name,
                       status, url) VALUES (%s,%s,'t','号A',%s,%s)""",
                (sn, sn, int(synced_status), publish_url))
        cur.execute("""UPDATE geo_douyin_posts
                          SET publish_status='publishing', publish_order_id=%s
                        WHERE id=%s""", (order_id, int(post_id)))
        return order_id, item_id
    finally:
        conn.close()


def test_d3_legacy_converger_skips_the_new_chain():
    """新链的作品不许被老收敛器碰 —— 它没有回填器的三道守卫。

    d2 开始给新链作品写 publish_order_id,没有这条排除,下一小时老收敛器
    就会绕过矛盾回执/结算守卫去覆盖 published_url。
    """
    from services.geo_douyin.publish_convergence import converge_publishing_posts

    new_chain = make_post(cards=3, status="ready")
    _seed_order_item(new_chain, billing_mode="freeze_per_item",
                     status="published", publish_url="https://x/new")

    out = converge_publishing_posts(limit=50)

    assert int(post_row(new_chain)["publish_order_id"]) > 0
    assert not post_row(new_chain)["published_url"], (
        "老收敛器写了新链的 published_url —— 同一列两个写入方,而它少三道守卫")
    assert str(post_row(new_chain)["publish_status"]) == "publishing"
    assert out["published"] == 0


def test_d3_reverse_control_legacy_chain_still_converges():
    """反向对照:老链(非 freeze_per_item)照旧收敛。

    少了这条,把谓词写成「谁都跳过」也能让上面那条绿 —— 而那等于把
    #151 修好的老链收敛又关掉。
    """
    from services.geo_douyin.publish_convergence import converge_publishing_posts

    legacy = make_post(cards=3, status="ready")
    # 🔴 `ck_mhz_item_billing_mode` 的合法域只有 {NULL, deduct_upfront,
    #    freeze_per_item}(生产 schema 核过)—— 老链是 deduct_upfront。
    #    所以 d3 的分区正好是**二分**的,不存在第三类被顺带排除。
    _seed_order_item(legacy, billing_mode="deduct_upfront",
                     status="published", publish_url="https://x/legacy")

    out = converge_publishing_posts(limit=50)

    assert post_row(legacy)["published_url"] == "https://x/legacy", (
        "老链没被收敛 —— 谓词把不该排除的也排除了")
    assert out["published"] >= 1


# ══════════════════════════════════════════════════════════════════════
# d3c · 新链失败投影 —— 否则失败的作品永远停在 publishing
# ══════════════════════════════════════════════════════════════════════
def test_d3c_all_terminal_and_none_succeeded_releases_the_post_from_publishing():
    """#151 的症状在新链重现:回填器只投影**成功**,失败没人管。

    服务商界面上「正在发布」会挂到天荒地老,而老链那条收敛器已被 d3 排除。
    """
    from services.geo_douyin.contract_worker import project_failed_publish_posts

    post_id = make_post(cards=3, status="ready")
    _seed_order_item(post_id, billing_mode="freeze_per_item", status="rejected")

    out = project_failed_publish_posts(limit=50)

    assert str(post_row(post_id)["publish_status"]) == "failed"
    assert not post_row(post_id)["published_url"], "失败不该有链接"
    assert out["failed_projected"] == 1


def test_d3c_in_flight_item_is_left_alone():
    """反向对照:还有在途项时**不动** —— 「卡了很久」不等于「失败了」。

    少了这条,把投影写成「凡 publishing 一律标 failed」也能让上面那条绿,
    而那会把正在发的作品全部误判成失败。
    """
    from services.geo_douyin.contract_worker import project_failed_publish_posts

    post_id = make_post(cards=3, status="ready")
    _seed_order_item(post_id, billing_mode="freeze_per_item", status="queued")

    out = project_failed_publish_posts(limit=50)

    assert str(post_row(post_id)["publish_status"]) == "publishing"
    assert out["failed_projected"] == 0
    assert out["still_in_flight"] == 1


def test_d3c_success_is_left_to_the_url_backfiller():
    """成功的那条不归它管:URL 由既有回填器投影(带矛盾回执与结算守卫)。

    这条钉住「一件事一个写入方」——失败投影不许顺手把成功也判了。
    """
    from services.geo_douyin.contract_worker import project_failed_publish_posts

    post_id = make_post(cards=3, status="ready")
    _seed_order_item(post_id, billing_mode="freeze_per_item",
                     status="published", publish_url="https://x/ok")

    out = project_failed_publish_posts(limit=50)

    assert str(post_row(post_id)["publish_status"]) == "publishing"
    assert out["failed_projected"] == 0


# ══════════════════════════════════════════════════════════════════════
# d5 / d6 · 内容来源铁律的守门人 + 删掉那个假把关字段
# ══════════════════════════════════════════════════════════════════════
def test_d5_content_fields_are_rejected_with_a_code():
    """内容只能由服务端按 post_id 从冻结版本取。

    改前这些键是**静默忽略**的:调用方传了标题,它既不生效也不报错 ——
    而调用方会以为生效了。
    """
    from api.geo_image_note_api import PublishBatchRequest

    for key, value in (("title", "我的标题"), ("body", "正文"),
                       ("image_urls", ["a"]), ("content", "x")):
        with pytest.raises(Exception) as err:
            PublishBatchRequest(request_id="r", expected_total_price_points=1,
                                items=[], **{key: value})
        assert "CLIENT_CONTENT_NOT_ACCEPTED" in str(err.value), key


def test_d5_is_a_reject_list_not_a_blanket_forbid():
    """🔴 反向对照:无害的未知键**照旧放行**。

    `extra="forbid"` 在本仓炸过三次生产(geo_image_note_api:1601 自记)——
    它会把任何一个新增键都变成 422。少了这条,把守卫写成整体 forbid 也能
    让上面那条绿,而那是拿一次生产事故换一条判据。
    """
    from api.geo_image_note_api import PublishBatchRequest

    ok = PublishBatchRequest(request_id="r", expected_total_price_points=1,
                             items=[], some_future_field=123)
    assert ok.request_id == "r"


def test_d6_dead_price_field_is_gone():
    """`expected_price_points` 全仓零读,而注释说它在逐项比对 —— 假的。

    真正比价身份的是 `expected_price_fingerprint`
    (publish_coordinator.py:164 逐项 assert_price_unchanged,价格编在指纹里)。
    留一个没人读的字段 + 一句说它在把关的注释,比没有更糟:
    下一个人会以为那道闸存在。
    """
    from api.geo_image_note_api import PublishBatchItem

    assert "expected_price_points" not in PublishBatchItem.model_fields
    assert "expected_price_fingerprint" in PublishBatchItem.model_fields


# ══════════════════════════════════════════════════════════════════════
# d1c · 整篇重做在跑时不许重抽单张
# ══════════════════════════════════════════════════════════════════════
def _redraw_api(post_id, card_index=1, monkeypatch=None):
    """调重抽端点。

    🔴 必须先打开总闸 `GEO_DOUYIN_PIPELINE_ENABLED`:端点第一行就是
       `if not is_pipeline_enabled(): return _COMING_SOON` ——
       闸关着时它**什么都不做就返回**,我的守卫根本不会被触达,
       而判据看起来就像"守卫没生效"。
    """
    import asyncio
    import os

    import api.geo_douyin_api as gapi

    os.environ["GEO_DOUYIN_PIPELINE_ENABLED"] = "1"

    class _Req:
        class state:
            user = {"user_id": USER_ID, "is_admin": True}
            organization_identity = None

    body = gapi.RedrawCardRequest() if hasattr(gapi, "RedrawCardRequest") else None
    return asyncio.run(gapi.api_redraw_card(post_id, card_index, body, _Req()))


def test_d1c_redraw_is_refused_while_the_whole_post_is_generating():
    """d1 之后重抽会**接管在跑的那一代**,那一代随即作废并退款。

    用户点的是「重做这一张」,结果是整篇正在做的内容被作废 —— 而他不知道。
    当场判得出来的事,不该丢到后台去产生副作用。
    """
    from fastapi import HTTPException

    post_id = make_post(cards=3, status="generating")
    with pytest.raises(HTTPException) as err:
        _redraw_api(post_id)
    assert err.value.status_code == 409
    assert (err.value.detail or {}).get("code") == "POST_IS_GENERATING"


def test_d1c_reverse_control_ready_post_is_not_refused():
    """反向对照:ready 的作品照旧可以重抽。

    少了这条,把守卫写成「一律 409」也能让上面那条绿,而那等于把重抽功能关掉。
    """
    from fastapi import HTTPException

    post_id = make_post(cards=3, status="ready")
    try:
        _redraw_api(post_id)
    except HTTPException as e:
        assert e.status_code != 409 or (e.detail or {}).get("code") != "POST_IS_GENERATING", (
            "ready 的作品被 d1c 的守卫拦了 —— 守卫过宽")
    except Exception:
        pass   # 后面的计费/生图链在本包没桩,走到那里就说明守卫放行了


# ══════════════════════════════════════════════════════════════════════
# J4 · 存量回填脚本(默认 dry-run;执行等 Owner)
# ══════════════════════════════════════════════════════════════════════
def _legacy_ready_post():
    """造一条 d1 之前那种作品:ready、有资产、**没有版本也没有代际**。"""
    post_id = make_post(cards=3, status="ready")
    assert post_row(post_id)["active_revision_id"] is None
    assert post_row(post_id)["active_generation_task_id"] is None
    return post_id


def test_j4_dry_run_writes_nothing():
    """默认 dry-run 必须**一个字节都不写**。

    「默认安全」不是文档里的一句话,是一条会红的判据 ——
    脚本的 --execute 是写库动作,要 Owner 在 Deploy 窗口批。
    """
    import importlib

    mod = importlib.import_module("scripts.backfill_active_revisions_2026_09_13")
    post_id = _legacy_ready_post()
    before = dict(post_row(post_id))

    import sys
    argv = sys.argv
    sys.argv = ["backfill"]        # 不带 --execute,也不设 limit
    try:
        rc = mod.main()
    finally:
        sys.argv = argv

    assert rc == 0
    after = post_row(post_id)
    assert after["active_revision_id"] is None, "dry-run 写库了"
    assert after["active_generation_task_id"] == before["active_generation_task_id"]
    assert revisions_of(post_id) == []


def test_j4_execute_goes_through_the_same_door_not_a_direct_write():
    """回填必须走**代际机制**,不是给那一列填个值。

    绕过去直接 UPDATE 会造出一条没有 revision 行、或与 generation_epoch
    对不上的作品:看起来可发布,一发就撞 artifact 身份校验,而那时钱已经冻上了。
    """
    import importlib
    import sys

    mod = importlib.import_module("scripts.backfill_active_revisions_2026_09_13")
    post_id = _legacy_ready_post()

    argv = sys.argv
    sys.argv = ["backfill", "--execute"]
    try:
        rc = mod.main()
    finally:
        sys.argv = argv

    assert rc == 0
    row = post_row(post_id)
    assert row["active_revision_id"] is not None, "回填没生效"
    # 🔴 这三条才是"走了同一道门"的证据 —— 只看 active_revision_id 非空,
    #    直写也能满足。
    assert row["active_generation_task_id"] is not None, "没有代际持有者 ⇒ 绕过了那道门"
    assert int(row["generation_epoch"]) >= 1
    revs = [r for r in revisions_of(post_id) if str(r["status"]) == "active"]
    assert len(revs) == 1 and revs[0]["post_revision_id"] == row["active_revision_id"]
    # 版本内容取自库里那一版,不是空壳
    manifest = revs[0]["asset_manifest"]
    assert len(manifest["oss_keys"]) == 3
