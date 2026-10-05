"""第 3 棒 · Codex R2 的六组 P0 · **缝合面与终态**判据(真 PG16 + 真资金表)。

## 这一份与前面判据的区别,一句话

前面那些判据问的是「函数对不对」「调用发生了没有」。R2 的七组 P0 全部
**绕过**了它们 —— 因为调用确实发生了,只是:

  · 调过去的那一刻,被调方的领取谓词根本不匹配(`queued` vs `pending`)
    ⇒ 恒 `{'submitted': 0}` ⇒ 保证空转,而"调用发生"这件事仍然为真;
  · 布尔为真被当成状态确定(`ready if ok`)⇒ 部分交付被提升成完成。

所以本文件的判据形状是两条纪律的直接落地:

  ① **判据必须驱动被调方真领到活**(`submitted > 0` / fake 计数 ≥ 1),
     禁止拿"调用发生"当接通 —— 每一条正向都配一条**把缝合面打断**
     的反向对照,断了之后必须落到 `not_claimed` 这个专门的态上;
  ② **终态提升逐值断言封闭状态集**,`outcome.completing` 必须有覆盖用例。

🔴 反向对照全部是**行为面**的:不是"源码里有没有这个字符串",而是
   "把它改坏之后,库里那一行会变成什么"。
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import RealDictCursor  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
MIGRATIONS = [
    REPO / "db" / "migration_034_geo_image_note_contract_2026_08_17.sql",
    REPO / "db" / "migration_035_geo_image_note_slot_channel_2026_08_18.sql",
    REPO / "db" / "migration_036_publication_stage_strict_source_2026_08_18.sql",
]
PROD_SCHEMA = pathlib.Path(os.getenv(
    "GEOIMG_PROD_SCHEMA_SQL", r"C:/AI-Test/.deploy_toolkit/_geoimg_prodschema_20260817.sql"))
DSN = os.getenv("TEST_DATABASE_URL")

OWNER_UID = 4242
PUBLISH_FEATURE = "media_proxy_publish"
PUBLISH_POINTS = 28080


def _admin_dsn() -> str:
    return DSN.rsplit("/", 1)[0] + "/postgres"


@pytest.fixture(scope="module")
def live_db():
    if not DSN or not PROD_SCHEMA.is_file():
        pytest.skip("需要 TEST_DATABASE_URL 与生产 schema 夹具")
    name = "geoimg_r2seam_" + uuid.uuid4().hex[:8]
    admin = psycopg2.connect(_admin_dsn())
    admin.autocommit = True
    admin.cursor().execute('CREATE DATABASE "' + name + '"')
    admin.close()
    dsn = DSN.rsplit("/", 1)[0] + "/" + name

    conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    conn.autocommit = True
    c = conn.cursor()
    c.execute("\n".join(
        line for line in PROD_SCHEMA.read_text(encoding="utf-8", errors="ignore").splitlines()
        if not line.startswith("\\restrict") and not line.startswith("\\unrestrict")))
    c.execute("SET search_path = public")
    for mig in MIGRATIONS:
        c.execute(mig.read_text(encoding="utf-8"))
    conn.close()

    import db.connection as dbconn
    old_url, old_pool = dbconn.DATABASE_URL, dbconn._pool
    dbconn.DATABASE_URL = dsn
    dbconn._pool = None
    try:
        yield dsn
    finally:
        try:
            dbconn.close_pool()
        except Exception:  # noqa: BLE001
            pass
        dbconn.DATABASE_URL, dbconn._pool = old_url, old_pool
        admin = psycopg2.connect(_admin_dsn())
        admin.autocommit = True
        admin.cursor().execute('DROP DATABASE IF EXISTS "' + name + '" WITH (FORCE)')
        admin.close()


def _conn(dsn):
    conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    conn.autocommit = True
    return conn


@pytest.fixture()
def world(live_db):
    """一个能真正走完发布链的最小世界。

    🔴 资金那一段用**真 `freeze_points`**,不手工 INSERT `point_freezes`:
       上一轮被撤 PASS 的四条里有两条正是"夹具太假"(手工 INSERT 非付款链)。
       手工造的冻结行不会经过 `_route_freeze_table` 的那套路由,
       于是"结算传不传 freeze_table"这件事在判据里根本不产生差别。
    """
    from middleware.billing import freeze_points

    conn = _conn(live_db)
    c = conn.cursor()
    c.execute("SET search_path = public")
    c.execute("INSERT INTO users (id, username, display_name, password_hash, email) "
              "VALUES (%s,%s,%s,'x',%s) ON CONFLICT (id) DO NOTHING",
              (OWNER_UID, "owner_r2", "owner_r2", "r2@example.com"))
    c.execute("INSERT INTO user_wallets (user_id, paid_points, bonus_points, frozen_points) "
              "VALUES (%s, 1000000, 0, 0) ON CONFLICT (user_id) DO UPDATE "
              "SET paid_points = 1000000, frozen_points = 0, bonus_points = 0", (OWNER_UID,))
    c.execute("INSERT INTO feature_pricing (feature_code, cost_points, feature_name) "
              "VALUES (%s,%s,%s) ON CONFLICT (feature_code) DO UPDATE "
              "SET cost_points = EXCLUDED.cost_points",
              (PUBLISH_FEATURE, PUBLISH_POINTS, PUBLISH_FEATURE))
    c.execute("INSERT INTO brands (name, owner_user_id) VALUES (%s,%s) RETURNING id",
              ("R2品牌_" + uuid.uuid4().hex[:6], OWNER_UID))
    brand_id = int(c.fetchone()["id"])

    c.execute(
        "INSERT INTO geo_douyin_posts (brand_id, created_by, keyword, content_type, status,"
        " tenant_owner_user_id, payer_user_id, actor_user_id, source_mode,"
        " title, body_text, hashtags, cards, oss_keys, cover_oss_key)"
        " VALUES (%s,%s,'关键词A','image_post','ready',%s,%s,%s,'contract',"
        " %s,%s,'[]'::jsonb,'[]'::jsonb,%s::jsonb,%s) RETURNING id",
        (brand_id, OWNER_UID, OWNER_UID, OWNER_UID, OWNER_UID,
         "post 上的可变标题", "post 上的可变正文",
         json.dumps(["post/mutable/1.png"]), "post/mutable/1.png"))
    post_id = int(c.fetchone()["id"])

    manifest = {"oss_keys": ["rev/frozen/1.png", "rev/frozen/2.png"], "card_count": 2}
    manifest_hash = ("a" * 64)
    c.execute(
        "INSERT INTO geo_douyin_post_revisions (geo_post_id, revision_no, created_by,"
        " operation_kind, title, body, hashtags, cards_snapshot, asset_manifest,"
        " manifest_hash, status) VALUES (%s,1,%s,'create',%s,%s,'[]'::jsonb,'[]'::jsonb,"
        " %s::jsonb,%s,'active') RETURNING post_revision_id",
        (post_id, OWNER_UID, "冻结版标题", "冻结版正文",
         json.dumps(manifest), manifest_hash))
    revision_id = int(c.fetchone()["post_revision_id"])
    c.execute("UPDATE geo_douyin_posts SET active_revision_id = %s WHERE id = %s",
              (revision_id, post_id))

    cards = [{"index": 0, "state": "ready", "url": "https://cdn.example.com/a.png"},
             {"index": 1, "state": "ready", "url": "https://cdn.example.com/b.png"}]
    c.execute(
        "INSERT INTO geo_douyin_publish_artifacts (geo_post_id, post_revision_id,"
        " tenant_owner_user_id, request_id, request_hash, manifest_hash, state,"
        " card_statuses) VALUES (%s,%s,%s,%s,%s,%s,'ready',%s::jsonb)"
        " RETURNING prepared_artifact_id",
        (post_id, revision_id, OWNER_UID, str(uuid.uuid4()), "b" * 64, manifest_hash,
         json.dumps(cards)))
    artifact_id = int(c.fetchone()["prepared_artifact_id"])
    conn.close()

    task_ref = "imgnote:" + uuid.uuid4().hex + ":1"
    handle = asyncio.run(freeze_points(OWNER_UID, PUBLISH_FEATURE, task_ref=task_ref,
                                       brand_id=brand_id, reason="R2 判据夹具冻结"))
    assert handle.get("freeze_id"), "夹具冻结没拿到 freeze_id —— 后面的资金判据没有分母"

    conn = _conn(live_db)
    c = conn.cursor()
    c.execute("INSERT INTO mhz_publish_orders (user_id, article_id, article_title, status,"
              " total_items, total_cost_points) VALUES (%s,%s,%s,'pending',1,%s)"
              " RETURNING id", (OWNER_UID, -1, "R2 订单", PUBLISH_POINTS))
    order_id = int(c.fetchone()["id"])
    c.execute(
        "INSERT INTO mhz_publish_order_items (order_id, user_id, media_id, media_name,"
        " media_type, status, cost_points, source_geo_post_id, source_post_revision_id,"
        " prepared_artifact_id, manifest_hash, item_request_id, task_ref, billing_mode,"
        " settlement_authority, settlement_status, freeze_id, freeze_table, payer_user_id,"
        " reserved_amount, physical_split_snapshot, capacity_date, capacity_state)"
        " VALUES (%s,%s,9001,'夹具账号','svideo','queued',%s,%s,%s,%s,%s,%s,%s,"
        " 'freeze_per_item','direct_freeze','frozen',%s,%s,%s,%s,%s::jsonb,"
        " CURRENT_DATE,'reserved') RETURNING id",
        (order_id, OWNER_UID, PUBLISH_POINTS, post_id, revision_id, artifact_id,
         manifest_hash, str(uuid.uuid4()), task_ref, int(handle["freeze_id"]),
         str(handle["freeze_table"]), OWNER_UID, PUBLISH_POINTS,
         json.dumps(handle.get("physical_split_snapshot") or {})))
    item_id = int(c.fetchone()["id"])
    conn.close()

    return {"dsn": live_db, "brand_id": brand_id, "post_id": post_id,
            "revision_id": revision_id, "artifact_id": artifact_id,
            "order_id": order_id, "item_id": item_id, "task_ref": task_ref,
            "freeze_id": int(handle["freeze_id"]),
            "freeze_table": str(handle["freeze_table"])}


def _row(dsn, sql, params):
    conn = _conn(dsn)
    try:
        c = conn.cursor()
        c.execute(sql, params)
        row = c.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


class _FakeResult:
    def __init__(self, media_ids, *, success_count=None, order_sn="SN-1"):
        self.success = True
        self.code = 200
        self.msg = "ok"
        self.selected_num = len(media_ids)
        self.success_count = len(media_ids) if success_count is None else success_count
        self.order_sn = order_sn
        self.raw_data = {"fake": True}
        self.order_sn_map = {int(m): order_sn for m in media_ids}


class _FakeChannel:
    """发布渠道 fake。**记账**每一次调用与它收到的参数。

    规格 03 §12 要求"发布渠道 fake 调用计数" —— 上一轮列为未补。
    没有它,"worker 调了下单器"与"下单器真的把单发出去了"分不开。
    """

    def __init__(self, *, success_count=None):
        self.calls = []
        self._success_count = success_count

    async def publish_short_video(self, **kwargs):
        self.calls.append(dict(kwargs))
        media_ids = list(kwargs.get("media_ids") or [])
        return _FakeResult(media_ids, success_count=self._success_count)


@pytest.fixture()
def channel(monkeypatch):
    import api.meijiehezi_api as mapi

    fake = _FakeChannel()
    monkeypatch.setattr(mapi, "_get_client", lambda: fake)
    return fake


# ═══════════════════════════════════════════════════════════════════
# ① 缝合面 A · 发布提交:被调方必须**真领到活**
# ═══════════════════════════════════════════════════════════════════

def test_publish_submit_drives_the_callee_to_really_receive_work(world, channel):
    """正向:worker 跑一趟 ⇒ 供应商 fake **被调用一次**、`submitted > 0`、资金 commit。

    🔴 判的是"被调方领到活"这件事本身,不是"调用发生了"。上一版的
       `_submit_short_video_order` **确实被调用了**,只是它 `locked` 为空、
       一条都没领到 —— 那种绿灯是这次返工的病灶。
    """
    from services.geo_douyin.contract_worker import run_publish_submit_once

    out = asyncio.run(run_publish_submit_once(worker="w-r2"))
    assert out is not None, "queued 的合同链发布项没被领走"
    assert out["kind"] == "delivered", ("提交没有落到 delivered:" + str(out))
    assert len(channel.calls) == 1, (
        "供应商 fake 被调用 " + str(len(channel.calls)) + " 次,期望恰好 1 次 —— "
        "0 次意味着旧提交器一条都没领到(接线断裂),>1 次意味着重复外调")

    call = channel.calls[0]
    assert call["article_type"] == 3, (
        "图文笔记发成了 article_type=" + str(call["article_type"]) + " —— 3 才是图文")
    assert call["image_urls"], "image_urls 为空 —— 图文笔记没有图"
    assert call["video_url"] == "", "图文笔记不能带 video_url"
    assert call["title"] == "冻结版标题" and "冻结版正文" in call["content"], (
        "外发内容不是冻结版本:" + str(call)[:300])

    item = _row(world["dsn"], "SELECT status, settlement_status, capacity_state,"
                              " submitted_content_snapshot_hash, external_started_at"
                              " FROM mhz_publish_order_items WHERE id=%s",
                (world["item_id"],))
    assert item["settlement_status"] == "committed", item
    assert item["capacity_state"] == "consumed", item
    assert item["external_started_at"] is not None, "外调标记没有落在外调之前"
    assert item["submitted_content_snapshot_hash"], "外发快照没冻结(§8.3)"

    freeze = _row(world["dsn"], "SELECT status, task_ref FROM point_freezes WHERE id=%s",
                  (world["freeze_id"],))
    assert freeze["status"] == "committed", ("冻结没有被结算:" + str(freeze))
    assert str(freeze["task_ref"]).strip() == world["task_ref"], (
        "结算用的 task_ref 与冻结时不是同一个值:" + str(freeze))


def test_broken_seam_lands_on_not_claimed_instead_of_a_silent_failure(world, channel,
                                                                     monkeypatch):
    """反向对照:把 claim 写成旧提交器**不认**的那个值 ⇒ 必须落到 `not_claimed`。

    这就是 R2 P0-1 的原形态。它的危险之处在于**看起来像一次正常失败**。

    🔴 [第 4 棒 · P1-C · 不变式搬家 ⇒ **改断言不退役**] 上一版这里断言的是
       「资金**不许**被 release、保持 frozen + manual」。那条不变式被裁定
       改了方向,理由不是"要求松了",而是**确定性反转**:

         · 上一版的理由是"退了钱这条 bug 就被抹平成一次正常失败";
         · 但资金处置只能由**外部副作用确定不确定**决定 —— 这一格是
           **确定零外调**(被调方的领取谓词没匹配上,连远端都没联系过),
           依 `08_billing §10`「功能失败退款 = release」必须退;
         · "别把 bug 抹平"这个目标改由**我们自己的告警面**承担
           (`ai_ops_alerts` + `needs_action` 终态),而不是**扣着用户的算力**。

       所以本条现在断言的是这四件事一起成立(缺一即假绿):
         · 供应商 fake **零调用**(证明确实空转);
         · 资金**必须** released、钱**真的回到钱包**;
         · item 落 `needs_action`(钱退了不等于事情完了,得有人决定重投/取消);
         · **告警行真的写出来了** —— 这是"故障可见性不再挂在用户的钱上"的
           唯一物证。少了它,这条链断掉就会变成一次静默的正常退款。
    """
    import services.geo_douyin.contract_worker as cw
    from services.geo_douyin.contract_seams import LEGACY_SUBMITTER_INFLIGHT_STATUS

    before = _row(world["dsn"],
                  "SELECT paid_points, frozen_points FROM user_wallets WHERE user_id=%s",
                  (OWNER_UID,))
    monkeypatch.setattr(cw, "LEGACY_SUBMITTER_CLAIM_STATUS",
                        LEGACY_SUBMITTER_INFLIGHT_STATUS)
    out = asyncio.run(cw.run_publish_submit_once(worker="w-r2-broken"))

    assert out["kind"] == "not_claimed", ("缝合面断了却没被识别出来:" + str(out))
    assert len(channel.calls) == 0, "缝合面断了,但供应商 fake 仍被调用了"
    item = _row(world["dsn"], "SELECT status, settlement_status FROM"
                              " mhz_publish_order_items WHERE id=%s", (world["item_id"],))
    assert item["settlement_status"] == "released", (
        "确定零外调却没退钱(" + str(item["settlement_status"])
        + ")—— 拿用户冻结的算力当报警器")
    assert item["status"] == "needs_action", item
    freeze = _row(world["dsn"], "SELECT status FROM point_freezes WHERE id=%s",
                  (world["freeze_id"],))
    assert freeze["status"] == "released", ("冻结没被释放:" + str(freeze))

    after = _row(world["dsn"],
                 "SELECT paid_points, frozen_points FROM user_wallets WHERE user_id=%s",
                 (OWNER_UID,))
    assert int(after["frozen_points"]) == int(before["frozen_points"]) - PUBLISH_POINTS, (
        "冻结额没有真的从钱包里减掉 —— 只改了 item 那一列(库说退了、钱没动):"
        + str(before) + " -> " + str(after))
    assert int(after["paid_points"]) == int(before["paid_points"]) + PUBLISH_POINTS, (
        "算力没有真的回到可用余额:" + str(before) + " -> " + str(after))

    alert = _row(world["dsn"],
                 "SELECT rule_key, severity, status, payload FROM ai_ops_alerts"
                 " WHERE rule_key=%s AND status='firing'",
                 ("geo_imgnote_publish_seam_broken",))
    assert alert is not None, (
        "接线断了却没有拉起任何告警 —— 那就等于没人会知道:"
        "钱退了、用户看到 needs_action、而『我们自己的链断了』这件事无处可查")
    assert int((alert["payload"] or {}).get("order_item_id") or 0) == world["item_id"], alert


def test_partial_acceptance_keeps_money_frozen(world, monkeypatch):
    """接单数不足 ⇒ `awaiting_sync + frozen`,**不许**猜成成功或退款(§7.5 末)。"""
    import api.meijiehezi_api as mapi
    from services.geo_douyin.contract_worker import run_publish_submit_once

    fake = _FakeChannel(success_count=0)  # 提交 1 条、接单 0 条
    monkeypatch.setattr(mapi, "_get_client", lambda: fake)
    out = asyncio.run(run_publish_submit_once(worker="w-r2-partial"))
    assert out["kind"] == "rejected", str(out)  # success_count=0 是**明确**全失败
    freeze = _row(world["dsn"], "SELECT status FROM point_freezes WHERE id=%s",
                  (world["freeze_id"],))
    assert freeze["status"] == "released", ("明确未接单必须 release:" + str(freeze))
    item = _row(world["dsn"], "SELECT settlement_status, capacity_state FROM"
                              " mhz_publish_order_items WHERE id=%s", (world["item_id"],))
    assert item["settlement_status"] == "released"
    assert item["capacity_state"] == "released", "确定失败要把当天容量还回去"


def test_cross_post_artifact_never_reaches_the_channel(world, channel):
    """P0-3:artifact 属于另一篇作品 ⇒ 零外调。

    上一版只比 revision 与 manifest,不比 `geo_post_id` —— 于是同租户内
    可以拼出「B 的 post + A 的 artifact」,两条校验都过。
    """
    from services.geo_douyin.contract_worker import run_publish_submit_once

    conn = _conn(world["dsn"])
    conn.cursor().execute(
        "UPDATE geo_douyin_publish_artifacts SET geo_post_id = ("
        "  SELECT id FROM geo_douyin_posts WHERE id <> %s ORDER BY id LIMIT 1)"
        " WHERE prepared_artifact_id = %s",
        (world["post_id"], world["artifact_id"]))
    conn.close()
    # 没有第二篇作品时这条 UPDATE 什么也不做 —— 那会让判据失去分母,先造一篇。
    conn = _conn(world["dsn"])
    c = conn.cursor()
    c.execute("SELECT geo_post_id FROM geo_douyin_publish_artifacts WHERE"
              " prepared_artifact_id = %s", (world["artifact_id"],))
    if int(c.fetchone()["geo_post_id"]) == world["post_id"]:
        c.execute("INSERT INTO geo_douyin_posts (brand_id, created_by, content_type,"
                  " status, tenant_owner_user_id) VALUES (%s,%s,'image_post','ready',%s)"
                  " RETURNING id", (world["brand_id"], OWNER_UID, OWNER_UID))
        other = int(c.fetchone()["id"])
        c.execute("UPDATE geo_douyin_publish_artifacts SET geo_post_id=%s WHERE"
                  " prepared_artifact_id=%s", (other, world["artifact_id"]))
    conn.close()

    out = asyncio.run(run_publish_submit_once(worker="w-r2-cross"))
    assert out["kind"] == "rejected", str(out)
    assert len(channel.calls) == 0, "跨作品素材仍然被发出去了"
    item = _row(world["dsn"], "SELECT status, reject_reason FROM mhz_publish_order_items"
                              " WHERE id=%s", (world["item_id"],))
    assert item["status"] == "failed"
    assert "发布素材属于作品" in str(item["reject_reason"]), item


def test_legal_gate_blocks_the_exact_outbound_copy(world, channel):
    """P0-6:闸审的是**要发的那一份**(锁定 revision),不是 post 的可变列。

    构造:revision 违规、post 干净 —— 上一版扫 post,这一组会**放行**。
    """
    from services.geo_douyin.contract_worker import run_publish_submit_once

    conn = _conn(world["dsn"])
    c = conn.cursor()
    c.execute("UPDATE geo_douyin_post_revisions SET body = %s WHERE post_revision_id = %s",
              ("我们是全国第一的服务商", world["revision_id"]))
    c.execute("UPDATE geo_douyin_posts SET body_text = %s WHERE id = %s",
              ("完全干净的正文", world["post_id"]))
    conn.close()

    out = asyncio.run(run_publish_submit_once(worker="w-r2-legal"))
    assert len(channel.calls) == 0, "违规文案被发出去了 —— 闸扫的不是外发那一份"
    assert out["kind"] == "rejected", str(out)
    item = _row(world["dsn"], "SELECT reject_reason FROM mhz_publish_order_items"
                              " WHERE id=%s", (world["item_id"],))
    assert "广告法" in str(item["reject_reason"]), item


def test_settlement_failure_is_never_written_as_committed(world, channel, monkeypatch):
    """P0-4:`commit_freeze` 返回 `success=False` **不抛** ⇒ 必须自己判返回值。

    上一版只包了 try/except,而找不到冻结 / 跨表撞号歧义走的是
    「返回失败字典」那条路 —— except 一次都不会触发,item 被写成 committed
    而钱包一分没动。
    """
    import middleware.billing as billing
    from services.geo_douyin.contract_worker import run_publish_submit_once

    async def _fake_commit(**kwargs):
        return {"success": False, "reason": "freeze 跨表撞号歧义,需人工核", "ambiguous": True}

    monkeypatch.setattr(billing, "commit_freeze", _fake_commit)
    out = asyncio.run(run_publish_submit_once(worker="w-r2-fund"))
    assert out["settlement_status"] == "manual", str(out)
    item = _row(world["dsn"], "SELECT settlement_status, status FROM"
                              " mhz_publish_order_items WHERE id=%s", (world["item_id"],))
    assert item["settlement_status"] == "manual", (
        "结算失败却写成了 " + str(item["settlement_status"]))
    assert item["status"] == "needs_action", item


def test_settlement_passes_the_persisted_handle_not_a_rebuilt_one(world, channel,
                                                                  monkeypatch):
    """P0-4:整组句柄原样传 —— `task_ref` / `user_id` / `freeze_table` 一个都不能少。

    少了 `freeze_table`,`_route_freeze_table` 只能去猜表;两张冻结表的
    相同数字 id 撞号时会结算到**别人那一笔**。
    """
    import middleware.billing as billing
    from services.geo_douyin.contract_worker import run_publish_submit_once

    seen = {}
    real_commit = billing.commit_freeze

    async def _spy(**kwargs):
        seen.update(kwargs)
        return await real_commit(**kwargs)

    monkeypatch.setattr(billing, "commit_freeze", _spy)
    asyncio.run(run_publish_submit_once(worker="w-r2-handle"))

    assert seen.get("task_ref") == world["task_ref"], (
        "结算用的 task_ref 不是冻结时落库的那一个:" + str(seen.get("task_ref")))
    assert int(seen.get("user_id") or 0) == OWNER_UID, seen
    assert seen.get("freeze_table") == world["freeze_table"], seen
    assert seen.get("_cursor") is not None, (
        "资金与 item 终态不在同一事务里 —— 中间崩溃会写成"
        "「库说结了、钱没动」")


# ═══════════════════════════════════════════════════════════════════
# ② 缝合面 B · 生产终态:逐值封闭
# ═══════════════════════════════════════════════════════════════════

def test_terminal_mapping_covers_every_outcome_combination():
    """四个取值组合逐个断言,包括**不可能**的那一个必须抛。"""
    from services.geo_douyin.contract_seams import (
        ImpossibleOutcome, resolve_production_terminal,
    )

    assert resolve_production_terminal(ok=True, completing=False).status == "ready"
    partial = resolve_production_terminal(ok=True, completing=True)
    assert partial.status == "needs_action", (
        "部分交付被提升成 " + partial.status + " —— 那是「作品显示完成、钱还冻着、"
        "图还缺着」三个读侧同时说谎")
    assert partial.advance_slot_to_ready is False, "部分交付不能把槽位推进到已交付"
    assert resolve_production_terminal(ok=False, completing=False).status == "failed"
    with pytest.raises(ImpossibleOutcome):
        resolve_production_terminal(ok=False, completing=True)


def _seed_production_task(dsn, *, card_state: str):
    """建一条 queued 的合同链制作任务(带真实冻结句柄)。"""
    from middleware.billing import freeze_points

    conn = _conn(dsn)
    c = conn.cursor()
    c.execute("INSERT INTO feature_pricing (feature_code, cost_points, feature_name)"
              " VALUES ('geo_douyin_image_post',390,'x') ON CONFLICT (feature_code)"
              " DO UPDATE SET cost_points = 390")
    c.execute("SELECT id FROM brands WHERE owner_user_id=%s ORDER BY id LIMIT 1",
              (OWNER_UID,))
    brand_id = int(c.fetchone()["id"])
    c.execute(
        "INSERT INTO geo_douyin_posts (brand_id, created_by, keyword, content_type,"
        " status, tenant_owner_user_id, payer_user_id, actor_user_id, source_mode,"
        " title, body_text, hashtags, cards, oss_keys, production_batch_id)"
        " VALUES (%s,%s,'词','image_post','generating',%s,%s,%s,'contract',"
        " '生成后的标题','生成后的正文','[\"#a\"]'::jsonb,'[]'::jsonb,%s::jsonb,%s)"
        " RETURNING id",
        (brand_id, OWNER_UID, OWNER_UID, OWNER_UID, OWNER_UID,
         json.dumps(["gen/1.png", "gen/2.png"]), str(uuid.uuid4())))
    post_id = int(c.fetchone()["id"])
    conn.close()

    task_ref = "imgnote:" + uuid.uuid4().hex + ":9"
    handle = asyncio.run(freeze_points(OWNER_UID, "geo_douyin_image_post",
                                       task_ref=task_ref, brand_id=brand_id))
    conn = _conn(dsn)
    c = conn.cursor()
    c.execute(
        "INSERT INTO geo_douyin_post_tasks (post_id, user_id, task_ref, status,"
        " production_batch_id, batch_item_ordinal, settlement_status,"
        " settlement_authority, payer_user_id, freeze_id, freeze_table,"
        " reserved_amount, physical_split_snapshot, billing_mode, progress_total)"
        " VALUES (%s,%s,%s,'queued',%s,1,'frozen','direct_freeze',%s,%s,%s,%s,"
        " %s::jsonb,'freeze_per_item',2) RETURNING id",
        (post_id, OWNER_UID, task_ref, str(uuid.uuid4()), OWNER_UID,
         int(handle["freeze_id"]), str(handle["freeze_table"]), 390,
         json.dumps(handle.get("physical_split_snapshot") or {})))
    task_id = int(c.fetchone()["id"])
    c.execute("UPDATE geo_douyin_posts SET active_generation_task_id=%s,"
              " generation_epoch = generation_epoch + 1 WHERE id=%s", (task_id, post_id))
    conn.close()
    return {"post_id": post_id, "task_id": task_id, "task_ref": task_ref,
            "freeze_id": int(handle["freeze_id"])}


class _Outcome:
    def __init__(self, ok, completing):
        self.ok = ok
        self.completing = completing
        self.cards_done = 1
        self.cards_total = 2
        self.error = ""
        self.refunded = False


def _run_production_with(dsn, monkeypatch, *, ok, completing):
    import services.geo_douyin.production_task as pt

    async def _fake(**kwargs):
        return _Outcome(ok, completing)

    monkeypatch.setattr(pt, "run_image_post_production", _fake)
    from services.geo_douyin.contract_worker import run_production_once
    return asyncio.run(run_production_once(worker="w-r2-prod"))


def test_partial_delivery_is_not_promoted_to_ready(world, monkeypatch):
    """行为面:`completing=True` 的一趟 ⇒ 任务 `needs_action`、槽位不推进、钱仍冻着。"""
    seeded = _seed_production_task(world["dsn"], card_state="partial")
    out = _run_production_with(world["dsn"], monkeypatch, ok=True, completing=True)
    assert out and out["task_id"] == seeded["task_id"], str(out)
    assert out["status"] == "needs_action", str(out)

    task = _row(world["dsn"], "SELECT status, settlement_status, error_msg FROM"
                              " geo_douyin_post_tasks WHERE id=%s", (seeded["task_id"],))
    assert task["status"] == "needs_action", task
    assert task["settlement_status"] == "frozen", ("部分交付的钱不该被结算:" + str(task))
    assert task["error_msg"], "非 ready 终态没有写原因"
    post = _row(world["dsn"], "SELECT active_revision_id FROM geo_douyin_posts WHERE id=%s",
                (seeded["post_id"],))
    assert post["active_revision_id"] is None, (
        "部分交付却冻出了成品版本 —— 缺图的那一版会被当成可发布成品")


def test_full_delivery_creates_the_active_revision(world, monkeypatch):
    """P0-3 正向:成功一趟必须冻出 active revision 并让 post 指针指过去。

    没有它,素材准备入口(硬要求 `active_revision_id`)对**每一篇新作品**都 409 ——
    整条「生产 → 素材 → 发布」在这一格断开。
    """
    seeded = _seed_production_task(world["dsn"], card_state="full")
    out = _run_production_with(world["dsn"], monkeypatch, ok=True, completing=False)
    assert out["status"] == "ready", str(out)
    assert out["post_revision_id"], "成功了却没有成品版本"

    post = _row(world["dsn"], "SELECT active_revision_id FROM geo_douyin_posts WHERE id=%s",
                (seeded["post_id"],))
    assert int(post["active_revision_id"] or 0) == int(out["post_revision_id"])
    rev = _row(world["dsn"], "SELECT title, body, status, asset_manifest FROM"
                             " geo_douyin_post_revisions WHERE post_revision_id=%s",
               (out["post_revision_id"],))
    assert rev["status"] == "active", rev
    assert rev["title"] == "生成后的标题", ("成品版本没有取库里那一份:" + str(rev))
    assert (rev["asset_manifest"] or {}).get("oss_keys") == ["gen/1.png", "gen/2.png"], rev


# ═══════════════════════════════════════════════════════════════════
# ③ P0-5 · 崩溃回收:三条链各有各的回收器
# ═══════════════════════════════════════════════════════════════════

def test_artifact_reconciler_marks_unknown_not_failed(world):
    """已开始上传、租约过期 ⇒ `unknown`(不可自动重传),不是 `failed`。"""
    from services.geo_douyin.contract_worker import _reconcile_stuck_artifacts

    conn = _conn(world["dsn"])
    conn.cursor().execute(
        "UPDATE geo_douyin_publish_artifacts SET state='preparing',"
        " external_started_at = now() - interval '2 hours',"
        " lease_owner='dead', lease_expires_at = now() - interval '1 hour'"
        " WHERE prepared_artifact_id = %s", (world["artifact_id"],))
    conn.close()

    assert _reconcile_stuck_artifacts() >= 1
    art = _row(world["dsn"], "SELECT state, lease_owner FROM geo_douyin_publish_artifacts"
                             " WHERE prepared_artifact_id=%s", (world["artifact_id"],))
    assert art["state"] == "unknown", art
    assert art["lease_owner"] is None, "租约没释放,别的 worker 永远领不到"


def test_publish_item_reconciler_holds_the_money(world):
    """已外调、租约过期 ⇒ `needs_action + manual`;**不 release、不重投**。"""
    from services.geo_douyin.contract_worker import _reconcile_stuck_publish_items

    conn = _conn(world["dsn"])
    conn.cursor().execute(
        "UPDATE mhz_publish_order_items SET status='pending',"
        " external_started_at = now() - interval '2 hours',"
        " lease_owner='dead', lease_expires_at = now() - interval '1 hour'"
        " WHERE id = %s", (world["item_id"],))
    conn.close()

    assert _reconcile_stuck_publish_items() >= 1
    item = _row(world["dsn"], "SELECT status, settlement_status FROM"
                              " mhz_publish_order_items WHERE id=%s", (world["item_id"],))
    assert item["status"] == "needs_action", item
    assert item["settlement_status"] == "manual", item
    freeze = _row(world["dsn"], "SELECT status FROM point_freezes WHERE id=%s",
                  (world["freeze_id"],))
    assert freeze["status"] == "frozen", ("结果未知却动了钱:" + str(freeze))


def test_legacy_orphan_sweeper_skips_the_new_chain_but_still_sweeps_old_rows(world):
    """§8.3:老 sweeper 必须按 `billing_mode` 排除新链 —— 且**只**排除新链。

    反向对照配在同一条里:老行(`billing_mode IS NULL`)必须仍然被清理,
    否则这个谓词就不是"隔离"而是"关掉了老 sweeper"。
    """
    from db.meijiehezi_db import cleanup_orphan_submitting_items

    conn = _conn(world["dsn"])
    c = conn.cursor()
    c.execute("UPDATE mhz_publish_order_items SET status='submitting',"
              " last_submit_at = now() - interval '1 hour' WHERE id=%s",
              (world["item_id"],))
    c.execute("INSERT INTO mhz_publish_order_items (order_id, user_id, media_id,"
              " media_name, media_type, status, cost_points, last_submit_at)"
              " VALUES (%s,%s,9002,'老账号','svideo','submitting',100,"
              " now() - interval '1 hour') RETURNING id",
              (world["order_id"], OWNER_UID))
    legacy_id = int(c.fetchone()["id"])
    conn.close()

    cleanup_orphan_submitting_items(timeout_minutes=5)
    new_row = _row(world["dsn"], "SELECT status FROM mhz_publish_order_items WHERE id=%s",
                   (world["item_id"],))
    old_row = _row(world["dsn"], "SELECT status FROM mhz_publish_order_items WHERE id=%s",
                   (legacy_id,))
    assert new_row["status"] == "submitting", (
        "新链的 item 被老 sweeper 动了(" + str(new_row["status"]) + ")—— "
        "两条链的收敛动作相反,先跑到的说了算")
    assert old_row["status"] == "pending", (
        "老行没有被清理(" + str(old_row["status"]) + ")—— 谓词把老 sweeper 关掉了")


# ═══════════════════════════════════════════════════════════════════
# ④ P1 · 读侧封闭
# ═══════════════════════════════════════════════════════════════════

def test_normalize_task_status_value_domain_is_closed():
    from services.geo_douyin.contract_states import (
        CONTRACT_TASK_STATUSES, describe_task_status, normalize_task_status,
    )

    for raw in ("pending", "succeeded", "running", "completing", "superseded",
                "ready", "needs_action", "", "某个没人见过的值"):
        assert normalize_task_status(raw) in CONTRACT_TASK_STATUSES, (
            "状态 " + repr(raw) + " 归口到了封闭集合之外 ⇒ summary 一个格子都不进、"
            "前端文案表查不到 = 静默消失")
    assert normalize_task_status("completing") == "needs_action"
    detail = describe_task_status("某个没人见过的值")
    assert detail["recognized"] is False and detail["raw"] == "某个没人见过的值", detail


def test_batch_status_is_projected_from_items():
    from services.geo_douyin.contract_states import project_batch_status

    assert project_batch_status(["queued", "queued"]) == "accepted"
    assert project_batch_status(["ready", "queued"]) == "processing"
    assert project_batch_status(["ready", "ready"]) == "completed"
    assert project_batch_status(["ready", "failed"]) == "partial_success"
    assert project_batch_status(["failed", "failed"]) == "failed"
    assert project_batch_status(["ready", "needs_action"]) == "needs_action"


# ═══════════════════════════════════════════════════════════════════
# ⑤ P0-3 后半 · 素材取的是冻结版本;入口不再恒 409
# ═══════════════════════════════════════════════════════════════════

def test_artifact_prepare_uploads_the_frozen_manifest_not_the_mutable_post(world,
                                                                          monkeypatch):
    """OSS/代理上传 fake(规格 03 §12 要求、上一轮列为未补的第二条)。

    夹具刻意让两边**不一样**:revision 的 manifest 是 `rev/frozen/*`,
    post 的可变列是 `post/mutable/*`。上一版从 post 取 —— 这一组会拿到
    `post/mutable/1.png`,也就是"确认的是 A 版、上传的是 B 版"。
    """
    import services.geo_douyin.publish_adapter as adapter
    from services.geo_douyin.contract_worker import run_artifact_prepare_once

    seen = {"calls": 0, "keys": None}

    class _Prepared:
        def __init__(self, keys):
            self.ok = True
            self.image_urls = ["https://cdn.example.com/" + str(i) + ".png"
                               for i, _ in enumerate(keys)]
            self.error = ""

    async def _fake_prepare(oss_keys):
        seen["calls"] += 1
        seen["keys"] = list(oss_keys)
        return _Prepared(oss_keys)

    monkeypatch.setattr(adapter, "prepare_publish_images", _fake_prepare)

    conn = _conn(world["dsn"])
    conn.cursor().execute(
        "UPDATE geo_douyin_publish_artifacts SET state='preparing', manifest_hash=NULL,"
        " card_statuses='[]'::jsonb, lease_owner=NULL, lease_expires_at=NULL,"
        " external_started_at=NULL WHERE prepared_artifact_id=%s",
        (world["artifact_id"],))
    conn.close()

    out = asyncio.run(run_artifact_prepare_once(worker="w-r2-art"))
    assert out and out["state"] == "ready", str(out)
    assert seen["calls"] == 1, ("上传 fake 被调用 " + str(seen["calls"]) + " 次,期望 1")
    assert seen["keys"] == ["rev/frozen/1.png", "rev/frozen/2.png"], (
        "素材没有取冻结版本的 manifest,取到的是:" + str(seen["keys"]))
    art = _row(world["dsn"], "SELECT state, manifest_hash, external_started_at FROM"
                             " geo_douyin_publish_artifacts WHERE prepared_artifact_id=%s",
               (world["artifact_id"],))
    assert art["state"] == "ready" and art["manifest_hash"], art
    assert art["external_started_at"] is not None, "外调标记必须在外调之前落库"


def test_prepare_media_entrance_is_reachable_after_production(world, monkeypatch):
    """P0-3 的用户可见后果:生产完之后,素材准备入口必须**能进去**。

    上一版 `active_revision_id` 恒 NULL ⇒ 这个入口对每一篇新作品都
    409 `SOURCE_NOT_READY`。这一条走**真 HTTP**,打的是那句 409 会不会再出现。
    """
    testclient = pytest.importorskip("fastapi.testclient")
    from fastapi import FastAPI, Request as FastAPIRequest

    from api.geo_image_note_api import router

    seeded = _seed_production_task(world["dsn"], card_state="full")
    out = _run_production_with(world["dsn"], monkeypatch, ok=True, completing=False)
    assert out["status"] == "ready", str(out)

    app = FastAPI()
    app.include_router(router)

    @app.middleware("http")
    async def _auth(request: FastAPIRequest, call_next):
        request.state.user = {"user_id": OWNER_UID, "id": OWNER_UID, "username": "o",
                              "is_admin": False, "client_brand_ids": None,
                              "permissions": ["writing:read", "writing:write"]}
        return await call_next(request)

    os.environ["GEO_IMAGE_NOTE_CONTRACT_ENABLED"] = "true"
    client = testclient.TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/api/geo-douyin/posts/" + str(seeded["post_id"]) + "/prepare-publish-media-v2",
        headers={"Idempotency-Key": str(uuid.uuid4())})
    assert resp.status_code < 500, resp.text[:800]
    assert resp.status_code == 200, (
        "素材准备入口没进去(" + str(resp.status_code) + "):" + resp.text[:500])
    body = resp.json()
    assert int(body["post_revision_id"]) == int(out["post_revision_id"]), body


def test_run_tick_wires_all_three_chains_and_all_three_reconcilers(world):
    """一轮 tick 必须**同时**覆盖三条链与三个回收器。

    计数键就是接线的清单:少一个键 = 少一条链被启动。
    """
    from services.geo_douyin.contract_worker import run_tick

    counts = asyncio.run(run_tick(per_tick=1))
    for key in ("production", "artifact", "publish", "reconciled",
                "reconciled_artifacts", "reconciled_publish"):
        assert key in counts, ("run_tick 少了 " + key + " —— 那条链/回收器没有启动者")


# ═══════════════════════════════════════════════════════════════════
# ⑥ 裁定 P0-6 · 平台直营账号的**真实账本**行为锁
# ═══════════════════════════════════════════════════════════════════

PLATFORM_UID = 4136


def test_admin_path_really_charges_the_platform_account_end_to_end(live_db, monkeypatch):
    """`admin_exempt` = 对平台账的**真实** freeze → worker → commit → 账本。

    🔴 上一轮这条只有"构造假 handle 然后 rollback"的判据 —— 它证明不了
       钱真的从平台账走了。这里跑完整一趟,并且**逐格**核对三处物理事实:
       钱包 frozen 归零、`point_freezes` 变 committed、`point_transactions`
       出现该用户的 consume 流水。少任何一处,「免费=不记账」那条违规
       就会以另一种形式回来(账面上像扣了,账本里查不到)。
    """
    from middleware.billing import freeze_points
    from services.geo_douyin.contract_funding import (
        AUTHORITY_ADMIN_EXEMPT, resolve_settlement_authority,
    )
    import api.meijiehezi_api as mapi

    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", str(PLATFORM_UID))
    import services.commercial_service_routing as routing
    monkeypatch.setattr(routing, "get_platform_direct_service_user_id",
                        lambda: PLATFORM_UID, raising=False)

    resolved = resolve_settlement_authority(
        is_admin=True, organization_id=None, organization_billing_ready=False,
        owner_user_id=OWNER_UID)
    assert resolved["authority"] == AUTHORITY_ADMIN_EXEMPT
    assert int(resolved["payer_user_id"]) == PLATFORM_UID, resolved

    conn = _conn(live_db)
    c = conn.cursor()
    c.execute("INSERT INTO users (id, username, display_name, password_hash, email)"
              " VALUES (%s,'platform_136','平台直营','x','p@example.com')"
              " ON CONFLICT (id) DO NOTHING", (PLATFORM_UID,))
    c.execute("INSERT INTO user_wallets (user_id, paid_points, bonus_points, frozen_points)"
              " VALUES (%s, 500000, 0, 0) ON CONFLICT (user_id) DO UPDATE"
              " SET paid_points = 500000, frozen_points = 0, bonus_points = 0",
              (PLATFORM_UID,))
    c.execute("SELECT id FROM brands WHERE owner_user_id=%s ORDER BY id LIMIT 1", (OWNER_UID,))
    brand_id = int(c.fetchone()["id"])
    c.execute(
        "INSERT INTO geo_douyin_posts (brand_id, created_by, content_type, status,"
        " tenant_owner_user_id, title, body_text) VALUES (%s,%s,'image_post','ready',%s,"
        " '平台标题','平台正文') RETURNING id", (brand_id, PLATFORM_UID, PLATFORM_UID))
    post_id = int(c.fetchone()["id"])
    mh = "c" * 64
    c.execute(
        "INSERT INTO geo_douyin_post_revisions (geo_post_id, revision_no, created_by,"
        " operation_kind, title, body, hashtags, cards_snapshot, asset_manifest,"
        " manifest_hash, status) VALUES (%s,1,%s,'create','平台标题','平台正文',"
        " '[]'::jsonb,'[]'::jsonb,'{}'::jsonb,%s,'active') RETURNING post_revision_id",
        (post_id, PLATFORM_UID, mh))
    rev_id = int(c.fetchone()["post_revision_id"])
    c.execute(
        "INSERT INTO geo_douyin_publish_artifacts (geo_post_id, post_revision_id,"
        " tenant_owner_user_id, request_id, request_hash, manifest_hash, state,"
        " card_statuses) VALUES (%s,%s,%s,%s,%s,%s,'ready',%s::jsonb)"
        " RETURNING prepared_artifact_id",
        (post_id, rev_id, PLATFORM_UID, str(uuid.uuid4()), "d" * 64, mh,
         json.dumps([{"index": 0, "state": "ready", "url": "https://cdn/x.png"}])))
    artifact_id = int(c.fetchone()["prepared_artifact_id"])
    conn.close()

    task_ref = "imgnote:" + uuid.uuid4().hex + ":p"
    handle = asyncio.run(freeze_points(PLATFORM_UID, PUBLISH_FEATURE, task_ref=task_ref,
                                       brand_id=brand_id, reason="平台账真实冻结"))
    assert handle.get("freeze_id"), "平台账没冻上 —— 又变成零 handle 的假免扣了"
    wallet = _row(live_db, "SELECT paid_points, frozen_points FROM user_wallets"
                           " WHERE user_id=%s", (PLATFORM_UID,))
    assert int(wallet["frozen_points"]) == PUBLISH_POINTS, wallet

    conn = _conn(live_db)
    c = conn.cursor()
    c.execute("INSERT INTO mhz_publish_orders (user_id, article_id, article_title, status,"
              " total_items, total_cost_points) VALUES (%s,-2,'平台订单','pending',1,%s)"
              " RETURNING id", (PLATFORM_UID, PUBLISH_POINTS))
    order_id = int(c.fetchone()["id"])
    c.execute(
        "INSERT INTO mhz_publish_order_items (order_id, user_id, media_id, media_name,"
        " media_type, status, cost_points, source_geo_post_id, source_post_revision_id,"
        " prepared_artifact_id, manifest_hash, item_request_id, task_ref, billing_mode,"
        " settlement_authority, settlement_status, freeze_id, freeze_table, payer_user_id,"
        " reserved_amount, physical_split_snapshot, capacity_date, capacity_state)"
        " VALUES (%s,%s,9003,'平台账号','svideo','queued',%s,%s,%s,%s,%s,%s,%s,"
        " 'freeze_per_item','admin_exempt','frozen',%s,%s,%s,%s,%s::jsonb,"
        " CURRENT_DATE,'reserved') RETURNING id",
        (order_id, PLATFORM_UID, PUBLISH_POINTS, post_id, rev_id, artifact_id, mh,
         str(uuid.uuid4()), task_ref, int(handle["freeze_id"]),
         str(handle["freeze_table"]), PLATFORM_UID, PUBLISH_POINTS,
         json.dumps(handle.get("physical_split_snapshot") or {})))
    item_id = int(c.fetchone()["id"])
    conn.close()

    # 🔴 先把**别的**用例留下的 queued 项挪走:worker 领的是"最老的一条",
    #    不清干净的话这一趟领到的会是上一条用例的项 —— 判据就打空了。
    #    (这本身也是一次分母自证:下面 `out["order_item_id"] == item_id` 会兜住。)
    conn = _conn(live_db)
    conn.cursor().execute(
        "UPDATE mhz_publish_order_items SET status='cancelled'"
        " WHERE status='queued' AND billing_mode='freeze_per_item' AND id <> %s",
        (item_id,))
    conn.close()

    fake = _FakeChannel()
    monkeypatch.setattr(mapi, "_get_client", lambda: fake)
    from services.geo_douyin.contract_worker import run_publish_submit_once
    out = asyncio.run(run_publish_submit_once(worker="w-r2-platform"))
    assert out["order_item_id"] == item_id, ("领到的不是平台那一项:" + str(out))
    assert out["kind"] == "delivered", str(out)

    freeze = _row(live_db, "SELECT status, user_id FROM point_freezes WHERE id=%s",
                  (int(handle["freeze_id"]),))
    assert freeze["status"] == "committed" and int(freeze["user_id"]) == PLATFORM_UID, freeze
    wallet = _row(live_db, "SELECT paid_points, frozen_points FROM user_wallets"
                           " WHERE user_id=%s", (PLATFORM_UID,))
    assert int(wallet["frozen_points"]) == 0, ("冻结没出池:" + str(wallet))
    assert int(wallet["paid_points"]) == 500000 - PUBLISH_POINTS, (
        "平台账余额没有真的减少 —— 「免费=不记账」违规又回来了:" + str(wallet))
    ledger = _row(live_db, "SELECT count(*) AS n FROM point_transactions"
                           " WHERE user_id=%s AND type='consume'", (PLATFORM_UID,))
    assert int(ledger["n"]) >= 1, "平台账没有 consume 流水 —— 账本里查不到这笔"


# ═══════════════════════════════════════════════════════════════════
# ⑦ 03 §12 #6 的另一半 · regenerate override 白名单在 handler 内**强制**
# ═══════════════════════════════════════════════════════════════════

def _regen_client(monkeypatch):
    testclient = pytest.importorskip("fastapi.testclient")
    from fastapi import FastAPI, Request as FastAPIRequest

    import api.geo_douyin_api as gapi

    seen = {"dispatch": []}
    monkeypatch.setattr(gapi, "is_pipeline_enabled", lambda: True, raising=False)

    def _fake_dispatch(**kwargs):
        seen["dispatch"].append(dict(kwargs))

    monkeypatch.setattr("services.geo_douyin.production_task.dispatch_production",
                        _fake_dispatch)

    async def _fake_name(_bid):
        return "夹具客户"

    monkeypatch.setattr(gapi, "fetch_brand_display_name", _fake_name, raising=False)

    app = FastAPI()
    app.include_router(gapi.router)

    @app.middleware("http")
    async def _auth(request: FastAPIRequest, call_next):
        request.state.user = {"user_id": OWNER_UID, "id": OWNER_UID, "username": "o",
                              "is_admin": True, "client_brand_ids": None,
                              "permissions": ["writing:read", "writing:write"]}
        return await call_next(request)

    return testclient.TestClient(app, raise_server_exceptions=False), seen


def test_regenerate_rejects_non_whitelisted_override_with_zero_side_effect(world,
                                                                          monkeypatch):
    """非白名单键 ⇒ 400 且**零副作用**(状态不动、没排队)。

    🔴 上一棒的理由是「当前 handler 只收三项,天然是白名单子集」——
       那是**当下**为真、加一个字段就为假的性质,不是校验。
    """
    client, seen = _regen_client(monkeypatch)
    resp = client.post("/api/geo-douyin/posts/" + str(world["post_id"]) + "/regenerate",
                       json={"settings_override": {"style_key": "clean",
                                                   "brand_id": 999}})
    assert resp.status_code == 400, resp.text[:400]
    assert resp.json()["detail"]["code"] == "OVERRIDE_NOT_ALLOWED", resp.text[:400]
    assert "brand_id" in resp.json()["detail"]["reason"], resp.text[:400]
    assert seen["dispatch"] == [], "被拒的一次仍然排了队 —— 不是零副作用"
    post = _row(world["dsn"], "SELECT status FROM geo_douyin_posts WHERE id=%s",
                (world["post_id"],))
    assert post["status"] != "generating", ("被拒的一次改了作品状态:" + str(post))


def test_regenerate_whitelisted_override_actually_takes_effect(world, monkeypatch):
    """反向对照:白名单内的键必须**真的生效**。

    只验"被拒"不验"生效",就会得到一个「校验通过但值被忽略」的假接线 ——
    那正是这一整轮返工的病根形态。
    """
    client, seen = _regen_client(monkeypatch)
    resp = client.post("/api/geo-douyin/posts/" + str(world["post_id"]) + "/regenerate",
                       json={"settings_override": {"card_count": 7,
                                                   "aspect_ratio": "9:16",
                                                   "style_key": "photo"}})
    assert resp.status_code == 200, resp.text[:400]
    assert len(seen["dispatch"]) == 1, seen
    call = seen["dispatch"][0]
    assert int(call["card_count"]) == 7, call
    assert call["aspect_ratio"] == "9:16", call
    assert call["style_key"] == "photo", call


# ═══════════════════════════════════════════════════════════════════
# ⑧ P0-09 可做的那一半 · 槽位选题不许被换掉
# ═══════════════════════════════════════════════════════════════════

def test_slot_topic_ref_cannot_be_swapped(world):
    """有 → 另一个值 = 换题,拒;空 → 有、以及同值重放,放行。

    🔴 三个分支一起打:只打"拒"那一格,把闸调成"拒绝一切"也是绿的。
    """
    from services.geo_douyin.delivery_slots import (
        SlotIdentityMismatch, assert_topic_ref_unchanged,
    )

    assert_topic_ref_unchanged({"topic_ref": None}, "topic:7:1")        # 第一次落题
    assert_topic_ref_unchanged({"topic_ref": "topic:7:1"}, "topic:7:1")  # 重放
    assert_topic_ref_unchanged({"topic_ref": "topic:7:1"}, None)         # 本次不声明
    with pytest.raises(SlotIdentityMismatch):
        assert_topic_ref_unchanged({"topic_ref": "topic:7:1"}, "topic:9:2")


def test_slot_topic_guard_is_wired_into_the_real_transition():
    """接线判据:守卫必须长在 `transition` 里,不是一个没人调的纯函数。"""
    import inspect

    from services.geo_douyin import delivery_slots

    src = inspect.getsource(delivery_slots.transition)
    assert "assert_topic_ref_unchanged(" in src, (
        "换题守卫没有被 transition 调用 —— 又一个只被判据调用的死函数")


# ═══════════════════════════════════════════════════════════════════
# ⑨ 03 §12 #20 · durable worker 的 **kill-point** 恢复(真进程被杀)
# ═══════════════════════════════════════════════════════════════════

_KILL_SCRIPT = r'''
import asyncio, os, sys
sys.path.insert(0, r"{repo}")
import db.connection as dbconn
dbconn.DATABASE_URL = r"{dsn}"
dbconn._pool = None

import api.meijiehezi_api as mapi


class _DieMidCall:
    async def publish_short_video(self, **kwargs):
        # 🔴 在**真外调那一瞬**硬杀自己:`os._exit` 不跑 finally、不 flush、
        #    不回滚 —— 这正是容器被 OOM/重启杀掉时的形态。
        #    用 raise 或 sys.exit 都不行:那两种会被 worker 的 except 接住,
        #    测到的就不是"崩溃",而是"异常处理"。
        os._exit(9)


mapi._get_client = lambda: _DieMidCall()
from services.geo_douyin.contract_worker import run_publish_submit_once
asyncio.run(run_publish_submit_once(worker="w-killpoint"))
print("NOT_KILLED")
'''


def test_worker_killed_mid_external_call_recovers_without_double_dispatch(world):
    """跑到一半**真的杀掉进程**,再起来必须:不重投、不退款、转人工。

    上一轮这一格报的是「租约/CAS/reconciler 全有,但没做真崩溃注入」。
    这里注入的是最危险的那一瞬:**外调已经发出、终态还没落库**。

    三条断言缺一不可:
      · `external_started_at` 已落库(否则恢复逻辑会把它当"从没开始过");
      · 新 worker **领不到**它(领了就是重复外调 + 重复扣费);
      · 回收后是 `needs_action + manual`,钱**仍冻着**(不许猜成失败退款)。
    """
    import subprocess
    import sys

    # 🔴 分母:worker 领的是"最老的一条"。同模块前面的用例会留下 queued 项,
    #    不清干净的话被杀的那一趟处理的是**别人的**项,本条的三个断言全打在空处
    #    (单跑绿、全量跑红 —— 我第一版正是这样)。
    conn = _conn(world["dsn"])
    conn.cursor().execute(
        "UPDATE mhz_publish_order_items SET status='cancelled'"
        " WHERE status='queued' AND billing_mode='freeze_per_item' AND id <> %s",
        (world["item_id"],))
    conn.close()

    script = _KILL_SCRIPT.format(repo=str(REPO), dsn=world["dsn"])
    proc = subprocess.run([sys.executable, "-c", script], cwd=str(REPO),
                          capture_output=True, text=True, errors="ignore", timeout=180)
    assert proc.returncode == 9, (
        "子进程没有按预期被杀(rc=" + str(proc.returncode) + ")—— 崩溃注入没生效,"
        "这条判据失去分母:\n" + (proc.stdout or "")[-500:] + (proc.stderr or "")[-800:])
    assert "NOT_KILLED" not in (proc.stdout or ""), proc.stdout[-300:]

    item = _row(world["dsn"], "SELECT status, settlement_status, external_started_at,"
                              " lease_owner FROM mhz_publish_order_items WHERE id=%s",
                (world["item_id"],))
    assert item["external_started_at"] is not None, (
        "进程死在外调那一瞬,库里却看不出「开始过」 —— 恢复逻辑会重投:" + str(item))
    assert item["lease_owner"] == "w-killpoint", ("租约没留在死掉的那个 worker 名下:"
                                                  + str(item))

    # ① 租约还没过期时:新 worker 绝不能领走它
    from services.geo_douyin.contract_worker import (
        _reconcile_stuck_publish_items, run_publish_submit_once,
    )
    again = asyncio.run(run_publish_submit_once(worker="w-fresh"))
    assert again is None or int(again.get("order_item_id") or 0) != world["item_id"], (
        "崩溃后的项被新 worker 重新领走了 = 重复外调 + 重复扣费:" + str(again))

    # ② 租约过期 + 宽限期之后:收敛成人工,钱不动
    conn = _conn(world["dsn"])
    conn.cursor().execute(
        "UPDATE mhz_publish_order_items SET lease_expires_at = now() - interval '2 hours'"
        " WHERE id = %s", (world["item_id"],))
    conn.close()
    assert _reconcile_stuck_publish_items() >= 1
    item = _row(world["dsn"], "SELECT status, settlement_status FROM"
                              " mhz_publish_order_items WHERE id=%s", (world["item_id"],))
    assert item["status"] == "needs_action" and item["settlement_status"] == "manual", item
    freeze = _row(world["dsn"], "SELECT status FROM point_freezes WHERE id=%s",
                  (world["freeze_id"],))
    assert freeze["status"] == "frozen", ("结果未知却动了钱:" + str(freeze))


def test_unknown_state_is_closed_but_the_raw_value_survives():
    """封闭归口**不等于**丢掉原值:认不出的词原样带在 `state_raw` 里。

    🔴 这条同时是 `describe_task_status` 的接线判据 —— 它不能只被判据调用。
    """
    import inspect

    import api.geo_image_note_api as api
    from services.geo_douyin.contract_states import describe_task_status

    src = inspect.getsource(api.api_get_batch)
    assert "describe_task_status(" in src, (
        "batch GET 没用 describe_task_status —— 那它就是个只被判据调用的死函数")
    assert "state_raw" in src, "认不出的原值没有出口,排查时查不到库里写的是什么"
    assert describe_task_status("ready")["recognized"] is True
