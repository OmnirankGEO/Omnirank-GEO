"""第 4 棒 · Codex R3 的两 P0 两 P1:**崩溃窗口 / 整组句柄 / 确定即退 / 向前闸**。

## 这一份与第 3 棒判据的区别,一句话

第 3 棒的 kill-point 判据把 `os._exit(9)` 打在**供应商调用内部** ——
那一瞬 item 还停在我们自己写的 `pending`,**恰好落在既有回收器扫得到的那一格**。
于是它全绿,证明的却只是「已经覆盖的窗口被覆盖了」。

真正没人管的窗口在**后面一点点**:旧提交器自带独立连接,它成功/失败时
**自己就把终态 commit 了**(`submitted` / `awaiting_sync` / `failed`),
而我方的结算是**之后另一个事务**。崩在这条缝里留下:

    status='submitted' + settlement_status='frozen'      ← 钱永久冻着
    status='failed'    + settlement_status='frozen'      ← 同上
    task='ready' + settlement='committed' + revision NULL ← 钱已扣、作品永久 409

所以本文件每一条窗口判据都是**三段式**,缺一段就退化成没有判别力的绿灯:

  ① **窗口是真的**:真进程被杀之后,库里确实是那个组合;
  ② **老的收不到**:既有回收器跑一趟,`healed == 0`(证明这不是重复覆盖);
  ③ **新的收得到**:新收敛器跑一趟,item/task 收敛 **且钱包余额真的动了**,
     再跑一趟结果**不变**(幂等)。

🔴 `os._exit` 不能换成 `raise`:后者会被 worker 的 `except` 接住,
   测到的是异常处理,不是崩溃。本文件有一条判据专门自证这件事。
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import subprocess
import sys
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

OWNER_UID = 4343
PUBLISH_FEATURE = "media_proxy_publish"
PUBLISH_POINTS = 28080
PRODUCE_FEATURE = "geo_douyin_image_post"
PRODUCE_POINTS = 390


def _admin_dsn() -> str:
    return DSN.rsplit("/", 1)[0] + "/postgres"


@pytest.fixture(scope="module")
def live_db():
    if not DSN or not PROD_SCHEMA.is_file():
        pytest.skip("需要 TEST_DATABASE_URL 与生产 schema 夹具")
    name = "geoimg_r3win_" + uuid.uuid4().hex[:8]
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


def _row(dsn, sql, params):
    conn = _conn(dsn)
    try:
        c = conn.cursor()
        c.execute(sql, params)
        row = c.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _wallet(dsn):
    return _row(dsn, "SELECT paid_points, frozen_points FROM user_wallets WHERE user_id=%s",
                (OWNER_UID,))


@pytest.fixture()
def world(live_db):
    """能真正走完发布链的最小世界(资金走**真 `freeze_points`**,不手工造冻结行)。"""
    from middleware.billing import freeze_points

    conn = _conn(live_db)
    c = conn.cursor()
    c.execute("SET search_path = public")
    c.execute("INSERT INTO users (id, username, display_name, password_hash, email) "
              "VALUES (%s,%s,%s,'x',%s) ON CONFLICT (id) DO NOTHING",
              (OWNER_UID, "owner_r3", "owner_r3", "r3@example.com"))
    # 🔴 [第 7 棒] 先把**上一条用例留下的冻结**全部中性化,再重置钱包。
    #    本 fixture 每次都把 frozen_points 归零,而库是 module 级的 ——
    #    于是上一条用例那笔冻结行还在、钱包计数却已清零。后面任何一个收敛器
    #    把它 commit 掉,就会 `frozen_points 0 - 28080` 撞 CHECK 直接 500,
    #    而红的是**别的**用例(共享队列的分母污染,本仓已经踩过一次)。
    #    中性化用 `manual`:两个收敛器的谓词都只认 `frozen`,`manual` 等人工,
    #    不会被误当成"这一趟要处理的"。
    for _tbl in ("mhz_publish_order_items", "geo_douyin_post_tasks"):
        c.execute("UPDATE " + _tbl + " SET settlement_status='manual'"
                  " WHERE settlement_status='frozen'")
    #    冻结**行**本身也要一起收掉:只改业务表的 settlement_status 不够,
    #    `commit_freeze` 是按 freeze_id / task_ref 找 `status='frozen'` 那一行的。
    #    留着它,下一条用例的收敛器就会把它 commit 掉 —— 而钱包计数刚被下面那句
    #    重置成 0 ⇒ `frozen_points 0 - 390` 撞 CHECK,红的还是**别的**用例。
    c.execute("UPDATE point_freezes SET status='released', released_at=now()"
              " WHERE user_id=%s AND status='frozen'", (OWNER_UID,))
    c.execute("UPDATE customer_credit_freezes SET status='released', released_at=now()"
              " WHERE (customer_user_id=%s OR agent_user_id=%s) AND status='frozen'",
              (OWNER_UID, OWNER_UID))
    c.execute("INSERT INTO user_wallets (user_id, paid_points, bonus_points, frozen_points) "
              "VALUES (%s, 1000000, 0, 0) ON CONFLICT (user_id) DO UPDATE "
              "SET paid_points = 1000000, frozen_points = 0, bonus_points = 0", (OWNER_UID,))
    for code, pts in ((PUBLISH_FEATURE, PUBLISH_POINTS), (PRODUCE_FEATURE, PRODUCE_POINTS)):
        c.execute("INSERT INTO feature_pricing (feature_code, cost_points, feature_name) "
                  "VALUES (%s,%s,%s) ON CONFLICT (feature_code) DO UPDATE "
                  "SET cost_points = EXCLUDED.cost_points", (code, pts, code))
    c.execute("INSERT INTO brands (name, owner_user_id) VALUES (%s,%s) RETURNING id",
              ("R3品牌_" + uuid.uuid4().hex[:6], OWNER_UID))
    brand_id = int(c.fetchone()["id"])

    c.execute(
        "INSERT INTO geo_douyin_posts (brand_id, created_by, keyword, content_type, status,"
        " tenant_owner_user_id, payer_user_id, actor_user_id, source_mode,"
        " title, body_text, hashtags, cards, oss_keys, cover_oss_key)"
        " VALUES (%s,%s,'关键词R3','image_post','ready',%s,%s,%s,'contract',"
        " %s,%s,'[]'::jsonb,'[]'::jsonb,%s::jsonb,%s) RETURNING id",
        (brand_id, OWNER_UID, OWNER_UID, OWNER_UID, OWNER_UID,
         "post 标题", "post 正文", json.dumps(["post/1.png"]), "post/1.png"))
    post_id = int(c.fetchone()["id"])

    manifest = {"oss_keys": ["rev/1.png", "rev/2.png"], "card_count": 2}
    manifest_hash = ("c" * 64)
    c.execute(
        "INSERT INTO geo_douyin_post_revisions (geo_post_id, revision_no, created_by,"
        " operation_kind, title, body, hashtags, cards_snapshot, asset_manifest,"
        " manifest_hash, status) VALUES (%s,1,%s,'create',%s,%s,'[]'::jsonb,'[]'::jsonb,"
        " %s::jsonb,%s,'active') RETURNING post_revision_id",
        (post_id, OWNER_UID, "冻结版标题", "冻结版正文", json.dumps(manifest), manifest_hash))
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
        (post_id, revision_id, OWNER_UID, str(uuid.uuid4()), "d" * 64, manifest_hash,
         json.dumps(cards)))
    artifact_id = int(c.fetchone()["prepared_artifact_id"])
    conn.close()

    task_ref = "imgnote:" + uuid.uuid4().hex + ":1"
    handle = asyncio.run(freeze_points(OWNER_UID, PUBLISH_FEATURE, task_ref=task_ref,
                                       brand_id=brand_id, reason="R3 判据夹具冻结"))
    assert handle.get("freeze_id"), "夹具冻结没拿到 freeze_id —— 资金判据没有分母"

    conn = _conn(live_db)
    c = conn.cursor()
    c.execute("INSERT INTO mhz_publish_orders (user_id, article_id, article_title, status,"
              " total_items, total_cost_points) VALUES (%s,%s,%s,'pending',1,%s)"
              " RETURNING id", (OWNER_UID, -1, "R3 订单", PUBLISH_POINTS))
    order_id = int(c.fetchone()["id"])
    c.execute(
        "INSERT INTO mhz_publish_order_items (order_id, user_id, media_id, media_name,"
        " media_type, status, cost_points, source_geo_post_id, source_post_revision_id,"
        " prepared_artifact_id, manifest_hash, item_request_id, task_ref, billing_mode,"
        " settlement_authority, settlement_status, freeze_id, freeze_table, payer_user_id,"
        " reserved_amount, physical_split_snapshot, capacity_date, capacity_state)"
        " VALUES (%s,%s,9101,'夹具账号','svideo','queued',%s,%s,%s,%s,%s,%s,%s,"
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


# ═══════════════════════════════════════════════════════════════════
# ① 纯函数:崩溃窗口的**逐值**处置表(不需要真库,所以每个取值各打一发)
# ═══════════════════════════════════════════════════════════════════

def test_unsettled_classifier_is_value_closed_over_every_channel_state():
    """四格逐值 + 值域外**抛**。这张表是收敛器"不猜"的全部依据。"""
    from services.geo_douyin.contract_seams import (
        ImpossibleOutcome, classify_unsettled_publish_item,
    )

    delivered = classify_unsettled_publish_item(item_status="submitted",
                                                mhz_order_id="SN-9")
    assert (delivered.kind, delivered.settlement, delivered.capacity_state) == (
        "delivered", "committed", "consumed"), delivered

    # 🔴 单号为空的 submitted 是自相矛盾(写入点 fail-closed 过)⇒ 不许当成已接单
    no_sn = classify_unsettled_publish_item(item_status="submitted", mhz_order_id="")
    assert no_sn.settlement == "manual" and no_sn.item_status_override == "needs_action", no_sn

    unknown = classify_unsettled_publish_item(item_status="awaiting_sync", mhz_order_id="")
    assert unknown.settlement == "manual", ("回执不完整必须保持冻结:" + str(unknown))
    assert unknown.capacity_state == "reserved", unknown

    rejected = classify_unsettled_publish_item(item_status="failed", mhz_order_id="")
    assert (rejected.settlement, rejected.capacity_state) == ("released", "released"), rejected

    with pytest.raises(ImpossibleOutcome):
        classify_unsettled_publish_item(item_status="某个新态", mhz_order_id="")


# ═══════════════════════════════════════════════════════════════════
# ② 崩溃窗口 · 发布链(真进程被杀在**窗口正中**)
# ═══════════════════════════════════════════════════════════════════

_KILL_MID_WINDOW = r'''
import asyncio, os, sys
sys.path.insert(0, r"{repo}")
import db.connection as dbconn
dbconn.DATABASE_URL = r"{dsn}"
dbconn._pool = None

import api.meijiehezi_api as mapi


class _Result:
    def __init__(self, media_ids):
        self.success = True
        self.code = 200
        self.msg = "ok"
        self.selected_num = len(media_ids)
        # success_count=0 ⇒ 旧提交器走「明确全失败」分支(标 failed)。
        # 🔴 [第 5 棒 · P0-1] 「确定未接单」现在只能这么造 —— **不能**再用抛异常,
        #    异常已经按「结果未知」处置了(那正是 P0-1 修的东西)。
        self.success_count = ({success_count} if {success_count} >= 0
                              else len(media_ids))
        self.order_sn = "{order_sn}"
        self.raw_data = {{"fake": True}}
        self.order_sn_map = {{int(m): "{order_sn}" for m in media_ids}}


class _Channel:
    async def publish_short_video(self, **kwargs):
        return _Result(list(kwargs.get("media_ids") or []))


mapi._get_client = lambda: _Channel()

import services.geo_douyin.contract_worker as cw


async def _die_before_settlement(*a, **k):
    # 🔴 **窗口正中**:渠道已经返回、旧提交器已经用它自己的连接把终态 commit 了,
    #    我方的结算事务一个字节都还没写。`os._exit` 不跑 finally、不回滚 ——
    #    这正是容器被 OOM / 蓝绿切换掐掉时的形态。
    os._exit(9)


cw._settle_publish_item = _die_before_settlement
asyncio.run(cw.run_publish_submit_once(worker="w-midwindow"))
print("NOT_KILLED")
'''


def _clear_other_queued(dsn, keep_item_id):
    """分母:worker 领的是**最老的一条**。同模块前面的用例会留下 queued 项,
    不清干净的话被杀的那一趟处理的是别人的项,本条断言全打在空处。"""
    conn = _conn(dsn)
    conn.cursor().execute(
        "UPDATE mhz_publish_order_items SET status='cancelled'"
        " WHERE status='queued' AND billing_mode='freeze_per_item' AND id <> %s",
        (keep_item_id,))
    conn.close()


def _kill_mid_window(world, *, success_count=-1, order_sn="SN-KILL"):
    _clear_other_queued(world["dsn"], world["item_id"])
    script = _KILL_MID_WINDOW.format(repo=str(REPO), dsn=world["dsn"],
                                     order_sn=order_sn,
                                     success_count=int(success_count))
    proc = subprocess.run([sys.executable, "-c", script], cwd=str(REPO),
                          capture_output=True, text=True, errors="ignore", timeout=180)
    assert proc.returncode == 9, (
        "子进程没有按预期被杀(rc=" + str(proc.returncode) + ")—— 崩溃注入没生效,"
        "这条判据失去分母:\n" + (proc.stdout or "")[-400:] + (proc.stderr or "")[-900:])
    assert "NOT_KILLED" not in (proc.stdout or ""), proc.stdout[-300:]


def _age_the_lease(dsn, item_id):
    """把租约推到过期 + 超过宽限期。收敛器**故意**要求这一条:
    租约还在的时候插手,就会和一个还活着、正在结算的 worker 打架。"""
    conn = _conn(dsn)
    conn.cursor().execute(
        "UPDATE mhz_publish_order_items"
        "   SET lease_expires_at = now() - interval '2 days' WHERE id = %s", (item_id,))
    conn.close()


def test_crash_between_channel_return_and_settlement_leaves_a_stuck_frozen_item(world):
    """三段式 · 窗口 `submitted + frozen`。

    ① 窗口是真的;② 既有回收器**收不到**;③ 新收敛器收得到、钱真的动、且幂等。
    """
    from services.geo_douyin.contract_worker import (
        _reconcile_stuck_publish_items, reconcile_unsettled_publish_items,
    )

    before = _wallet(world["dsn"])
    _kill_mid_window(world)

    # ① 窗口是真的
    item = _row(world["dsn"], "SELECT status, settlement_status, mhz_order_id,"
                              " external_started_at FROM mhz_publish_order_items WHERE id=%s",
                (world["item_id"],))
    assert item["status"] == "submitted", (
        "旧提交器没有把终态提交上去 —— 那这条判据打的就不是本窗口:" + str(item))
    assert item["settlement_status"] == "frozen", (
        "结算居然写进去了?那就没有窗口可言:" + str(item))
    assert item["mhz_order_id"], "已提交却没有单号 —— 耐久证据不在,后面不能 commit"
    freeze = _row(world["dsn"], "SELECT status FROM point_freezes WHERE id=%s",
                  (world["freeze_id"],))
    assert freeze["status"] == "frozen", ("钱应该还冻着:" + str(freeze))

    _age_the_lease(world["dsn"], world["item_id"])

    # ② 既有回收器收不到(它的扫描集是 pending/submitting)
    healed_old = _reconcile_stuck_publish_items(grace_seconds=0)
    assert healed_old == 0, (
        "既有回收器居然收到了这一行 —— 那本条就是在重复覆盖已覆盖的窗口,没有判别力")
    still = _row(world["dsn"], "SELECT settlement_status FROM mhz_publish_order_items"
                               " WHERE id=%s", (world["item_id"],))
    assert still["settlement_status"] == "frozen", still

    # ③ 新收敛器收得到,而且**钱真的动了**
    counts = asyncio.run(reconcile_unsettled_publish_items(grace_seconds=0))
    assert counts["scanned"] == 1 and counts["committed"] == 1, counts
    healed = _row(world["dsn"], "SELECT status, settlement_status, capacity_state"
                                " FROM mhz_publish_order_items WHERE id=%s",
                  (world["item_id"],))
    assert healed["settlement_status"] == "committed", healed
    assert healed["capacity_state"] == "consumed", healed
    freeze = _row(world["dsn"], "SELECT status FROM point_freezes WHERE id=%s",
                  (world["freeze_id"],))
    assert freeze["status"] == "committed", ("库说结了但冻结行没动:" + str(freeze))
    after = _wallet(world["dsn"])
    assert int(after["frozen_points"]) == int(before["frozen_points"]) - PUBLISH_POINTS, (
        "冻结额没从钱包里出去 —— 只改了状态列,钱没动:" + str(before) + " -> " + str(after))

    # 幂等:再跑一趟,什么都不该发生
    again = asyncio.run(reconcile_unsettled_publish_items(grace_seconds=0))
    assert again["scanned"] == 0, ("收敛器不幂等,第二趟又领到了同一行:" + str(again))
    assert _wallet(world["dsn"]) == after, "第二趟把钱又动了一次"


def test_crash_after_channel_rejected_releases_the_money(world):
    """窗口 `failed + frozen`:渠道**明确未接单** ⇒ 确定零外部效果 ⇒ 必须退。

    🔴 老退款(`refund_for_publish_order`)对 `freeze_per_item` 已被隔离,
       所以这笔钱确实还冻着 —— 不退就是永久扣着用户的算力。
    """
    from services.geo_douyin.contract_worker import reconcile_unsettled_publish_items

    before = _wallet(world["dsn"])
    # 🔴 [第 5 棒 · P0-1 · 不变式搬家 ⇒ 改断言不退役] 不变式「确定未接单 ⇒ release」
    #    **没变**,变的是**怎么造出"确定未接单"**:上一版用抛异常造,而异常现在
    #    按「结果未知」处置(P0-1)。确定失败只能由渠道**明确 0 接单**产生。
    _kill_mid_window(world, success_count=0)

    item = _row(world["dsn"], "SELECT status, settlement_status FROM"
                              " mhz_publish_order_items WHERE id=%s", (world["item_id"],))
    assert item["status"] == "failed", ("渠道拒单后旧提交器该落 failed:" + str(item))
    assert item["settlement_status"] == "frozen", item

    _age_the_lease(world["dsn"], world["item_id"])
    counts = asyncio.run(reconcile_unsettled_publish_items(grace_seconds=0))
    assert counts["released"] == 1, counts
    freeze = _row(world["dsn"], "SELECT status FROM point_freezes WHERE id=%s",
                  (world["freeze_id"],))
    assert freeze["status"] == "released", freeze
    after = _wallet(world["dsn"])
    # 🔴 `before` 是**冻结之后**取的(夹具已经把钱冻上了),所以 release 之后
    #    可用余额要**多回来**一笔冻结额,而不是"等于崩溃前"。
    assert int(after["paid_points"]) == int(before["paid_points"]) + PUBLISH_POINTS, (
        "算力没有真的退回可用余额:" + str(before) + " -> " + str(after))
    assert int(after["frozen_points"]) == int(before["frozen_points"]) - PUBLISH_POINTS, after


def test_crash_with_incomplete_receipt_keeps_the_money_frozen(world):
    """窗口 `awaiting_sync + frozen`:结果**未知** ⇒ 不许猜成成功、也不许猜成退款。

    这一格是 WP3「不确定不 release」那条纪律**没有**反转的证明 ——
    确定性没变,处置就不许变。同一个收敛器里两条相反的处置并存才是对的。
    """
    from services.geo_douyin.contract_worker import reconcile_unsettled_publish_items

    before = _wallet(world["dsn"])
    _kill_mid_window(world, order_sn="")   # 单号为空 ⇒ 旧提交器落 awaiting_sync

    item = _row(world["dsn"], "SELECT status, settlement_status FROM"
                              " mhz_publish_order_items WHERE id=%s", (world["item_id"],))
    assert item["status"] == "awaiting_sync", item
    _age_the_lease(world["dsn"], world["item_id"])

    counts = asyncio.run(reconcile_unsettled_publish_items(grace_seconds=0))
    assert counts["manual"] == 1, counts
    freeze = _row(world["dsn"], "SELECT status FROM point_freezes WHERE id=%s",
                  (world["freeze_id"],))
    assert freeze["status"] == "frozen", ("结果未知却动了钱:" + str(freeze))
    assert _wallet(world["dsn"]) == before, "结果未知那一格不许碰钱包"
    healed = _row(world["dsn"], "SELECT settlement_status FROM mhz_publish_order_items"
                                " WHERE id=%s", (world["item_id"],))
    assert healed["settlement_status"] == "manual", healed


def test_raise_instead_of_exit_would_not_reproduce_the_crash(world):
    """判据自证:把 `os._exit` 换成 `raise`,注入就**不再是崩溃**。

    没有这一条,谁都可以把上面三条的注入方式悄悄换成 `raise` —— 那时候
    走的是 worker 的异常处理路径,测到的东西完全不同,而三条依旧"全绿"。
    """
    _clear_other_queued(world["dsn"], world["item_id"])
    script = _KILL_MID_WINDOW.format(repo=str(REPO), dsn=world["dsn"],
                                     order_sn="SN-RAISE", success_count=-1)
    script = script.replace("os._exit(9)", 'raise RuntimeError("not a crash")')
    proc = subprocess.run([sys.executable, "-c", script], cwd=str(REPO),
                          capture_output=True, text=True, errors="ignore", timeout=180)
    assert proc.returncode != 9, (
        "换成 raise 之后进程仍以 9 退出?那说明退出码不是崩溃注入给的,"
        "上面三条的分母是假的")


# ═══════════════════════════════════════════════════════════════════
# ③ 崩溃窗口 · 制作链(钱已结、成品版本没冻)
# ═══════════════════════════════════════════════════════════════════

def _seed_contract_task(dsn, brand_id, post_id):
    """一条**真冻结**的合同链制作任务,并把 post 的 revision 指针清空
    (模拟一篇刚开始生产、还没有成品版本的作品)。"""
    from middleware.billing import freeze_points

    task_ref = "imgnote:" + uuid.uuid4().hex + ":9"
    handle = asyncio.run(freeze_points(OWNER_UID, PRODUCE_FEATURE, task_ref=task_ref,
                                       brand_id=brand_id, reason="R3 制作链夹具"))
    conn = _conn(dsn)
    c = conn.cursor()
    # 🔴 分母:worker 领的是**最老的一条** queued。同模块前面的用例会留下任务,
    #    不清干净的话这一趟跑的是**别人的**任务,本条断言全打在空处
    #    (单跑绿、全量跑红 —— 本仓已经踩过一次)。
    #    🔴 [第 7 棒] `running` 也要一起清:`claim_next_task` 会**重领租约过期的
    #       running 行**,而收敛器判据留下的正是"租约推老的 running"。
    #       只清 queued 的话,下一条用例的 `run_production_once` 领到的是
    #       **上一条用例的**任务 —— 断言全打在空处(单跑绿、全量跑红,又一次)。
    c.execute("UPDATE geo_douyin_post_tasks SET status='cancelled'"
              " WHERE status IN ('queued','pending','running')"
              "   AND production_batch_id IS NOT NULL")
    # 🔴 [第 6 棒 · R6 P0-A] 夹具**禁止预置 `ready`**:真实崩溃态是 `generating`
    #    (成功路径 ③ `set_post_status(ready)` 还没跑到)。上一版夹具预置了 `ready`,
    #    等于替被测代码把活干了 —— 收敛器不收作品状态这件事就永远验不出来。
    c.execute("UPDATE geo_douyin_posts SET active_revision_id = NULL,"
              " status = 'generating',"
              " generation_epoch = generation_epoch + 1 WHERE id = %s", (post_id,))
    c.execute(
        "INSERT INTO geo_douyin_post_tasks (post_id, user_id, task_ref, status,"
        " production_batch_id, batch_item_ordinal, settlement_status,"
        " settlement_authority, payer_user_id, freeze_id, freeze_table,"
        " reserved_amount, physical_split_snapshot, billing_mode, progress_total)"
        " VALUES (%s,%s,%s,'queued',%s,1,'frozen','direct_freeze',%s,%s,%s,%s,"
        " %s::jsonb,'freeze_per_item',2) RETURNING id",
        (post_id, OWNER_UID, task_ref, str(uuid.uuid4()), OWNER_UID,
         int(handle["freeze_id"]), str(handle["freeze_table"]), PRODUCE_POINTS,
         json.dumps(handle.get("physical_split_snapshot") or {})))
    task_id = int(c.fetchone()["id"])
    c.execute("UPDATE geo_douyin_posts SET active_generation_task_id = %s WHERE id = %s",
              (task_id, post_id))
    conn.close()
    return {"task_id": task_id, "task_ref": task_ref,
            "freeze_id": int(handle["freeze_id"])}


_KILL_PRODUCTION_TAIL = r'''
import asyncio, os, sys
sys.path.insert(0, r"{repo}")
import db.connection as dbconn
dbconn.DATABASE_URL = r"{dsn}"
dbconn._pool = None

import services.geo_douyin.production_task as pt
from db import geo_douyin_db as ddb


class _Outcome:
    ok = True
    completing = False
    settlement_ok = True
    cards_done = 2
    cards_total = 2
    error = ""
    refunded = False
    post_id = 0
    task_id = 0


async def _fake_production(**kwargs):
    # 复刻真 `run_image_post_production` **自己那一段事务**:
    # 它在返回之前就已经 commit 了「资金 committed + 任务 ready + 作品 ready」。
    ct = kwargs.get("contract_task") or {{}}
    pt._set_contract_settlement(int(ct["task_id"]), "committed")
    ddb.update_task(int(ct["task_id"]), status="ready", stage="done", mark_finished=True)
    # 🔴 [第 6 棒 · R6 P0-A] **故意不跑** `set_post_status(post_id, "ready")`。
    #    真实成功路径的次序是 ①资金 → ②update_task → ③set_post_status,
    #    本条复现的正是崩在 ②③ 之间那一格:任务已终态、作品还停在 `generating`。
    #    上一版这里替被测代码把 ③ 跑了,于是"收敛器不收作品状态"永远验不出来
    #    —— 夹具替被测代码干活,判据就退化成恒绿(变异 Q1/Q2 实测存活)。
    return _Outcome()


pt.run_image_post_production = _fake_production

import services.geo_douyin.contract_worker as cw

# 🔴 窗口正中:生产那一段事务已提交,worker 的收尾事务(冻成品版本 + 推进槽位
#    + finish_task)一个字节都还没写。
cw._freeze_active_revision = lambda *a, **k: os._exit(9)
asyncio.run(cw.run_production_once(worker="w-tailwindow"))
print("NOT_KILLED")
'''


def test_crash_after_commit_before_revision_leaves_a_paid_but_unusable_post(world):
    """三段式 · 窗口 `ready + committed + revision NULL`。

    这一格的后果不是"少一行记录":素材准备入口**硬要求** `active_revision_id`,
    所以这一篇**永久 409**,而钱已经扣了。
    """
    from services.geo_douyin.contract_worker import (
        reconcile_unfinished_production_tails,
    )
    from services.geo_douyin.durable_worker import reconcile_stuck_external_tasks

    seeded = _seed_contract_task(world["dsn"], world["brand_id"], world["post_id"])
    script = _KILL_PRODUCTION_TAIL.format(repo=str(REPO), dsn=world["dsn"])
    proc = subprocess.run([sys.executable, "-c", script], cwd=str(REPO),
                          capture_output=True, text=True, errors="ignore", timeout=180)
    assert proc.returncode == 9, (
        "崩溃注入没生效(rc=" + str(proc.returncode) + "):\n"
        + (proc.stdout or "")[-400:] + (proc.stderr or "")[-900:])

    # ① 窗口是真的
    task = _row(world["dsn"], "SELECT status, settlement_status, lease_owner FROM"
                              " geo_douyin_post_tasks WHERE id=%s", (seeded["task_id"],))
    assert task["status"] == "ready" and task["settlement_status"] == "committed", task
    post = _row(world["dsn"], "SELECT active_revision_id, status FROM geo_douyin_posts"
                              " WHERE id=%s", (world["post_id"],))
    assert post["active_revision_id"] is None, (
        "成品版本居然冻出来了 —— 那就没有窗口可言:" + str(post))
    # 🔴 [第 6 棒 · R6 P0-A] 真实崩溃态里**作品自己**停在 `generating`
    #    (成功路径 ③ `set_post_status(ready)` 还没跑到)。
    #    上一版夹具把它预置成 `ready`,等于替被测代码把活干了 ——
    #    "收敛器不收作品状态"这件事就永远验不出来。
    assert post["status"] == "generating", (
        "夹具又把作品预置成终态了 —— 这条判据会退化成恒绿:" + str(post))

    # 🔴 与发布链同理:收敛器**故意**要求租约已过期 —— 活着的 worker 正在收尾时
    #    不许插手。所以这里必须把租约推老,否则测到的是"收敛器没资格动手",
    #    而不是"收敛器收不到"。
    conn = _conn(world["dsn"])
    conn.cursor().execute(
        "UPDATE geo_douyin_post_tasks SET lease_expires_at = now() - interval '2 days'"
        " WHERE id = %s", (seeded["task_id"],))
    conn.close()

    # ② 既有回收器收不到(它只扫 status='running')
    conn = _conn(world["dsn"])
    healed_old = reconcile_stuck_external_tasks(conn.cursor(), grace_seconds=0)
    conn.close()
    assert seeded["task_id"] not in healed_old, (
        "既有回收器收到了这一行 —— 本条退化成重复覆盖")

    # ③ 新收敛器把收尾补上
    healed = reconcile_unfinished_production_tails(grace_seconds=0)
    assert healed == 1, ("收尾没被补做:healed=" + str(healed))
    post = _row(world["dsn"], "SELECT active_revision_id, status FROM geo_douyin_posts"
                              " WHERE id=%s", (world["post_id"],))
    assert post["active_revision_id"], (
        "补做之后作品仍然没有成品版本 —— 素材准备入口对它还是永久 409")
    # 🔴 收敛必须在**同一个事务**里把作品状态也收回来,否则这一篇在用户眼里
    #    永远"还在做",而钱早就扣了。
    assert post["status"] == "ready", (
        "任务/成品版本都收了,作品状态还停在生成中 —— 用户看到的仍然是没做完:"
        + str(post))
    task = _row(world["dsn"], "SELECT lease_owner FROM geo_douyin_post_tasks WHERE id=%s",
                (seeded["task_id"],))
    assert task["lease_owner"] is None, ("死 worker 的租约没被放掉:" + str(task))

    # 幂等
    assert reconcile_unfinished_production_tails(grace_seconds=0) == 0, "收敛器不幂等"


# ═══════════════════════════════════════════════════════════════════
# ③b 收敛器**全对象原子**(第 7 棒 · Codex R7 P0-A)
# ═══════════════════════════════════════════════════════════════════
#
# 🔴 收敛器一次要写四个对象:task 终态 / 成品版本 / 作品状态 / 槽位。
#    claim 语句只锁住了 **task 行**(`FOR UPDATE SKIP LOCKED` 打在 task 子查询上),
#    另外三个挂在 post 上、**没被锁**。所以必须在动手之前把 post 也锁上、核身份,
#    任一不符 ⇒ **四对象零变更退出**。


def _seed_slot(dsn, brand_id, *, fulfillment="generating"):
    """一个真的合同槽位(事件 + 投影两张表都要),挂在 douyin_image_note 渠道上。

    🔴 槽位必须是**真**的:没有槽位时"槽位没被推进"这句断言的分母是零 ——
       零分母的判据和恒绿的判据一样废。
    """
    h = lambda: uuid.uuid4().hex + uuid.uuid4().hex[:32]
    conn = _conn(dsn)
    c = conn.cursor()
    c.execute("INSERT INTO quotes (brand_id, owner_user_id, status,"
              " article_plan_writing_mode, article_plan_enrolled_at)"
              " VALUES (%s,%s,'confirmed','image_note_contract', now()) RETURNING id",
              (brand_id, OWNER_UID))
    quote_id = int(c.fetchone()["id"])
    c.execute(
        "INSERT INTO geo_article_contract_revisions(revision_key, source_event_key,"
        " source_version, owner_user_id, brand_id, quote_id, authority_snapshot,"
        " authority_snapshot_hash, delivery_count, contract_version)"
        " VALUES (%s,%s,'v1',%s,%s,%s,'{}'::jsonb,%s,1,'c1') RETURNING id",
        (h(), h(), OWNER_UID, brand_id, quote_id, h()))
    revision_id = int(c.fetchone()["id"])
    c.execute(
        "INSERT INTO geo_article_plan_runs(run_key, contract_revision_id, owner_user_id,"
        " brand_id, quote_id, run_mode, compiler_version, input_snapshot,"
        " input_snapshot_hash, status)"
        " VALUES (%s,%s,%s,%s,%s,'shadow','c1','{}'::jsonb,%s,'completed') RETURNING id",
        (h(), revision_id, OWNER_UID, brand_id, quote_id, h()))
    run_id = int(c.fetchone()["id"])
    slot_key = str(uuid.uuid4())
    c.execute(
        "INSERT INTO geo_article_delivery_slot_events(event_key, delivery_slot_key,"
        " slot_version, event_kind, target_state, contract_revision_id, plan_run_id,"
        " owner_user_id, brand_id, quote_id, source_version, occurred_at)"
        " VALUES (%s,%s,1,'created','active',%s,%s,%s,%s,%s,'v1',now()) RETURNING id",
        (h(), slot_key, revision_id, run_id, OWNER_UID, brand_id, quote_id))
    event_id = int(c.fetchone()["id"])
    c.execute(
        "INSERT INTO geo_article_delivery_slots(delivery_slot_key, contract_revision_id,"
        " contract_ordinal, owner_user_id, brand_id, quote_id, current_state,"
        " projection_version, current_event_id, last_event_key, delivery_channel,"
        " fulfillment_state)"
        " VALUES (%s,%s,1,%s,%s,%s,'active',1,%s,%s,'douyin_image_note',%s)",
        (slot_key, revision_id, OWNER_UID, brand_id, quote_id, event_id, h(),
         fulfillment))
    conn.close()
    return slot_key


def _put_in_tail_window(dsn, task_id, post_id, *, task_status="running",
                        slot_key=None):
    """把库摆成「资金已结、收尾没写完」那一格 —— 也就是上一条用**真崩溃**
    复现出来的同一个状态(见 `test_crash_after_commit_before_revision…`)。

    这里直接摆状态而不再杀一次进程:本组判据打的是**收敛器的身份闸**,
    窗口本身可达这件事已经由那一条用真 `os._exit(9)` 证过了。
    """
    conn = _conn(dsn)
    c = conn.cursor()
    c.execute("UPDATE geo_douyin_post_tasks SET status=%s, settlement_status='committed',"
              " lease_owner='dead-worker', lease_expires_at = now() - interval '2 days'"
              " WHERE id=%s", (task_status, task_id))
    c.execute("UPDATE geo_douyin_posts SET status='generating', active_revision_id=NULL,"
              " active_generation_task_id=%s, delivery_slot_key=%s WHERE id=%s",
              (task_id, slot_key, post_id))
    conn.close()


def _tail_snapshot(dsn, task_id, post_id, slot_key):
    task = _row(dsn, "SELECT status, lease_owner, superseded_at, finished_at FROM"
                     " geo_douyin_post_tasks WHERE id=%s", (task_id,))
    post = _row(dsn, "SELECT status, active_revision_id FROM geo_douyin_posts"
                     " WHERE id=%s", (post_id,))
    slot = _row(dsn, "SELECT fulfillment_state, projection_version FROM"
                     " geo_article_delivery_slots WHERE delivery_slot_key=%s", (slot_key,))
    revs = _row(dsn, "SELECT count(*) AS n FROM geo_douyin_post_revisions"
                     " WHERE geo_post_id=%s", (post_id,))
    return {"task": task, "post": post, "slot": slot, "revisions": revs["n"]}


def test_a_failed_post_is_never_resurrected_by_the_converger(world):
    """🔴 **Codex R7 反例原样复现**:作品已 `failed` ⇒ 收敛后
    revision / task / slot / post **四个对象逐字段原样**。

    上一版收敛器只看 task 那几列(它们都在 claim 里判过、且 task 行已被锁),
    对 post **看都不看**就冻成品版本、推槽位、把 task 写成 ready ——
    一篇已经判失败的作品被**复活**成已完成。收敛器的职责是"补写没写完的那一笔",
    不是"改写已经定下的事实"。
    """
    from services.geo_douyin.contract_worker import (
        reconcile_unfinished_production_tails,
    )

    seeded = _seed_contract_task(world["dsn"], world["brand_id"], world["post_id"])
    slot_key = _seed_slot(world["dsn"], world["brand_id"])
    _put_in_tail_window(world["dsn"], seeded["task_id"], world["post_id"],
                        slot_key=slot_key)
    # 唯一的差别:这一篇已经被判失败。
    conn = _conn(world["dsn"])
    conn.cursor().execute("UPDATE geo_douyin_posts SET status='failed' WHERE id=%s",
                          (world["post_id"],))
    conn.close()

    before = _tail_snapshot(world["dsn"], seeded["task_id"], world["post_id"], slot_key)
    healed = reconcile_unfinished_production_tails(grace_seconds=0)
    after = _tail_snapshot(world["dsn"], seeded["task_id"], world["post_id"], slot_key)

    assert healed == 0, "已判失败的作品被收敛器当成'没做完'补做了"
    assert after == before, (
        "身份不符时必须**四对象零变更**,实际动了:\nbefore=" + str(before)
        + "\nafter =" + str(after))
    # 连 claim 写的租约都必须被 rollback 掉 —— "不符就不动",不是"只动一点点"。
    assert after["task"]["lease_owner"] == "dead-worker", (
        "零变更退出却把租约改了:" + str(after["task"]))
    alert = _row(world["dsn"],
                 "SELECT rule_key, severity FROM ai_ops_alerts"
                 " WHERE rule_key='geo_imgnote_tail_identity_mismatch'"
                 "   AND fingerprint=%s", ("task:%s" % seeded["task_id"],))
    assert alert, "零变更退出没留痕 —— 读侧分不清'跳过了'与'处理过了'"


def test_a_post_whose_generation_moved_on_is_never_healed_by_the_old_task(world):
    """身份的第二维:作品的**活动生成已易主**(新一代接管)⇒ 老任务零变更退出。

    这一格与上一格是两条**独立**的失败面:post 状态对(还在 generating)、
    但 `active_generation_task_id` 已经指向别人。上一版会拿**新一代的正文**
    冻出一个署着**老任务**的成品版本。
    """
    from services.geo_douyin.contract_worker import (
        reconcile_unfinished_production_tails,
    )

    seeded = _seed_contract_task(world["dsn"], world["brand_id"], world["post_id"])
    slot_key = _seed_slot(world["dsn"], world["brand_id"])
    _put_in_tail_window(world["dsn"], seeded["task_id"], world["post_id"],
                        slot_key=slot_key)
    conn = _conn(world["dsn"])
    conn.cursor().execute(
        "UPDATE geo_douyin_posts SET active_generation_task_id = %s WHERE id = %s",
        (int(seeded["task_id"]) + 100000, world["post_id"]))
    conn.close()

    before = _tail_snapshot(world["dsn"], seeded["task_id"], world["post_id"], slot_key)
    healed = reconcile_unfinished_production_tails(grace_seconds=0)
    after = _tail_snapshot(world["dsn"], seeded["task_id"], world["post_id"], slot_key)
    assert healed == 0 and after == before, (
        "生成已易主还被老任务收敛了:\nbefore=" + str(before) + "\nafter =" + str(after))


def test_when_the_revision_cannot_be_frozen_nothing_downstream_is_written(world):
    """🔴 成品版本冻不成 ⇒ **一律不许**继续写 task ready / 作品 ready / 推槽位。

    `_freeze_active_revision` 的 CAS 输了时会把这条 task 标成 `superseded`。
    上一版紧接着有一句**无条件** `UPDATE ... SET status='ready'` ——
    它把刚标好的 superseded **又改回了 ready**,一条已易主的任务在库里显示成功。

    这里让真依赖(`post_revisions.activate_revision`)抛它真实的
    `GenerationSuperseded`:被测代码一个字节没动,动的是它的下游。
    """
    from services.geo_douyin import contract_worker as cw
    from services.geo_douyin import post_revisions as pr

    seeded = _seed_contract_task(world["dsn"], world["brand_id"], world["post_id"])
    slot_key = _seed_slot(world["dsn"], world["brand_id"])
    _put_in_tail_window(world["dsn"], seeded["task_id"], world["post_id"],
                        slot_key=slot_key)

    real = pr.activate_revision

    def _lose_the_cas(*a, **k):
        raise pr.GenerationSuperseded("判据:活动生成已易主")

    pr.activate_revision = _lose_the_cas
    try:
        healed = cw.reconcile_unfinished_production_tails(grace_seconds=0)
    finally:
        pr.activate_revision = real

    after = _tail_snapshot(world["dsn"], seeded["task_id"], world["post_id"], slot_key)
    assert healed == 0, "成品版本没冻成,却按'补做成功'计数了"
    assert after["task"]["status"] == "superseded", (
        "superseded 被那句无条件的 SET status='ready' 改回去了:" + str(after["task"]))
    assert after["post"]["status"] == "generating", (
        "成品版本都没冻成,作品却被写成了终态:" + str(after["post"]))
    assert after["post"]["active_revision_id"] is None, str(after["post"])
    assert after["slot"]["fulfillment_state"] == "generating", (
        "成品版本没冻成却把槽位推进到了 ready —— 合同侧会把它算成已交付:"
        + str(after["slot"]))


def test_the_happy_path_does_advance_the_slot(world):
    """正向对照:身份齐备时槽位**确实**会被推到 ready。

    没有这一条,上面三条"槽位没动"就可能只是因为槽位**从来就推不动** ——
    那样它们全是零判别力的绿灯。
    """
    from services.geo_douyin.contract_worker import (
        reconcile_unfinished_production_tails,
    )

    seeded = _seed_contract_task(world["dsn"], world["brand_id"], world["post_id"])
    slot_key = _seed_slot(world["dsn"], world["brand_id"])
    _put_in_tail_window(world["dsn"], seeded["task_id"], world["post_id"],
                        slot_key=slot_key)

    assert reconcile_unfinished_production_tails(grace_seconds=0) == 1
    after = _tail_snapshot(world["dsn"], seeded["task_id"], world["post_id"], slot_key)
    assert after["task"]["status"] == "ready", str(after["task"])
    assert after["post"]["status"] == "ready", str(after["post"])
    assert after["post"]["active_revision_id"], str(after["post"])
    assert after["slot"]["fulfillment_state"] == "ready", (
        "槽位没被推进 —— 上面三条'槽位没动'因此没有判别力:" + str(after["slot"]))


def test_tail_identity_classifier_is_value_closed():
    """纯函数逐值:每一维**单独**不符都必须被拦下(而不是靠别的维度顺手兜住)。"""
    from services.geo_douyin.contract_seams import classify_production_tail_identity

    ok_row = {"post_status": "generating", "post_deleted_at": None,
              "active_revision_id": None, "active_generation_task_id": 7,
              "generation_epoch": 3}
    assert classify_production_tail_identity(task_id=7, row=ok_row).ok

    for key, bad in (("post_status", "failed"), ("post_status", "ready"),
                     ("post_deleted_at", "2026-08-19"),
                     ("active_revision_id", 42),
                     ("active_generation_task_id", 8)):
        row = dict(ok_row)
        row[key] = bad
        got = classify_production_tail_identity(task_id=7, row=row)
        assert not got.ok, "这一维单独不符却放行了:" + key + "=" + str(bad)
        assert got.reasons, "拦下了却说不出哪一维不符 —— 排障时等于没说"
    assert not classify_production_tail_identity(task_id=7, row=None).ok
    assert not classify_production_tail_identity(task_id=7, row={}).ok


# ═══════════════════════════════════════════════════════════════════
# ④ P0-B · 制作链的整组句柄 + 逐格判 `success=False`
# ═══════════════════════════════════════════════════════════════════

class _SpyOutcome:
    ok = True
    completing = False
    settlement_ok = True
    cards_done = 2
    cards_total = 2
    error = ""
    refunded = False


def test_production_settlement_receives_the_whole_persisted_handle(world, monkeypatch):
    """`commit_freeze` 必须收到 `user_id` + `freeze_table`,不是只有 freeze_id/task_ref。

    🔴 句柄该带哪几个键,锚的是**生产者**(`middleware/billing.py` 的真实签名),
       不是我自己拍的形状。
    """
    import services.geo_douyin.production_task as pt

    seeded = _seed_contract_task(world["dsn"], world["brand_id"], world["post_id"])
    seen = {}

    async def _spy_commit(**kwargs):
        seen.update(kwargs)
        return {"success": True}

    monkeypatch.setattr(pt, "commit_freeze", _spy_commit, raising=False)

    real = pt.run_image_post_production

    async def _thin_production(**kwargs):
        # 只跑资金那一段:直接调被 spy 掉的 commit_freeze,走的是真实的
        # `_settlement_handle` 组装路径。
        ct = kwargs.get("contract_task") or {}
        from middleware.billing import commit_freeze  # noqa: F401  (证明导入面还在)
        funds = await _spy_commit(
            reason="判据:整组句柄",
            **pt._settlement_handle(ct, freeze_id=ct.get("freeze_id"),
                                    task_ref=ct.get("task_ref")))
        out = _SpyOutcome()
        out.settlement_ok = bool(funds.get("success"))
        return out

    monkeypatch.setattr(pt, "run_image_post_production", _thin_production)
    from services.geo_douyin.contract_worker import run_production_once
    asyncio.run(run_production_once(worker="w-r3-handle"))
    assert real is not None

    assert seen.get("freeze_id") == seeded["freeze_id"], seen
    assert str(seen.get("task_ref") or "") == seeded["task_ref"], seen
    assert int(seen.get("user_id") or 0) == OWNER_UID, (
        "结算没收到 payer_user_id —— `_route_freeze_table` 只能去猜表,"
        "两张冻结表 id 撞号时会结算到别人那一笔:" + str(seen))
    assert str(seen.get("freeze_table") or "") == "legacy", (
        "结算没收到 freeze_table:" + str(seen))


def test_cross_table_id_collision_without_the_handle_must_fail_the_settlement(world):
    """反向对照(真撞号):同一个 `freeze_id` 在两张冻结表里各有一行 frozen。

    · **不传**整组句柄 ⇒ `_route_freeze_table` 判 `ambiguous` ⇒ `success=False`;
    · **传**整组句柄 ⇒ 直接按 `freeze_table` 路由 ⇒ 正常 commit。

    这一对是 P0-B 的判别力本体:没有它,"传了句柄"和"没传句柄"在判据里
    看不出任何差别(这正是上一版能全绿的原因)。
    """
    from middleware.billing import commit_freeze
    import services.geo_douyin.production_task as pt

    seeded = _seed_contract_task(world["dsn"], world["brand_id"], world["post_id"])
    conn = _conn(world["dsn"])
    # 撞号那一行属于**别的**客户,所以先把那两个用户建出来(FK 是真的,不绕)
    conn.cursor().execute(
        "INSERT INTO users (id, username, display_name, password_hash, email)"
        " VALUES (%s,%s,%s,'x',%s),(%s,%s,%s,'x',%s) ON CONFLICT (id) DO NOTHING",
        (OWNER_UID + 1, "other_cust", "other_cust", "oc@example.com",
         OWNER_UID + 2, "other_agent", "other_agent", "oa@example.com"))
    conn.cursor().execute(
        # 🔴 三池分解之和必须 = amount_total(chk_ccf_pool_sum)。
        #    夹具照真表的约束写,不绕过 —— 绕过的夹具证明不了真撞号。
        "INSERT INTO customer_credit_freezes (id, customer_user_id, agent_user_id,"
        " feature_code, amount_total, amount_tool, amount_publish, amount_bonus,"
        " status, task_ref) VALUES (%s,%s,%s,%s,%s,%s,0,0,'frozen',%s)",
        (seeded["freeze_id"], OWNER_UID + 1, OWNER_UID + 2, PRODUCE_FEATURE,
         PRODUCE_POINTS, PRODUCE_POINTS, "别人的:" + uuid.uuid4().hex))
    conn.close()

    # ① 老形态(只有 freeze_id + task_ref,且 user_id 缺席)⇒ 必须拒绝动钱
    blind = asyncio.run(commit_freeze(freeze_id=seeded["freeze_id"], reason="盲结算"))
    assert not blind.get("success"), (
        "跨表撞号却照样结算了 —— 这就是句柄不全的真实后果:" + str(blind))

    # ② 整组句柄 ⇒ 正常结算
    ct = {"task_id": seeded["task_id"], "task_ref": seeded["task_ref"],
          "freeze_id": seeded["freeze_id"], "freeze_table": "legacy",
          "payer_user_id": OWNER_UID}
    full = asyncio.run(commit_freeze(
        reason="整组句柄", **pt._settlement_handle(ct, freeze_id=seeded["freeze_id"],
                                                  task_ref=seeded["task_ref"])))
    assert full.get("success"), ("带齐句柄仍然结算失败:" + str(full))


def test_failed_settlement_never_becomes_ready(world, monkeypatch):
    """`commit_freeze` 返回 `success=False` ⇒ 任务 `needs_action` + `manual`,
    **不许**写 ready/committed,槽位也不许推进。"""
    import services.geo_douyin.production_task as pt

    seeded = _seed_contract_task(world["dsn"], world["brand_id"], world["post_id"])

    async def _thin_production(**kwargs):
        ct = kwargs.get("contract_task") or {}
        pt._set_contract_settlement(int(ct["task_id"]), "manual")
        out = _SpyOutcome()
        out.settlement_ok = False
        return out

    monkeypatch.setattr(pt, "run_image_post_production", _thin_production)
    from services.geo_douyin.contract_worker import run_production_once
    out = asyncio.run(run_production_once(worker="w-r3-badfunds"))
    assert out and out["status"] == "needs_action", (
        "资金没结清却把任务提升成了终态成功:" + str(out))

    task = _row(world["dsn"], "SELECT status, settlement_status, error_msg FROM"
                              " geo_douyin_post_tasks WHERE id=%s", (seeded["task_id"],))
    assert task["status"] == "needs_action", task
    assert task["settlement_status"] == "manual", task
    assert task["error_msg"], "非 ready 终态必须带原因"
    post = _row(world["dsn"], "SELECT active_revision_id FROM geo_douyin_posts WHERE id=%s",
                (world["post_id"],))
    assert post["active_revision_id"] is None, (
        "钱还没结清就冻出了成品版本 —— 这一篇会被当作可交付流进发布链")


def test_terminal_resolver_is_closed_over_the_settlement_dimension():
    """第三维的**逐值**表:默认路径(不传 settlement_ok)行为必须逐字节不变。"""
    from services.geo_douyin.contract_seams import resolve_production_terminal

    ok_default = resolve_production_terminal(ok=True, completing=False)
    assert (ok_default.status, ok_default.advance_slot_to_ready) == ("ready", True)
    ok_explicit = resolve_production_terminal(ok=True, completing=False, settlement_ok=True)
    assert ok_explicit == ok_default, "追加维度改变了没传它的老路 —— 那是改写不是追加"

    bad = resolve_production_terminal(ok=True, completing=False, settlement_ok=False)
    assert bad.status == "needs_action" and bad.advance_slot_to_ready is False, bad
    assert bad.settlement_expectation == "manual", bad

    part = resolve_production_terminal(ok=True, completing=True, settlement_ok=False)
    assert part.status == "needs_action", part


# ═══════════════════════════════════════════════════════════════════
# ⑤ 接线锁:收敛器必须**真的挂在调度里**,不是只存在于模块里
# ═══════════════════════════════════════════════════════════════════

def test_run_tick_actually_drives_the_new_convergers(world):
    """本包最容易重演的老病:函数写好了、判据直接调它,**调度器却没接**。

    所以这一条不调收敛器,它调 `run_tick` —— 只有真的挂上去了,
    崩溃窗口才会在生产里被收掉。
    (「死函数 = 复审漏接线」是本包被撤过一次 PASS 的根因。)
    """
    from services.geo_douyin.contract_worker import run_tick

    _kill_mid_window(world)
    _age_the_lease(world["dsn"], world["item_id"])
    item = _row(world["dsn"], "SELECT settlement_status FROM mhz_publish_order_items"
                              " WHERE id=%s", (world["item_id"],))
    assert item["settlement_status"] == "frozen", ("窗口没造出来:" + str(item))

    counts = asyncio.run(run_tick(per_tick=1))
    assert "reconciled_unsettled_publish" in counts, (
        "run_tick 的返回里根本没有这一项 —— 收敛器没接进调度:" + str(counts))
    healed = _row(world["dsn"], "SELECT settlement_status FROM mhz_publish_order_items"
                                " WHERE id=%s", (world["item_id"],))
    assert healed["settlement_status"] == "committed", (
        "run_tick 跑完了,窗口还冻着 —— 收敛器没被调度调用:" + str(healed))


def test_commit_settlement_reads_the_return_value_not_just_the_absence_of_an_exception(
        world):
    """P0-B 的**那一行**:`commit_freeze` 返回 `success=False` 时不许写 committed。

    🔴 这条判据是被**变异存活**逼出来的:第一版把这段逻辑留在几百行的成功分支里,
       判据够不到,于是把「看返回值」那一行改成 `_committed = True` 之后**全绿**。
       抽成函数之后判据才打得到出口本身。
    """
    import middleware.billing as billing
    import services.geo_douyin.production_task as pt

    seeded = _seed_contract_task(world["dsn"], world["brand_id"], world["post_id"])
    ct = {"task_id": seeded["task_id"], "task_ref": seeded["task_ref"],
          "freeze_id": seeded["freeze_id"], "freeze_table": "legacy",
          "payer_user_id": OWNER_UID, "status_ready": "ready"}

    real = billing.commit_freeze
    try:
        async def _refuse(**kwargs):
            return {"success": False, "reason": "找不到冻结记录"}

        billing.commit_freeze = _refuse
        ok = asyncio.run(pt.commit_contract_settlement(
            ct, freeze_id=seeded["freeze_id"], task_ref=seeded["task_ref"],
            task_id=seeded["task_id"]))
        assert ok is False, "结算失败却报成功"
        task = _row(world["dsn"], "SELECT settlement_status FROM geo_douyin_post_tasks"
                                  " WHERE id=%s", (seeded["task_id"],))
        assert task["settlement_status"] == "manual", (
            "钱没结成却把库写成了 committed —— 『库说结了、钱没动』:" + str(task))
        freeze = _row(world["dsn"], "SELECT status FROM point_freezes WHERE id=%s",
                      (seeded["freeze_id"],))
        assert freeze["status"] == "frozen", ("冻结行不该被动:" + str(freeze))
    finally:
        billing.commit_freeze = real

    # 正向:真结算成功 ⇒ committed(证明上面的红不是因为这条路本来就走不通)
    ok = asyncio.run(pt.commit_contract_settlement(
        ct, freeze_id=seeded["freeze_id"], task_ref=seeded["task_ref"],
        task_id=seeded["task_id"]))
    assert ok is True, "带齐句柄的真结算失败了"
    task = _row(world["dsn"], "SELECT settlement_status FROM geo_douyin_post_tasks"
                              " WHERE id=%s", (seeded["task_id"],))
    assert task["settlement_status"] == "committed", task
