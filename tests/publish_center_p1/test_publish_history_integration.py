from datetime import datetime, timedelta

import psycopg2
import psycopg2.extras
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from db.connection import get_connection
from db.meijiehezi_db import list_user_publish_history
from api import meijiehezi_api


USER_A = 910001
USER_B = 910002
BRAND_ID = 910003


def _seed_local(
    *,
    user_id=USER_A,
    item_status="submitted",
    article_id=920001,
    title="立即可见文章",
    created_at=None,
    publish_url=None,
):
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("INSERT INTO brands(id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING", (BRAND_ID, "很长的测试客户名称"))
        c.execute("""
            INSERT INTO mhz_publish_orders(user_id, article_id, article_title, status, total_items, total_cost_points, created_at)
            VALUES (%s, %s, %s, 'pending', 1, 1500, COALESCE(%s, NOW())) RETURNING id
        """, (user_id, article_id, title, created_at))
        order_id = c.fetchone()["id"]
        c.execute("""
            INSERT INTO mhz_publish_order_items(
                order_id, user_id, media_id, media_name, status, cost_points,
                brand_id, media_type, created_at, publish_url
            ) VALUES (%s, %s, 7001, '测试发布渠道', %s, 1500, %s, 'mhz', COALESCE(%s, NOW()), %s)
            RETURNING id
        """, (order_id, user_id, item_status, BRAND_ID, created_at, publish_url))
        item_id = c.fetchone()["id"]
        conn.commit()
        return order_id, item_id
    finally:
        conn.close()


def _sync_item(item_id: int, *, status=1, synced_id="SYNC-1", order_sn="ORDER-1"):
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("UPDATE mhz_publish_order_items SET mhz_order_id=%s WHERE id=%s", (order_sn, item_id))
        c.execute("""
            INSERT INTO mhz_synced_orders(
                id, order_sn, title, media_name, price, status, user_id,
                brand_id, media_type, article_id, created_at, updated_at, synced_at
            ) VALUES (%s, %s, '立即可见文章', '测试发布渠道', 10, %s, %s,
                      %s, 'mhz', 920001, NOW(), NOW(), NOW())
        """, (synced_id, order_sn, status, USER_A, BRAND_ID))
        conn.commit()
    finally:
        conn.close()


def test_brands_fixture_matches_production_schema():
    """🔴 夹具建出来的 brands 必须与生产同形,不许是手搓的两列。

    为什么这条判据存在(2026-08-20 WO-D ②):
      本目录 conftest 曾经自己 `CREATE TABLE IF NOT EXISTS brands (id, name)`。
      在 brands 尚不存在的一次性库上,它会把这张生产表**钉死成两列**;
      之后同一个库里任何 `db.diagnosis_db.init_db()` 都会在建 idx_brands_company
      时炸 UndefinedColumn,把**下游整片文件**染红,红因与被测代码零关系。

    判据成对:
      · 必须命中 —— 生产 brands 的这几列都在(它们正是老 DDL 缺的那批);
      · 必须不命中 —— 把 conftest 换回两列 DDL,本条立刻红,
        且红在「夹具 schema 过旧」这个真因上,不是散成下游一堆 UndefinedColumn。
    """
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema='public' AND table_name='brands'"
        )
        cols = {r["column_name"] for r in c.fetchall()}
    finally:
        conn.close()

    # 老的两列 DDL 只给 id/name;下面每一列都是它缺的。
    missing = {"company_name", "owner_user_id", "is_deleted", "industry",
               "created_at", "updated_at"} - cols
    assert not missing, (
        f"brands 夹具 schema 过旧,缺列 {sorted(missing)} —— "
        "conftest 又在自己 author 生产表了?建法必须走 db.diagnosis_db.init_db()。"
    )

def test_committed_local_item_is_immediately_visible_and_idor_is_closed():
    _, item_id = _seed_local()

    own = list_user_publish_history(USER_A, source="proxy")
    other = list_user_publish_history(USER_B, source="proxy")

    assert own["total"] == 1
    assert own["records"][0]["record_key"] == f"publish:proxy:{item_id}"
    assert own["records"][0]["status_label"] == "已提交，等待平台同步"
    assert own["records"][0]["points"] == 1500
    assert other["total"] == 0
    assert other["records"] == []


