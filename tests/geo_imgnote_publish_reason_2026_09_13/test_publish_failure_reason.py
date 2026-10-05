"""#195 c1 判据 —— 列表带发布失败的**原话**。

🔴 同一行上已经有个 `failure_reason`,那是**制作**任务的 error_msg。
   两者混用会让"发布被拒"显示成"生成失败",反过来也一样 ——
   用户按前者会去重做内容,而实际要做的是换账号或改文案。
"""
import asyncio
import json

import pytest

from .conftest import USER_ID, _conn, make_post, post_row


def _seed_failed_item(post_id, reason, *, item_status="rejected"):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO mhz_publish_orders (user_id, article_id, article_title,
                   total_cost_points, status)
               VALUES (%s,-1,'t',0,'pending') RETURNING id""", (USER_ID,))
        order_id = int(cur.fetchone()["id"])
        cur.execute(
            """INSERT INTO mhz_publish_order_items
                   (order_id, user_id, media_id, media_name, media_type, status,
                    cost_points, source_geo_post_id, billing_mode, settlement_status,
                    settlement_authority, freeze_id, freeze_table, payer_user_id,
                    task_ref, reserved_amount, physical_split_snapshot, reject_reason)
               VALUES (%s,%s,9001,'号A','mhz',%s,0,%s,'freeze_per_item','committed',
                       'direct_freeze',4242,'legacy',%s,%s,0,%s::jsonb,%s)
               RETURNING id""",
            (order_id, USER_ID, item_status, int(post_id), USER_ID,
             "tr-%d-%s" % (order_id, item_status), json.dumps({"paid": 0}), reason))
        return order_id, int(cur.fetchone()["id"])
    finally:
        conn.close()


def _set_publish_status(post_id, status):
    conn = _conn()
    try:
        conn.cursor().execute(
            "UPDATE geo_douyin_posts SET publish_status=%s WHERE id=%s",
            (status, int(post_id)))
    finally:
        conn.close()


def _list_rows(monkeypatch):
    import os

    from services.geo_douyin import image_pipeline
    import api.geo_douyin_api as gapi

    os.environ["GEO_DOUYIN_PIPELINE_ENABLED"] = "1"

    async def _fake_sign(keys, expires_seconds=900):
        return ["" for _ in (keys or [])]

    monkeypatch.setattr(image_pipeline, "signed_card_urls", _fake_sign)

    class _Req:
        class state:
            user = {"user_id": USER_ID, "is_admin": True}
            organization_identity = None

    out = asyncio.run(gapi.api_list_posts(_Req(), brand_id=None, status="",
                                          limit=50, offset=0))
    return {int(r["id"]): r for r in out["posts"]}


def test_c1_failed_post_carries_the_providers_own_words(monkeypatch):
    post_id = make_post(cards=3, status="ready")
    _seed_failed_item(post_id, "内容含违规词「最」,请修改后重投")
    _set_publish_status(post_id, "failed")

    row = _list_rows(monkeypatch)[post_id]

    assert row["publish_failure_reason"] == "内容含违规词「最」,请修改后重投", (
        "没拿到供应商原话 —— 用户只会看到一个没法行动的「失败」")


def test_c1_non_failed_post_has_empty_reason(monkeypatch):
    """反向对照:非失败行必须是空串。

    少了它,把字段写成"永远取最近一条 reject_reason"也能让主臂绿,
    而那会让一篇发成功了的作品仍然挂着上一次被拒的原话。
    """
    post_id = make_post(cards=3, status="ready")
    _seed_failed_item(post_id, "上一次被拒的原话")
    _set_publish_status(post_id, "published")

    row = _list_rows(monkeypatch)[post_id]
    assert row["publish_failure_reason"] == ""


def test_c1_is_not_the_production_error_message(monkeypatch):
    """🔴 与制作任务的 error_msg 分得开。

    这条钉的是本单最容易写错的那一步:取成 `failure_reason` 也能让主臂"有值",
    但那是另一件事的原因。
    """
    post_id = make_post(cards=3, status="ready")
    _seed_failed_item(post_id, "供应商说:账号不支持图文")
    _set_publish_status(post_id, "failed")

    row = _list_rows(monkeypatch)[post_id]
    assert row["publish_failure_reason"] == "供应商说:账号不支持图文"
    # 制作侧那个字段是独立的(本夹具没有制作任务,所以它是空/None)
    assert not (row.get("failure_reason") or ""), (
        "两个字段串了 —— 发布被拒会显示成生成失败")


def test_c1_latest_rejection_wins(monkeypatch):
    """一篇被拒两次 ⇒ 显示**最近**那次。显示第一次会让人以为没更新。"""
    post_id = make_post(cards=3, status="ready")
    _seed_failed_item(post_id, "第一次:标题太长", item_status="rejected")
    _seed_failed_item(post_id, "第二次:图片不清晰", item_status="failed")
    _set_publish_status(post_id, "failed")

    row = _list_rows(monkeypatch)[post_id]
    assert row["publish_failure_reason"] == "第二次:图片不清晰"
