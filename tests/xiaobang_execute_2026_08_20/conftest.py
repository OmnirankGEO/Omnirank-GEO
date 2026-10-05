"""WO-B ②③ 判据底座:**真 PG16 + 真生产 schema** 的一次性库。

## 为什么不复用 xiaobang_vnext 那套

那一套只跑 `migration_038`(两张协调层新表)。本包要打的是**执行段**:
claim → 审批门 → 资格/定价 → 冻结 → 建单,涉及 `mhz_short_video` /
`mhz_publish_orders` / `mhz_publish_order_items` / `publish_idempotency_keys` /
`user_wallets` / `feature_pricing` / `geo_douyin_*` 十几张**现役**表。
手写一份精简 schema 就是第二套表定义 —— 列一漏、类型一错,测试全绿而生产照样炸。

所以底座与 `tests/geo_image_note_2026_08_17` 同形:直接灌**生产 schema dump**
再叠迁移 034/035/036 + 038,并把 `db.connection` 指过去。

🔴 指过去的方式是改 `db.connection.DATABASE_URL` + 清 `_pool` ——
   那个模块在 import 时就把 URL 读进模块变量了,只改 `os.environ`
   对**已经 import 过**的进程没有任何作用(本仓踩过)。

🔴 库名含 `test`:双库安全栓。库名不含 test 会让别的包的 conftest 安全栓失效,
   而"没跑起来"和"跑了全过"在退出码上一模一样(2026-08-15 实测)。
"""
from __future__ import annotations

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
    REPO / "db" / "migration_038_xiaobang_intent_2026_08_18.sql",
    # 🔴 [窗G 段二①] 小榜 prepare 现在会读 defgeo_xiaobang_frozen_reasons
    #    (§9.6 只读冻结理由)。046 是五张**全新**表、零 ALTER、体内零 DML,
    #    所以叠在这份 2026-08-17 的生产 dump 上是安全的。
    REPO / "db" / "migration_046_defgeo_monitoring_lineage_2026_08_22.sql",
]
PROD_SCHEMA = pathlib.Path(os.getenv(
    "GEOIMG_PROD_SCHEMA_SQL", r"C:/AI-Test/.deploy_toolkit/_geoimg_prodschema_20260817.sql"))
DSN = os.getenv("TEST_DATABASE_URL")

OWNER_UID = 7788
PUBLISH_FEATURE = "media_proxy_publish"

#: 媒体账号的投放价(``mhz_short_video.our_price_points``)。
#: **这就是用户在确认屏上看到的那个数**:``publish_price_resolver`` 从这一列
#: (× markup)算出 ``final_price_points``,prepare 把它冻进 intent 的报价,
#: confirm 屏原样展示,execute 按它冻结。
PUBLISH_POINTS = 28080

#: 🔴 [R2] ``feature_pricing.media_proxy_publish.cost_points`` = **生产真值 0**。
#:
#: 这里原先写的是「三个数必须同一个:媒体价 / 目录价 / intent 报价」,
#: 并把目录价也拉平成 ``PUBLISH_POINTS``。**那个口径已撤除。**
#: 撤除的理由不是洁癖 —— 它是本仓记过的「夹具替被测代码干活 ⇒ 判据恒绿」的
#: 教科书形态:``media_proxy_publish`` 是**动态定价** SKU,生产目录价就是 0
#: (``db/wallet_db.py:377``;Review 2026-08-24 生产只读实证 = 0),价钱整个
#: 来自媒体目录。把目录价拉平成媒体价,等于让夹具替 ``_freeze`` 补上了它
#: **没有传**的那个 ``extra_cost``:``total_cost = 28080 + 0`` 侥幸非 0,
#: 冻结成功,于是「冻结额 == 预估额」全绿 —— 而生产上
#: ``total_cost = 0 + 0 = 0`` ⇒ free 短路 ⇒ 零句柄 ⇒ **HTTP 500**,
#: 「小榜可真执行、必收费」一次都没成立过。
#:
#: 现在夹具按生产形态种 0,让真链自己说话。
PUBLISH_CATALOG_POINTS = 0

