"""第 5 棒 · Codex R5 的资金守恒与对象身份(P0-1 / P0-2 / P0-4a)。

三条病一个形状:**把"我们不知道"或"客户端说的"当成了事实**。

  · **P0-1**:POST 之后的网络超时被旧提交器吞成 `{submitted:0, failed:n}`,
    新链读成"确定没接单"⇒ release。可供应商**可能已经接单** —— 退款 = 白送;
  · **P0-2**:资金 commit 与 `settlement_status` 落列分属两个事务,
    中间崩溃留下「账已扣、settlement 仍 frozen」 —— 比第 4 棒收的那个窗口**更早**,
    收敛器扫不到;
  · **P0-4a**:`/publish-result` 把**客户端自报的** order_id / item_ids 写进了
    作品的权威关联,还能把 managed 链已经写好的发布态盖成"待核实"。

判据形状照旧:**打真库、打钱包余额、每条正向配一条反向**。
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

OWNER_UID = 4545
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
    name = "geoimg_r5_" + uuid.uuid4().hex[:8]
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
    from middleware.billing import freeze_points

    conn = _conn(live_db)
    c = conn.cursor()
    c.execute("SET search_path = public")
    c.execute("INSERT INTO users (id, username, display_name, password_hash, email) "
              "VALUES (%s,%s,%s,'x',%s) ON CONFLICT (id) DO NOTHING",
              (OWNER_UID, "owner_r5", "owner_r5", "r5@example.com"))
    c.execute("INSERT INTO user_wallets (user_id, paid_points, bonus_points, frozen_points) "
              "VALUES (%s, 1000000, 0, 0) ON CONFLICT (user_id) DO UPDATE "
              "SET paid_points = 1000000, frozen_points = 0, bonus_points = 0", (OWNER_UID,))
    for code, pts in ((PUBLISH_FEATURE, PUBLISH_POINTS), (PRODUCE_FEATURE, PRODUCE_POINTS)):
        c.execute("INSERT INTO feature_pricing (feature_code, cost_points, feature_name) "
                  "VALUES (%s,%s,%s) ON CONFLICT (feature_code) DO UPDATE "
                  "SET cost_points = EXCLUDED.cost_points", (code, pts, code))
    c.execute("INSERT INTO brands (name, owner_user_id) VALUES (%s,%s) RETURNING id",
              ("R5品牌_" + uuid.uuid4().hex[:6], OWNER_UID))
    brand_id = int(c.fetchone()["id"])
    c.execute(
        "INSERT INTO geo_douyin_posts (brand_id, created_by, keyword, content_type, status,"
        " tenant_owner_user_id, payer_user_id, actor_user_id, source_mode,"
        " title, body_text, hashtags, cards, oss_keys, cover_oss_key)"
        " VALUES (%s,%s,'关键词R5','image_post','ready',%s,%s,%s,'contract',"
        " %s,%s,'[]'::jsonb,'[]'::jsonb,%s::jsonb,%s) RETURNING id",
        (brand_id, OWNER_UID, OWNER_UID, OWNER_UID, OWNER_UID,
         "post 标题", "post 正文", json.dumps(["post/1.png"]), "post/1.png"))
    post_id = int(c.fetchone()["id"])

    manifest = {"oss_keys": ["rev/1.png", "rev/2.png"], "card_count": 2}
    manifest_hash = ("e" * 64)
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
        (post_id, revision_id, OWNER_UID, str(uuid.uuid4()), "f" * 64, manifest_hash,
         json.dumps(cards)))
    artifact_id = int(c.fetchone()["prepared_artifact_id"])
    conn.close()

    task_ref = "imgnote:" + uuid.uuid4().hex + ":1"
    handle = asyncio.run(freeze_points(OWNER_UID, PUBLISH_FEATURE, task_ref=task_ref,
                                       brand_id=brand_id, reason="R5 夹具冻结"))
    conn = _conn(live_db)
    c = conn.cursor()
    c.execute("INSERT INTO mhz_publish_orders (user_id, article_id, article_title, status,"
              " total_items, total_cost_points) VALUES (%s,%s,%s,'pending',1,%s)"
              " RETURNING id", (OWNER_UID, -1, "R5 订单", PUBLISH_POINTS))
    order_id = int(c.fetchone()["id"])
    c.execute(
        "INSERT INTO mhz_publish_order_items (order_id, user_id, media_id, media_name,"
        " media_type, status, cost_points, source_geo_post_id, source_post_revision_id,"
        " prepared_artifact_id, manifest_hash, item_request_id, task_ref, billing_mode,"
        " settlement_authority, settlement_status, freeze_id, freeze_table, payer_user_id,"
        " reserved_amount, physical_split_snapshot, capacity_date, capacity_state)"
        " VALUES (%s,%s,9201,'夹具账号','svideo','queued',%s,%s,%s,%s,%s,%s,%s,"
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
            "freeze_id": int(handle["freeze_id"])}


def _isolate_receipts(dsn, item_id):
    """🔴 分母:回填是**全表扫描**型的,同模块前面用例留下的 item + 镜像行都会被扫到。
    不清干净,本条的计数断言就打在别人的行上(实测第一版正是这样:
    `contradictions` 里那个 1 来自上一条用例的 item)。"""
    conn = _conn(dsn)
    conn.cursor().execute(
        "UPDATE mhz_publish_order_items SET mhz_order_id = NULL"
        " WHERE billing_mode='freeze_per_item' AND id <> %s", (item_id,))
    conn.close()

def _only_this_item(dsn, item_id):
    conn = _conn(dsn)
    conn.cursor().execute(
        "UPDATE mhz_publish_order_items SET status='cancelled'"
        " WHERE status='queued' AND billing_mode='freeze_per_item' AND id <> %s",
        (item_id,))
    conn.close()


# ═══════════════════════════════════════════════════════════════════
# P0-1 · POST 之后的网络歧义**禁**降级为确定失败
# ═══════════════════════════════════════════════════════════════════

class _TimeoutChannel:
    """POST 已经发出去了,响应在回来的路上丢了。**远端可能已经接单**。"""

    def __init__(self):
        self.calls = 0

    async def publish_short_video(self, **kwargs):
        self.calls += 1
        import httpx

        raise httpx.ReadTimeout("response lost after POST")


def test_network_ambiguity_after_post_is_unknown_not_a_definite_failure(world, monkeypatch):
    """`httpx.ReadTimeout` ⇒ `unknown` + **保持 frozen** + `manual`,
    **不是** `rejected` + `released`。

    🔴 为什么这条是资金 P0:旧提交器的兜底 `except Exception` 把它吞成
       `{submitted:0, failed:n}`,新链读成"确定没接单"就 release。
       而 POST 已经发出去了 —— 供应商很可能**真的接了单**。
       退款 = 白送一次投放,而且 item 标 failed 之后再没人去查。

    🔴 断言必须落到**钱包余额**:只看 `settlement_status` 那一列,
       "退没退"这件事验不出来(列可以写对而钱照样动了,反之亦然)。
    """
    import api.meijiehezi_api as mapi
    from services.geo_douyin.contract_worker import run_publish_submit_once

    _only_this_item(world["dsn"], world["item_id"])
    fake = _TimeoutChannel()
    monkeypatch.setattr(mapi, "_get_client", lambda: fake)
    before = _wallet(world["dsn"])

    out = asyncio.run(run_publish_submit_once(worker="w-r5-timeout"))
    assert fake.calls == 1, ("渠道没被调用,这条判据没有分母:" + str(fake.calls))
    assert out is not None and out["kind"] == "unknown", (
        "POST 后超时被判成了别的语义(最危险的是 rejected ⇒ 退款):" + str(out))

    item = _row(world["dsn"], "SELECT status, settlement_status, capacity_state"
                              " FROM mhz_publish_order_items WHERE id=%s",
                (world["item_id"],))
    assert item["settlement_status"] == "manual", (
        "结果未知却给了确定的资金处置:" + str(item))
    assert item["capacity_state"] == "reserved", (
        "结果未知却把当天容量还了 —— 远端可能已经占了那个名额:" + str(item))
    assert item["status"] != "failed", (
        "结果未知却把 item 标成了确定失败:" + str(item))

    freeze = _row(world["dsn"], "SELECT status FROM point_freezes WHERE id=%s",
                  (world["freeze_id"],))
    assert freeze["status"] == "frozen", ("钱被动了 —— 结果未知不许动钱:" + str(freeze))
    assert _wallet(world["dsn"]) == before, (
        "钱包余额变了:" + str(before) + " -> " + str(_wallet(world["dsn"])))


def test_definite_rejection_still_releases(world, monkeypatch):
    """反向对照:渠道**明确 0 接单** ⇒ 仍然是 `rejected` + `released`。

    没有这一条,P0-1 的修法可能被写成"什么异常都当未知",
    于是**真正的确定失败**也不退款了 —— 那是另一个方向的资金错。
    """
    import api.meijiehezi_api as mapi
    from services.geo_douyin.contract_worker import run_publish_submit_once

    _only_this_item(world["dsn"], world["item_id"])

    class _R:
        def __init__(self, media_ids):
            self.success, self.code, self.msg = True, 200, "ok"
            self.selected_num = len(media_ids)
            self.success_count = 0          # 明确一条都没接
            self.order_sn = ""
            self.raw_data = {"fake": True}
            self.order_sn_map = {}

    class _C:
        async def publish_short_video(self, **kwargs):
            return _R(list(kwargs.get("media_ids") or []))

    monkeypatch.setattr(mapi, "_get_client", lambda: _C())
    out = asyncio.run(run_publish_submit_once(worker="w-r5-reject"))
    assert out["kind"] == "rejected", str(out)
    freeze = _row(world["dsn"], "SELECT status FROM point_freezes WHERE id=%s",
                  (world["freeze_id"],))
    assert freeze["status"] == "released", ("确定未接单必须退:" + str(freeze))


def test_legacy_default_still_swallows_the_exception(world):
    """默认路径(`raise_on_ambiguity=False`)**行为不变** —— 老链一个字节都没动。

    🔴 这条是"加可选限定条件用追加谓词、别改写默认路径"的字面判据:
       新参数只在新链传 True 时生效,老调用方拿到的仍是旧口径的返回值。
    """
    import inspect

    import api.meijiehezi_api as mapi

    sig = inspect.signature(mapi._submit_short_video_order)
    param = sig.parameters.get("raise_on_ambiguity")
    assert param is not None, "新参数不见了"
    assert param.default is False, (
        "默认值不是 False —— 那就是把老链的行为一起改了:" + str(param.default))
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
        "不是 keyword-only:位置参数会被既有调用方按位置误传:" + str(param.kind))


# ═══════════════════════════════════════════════════════════════════
# P0-2 · 资金 commit 与 settlement 列必须同一事务
# ═══════════════════════════════════════════════════════════════════

def _seed_task(dsn, brand_id, post_id):
    from middleware.billing import freeze_points

    task_ref = "imgnote:" + uuid.uuid4().hex + ":9"
    handle = asyncio.run(freeze_points(OWNER_UID, PRODUCE_FEATURE, task_ref=task_ref,
                                       brand_id=brand_id, reason="R5 制作链夹具"))
    conn = _conn(dsn)
    c = conn.cursor()
    c.execute("UPDATE geo_douyin_post_tasks SET status='cancelled'"
              " WHERE status IN ('queued','pending') AND production_batch_id IS NOT NULL")
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
    c.execute("UPDATE geo_douyin_posts SET active_generation_task_id=%s WHERE id=%s",
              (task_id, post_id))
    conn.close()
    return {"task_id": task_id, "task_ref": task_ref, "freeze_id": int(handle["freeze_id"])}


_KILL_BETWEEN_MONEY_AND_COLUMN = r'''
import asyncio, os, sys
sys.path.insert(0, r"{repo}")
import db.connection as dbconn
dbconn.DATABASE_URL = r"{dsn}"
dbconn._pool = None

import middleware.billing as billing
import services.geo_douyin.production_task as pt

_real = billing.commit_freeze


async def _commit_then_die(**kwargs):
    # 让**真的** commit_freeze 跑完(它在调用方事务里做完了扣款那几笔),
    # 然后在写 settlement 列**之前**硬杀自己 —— 这就是原来那条缝的正中。
    out = await _real(**kwargs)
    os._exit(9)


billing.commit_freeze = _commit_then_die
asyncio.run(pt.commit_contract_settlement(
    {{"task_id": {task_id}, "task_ref": "{task_ref}", "freeze_id": {freeze_id},
      "freeze_table": "legacy", "payer_user_id": {uid}}},
    freeze_id={freeze_id}, task_ref="{task_ref}", task_id={task_id}))
print("NOT_KILLED")
'''


def test_money_and_settlement_column_are_one_transaction(world):
    """在「钱扣了、settlement 列还没写」的**正中**杀掉进程 ⇒ 中间态**不可观测**。

    上一版这两件事分属两个事务(`commit_freeze` 自带连接先提交,
    `_set_contract_settlement` 另开连接再提交),崩在中间留下
    「账已扣、settlement 仍 frozen」—— 比第 4 棒收的那个窗口**更早**,
    而收敛器扫的是 `settlement='committed'`,**扫不到它**。

    原子化之后只剩两种可观测状态:两件事都发生,或两件事都没发生。
    """
    seeded = _seed_task(world["dsn"], world["brand_id"], world["post_id"])
    before = _wallet(world["dsn"])
    script = _KILL_BETWEEN_MONEY_AND_COLUMN.format(
        repo=str(REPO), dsn=world["dsn"], task_id=seeded["task_id"],
        task_ref=seeded["task_ref"], freeze_id=seeded["freeze_id"], uid=OWNER_UID)
    proc = subprocess.run([sys.executable, "-c", script], cwd=str(REPO),
                          capture_output=True, text=True, errors="ignore", timeout=180)
    assert proc.returncode == 9, (
        "崩溃注入没生效(rc=" + str(proc.returncode) + "):\n"
        + (proc.stdout or "")[-400:] + (proc.stderr or "")[-900:])

    freeze = _row(world["dsn"], "SELECT status FROM point_freezes WHERE id=%s",
                  (seeded["freeze_id"],))
    task = _row(world["dsn"], "SELECT settlement_status FROM geo_douyin_post_tasks"
                              " WHERE id=%s", (seeded["task_id"],))
    money_moved = str(freeze["status"]) != "frozen"
    column_written = str(task["settlement_status"]) == "committed"
    assert money_moved == column_written, (
        "出现了中间态:冻结=%s / settlement=%s —— 资金与列不在同一事务里"
        % (freeze["status"], task["settlement_status"]))
    # 事务整体回滚 ⇒ 钱包一分没动
    assert _wallet(world["dsn"]) == before, (
        "事务没整体回滚,钱包被改了:" + str(before) + " -> " + str(_wallet(world["dsn"])))


def test_converger_also_covers_the_running_plus_committed_window(world):
    """收尾收敛器必须盖住**更早**那一格:`running + committed`。

    资金那一段提交之后,任务终态是**再下一个**事务写的。崩在中间留下
    `running + committed + revision NULL` —— 上一版收敛器只扫 `ready`,
    这一条会永远停在 running,而它其实早就做完并结完账了。
    """
    from services.geo_douyin.contract_worker import reconcile_unfinished_production_tails

    seeded = _seed_task(world["dsn"], world["brand_id"], world["post_id"])
    conn = _conn(world["dsn"])
    conn.cursor().execute(
        "UPDATE geo_douyin_post_tasks"
        "   SET status='running', settlement_status='committed',"
        "       external_started_at = now(), lease_owner='dead-worker',"
        "       lease_expires_at = now() - interval '2 days' WHERE id=%s",
        (seeded["task_id"],))
    conn.close()

    healed = reconcile_unfinished_production_tails(grace_seconds=0)
    assert healed == 1, ("`running + committed` 这一格没被收:healed=" + str(healed))
    task = _row(world["dsn"], "SELECT status, lease_owner FROM geo_douyin_post_tasks"
                              " WHERE id=%s", (seeded["task_id"],))
    assert task["status"] == "ready", (
        "钱都结完了,终态却没补上 —— 这一条会永远显示「还在做」:" + str(task))
    assert task["lease_owner"] is None, task
    post = _row(world["dsn"], "SELECT active_revision_id FROM geo_douyin_posts WHERE id=%s",
                (world["post_id"],))
    assert post["active_revision_id"], "成品版本没补出来"


# ═══════════════════════════════════════════════════════════════════
# P0-4a · 自报 ID 禁写权威关联;禁降级既有发布态
# ═══════════════════════════════════════════════════════════════════

def _client():
    testclient = pytest.importorskip("fastapi.testclient")
    from fastapi import FastAPI, Request as FastAPIRequest

    import api.geo_douyin_api as gapi

    app = FastAPI()
    app.include_router(gapi.router)

    @app.middleware("http")
    async def _fake_auth(request: FastAPIRequest, call_next):
        request.state.user = {
            "user_id": OWNER_UID, "id": OWNER_UID, "username": "owner",
            "is_admin": False, "client_brand_ids": None,
            "permissions": ["writing:read", "writing:write"],
        }
        return await call_next(request)

    return testclient.TestClient(app, raise_server_exceptions=False)


def test_self_reported_ids_never_touch_the_canonical_linkage(world):
    """自报的 `order_id` / `item_ids` **一个都不许**进作品的权威关联。

    🔴 上一版注释写「order_id / item_ids 是服务端产物」,而代码传的是
       `req.order_id` / `req.item_ids` —— **注释与代码相反**。
       后果:拿别人的 order_id 就能把自己的作品挂到别人的投放单上,
       而"降级为线索"只降了状态那一半。
    """
    client = _client()
    resp = client.post("/api/geo-douyin/publish-result", json={
        "post_id": world["post_id"], "order_id": 999999, "item_ids": [123456],
        "publish_status": "published",
        "published_url": "https://www.douyin.com/note/伪造",
    })
    assert resp.status_code == 200, resp.text[:300]

    post = _row(world["dsn"], "SELECT publish_order_id, publish_item_ids, publish_status,"
                              " published_url FROM geo_douyin_posts WHERE id=%s",
                (world["post_id"],))
    assert post["publish_order_id"] is None, (
        "自报的 order_id 被写进了权威关联:" + str(post))
    assert not post["publish_item_ids"], (
        "自报的 item_ids 被写进了权威关联:" + str(post))
    assert not post["published_url"], ("自报的 URL 又被写进归因锚:" + str(post))
    assert post["publish_status"] == "self_reported_unverified", str(post)

    alert = _row(world["dsn"], "SELECT payload FROM ai_ops_alerts WHERE rule_key=%s",
                 ("geo_imgnote_self_reported_publish",))
    assert alert is not None, "线索没留下可查询的记录"
    assert int((alert["payload"] or {}).get("claimed_order_id") or 0) == 999999, (
        "自报的 ID 连在告警里都没留 —— 那是真丢了,不是降级:" + str(alert))


def test_self_report_cannot_downgrade_an_existing_publication(world):
    """managed 链已经写过发布态 ⇒ 自报**不许**把它盖成"待核实"。

    降级同样是"自报决定事实"的一种 —— 方向反过来而已:
    一条真发布可以被任何调用方一句自报打回未核实。
    """
    conn = _conn(world["dsn"])
    conn.cursor().execute(
        "UPDATE geo_douyin_posts SET publish_status='publishing', publish_order_id=%s"
        " WHERE id=%s", (world["order_id"], world["post_id"]))
    conn.close()

    client = _client()
    resp = client.post("/api/geo-douyin/publish-result", json={
        "post_id": world["post_id"], "order_id": None, "item_ids": [],
        "publish_status": "failed", "published_url": "",
    })
    assert resp.status_code == 200, resp.text[:300]
    assert resp.json().get("publish_status") == "publishing", (
        "响应把既有发布态报成了别的:" + resp.text[:300])

    post = _row(world["dsn"], "SELECT publish_status, publish_order_id FROM"
                              " geo_douyin_posts WHERE id=%s", (world["post_id"],))
    assert post["publish_status"] == "publishing", (
        "managed 链写好的发布态被一条自报盖掉了:" + str(post))
    assert int(post["publish_order_id"] or 0) == world["order_id"], (
        "权威关联被自报动了:" + str(post))


# ── 已退役（Review #161）· 规格 01 §10/§3.1/§5 该条款由 Owner #150 作废 ──────────
# test_frontend_does_not_count_a_lead_as_an_active_publication：错 self_reported_unverified 前端面已撤（0 命中）



# ═══════════════════════════════════════════════════════════════════
# P1 · 归因锚:provider 回执是唯一写入方
# ═══════════════════════════════════════════════════════════════════

def test_published_url_is_backfilled_from_the_provider_receipt(world):
    """供应商回执(`mhz_synced_orders.url`)⇒ 作品的 `published_url`。

    第 4 棒把客户端自报降级之后,这一列就**没有写入方**了 ——
    「终态只能由 managed 链落」必须真的有那一条,否则降级 = 删功能。
    """
    from services.geo_douyin.contract_worker import backfill_published_urls_from_provider

    _isolate_receipts(world["dsn"], world["item_id"])
    conn = _conn(world["dsn"])
    c = conn.cursor()
    # 🔴 [第 6 棒 · R6 P0-B] 权威前置态要齐:`submitted` + **钱已结清**。
    #    少了 settlement 那一半,回填就该按"还没结算"等着,而不是回填。
    c.execute("UPDATE mhz_publish_order_items SET mhz_order_id='SN-R5-OK',"
              " status='submitted', settlement_status='committed' WHERE id=%s",
              (world["item_id"],))
    c.execute("INSERT INTO mhz_synced_orders (id, order_sn, title, status, url,"
              " published_at) VALUES ('r5-ok','SN-R5-OK','标题',2,"
              " 'https://www.douyin.com/note/real', now())")
    conn.close()

    out = backfill_published_urls_from_provider()
    assert out["item_published"] == 1 and out["post_projected"] == 1, out
    # 🔴 次序:**canonical item 先变 published**,post 才是它的投影
    item = _row(world["dsn"], "SELECT status, publish_url, published_at FROM"
                              " mhz_publish_order_items WHERE id=%s", (world["item_id"],))
    assert item["status"] == "published", (
        "post 被回填了,而 canonical item 还不是 published —— 投影跑到了权威前面:"
        + str(item))
    assert item["publish_url"] == "https://www.douyin.com/note/real", str(item)
    post = _row(world["dsn"], "SELECT published_url, published_at, publish_status"
                              " FROM geo_douyin_posts WHERE id=%s", (world["post_id"],))
    assert post["published_url"] == "https://www.douyin.com/note/real", str(post)
    assert post["published_at"] is not None, str(post)
    assert post["publish_status"] == "published", str(post)
    # 幂等:再跑一趟不重复回填
    again = backfill_published_urls_from_provider()
    assert again["item_published"] == 0 and again["post_projected"] == 0, (
        "回填不幂等:" + str(again))


def test_backfill_refuses_non_terminal_provider_states(world):
    """反向对照:供应商侧**不是"已完成"**(或没有地址)⇒ 一个字都不许写。

    没有这一条,回填会把「待接单 / 发布中 / 已拒稿」一律写成"已发布",
    而那正是这条链最不该说的谎。
    """
    from services.geo_douyin.contract_worker import backfill_published_urls_from_provider

    _isolate_receipts(world["dsn"], world["item_id"])
    conn = _conn(world["dsn"])
    c = conn.cursor()
    c.execute("UPDATE mhz_publish_order_items SET mhz_order_id='SN-R5-PENDING',"
              " status='submitted', settlement_status='committed' WHERE id=%s",
              (world["item_id"],))
    # status=1 发布中 + 有地址;status=2 但地址为空 —— 两种都不许写
    c.execute("INSERT INTO mhz_synced_orders (id, order_sn, title, status, url)"
              " VALUES ('r5-p','SN-R5-PENDING','标题',1,'https://x/inflight')")
    conn.close()
    assert backfill_published_urls_from_provider()["post_projected"] == 0, (
        "把非终态写成了已发布")
    post = _row(world["dsn"], "SELECT published_url, publish_status FROM"
                              " geo_douyin_posts WHERE id=%s", (world["post_id"],))
    assert not post["published_url"], str(post)

    conn = _conn(world["dsn"])
    conn.cursor().execute("UPDATE mhz_synced_orders SET status=2, url='' WHERE id='r5-p'")
    conn.close()
    assert backfill_published_urls_from_provider()["post_projected"] == 0, (
        "空地址也被当成锚写了")


def test_run_tick_actually_drives_the_backfill(world):
    """接线锁:归因锚回填必须**真的挂在调度里**。

    🔴 这条是被变异 N12 逼出来的:上一版我只有"函数能回填"的判据,
       把它从 `run_tick` 里摘掉**全绿** —— 又是一个"函数写好了没人调"。
       本包被撤过一次 PASS 的根因就是这个,不能在同一个包里重演第三次。
    """
    from services.geo_douyin.contract_worker import run_tick

    _isolate_receipts(world["dsn"], world["item_id"])
    conn = _conn(world["dsn"])
    c = conn.cursor()
    c.execute("UPDATE mhz_publish_order_items SET mhz_order_id='SN-R5-TICK',"
              " status='submitted', settlement_status='committed' WHERE id=%s",
              (world["item_id"],))
    c.execute("INSERT INTO mhz_synced_orders (id, order_sn, title, status, url,"
              " published_at) VALUES ('r5-tick','SN-R5-TICK','标题',2,"
              " 'https://www.douyin.com/note/by-tick', now())")
    conn.close()

    counts = asyncio.run(run_tick(per_tick=1))
    assert "backfilled_urls" in counts, (
        "run_tick 返回里根本没有这一项 —— 回填没接进调度:" + str(counts))
    post = _row(world["dsn"], "SELECT published_url FROM geo_douyin_posts WHERE id=%s",
                (world["post_id"],))
    assert post["published_url"] == "https://www.douyin.com/note/by-tick", (
        "run_tick 跑完了,归因锚还是空的 —— 回填没被调度调用:" + str(post))


# ═══════════════════════════════════════════════════════════════════
# 第 6 棒 · R6 P0-B · 矛盾迟到回执:权威说失败 ⇒ post 一个字都不许改
# ═══════════════════════════════════════════════════════════════════

def test_contradicting_late_receipt_never_touches_the_post(world):
    """[Codex R6 的 PG16 反例原样复现] item 已 `failed + released`,
    而渠道镜像迟到同步成 `status=2 + 真链接` ⇒ **post 必须保持不变**。

    🔴 这是第 5 棒回填的真实缺陷:它 `mirror JOIN item → UPDATE post`,
       **跳过了 canonical item 终态**。于是一条迟到回执就能把作品写成
       "已发布 + 真链接",而资金侧刚刚告诉用户"没发出去,钱退了"。
       两边都言之凿凿,而且**资金那边是对的**(钱真的退了)。

    🔴 处置口径:**不猜哪边对**。两边都不动,落一条 critical 告警等人工。
    """
    from services.geo_douyin.contract_worker import backfill_published_urls_from_provider

    _isolate_receipts(world["dsn"], world["item_id"])
    conn = _conn(world["dsn"])
    c = conn.cursor()
    c.execute("UPDATE mhz_publish_order_items SET mhz_order_id='SN-R6-CONFLICT',"
              " status='failed', settlement_status='released' WHERE id=%s",
              (world["item_id"],))
    c.execute("INSERT INTO mhz_synced_orders (id, order_sn, title, status, url,"
              " published_at) VALUES ('r6-c','SN-R6-CONFLICT','标题',2,"
              " 'https://www.douyin.com/note/late', now())")
    conn.close()
    before = _row(world["dsn"], "SELECT status, publish_status, published_url,"
                                " published_at FROM geo_douyin_posts WHERE id=%s",
                  (world["post_id"],))

    out = backfill_published_urls_from_provider()
    assert out["contradictions"] == 1, ("矛盾没被识别出来:" + str(out))
    assert out["post_projected"] == 0 and out["item_published"] == 0, out

    after = _row(world["dsn"], "SELECT status, publish_status, published_url,"
                               " published_at FROM geo_douyin_posts WHERE id=%s",
                 (world["post_id"],))
    assert after == before, (
        "矛盾回执改动了作品 —— 资金说退款、作品说已发布:"
        + str(before) + " -> " + str(after))
    item = _row(world["dsn"], "SELECT status, settlement_status FROM"
                              " mhz_publish_order_items WHERE id=%s", (world["item_id"],))
    assert item["status"] == "failed" and item["settlement_status"] == "released", (
        "权威侧也被改了:" + str(item))

    alert = _row(world["dsn"], "SELECT severity, payload FROM ai_ops_alerts"
                               " WHERE rule_key=%s", ("geo_imgnote_receipt_contradiction",))
    assert alert is not None, "矛盾没留下任何可查询的记录 —— 那就等于没人会知道"
    assert alert["severity"] == "critical", str(alert)


def test_receipt_arriving_before_settlement_waits_instead_of_alerting(world):
    """回执比结算先到 = **正常时序**,不是矛盾:既不写,也不告警,记 `pending` 等下一轮。

    🔴 分不开"还没结算"和"真矛盾",告警面就会被日常噪声淹掉 ——
       那时候真矛盾来了也没人看。
    """
    from services.geo_douyin.contract_worker import backfill_published_urls_from_provider

    _isolate_receipts(world["dsn"], world["item_id"])
    conn = _conn(world["dsn"])
    c = conn.cursor()
    c.execute("UPDATE mhz_publish_order_items SET mhz_order_id='SN-R6-EARLY',"
              " status='submitted', settlement_status='frozen' WHERE id=%s",
              (world["item_id"],))
    c.execute("INSERT INTO mhz_synced_orders (id, order_sn, title, status, url)"
              " VALUES ('r6-e','SN-R6-EARLY','标题',2,'https://x/early')")
    conn.close()

    out = backfill_published_urls_from_provider()
    assert out["pending_settlement"] == 1, out
    assert out["contradictions"] == 0, ("把正常时序报成了矛盾:" + str(out))
    assert out["post_projected"] == 0, out
    item = _row(world["dsn"], "SELECT status FROM mhz_publish_order_items WHERE id=%s",
                (world["item_id"],))
    assert item["status"] == "submitted", ("钱还没结就把权威推成 published 了:" + str(item))


def test_projection_never_runs_ahead_of_the_canonical_writer(world):
    """**次序锁**(不是幂等锁):post 的回填只能发生在 item 权威变 published **之后**。

    🔴 通则:任何新 writer 写 canonical 邻接表,必须声明它与 canonical writer 的
       写入次序,判据要**打次序**。这里的打法是:把 CAS 那一步的前置态改成
       永不匹配 ⇒ 权威转不动 ⇒ post 也必须一个字不写。
       只打幂等的话,"投影跑在权威前面"这件事验不出来。
    """
    import services.geo_douyin.contract_worker as cw

    _isolate_receipts(world["dsn"], world["item_id"])
    conn = _conn(world["dsn"])
    c = conn.cursor()
    # 🔴 [第 6 棒 · 变异 Q6 逼出来的] item 上**故意留一个陈旧 publish_url**。
    #    没有它,投影那条 SQL 里 `i.status = 'published'` 这半句改坏了也不会红 ——
    #    因为另一半 `publish_url <> ''` 顺手把它兜住了。要验"投影读的是权威态",
    #    就必须造一个"有地址、但权威还不是 published"的行。
    c.execute("UPDATE mhz_publish_order_items SET mhz_order_id='SN-R6-ORDER',"
              " status='submitted', settlement_status='committed',"
              " publish_url='https://x/stale-not-authoritative' WHERE id=%s",
              (world["item_id"],))
    c.execute("INSERT INTO mhz_synced_orders (id, order_sn, title, status, url)"
              " VALUES ('r6-o','SN-R6-ORDER','标题',2,'https://x/ordered')")
    conn.close()

    real_from = cw.ITEM_PUBLISHABLE_FROM
    try:
        cw.ITEM_PUBLISHABLE_FROM = ("一个永不匹配的态",)
        out = cw.backfill_published_urls_from_provider()
    finally:
        cw.ITEM_PUBLISHABLE_FROM = real_from
    assert out["item_published"] == 0, out
    post = _row(world["dsn"], "SELECT published_url FROM geo_douyin_posts WHERE id=%s",
                (world["post_id"],))
    assert not post["published_url"], (
        "canonical item 没变 published,post 却被回填了 —— 投影跑在了权威前面:"
        + str(post))

    # 次序恢复 ⇒ 两段都发生(证明上面的空不是因为这条路本来就走不通)
    out2 = cw.backfill_published_urls_from_provider()
    assert out2["item_published"] == 1 and out2["post_projected"] == 1, out2


def test_converger_never_rewrites_a_post_that_is_not_generating(world):
    """[变异 Q2 逼出来的] 收敛器补的是"**没写完的那一笔**",不是重写事实。

    作品若已经是别的终态(这里用 `failed`),即便任务行说资金已结,
    收敛器也**不许**把它抬成 `ready` —— 那是拿一条陈旧的任务行去覆盖
    作品自己的事实。谓词必须窄到只认 `generating`。
    """
    from services.geo_douyin.contract_worker import reconcile_unfinished_production_tails

    seeded = _seed_task(world["dsn"], world["brand_id"], world["post_id"])
    conn = _conn(world["dsn"])
    c = conn.cursor()
    c.execute("UPDATE geo_douyin_post_tasks"
              "   SET status='running', settlement_status='committed',"
              "       external_started_at = now(), lease_owner='dead-worker',"
              "       lease_expires_at = now() - interval '2 days' WHERE id=%s",
              (seeded["task_id"],))
    c.execute("UPDATE geo_douyin_posts SET status='failed' WHERE id=%s",
              (world["post_id"],))
    conn.close()

    reconcile_unfinished_production_tails(grace_seconds=0)
    post = _row(world["dsn"], "SELECT status FROM geo_douyin_posts WHERE id=%s",
                (world["post_id"],))
    assert post["status"] == "failed", (
        "收敛器把一个已经失败的作品抬成了 ready —— 谓词太宽,它在重写事实:"
        + str(post))


# ═══════════════════════════════════════════════════════════════════
# (7) 回填必须走 **canonical 终态 writer**(第 7 棒 · Codex R7 P0-B)
# ═══════════════════════════════════════════════════════════════════
#
# 🔴 Review 追加意见:上一版把 item CAS 的双谓词拆开做变异,全量只红 1 发 ——
#    **失败面太单薄**。所以这一组给每一层守卫各配一条**独立可红**的判据:
#      ① canonical writer 真的被调到(订单头 / 快照 / 两个 outbox 都动了);
#      ② `expect_status_in` 单独生效;
#      ③ `expect_settlement` 单独生效;
#      ④ 不传谓词的**老调用方**行为不变(默认路径不许被顺手改掉);
#      ⑤ `cursor` 真的并进了调用方事务(它不许自己 commit);
#      ⑥ 图文链里**不许有第二个** published 写入者(结构锚 + 毒化自证)。


def _seed_linked_article(dsn, order_id):
    """给订单挂一篇**真文章** —— 发布快照那一层要求 `orders.article_id` 有效。

    🔴 不挂真文章的话,`capture_mhz_publication_snapshot_with_cursor` 会在第一步
       就 `article_link_missing` 返回,于是"快照生成了没有"这条断言的分母是零 ——
       零分母的绿灯什么都没证明。
    """
    h = lambda: uuid.uuid4().hex + uuid.uuid4().hex[:32]
    conn = _conn(dsn)
    c = conn.cursor()
    c.execute("SELECT brand_id FROM mhz_publish_order_items WHERE order_id=%s LIMIT 1",
              (order_id,))
    _r = c.fetchone()
    c.execute("SELECT id FROM brands ORDER BY id DESC LIMIT 1")
    brand_id = int(c.fetchone()["id"])
    # 🔴 交付 lineage sidecar 要求文章**挂在活动槽位上**(article.quote_id +
    #    delivery_slot_key ↔ geo_article_delivery_slots)。不搭这条链的话
    #    它会以 `article_not_sidecar_linked` 早退 —— "outbox 生成了没有"
    #    这条断言就永远是零分母的绿灯。
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
    contract_revision_id = int(c.fetchone()["id"])
    c.execute(
        "INSERT INTO geo_article_plan_runs(run_key, contract_revision_id, owner_user_id,"
        " brand_id, quote_id, run_mode, compiler_version, input_snapshot,"
        " input_snapshot_hash, status)"
        " VALUES (%s,%s,%s,%s,%s,'shadow','c1','{}'::jsonb,%s,'completed') RETURNING id",
        (h(), contract_revision_id, OWNER_UID, brand_id, quote_id, h()))
    run_id = int(c.fetchone()["id"])
    slot_key = str(uuid.uuid4())
    c.execute(
        "INSERT INTO geo_article_delivery_slot_events(event_key, delivery_slot_key,"
        " slot_version, event_kind, target_state, contract_revision_id, plan_run_id,"
        " owner_user_id, brand_id, quote_id, source_version, occurred_at)"
        " VALUES (%s,%s,1,'created','active',%s,%s,%s,%s,%s,'v1',now()) RETURNING id",
        (h(), slot_key, contract_revision_id, run_id, OWNER_UID, brand_id, quote_id))
    event_id = int(c.fetchone()["id"])
    c.execute(
        "INSERT INTO geo_article_delivery_slots(delivery_slot_key, contract_revision_id,"
        " contract_ordinal, owner_user_id, brand_id, quote_id, current_state,"
        " projection_version, current_event_id, last_event_key, delivery_channel,"
        " fulfillment_state)"
        " VALUES (%s,%s,1,%s,%s,%s,'active',1,%s,%s,'douyin_image_note','open')",
        (slot_key, contract_revision_id, OWNER_UID, brand_id, quote_id, event_id, h()))
    # `topics` 只有 id / generation_revision 是 NOT NULL(prod schema 实核),
    # 这里不猜列名,建一行最小的即可。
    c.execute("INSERT INTO topics DEFAULT VALUES RETURNING id")
    topic_id = int(c.fetchone()["id"])
    c.execute("INSERT INTO articles (topic_id, title, content, quote_id,"
              " delivery_slot_key) VALUES (%s,%s,%s,%s,%s) RETURNING id",
              (topic_id, "R7 回填文章", "R7 正文" * 20, quote_id, slot_key))
    article_id = int(c.fetchone()["id"])
    c.execute("UPDATE mhz_publish_orders SET article_id = %s, status='pending'"
              " WHERE id = %s", (article_id, order_id))
    conn.close()
    return article_id


def test_backfill_runs_the_whole_canonical_terminal_write(world, monkeypatch):
    """🔴 **Codex R7 反例原样复现**:回填之后订单头**不得**停在 `pending`,
    发布快照与两个 outbox **必须**都生成。

    上一版回填是一句自己写的 `UPDATE ... SET status='published'`。三列写对了,
    canonical writer 在同一笔里做的其余四件事**一件都没做**:
    订单头重算 / 发布快照 / 交付 lineage outbox / 通知 outbox。
    结果就是:item 说已发布、订单头说还没开始、用户收不到通知、
    血缘里查不到这次发布。**同一件事有两个写入者,第二个永远会漏掉第一个的动作。**
    """
    monkeypatch.setenv("ARTICLE_PLAN_EVENT_OUTBOX_ENABLED", "1")
    from services.geo_douyin.contract_worker import backfill_published_urls_from_provider

    _isolate_receipts(world["dsn"], world["item_id"])
    article_id = _seed_linked_article(world["dsn"], world["order_id"])
    conn = _conn(world["dsn"])
    c = conn.cursor()
    c.execute("UPDATE mhz_publish_order_items SET mhz_order_id='SN-R7-CANON',"
              " status='submitted', settlement_status='committed' WHERE id=%s",
              (world["item_id"],))
    c.execute("INSERT INTO mhz_synced_orders (id, order_sn, title, status, url,"
              " published_at) VALUES ('r7-canon','SN-R7-CANON','标题',2,"
              " 'https://www.douyin.com/note/canon', now())")
    conn.close()

    head_before = _row(world["dsn"], "SELECT status FROM mhz_publish_orders WHERE id=%s",
                       (world["order_id"],))
    assert head_before["status"] == "pending", (
        "订单头本来就不是 pending —— 这条判据没有分母:" + str(head_before))

    out = backfill_published_urls_from_provider()
    assert out["item_published"] == 1 and out["post_projected"] == 1, out

    head = _row(world["dsn"], "SELECT status FROM mhz_publish_orders WHERE id=%s",
                (world["order_id"],))
    assert head["status"] != "pending", (
        "回填之后订单头还停在 pending —— canonical writer 的订单头重算没跑:"
        + str(head))
    assert head["status"] == "completed", (
        "唯一一项已发布,订单头却不是 completed:" + str(head))

    art = _row(world["dsn"], "SELECT publication_snapshot_at, first_published_at,"
                             " publication_snapshot_source FROM articles WHERE id=%s",
               (article_id,))
    assert art["publication_snapshot_at"] is not None, (
        "发布快照没抓 —— 这次发布的正文事实没有被冻下来:" + str(art))
    assert art["first_published_at"] is not None, str(art)
    assert art["publication_snapshot_source"] == "mhz_publish_order_items", str(art)

    notif = _row(world["dsn"], "SELECT count(*) AS n FROM notification_outbox"
                               " WHERE business_id=%s AND event_type='publication.completed'",
                 ("item:%s" % world["item_id"],))
    assert notif["n"] >= 1, "通知 outbox 没入队 —— 用户根本收不到'已发布'"

    lineage = _row(world["dsn"],
                   "SELECT count(*) AS n FROM geo_article_plan_outbox"
                   " WHERE event_kind='publication_locked' AND source_id=%s",
                   ("mhz_publish_order_items:%s" % world["item_id"],))
    assert lineage["n"] >= 1, "交付 lineage outbox 没入队 —— 血缘里查不到这次发布"


def test_the_replay_cas_predicates_are_each_independently_effective(world):
    """CAS 的**两个谓词各自**都要能单独拦住 —— 而且不传时老行为不变。

    Review 点名:上一版拆双谓词做变异全量只红 1 发,说明失败面太薄。
    这一条把四种组合逐格打:错状态拦 / 错结算拦 / 都对放行 / 不传谓词照旧放行。
    """
    from db.meijiehezi_db import update_order_item_status

    item_id = int(world["item_id"])
    dsn = world["dsn"]

    def _set(status, settlement):
        conn = _conn(dsn)
        conn.cursor().execute(
            "UPDATE mhz_publish_order_items SET status=%s, settlement_status=%s,"
            " publish_url=NULL, published_at=NULL WHERE id=%s",
            (status, settlement, item_id))
        conn.close()

    # ① 状态谓词单独生效:结算对、状态不对 ⇒ 不动
    _set("failed", "committed")
    got = update_order_item_status(item_id, "published", publish_url="https://x/1",
                                   expect_status_in=("submitted", "awaiting_sync"))
    assert not got["changed"], "状态谓词没拦住 failed 的项"
    assert _row(dsn, "SELECT status, publish_url FROM mhz_publish_order_items"
                     " WHERE id=%s", (item_id,))["status"] == "failed"

    # ② 结算谓词单独生效:状态对、钱没结 ⇒ 不动
    _set("submitted", "frozen")
    got = update_order_item_status(item_id, "published", publish_url="https://x/2",
                                   expect_settlement="committed")
    assert not got["changed"], "结算谓词没拦住钱还冻着的项"
    assert _row(dsn, "SELECT status FROM mhz_publish_order_items"
                     " WHERE id=%s", (item_id,))["status"] == "submitted"

    # ③ 两个都满足 ⇒ 放行(否则上面两条可能只是"永远拦")
    _set("submitted", "committed")
    got = update_order_item_status(item_id, "published", publish_url="https://x/3",
                                   expect_status_in=("submitted", "awaiting_sync"),
                                   expect_settlement="committed")
    assert got["changed"], "两个谓词都满足却没放行 —— 上面两条因此没有判别力"
    assert _row(dsn, "SELECT publish_url FROM mhz_publish_order_items"
                     " WHERE id=%s", (item_id,))["publish_url"] == "https://x/3"

    # ④ 老调用方(一个谓词都不传)行为**不变**:任何状态都推得动
    _set("failed", "frozen")
    got = update_order_item_status(item_id, "published", publish_url="https://x/4")
    assert got["changed"], (
        "不传谓词的老调用方被顺手加上了 CAS —— '默认参数=旧行为不变'必须落到字节")


def test_the_canonical_writer_joins_the_callers_transaction(world):
    """`cursor=` 传进来时它**不许自己 commit** —— 否则"权威转移 + post 投影"
    就不是一笔,崩在中间会留下"item 已发布、post 没投影"的半截事实。"""
    from db.meijiehezi_db import update_order_item_status

    item_id = int(world["item_id"])
    conn = _conn(world["dsn"])
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("UPDATE mhz_publish_order_items SET status='submitted',"
                " settlement_status='committed', publish_url=NULL WHERE id=%s", (item_id,))
    got = update_order_item_status(item_id, "published", publish_url="https://x/tx",
                                   cursor=cur)
    assert got["changed"], "并入调用方事务时反而没写成:" + str(got)
    conn.rollback()
    conn.close()

    after = _row(world["dsn"], "SELECT status, publish_url FROM mhz_publish_order_items"
                               " WHERE id=%s", (item_id,))
    assert after["publish_url"] != "https://x/tx", (
        "调用方 rollback 了,写入却留下了 —— 它自己 commit 了,根本没并入事务:"
        + str(after))


def test_the_image_note_chain_has_exactly_one_published_writer():
    """结构锚:图文链里**不许有第二个** `status='published'` 的写入者。

    上一版那句自写 CAS 正是"第二个写入者" —— 它必然漏掉 canonical writer
    后来新增的动作,而且漏得无声。现在只许通过 `update_order_item_status`。
    """
    import pathlib
    import re

    repo = pathlib.Path(__file__).resolve().parents[2]
    pat = re.compile(r"(?is)UPDATE\s+mhz_publish_order_items.{0,400}?"
                     r"status\s*=\s*'published'")
    # 毒化自证:先证明这个锚**抓得住**真的第二写入者。
    assert pat.search("UPDATE mhz_publish_order_items\n   SET status = 'published',"
                      "\n       publish_url = %(url)s\n WHERE id = %(item_id)s"), (
        "结构锚是死的 —— 它连教科书式的第二写入者都抓不住")
    offenders, checked = [], 0
    for f in sorted((repo / "services" / "geo_douyin").rglob("*.py")):
        if "__pycache__" in str(f):
            continue
        checked += 1
        if pat.search(f.read_text(encoding="utf-8", errors="ignore")):
            offenders.append(f.name)
    assert checked >= 5, "分母太小,锁扫了个空目录"
    assert not offenders, (
        "图文链又出现了第二个 published 写入者(必须改调 update_order_item_status):"
        + str(offenders))


def _second_receipt_item(dsn, world, order_sn):
    """再造一条**同批**的回执项(第二篇作品 + 第二个 item + 第二条镜像)。

    🔴 "一条毒行不许拖垮整批"这句话,没有第二行就没有分母。
    """
    conn = _conn(dsn)
    c = conn.cursor()
    c.execute(
        "INSERT INTO geo_douyin_posts (brand_id, created_by, keyword, content_type,"
        " status, tenant_owner_user_id, payer_user_id, actor_user_id, source_mode,"
        " title, body_text, hashtags, cards, oss_keys, cover_oss_key)"
        " VALUES (%s,%s,'关键词R7二','image_post','ready',%s,%s,%s,'contract',"
        " %s,%s,'[]'::jsonb,'[]'::jsonb,'[]'::jsonb,%s) RETURNING id",
        (world["brand_id"], OWNER_UID, OWNER_UID, OWNER_UID, OWNER_UID,
         "第二篇", "第二篇正文", "post/2.png"))
    post2 = int(c.fetchone()["id"])
    conn.close()
    # 🔴 `ck_mhz_item_authority_shape`:`direct_freeze` 必须**整组句柄齐全**
    #    (freeze_id / freeze_table / payer_user_id / reserved_amount)。
    #    所以这里走**真 freeze_points**,不手工造一半的冻结行。
    from middleware.billing import freeze_points
    task_ref2 = "r7-second-" + uuid.uuid4().hex[:8]
    handle2 = asyncio.run(freeze_points(OWNER_UID, PUBLISH_FEATURE, task_ref=task_ref2,
                                        brand_id=world["brand_id"], reason="R7 第二行"))
    conn = _conn(dsn)
    c = conn.cursor()
    c.execute(
        "INSERT INTO mhz_publish_order_items (order_id, user_id, media_id, media_name,"
        " media_type, status, cost_points, source_geo_post_id, item_request_id,"
        " task_ref, billing_mode, settlement_authority, settlement_status,"
        " payer_user_id, freeze_id, freeze_table, reserved_amount,"
        " physical_split_snapshot, mhz_order_id, capacity_date, capacity_state)"
        " VALUES (%s,%s,9102,'夹具账号二','svideo','submitted',%s,%s,%s,%s,"
        " 'freeze_per_item','direct_freeze','committed',%s,%s,%s,%s,%s::jsonb,%s,"
        " CURRENT_DATE,'reserved') RETURNING id",
        (world["order_id"], OWNER_UID, PUBLISH_POINTS, post2, str(uuid.uuid4()),
         task_ref2, OWNER_UID, int(handle2["freeze_id"]),
         str(handle2["freeze_table"]), PUBLISH_POINTS,
         json.dumps(handle2.get("physical_split_snapshot") or {}), order_sn))
    item2 = int(c.fetchone()["id"])
    conn.close()
    return {"post_id": post2, "item_id": item2}


def test_one_poisoned_row_never_rolls_back_the_whole_batch(world, monkeypatch):
    """🔴 逐行 SAVEPOINT:一条行抛异常,**同批其余行照常写入**。

    改走 canonical writer 之后,单行的失败面比原来那句自写 UPDATE 大得多
    (订单头重算 / 发布快照 / 交付 lineage / 通知 outbox 都在同一笔里)。
    没有 SAVEPOINT 的话,一条毒行会把整批一起回滚,下一轮还撞同一条 ⇒
    回填**永久停摆且无声**。
    """
    from services.geo_douyin import contract_worker as cw
    import db.meijiehezi_db as mdb

    _isolate_receipts(world["dsn"], world["item_id"])
    second = _second_receipt_item(world["dsn"], world, "SN-R7-BATCH-B")
    conn = _conn(world["dsn"])
    c = conn.cursor()
    c.execute("UPDATE mhz_publish_order_items SET mhz_order_id='SN-R7-BATCH-A',"
              " status='submitted', settlement_status='committed' WHERE id=%s",
              (world["item_id"],))
    for sn, oid in (("SN-R7-BATCH-A", "r7-a"), ("SN-R7-BATCH-B", "r7-b")):
        c.execute("INSERT INTO mhz_synced_orders (id, order_sn, title, status, url,"
                  " published_at) VALUES (%s,%s,'标题',2,%s, now())",
                  (oid, sn, "https://www.douyin.com/note/" + oid))
    conn.close()

    real = mdb.update_order_item_status
    poisoned_id = int(world["item_id"])

    def _poison(item_id, status, **kw):
        if int(item_id) == poisoned_id:
            raise RuntimeError("判据注入:这一行的权威转移炸了")
        return real(item_id, status, **kw)

    monkeypatch.setattr(mdb, "update_order_item_status", _poison)
    out = cw.backfill_published_urls_from_provider()

    assert out["row_errors"] == 1, ("毒行没被单独记账:" + str(out))
    assert out["item_published"] == 1 and out["post_projected"] == 1, (
        "毒行把同批**另一行**也拖回滚了 —— 整批停摆:" + str(out))

    good = _row(world["dsn"], "SELECT status, publish_url FROM mhz_publish_order_items"
                              " WHERE id=%s", (second["item_id"],))
    assert good["status"] == "published", ("好行没写成:" + str(good))
    bad = _row(world["dsn"], "SELECT status FROM mhz_publish_order_items WHERE id=%s",
               (poisoned_id,))
    assert bad["status"] == "submitted", ("毒行居然写进去了:" + str(bad))
    alert = _row(world["dsn"], "SELECT severity FROM ai_ops_alerts"
                               " WHERE rule_key='geo_imgnote_backfill_row_failed'"
                               "   AND fingerprint=%s", ("publish_item:%s" % poisoned_id,))
    assert alert and alert["severity"] == "critical", (
        "毒行被静默吞掉了 —— 没人知道有一条永远回填不进来:" + str(alert))