def test_scheduler_sync_updates_same_record_without_duplicate_or_flash():
    _, item_id = _seed_local()
    before = list_user_publish_history(USER_A, source="proxy")
    _sync_item(item_id, status=2)
    after = list_user_publish_history(USER_A, source="proxy")

    assert before["total"] == after["total"] == 1
    assert before["records"][0]["record_key"] == after["records"][0]["record_key"]
    assert after["records"][0]["status_label"] == "已完成"
    # [WO_ARTICLE_BROWSER_SELF_REPORT_2026-08-19 · R2 §②③] stats 多了一档
    # `reported_unverified`(浏览器回报过成功、服务端没核实过)。它是**新增的一格**,
    # 不是原有含义的改动 —— 代发这条 lane 走的是供应商回执,那一格恒 0。
    # 🔴 这条断言是全等式,新增一格就会红。此处**改断言**(不是退役):
    #    保留原来七格的逐格取值,再显式钉住新格 = 0。
    # 🔴 这一格的**正对照**不在本文件(本文件在素树上没有那一列,写这里会让素树臂
    #    平白变红):它在本包的
    #    `tests/article_self_report_2026_08_19/test_dual_axis_user_surfaces_pg16.py`
    #    ::test_unified_record_list_labels_and_family —— 那里插一条自报未核实的记录,
    #    断言这一格 == 1 且 completed == 0。没有那一臂,「恒 0」与「这格根本没被算」
    #    看起来一模一样。
    assert after["stats"] == {
        "total": 1,
        "completed": 1,
        "in_progress": 0,
        "pending": 0,
        "rejected": 0,
        "withdrawn": 0,
        "refunded": 0,
        "reported_unverified": 0,
    }


def test_legacy_item_linked_by_synced_primary_id_is_also_deduplicated():
    _, item_id = _seed_local()
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("UPDATE mhz_publish_order_items SET mhz_order_id='SYNC-LEGACY' WHERE id=%s", (item_id,))
        c.execute("""
            INSERT INTO mhz_synced_orders(id, order_sn, title, media_name, status, user_id, brand_id, media_type, article_id)
            VALUES ('SYNC-LEGACY', 'ORDER-LEGACY', '立即可见文章', '测试发布渠道', 1, %s, %s, 'mhz', 920001)
        """, (USER_A, BRAND_ID))
        conn.commit()
    finally:
        conn.close()

    result = list_user_publish_history(USER_A, source="proxy")
    assert result["total"] == 1
    assert result["records"][0]["record_key"] == f"publish:proxy:{item_id}"