#: 三个账号 = ③ 的资格翻转矩阵三档。
MEDIA_OK = 9101          # 有格
MEDIA_NO_IMAGE = 9102    # 无格(发不了图文)
MEDIA_FULL = 9103        # 过期/额度用完(今天发满)


def _admin_dsn() -> str:
    return DSN.rsplit("/", 1)[0] + "/postgres"


@pytest.fixture(scope="module")
def live_db():
    if not DSN or not PROD_SCHEMA.is_file():
        pytest.skip("需要 TEST_DATABASE_URL 与生产 schema 夹具")
    if "test" not in DSN.rsplit("/", 1)[-1].lower():
        raise RuntimeError("安全栓:测试库名必须含 test,实得 " + DSN)

    name = "xbexec_" + uuid.uuid4().hex[:8] + "_test"
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


def conn_for(dsn):
    conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    conn.autocommit = True
    return conn


def one(dsn, sql, params=()):
    conn = conn_for(dsn)
    try:
        c = conn.cursor()
        c.execute(sql, params)
        row = c.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def rows(dsn, sql, params=()):
    conn = conn_for(dsn)
    try:
        c = conn.cursor()
        c.execute(sql, params)
        return [dict(r) for r in (c.fetchall() or [])]
    finally:
        conn.close()


def wallet(dsn):
    return one(dsn, "SELECT paid_points, frozen_points FROM user_wallets WHERE user_id=%s",
               (OWNER_UID,))