def test_stable_db_pagination_filters_and_article_stats_have_no_overlap():
    anchor = datetime(2026, 7, 19, 12, 0, 0)
    for index in range(25):
        _seed_local(
            item_status="pending" if index % 2 == 0 else "submitted",
            article_id=930000 + (index // 2),
            title=f"分页文章 {index:02d}",
            created_at=anchor - timedelta(seconds=index),
        )
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("""
            INSERT INTO publish_records(user_id, article_id, article_title, platform, status, brand_id, created_at)
            VALUES (%s, 940001, '自助发布文章', 'zhihu', 'success', %s, %s)
        """, (str(USER_A), BRAND_ID, anchor + timedelta(seconds=1)))
        conn.commit()
    finally:
        conn.close()

    page_one = list_user_publish_history(USER_A, source="proxy", page=1, limit=20)
    page_two = list_user_publish_history(USER_A, source="proxy", page=2, limit=20)
    pending = list_user_publish_history(USER_A, source="proxy", status_filter="pending")
    self_only = list_user_publish_history(USER_A, source="self")
    article_view = list_user_publish_history(USER_A, source="proxy", view="article", limit=50)

    keys_one = {row["record_key"] for row in page_one["records"]}
    keys_two = {row["record_key"] for row in page_two["records"]}
    assert page_one["total"] == 25
    assert page_one["pages"] == 2
    assert len(keys_one) == 20
    assert len(keys_two) == 5
    assert keys_one.isdisjoint(keys_two)
    assert pending["total"] == 13
    assert pending["stats"]["total"] == 25
    assert self_only["total"] == 1
    assert article_view["total"] == 13
    assert sum(group["total"] for group in article_view["records"]) == 25


def test_uncommitted_scheduler_row_never_hides_committed_local_record(monkeypatch):
    import os

    _, item_id = _seed_local()
    test_url = os.environ["TEST_DATABASE_URL"]
    writer = psycopg2.connect(test_url, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        c = writer.cursor()
        c.execute("UPDATE mhz_publish_order_items SET mhz_order_id='ORDER-RACE' WHERE id=%s", (item_id,))
        c.execute("""
            INSERT INTO mhz_synced_orders(id, order_sn, title, media_name, status, user_id, brand_id, media_type, article_id)
            VALUES ('SYNC-RACE', 'ORDER-RACE', '立即可见文章', '测试发布渠道', 1, %s, %s, 'mhz', 920001)
        """, (USER_A, BRAND_ID))

        during = list_user_publish_history(USER_A, source="proxy")
        assert during["total"] == 1
        assert during["records"][0]["record_key"] == f"publish:proxy:{item_id}"

        writer.commit()
        after = list_user_publish_history(USER_A, source="proxy")
        assert after["total"] == 1
        assert after["records"][0]["record_key"] == f"publish:proxy:{item_id}"
        assert after["records"][0]["status_label"] == "处理中"
    finally:
        writer.rollback()
        writer.close()


def test_customer_projection_omits_provider_and_internal_payload_fields():
    _seed_local(item_status="awaiting_confirmation")
    result = list_user_publish_history(USER_A, source="proxy")
    serialized = str(result).lower()

    for forbidden in (
        "mhz_raw_response", "pending_confirm_codes", "manual_review_required",
        "cost_yuan", "price", "provider", "supplier", "table_name",
    ):
        assert forbidden not in serialized
    assert result["records"][0].get("status_detail") is None


def test_unsynced_terminal_local_record_does_not_disappear():
    _, item_id = _seed_local(item_status="failed")

    result = list_user_publish_history(USER_A, source="proxy")

    assert result["total"] == 1
    assert result["records"][0]["record_key"] == f"publish:proxy:{item_id}"
    assert result["records"][0]["status_label"] == "未完成"
    assert result["stats"]["rejected"] == 1


def test_unsynced_published_local_record_remains_visible_with_public_url():
    _, item_id = _seed_local(
        item_status="published",
        publish_url="https://example.test/published/article",
    )

    result = list_user_publish_history(USER_A, source="proxy")

    assert result["total"] == 1
    assert result["records"][0]["record_key"] == f"publish:proxy:{item_id}"
    assert result["records"][0]["status_label"] == "已完成"
    assert result["records"][0]["public_url"] == "https://example.test/published/article"
    assert result["stats"]["completed"] == 1


def test_historical_admin_dedupe_pause_never_causes_a_record_flash():
    _, item_id = _seed_local(item_status="paused_admin_dedupe")

    result = list_user_publish_history(USER_A, source="proxy")

    assert result["total"] == 1
    assert result["records"][0]["record_key"] == f"publish:proxy:{item_id}"
    assert result["records"][0]["status_label"] == "处理中"
    assert result["stats"]["in_progress"] == 1


@pytest.mark.asyncio
async def test_api_uses_authenticated_user_and_never_accepts_cross_tenant_scope(monkeypatch):
    captured = {}
    request = Request({"type": "http", "method": "GET", "path": "/api/meijiehezi/publish-history", "headers": []})
    request.state.user = {"user_id": USER_A, "is_admin": False}

    monkeypatch.setattr(meijiehezi_api, "get_config", lambda key: "1.5")
    monkeypatch.setattr(
        meijiehezi_api,
        "list_user_publish_history",
        lambda **kwargs: captured.update(kwargs) or {
            "records": [], "total": 0, "page": 1, "pages": 0,
            "stats": {}, "filters": {"brands": [], "media_types": []},
        },
    )

    response = await meijiehezi_api.api_publish_history(request)

    assert response["status"] == "success"
    assert captured["user_id"] == USER_A
    assert "all" not in captured
    assert "user_id" not in response


@pytest.mark.asyncio
async def test_api_requires_authentication():
    request = Request({"type": "http", "method": "GET", "path": "/api/meijiehezi/publish-history", "headers": []})
    with pytest.raises(HTTPException) as raised:
        await meijiehezi_api.api_publish_history(request)
    assert raised.value.status_code == 401