@pytest.fixture()
def world(live_db):
    """每条判据一套干净世界:清掉上一条留下的订单/幂等/intent 行。

    🔴 清库不是洁癖:幂等端点会让下一条判据读到**上一条**留下的行,
       于是变异代码根本没被执行而判据全绿(本仓 2026-08-11 记过这个形态)。
    """
    conn = conn_for(live_db)
    c = conn.cursor()
    c.execute("SET search_path = public")
    for table in ("xiaobang_confirmation_receipts", "xiaobang_operation_intents",
                  "publish_idempotency_keys", "mhz_publish_order_items",
                  "mhz_publish_orders", "mhz_short_video_drafts",
                  "point_freezes", "point_transactions"):
        try:
            c.execute("DELETE FROM " + table)
        except Exception:  # noqa: BLE001
            conn.rollback()

    c.execute("INSERT INTO users (id, username, display_name, password_hash, email) "
              "VALUES (%s,%s,%s,'x',%s) ON CONFLICT (id) DO NOTHING",
              (OWNER_UID, "owner_xbexec", "owner_xbexec", "xbexec@example.com"))
    c.execute("INSERT INTO user_wallets (user_id, paid_points, bonus_points, frozen_points) "
              "VALUES (%s, 1000000, 0, 0) ON CONFLICT (user_id) DO UPDATE "
              "SET paid_points = 1000000, frozen_points = 0, bonus_points = 0", (OWNER_UID,))
    # 🔴 [R2] 目录价按**生产形态**种 0(见 PUBLISH_CATALOG_POINTS 的说明)。
    #    这一行仍然必须存在 —— ``freeze_points`` 查不到 feature_code 会
    #    直接 ValueError,那**恰好证明** freeze 真的被调用了(不是被 mock 掉)。
    c.execute("INSERT INTO feature_pricing (feature_code, cost_points, feature_name) "
              "VALUES (%s,%s,%s) ON CONFLICT (feature_code) DO UPDATE "
              "SET cost_points = EXCLUDED.cost_points",
              (PUBLISH_FEATURE, PUBLISH_CATALOG_POINTS, PUBLISH_FEATURE))

    # ── 三档账号(③ 的翻转矩阵)────────────────────────────────────────
    for media_id, can_tuwen in ((MEDIA_OK, 1), (MEDIA_NO_IMAGE, 0), (MEDIA_FULL, 1)):
        c.execute(
            "INSERT INTO mhz_short_video (id, media_name, platform, is_active, blacklist,"
            " can_tuwen, our_price_points, price) VALUES (%s,%s,'抖音',TRUE,0,%s,%s,100)"
            " ON CONFLICT (id) DO UPDATE SET can_tuwen = EXCLUDED.can_tuwen,"
            " is_active = TRUE, blacklist = 0, platform = '抖音',"
            " our_price_points = EXCLUDED.our_price_points",
            (media_id, "账号" + str(media_id), can_tuwen, PUBLISH_POINTS))

    c.execute("INSERT INTO brands (name, owner_user_id) VALUES (%s,%s) RETURNING id",
              ("XBEXEC品牌_" + uuid.uuid4().hex[:6], OWNER_UID))
    brand_id = int(c.fetchone()["id"])

    c.execute(
        "INSERT INTO geo_douyin_posts (brand_id, created_by, keyword, content_type, status,"
        " tenant_owner_user_id, payer_user_id, actor_user_id, source_mode,"
        " title, body_text, hashtags, cards, oss_keys, cover_oss_key)"
        " VALUES (%s,%s,'执行链关键词','image_post','ready',%s,%s,%s,'contract',"
        " %s,%s,'[]'::jsonb,'[]'::jsonb,%s::jsonb,%s) RETURNING id",
        (brand_id, OWNER_UID, OWNER_UID, OWNER_UID, OWNER_UID,
         "冻结版标题", "冻结版正文", json.dumps(["post/1.png"]), "post/1.png"))
    post_id = int(c.fetchone()["id"])

    manifest_hash = "a" * 64
    c.execute(
        "INSERT INTO geo_douyin_post_revisions (geo_post_id, revision_no, created_by,"
        " operation_kind, title, body, hashtags, cards_snapshot, asset_manifest,"
        " manifest_hash, status) VALUES (%s,1,%s,'create',%s,%s,'[]'::jsonb,'[]'::jsonb,"
        " %s::jsonb,%s,'active') RETURNING post_revision_id",
        (post_id, OWNER_UID, "冻结版标题", "冻结版正文",
         json.dumps({"card_count": 1}), manifest_hash))
    revision_id = int(c.fetchone()["post_revision_id"])
    c.execute("UPDATE geo_douyin_posts SET active_revision_id = %s WHERE id = %s",
              (revision_id, post_id))

    cards = [{"index": 0, "state": "ready", "url": "https://cdn.example.com/a.png"}]
    c.execute(
        "INSERT INTO geo_douyin_publish_artifacts (geo_post_id, post_revision_id,"
        " tenant_owner_user_id, request_id, request_hash, manifest_hash, state,"
        " card_statuses) VALUES (%s,%s,%s,%s,%s,%s,'ready',%s::jsonb)"
        " RETURNING prepared_artifact_id",
        (post_id, revision_id, OWNER_UID, str(uuid.uuid4()), "b" * 64, manifest_hash,
         json.dumps(cards)))
    artifact_id = int(c.fetchone()["prepared_artifact_id"])

    # MEDIA_FULL:把今天的额度用满(状态必须是**消耗档**才算数)
    limit = _daily_limit()
    c.execute("INSERT INTO mhz_publish_orders (user_id, article_id, article_title, status,"
              " total_items, total_cost_points) VALUES (%s,-1,'占额度','pending',1,0)"
              " RETURNING id", (OWNER_UID,))
    filler_order = int(c.fetchone()["id"])
    for _ in range(limit):
        c.execute(
            "INSERT INTO mhz_publish_order_items (order_id, user_id, media_id, media_name,"
            " media_type, status, cost_points, created_at)"
            " VALUES (%s,%s,%s,'','svideo','queued',0, now())",
            (filler_order, OWNER_UID, MEDIA_FULL))
    conn.close()

    return {
        "dsn": live_db, "brand_id": brand_id, "post_id": post_id,
        "revision_id": revision_id, "artifact_id": artifact_id,
    }


def _daily_limit() -> int:
    from services.geo_douyin.config import douyin_per_account_daily_limit

    return int(douyin_per_account_daily_limit())
