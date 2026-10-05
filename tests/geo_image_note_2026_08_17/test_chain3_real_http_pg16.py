"""返工链 3 · 判据制度 #1 的**第二级**:真 HTTP → 真路由 → 真 Pydantic → **真 PG16**。

## 为什么 `test_real_http_contract.py` 还不够

那一套证明的是「前端 payload 过得了 Pydantic」。它把 `!= 422` 当判据 ——
于是**库层**的合同断裂全部漏过去了:

  · `geo_douyin_post_tasks.request_id` / `.batch_item_request_id`  → **uuid**
  · `mhz_publish_order_items.item_request_id`                      → **uuid**
  · `geo_douyin_publish_artifacts.request_id`                      → **uuid**
  · `geo_article_delivery_slot_events.plan_run_id`                 → NOT NULL + **FK**
  · `geo_article_delivery_slot_events.contract_revision_id`        → NOT NULL + **FK**

前端发的是 `item-<slot>` / `pi-<id>` / `prep-<id>-<rev>`,handler 传的是
`plan_run_id=0` / `contract_revision_id or 0`。这些**全部**过得了 Pydantic,
然后在 INSERT 那一刻 InvalidTextRepresentation / ForeignKeyViolation ⇒ **500**。

一句话:**模型放行 ≠ 库放行**。契约有两道,`!= 422` 只验了第一道。

## 本文件的两条硬规矩

1. **不许 422,也不许 5xx**。主 CTA 打到真库,要么 2xx,要么是**业务**回答
   (409 价格漂移 / 403 权限 / 404 找不到)。5xx 一律算合同断裂;
2. **判别力自证**:每一条「必须通过」都配一条**反向**判据 —— 故意发一个
   非 UUID 的 id、故意发一个错的 slot 版本,必须被挡住。没有反向对照的
   「全绿」证明不了闸在工作(本包被撤 PASS 的根因就是这个)。
"""
from __future__ import annotations

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

OWNER_UID = 42
# [WO_271 · 2026-09-23] 原来这里的 FRONTEND_API 指向 imageNoteStudioApi.ts —— 它随 #204 a2 制作台退役删了
#   (df9c142c5),本文件也已零读取(读它的 parity 格随 #161 退役,见下方退役注),故删掉这一行。


def _admin_dsn() -> str:
    return DSN.rsplit("/", 1)[0] + "/postgres"


@pytest.fixture(scope="module")
def live_db():
    """一整套真 schema 的抛弃库,并把 `db.connection` 指过去。

    🔴 指过去的方式是改 `db.connection.DATABASE_URL` + 清 `_pool` ——
       那个模块在 import 时就把 URL 读进模块变量了,只改 `os.environ`
       对**已经 import 过**的进程没有任何作用(本仓踩过)。
    """
    if not DSN or not PROD_SCHEMA.is_file():
        pytest.skip("需要 TEST_DATABASE_URL 与生产 schema 夹具")
    name = "geoimg_chain3_" + uuid.uuid4().hex[:8]
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
    c.execute("INSERT INTO monitoring_product_platform_matrices (version, platforms) "
              "VALUES ('monitoring-unified5-v1', 'dashscope,deepseek,kimi,doubao') "
              "ON CONFLICT DO NOTHING")
    conn.close()

    import db.connection as dbconn
    old_url, old_pool = dbconn.DATABASE_URL, dbconn._pool
    dbconn.DATABASE_URL = dsn
    dbconn._pool = None
    old_flag = os.environ.get("GEO_IMAGE_NOTE_CONTRACT_ENABLED")
    os.environ["GEO_IMAGE_NOTE_CONTRACT_ENABLED"] = "true"
    try:
        yield dsn
    finally:
        try:
            dbconn.close_pool()
        except Exception:  # noqa: BLE001
            pass
        dbconn.DATABASE_URL, dbconn._pool = old_url, old_pool
        if old_flag is None:
            os.environ.pop("GEO_IMAGE_NOTE_CONTRACT_ENABLED", None)
        else:
            os.environ["GEO_IMAGE_NOTE_CONTRACT_ENABLED"] = old_flag
        admin = psycopg2.connect(_admin_dsn())
        admin.autocommit = True
        admin.cursor().execute('DROP DATABASE IF EXISTS "' + name + '" WITH (FORCE)')
        admin.close()


def _h64() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex[:32]


@pytest.fixture()
def seeded(live_db):
    """一个能真正走完制作链的最小世界:用户 + 钱包 + 品牌 + 已 enroll 报价 + 一个开放槽位。

    🔴 全部用**真表真列**种,不 mock 任何一层。夹具越像真的,判据越有判别力 ——
       上一轮被撤 PASS 的四条里有两条(手工 INSERT 非付款链 / mock 素材 ready)
       正是夹具太假造成的。
    """
    conn = psycopg2.connect(live_db, cursor_factory=RealDictCursor)
    conn.autocommit = True
    c = conn.cursor()
    c.execute("SET search_path = public")

    c.execute("INSERT INTO users (id, username, display_name, password_hash, email) "
              "VALUES (%s,%s,%s,'x',%s) ON CONFLICT (id) DO NOTHING",
              (OWNER_UID, "owner_" + str(OWNER_UID), "owner_" + str(OWNER_UID),
               "o" + str(OWNER_UID) + "@example.com"))
    c.execute("INSERT INTO user_wallets (user_id, paid_points, bonus_points, frozen_points) "
              "VALUES (%s, 1000000, 0, 0) ON CONFLICT (user_id) DO UPDATE "
              "SET paid_points = 1000000, frozen_points = 0", (OWNER_UID,))
    for code, points in (("geo_douyin_image_post", 390),
                         ("geo_douyin_image_post_extra_card", 40),
                         ("media_proxy_publish", 28080)):
        c.execute("INSERT INTO feature_pricing (feature_code, cost_points, feature_name) "
                  "VALUES (%s,%s,%s) ON CONFLICT (feature_code) DO UPDATE "
                  "SET cost_points = EXCLUDED.cost_points", (code, points, code))

    c.execute("INSERT INTO brands (name, owner_user_id) VALUES (%s,%s) RETURNING id",
              ("链3品牌_" + uuid.uuid4().hex[:8], OWNER_UID))
    brand_id = int(c.fetchone()["id"])
    # 🔴 报价必须**显式 enrolled**:四前置里的第 4 条。不 enroll 就应当拿到
    #    activation_pending(零 claim 零资金)—— 那是另一条判据打的。
    c.execute("INSERT INTO quotes (brand_id, owner_user_id, status, "
              " article_plan_writing_mode, article_plan_enrolled_at) "
              "VALUES (%s,%s,'confirmed','image_note_contract', now()) RETURNING id",
              (brand_id, OWNER_UID))
    quote_id = int(c.fetchone()["id"])
    c.execute(
        "INSERT INTO geo_article_contract_revisions(revision_key, source_event_key,"
        " source_version, owner_user_id, brand_id, quote_id, authority_snapshot,"
        " authority_snapshot_hash, delivery_count, contract_version) "
        "VALUES (%s,%s,'v1',%s,%s,%s,'{}'::jsonb,%s,1,'c1') RETURNING id",
        (_h64(), _h64(), OWNER_UID, brand_id, quote_id, _h64()))
    revision_id = int(c.fetchone()["id"])
    c.execute(
        "INSERT INTO geo_article_plan_runs(run_key, contract_revision_id, owner_user_id,"
        " brand_id, quote_id, run_mode, compiler_version, input_snapshot,"
        " input_snapshot_hash, status) "
        "VALUES (%s,%s,%s,%s,%s,'shadow','c1','{}'::jsonb,%s,'completed') RETURNING id",
        (_h64(), revision_id, OWNER_UID, brand_id, quote_id, _h64()))
    run_id = int(c.fetchone()["id"])

    slot_key = str(uuid.uuid4())
    c.execute(
        "INSERT INTO geo_article_delivery_slot_events(event_key, delivery_slot_key,"
        " slot_version, event_kind, target_state, contract_revision_id, plan_run_id,"
        " owner_user_id, brand_id, quote_id, source_version, occurred_at) "
        "VALUES (%s,%s,1,'created','active',%s,%s,%s,%s,%s,'v1',now()) RETURNING id",
        (_h64(), slot_key, revision_id, run_id, OWNER_UID, brand_id, quote_id))
    event_id = int(c.fetchone()["id"])
    c.execute(
        "INSERT INTO geo_article_delivery_slots(delivery_slot_key, contract_revision_id,"
        " contract_ordinal, owner_user_id, brand_id, quote_id, current_state,"
        " projection_version, current_event_id, last_event_key, delivery_channel,"
        " fulfillment_state) "
        "VALUES (%s,%s,1,%s,%s,%s,'active',1,%s,%s,'douyin_image_note','open')",
        (slot_key, revision_id, OWNER_UID, brand_id, quote_id, event_id, _h64()))
    conn.close()
    return {"brand_id": brand_id, "quote_id": quote_id, "revision_id": revision_id,
            "plan_run_id": run_id, "slot_key": slot_key}


@pytest.fixture()
def client(live_db):
    testclient = pytest.importorskip("fastapi.testclient")
    from fastapi import FastAPI, Request as FastAPIRequest

    from api.geo_image_note_api import router

    app = FastAPI()
    app.include_router(router)

    @app.middleware("http")
    async def _fake_auth(request: FastAPIRequest, call_next):
        request.state.user = {
            "user_id": OWNER_UID, "id": OWNER_UID, "username": "owner",
            "is_admin": False, "client_brand_ids": None,
            "permissions": ["writing:read", "writing:write"],
        }
        return await call_next(request)

    return testclient.TestClient(app, raise_server_exceptions=False)


def _assert_no_5xx(resp, what: str):
    """5xx = 合同断裂。业务拒绝(4xx)不算 —— 那是系统在**回答**,不是在崩。"""
    assert resp.status_code < 500, (
        what + " 打真库拿到 " + str(resp.status_code)
        + " —— 这是库层合同断裂,不是业务拒绝:\n" + str(resp.text)[:1500])
    assert resp.status_code != 422, (
        what + " 被 Pydantic 拒绝(前后端合同缝合面断裂):\n" + str(resp.text)[:1500])


def _stable_id(namespace: str, key: str) -> str:
    """`imageNoteApi.ts::stableRequestId` 的**同算法** Python 复刻。

    🔴 这里必须与 TS 那份逐位一致,否则判据测的是"我另写的一个 uuid 能不能进库",
       而不是"前端真的会发的那个 id 能不能进库"。下面 `test_stable_id_parity_*`
       用 TS 源码里的算法常量做交叉核对,防两份实现漂移。
    """
    data = namespace + "::" + key

    def _round(seed: int) -> str:
        h = seed & 0xFFFFFFFF
        for ch in data:
            h ^= ord(ch)
            h = (h * 0x01000193) & 0xFFFFFFFF
        return format(h, "08x")

    a, b = _round(0x811C9DC5), _round(0x9E3779B9)
    c, d = _round(0x85EBCA6B), _round(0xC2B2AE35)
    variant = "89ab"[int(c[0], 16) & 3]
    return a + "-" + b[0:4] + "-4" + b[5:8] + "-" + variant + c[1:4] + "-" + c[4:8] + d


def _frontend_item(slot_key: str, *, slot_version: int, fingerprint: str) -> dict:
    """按前端 `createBatch` 的**真实**形状拼一项(含确定性 uuid 派生)。"""
    return {
        "item_request_id": _stable_id("imgnote-item", slot_key),
        "delivery_slot_key": slot_key,
        "topic_ref": "tr-1",
        "expected_price_fingerprint": fingerprint,
        "expected_slot_version": slot_version,
        "settings": {"card_count": 4, "aspect_ratio": "3:4", "content_form": "ranking",
                     "style_key": "clean", "contact_enabled": False},
    }


# ---------------------------------------------------------------------------
# 0. 判别力自证 —— 先证明这套 harness 打得穿到库
# ---------------------------------------------------------------------------

def test_the_client_reaches_the_real_database(client, seeded):
    """没有这一条,后面所有「没 500」都可能只是因为请求根本没走到库。

    用一个**不存在**的 quote_id:它只有查过 `quotes` 表才答得出 404。
    """
    resp = client.post("/api/geo-douyin/batches", json={
        "request_id": str(uuid.uuid4()), "quote_id": 999_999_999,
        "expected_total_price_points": 390,
        "items": [_frontend_item(seeded["slot_key"], slot_version=1, fingerprint="x")]})
    assert resp.status_code == 404, (
        "不存在的报价没拿到 404 而是 " + str(resp.status_code) + " —— 请求没走到库,"
        "后面的判据全部失去判别力:\n" + str(resp.text)[:800])


# ── 已退役（Review #161）· 规格 01 §10/§3.1/§5 该条款由 Owner #150 作废 ──────────
# test_stable_id_parity_with_frontend_source：前提消失：新前端 lineFingerprint 不再自算，而是回显服务端 hit.price_fingerprint（服务端 sha256 单点）⇒ 跨语言复刻已不存在，无漂移可钉



# ---------------------------------------------------------------------------
# 1. 制作主 CTA:真库全链
# ---------------------------------------------------------------------------

def _preview(client, seeded):
    return client.post("/api/geo-douyin/production-preview", json={
        "quote_id": seeded["quote_id"],
        "contract_revision_id": seeded["revision_id"],
        "items": [{"item_request_id": _stable_id("imgnote-item", seeded["slot_key"]),
                   "delivery_slot_key": seeded["slot_key"], "topic_ref": "tr-1",
                   "settings": {"card_count": 4, "aspect_ratio": "3:4",
                                "content_form": "ranking", "style_key": "clean",
                                "contact_enabled": False}}]})


def test_production_preview_returns_server_authoritative_price(client, seeded):
    resp = _preview(client, seeded)
    _assert_no_5xx(resp, "制作预览")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_price_points"] == 390, body
    assert body["items"][0]["production_price_fingerprint"], body


def test_create_batch_full_chain_reaches_2xx_on_real_pg16(client, seeded):
    """🔴 返工的**总判据**:第一页主 CTA 打真库,必须 2xx。

    它一条就盖住五处库层断裂(三个 uuid 列 + 两个 NOT NULL FK)。
    在返工前,这条会拿到 500 —— 而 `!= 422` 那一版是绿的。
    """
    preview = _preview(client, seeded).json()
    fp = preview["items"][0]["production_price_fingerprint"]
    resp = client.post("/api/geo-douyin/batches", json={
        "request_id": str(uuid.uuid4()),
        "quote_id": seeded["quote_id"],
        "contract_revision_id": seeded["revision_id"],
        "expected_total_price_points": preview["total_price_points"],
        "items": [_frontend_item(seeded["slot_key"], slot_version=1, fingerprint=fp)]})
    _assert_no_5xx(resp, "制作提交")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "accepted", body
    assert body["items"] and body["items"][0]["geo_post_id"], body


def test_create_batch_actually_freezes_the_recomputed_price(client, seeded, live_db):
    """资金闭环的**入口**证据:冻结额 = 服务端重算价,句柄整组落库。"""
    preview = _preview(client, seeded).json()
    fp = preview["items"][0]["production_price_fingerprint"]
    resp = client.post("/api/geo-douyin/batches", json={
        "request_id": str(uuid.uuid4()), "quote_id": seeded["quote_id"],
        "contract_revision_id": seeded["revision_id"],
        "expected_total_price_points": preview["total_price_points"],
        "items": [_frontend_item(seeded["slot_key"], slot_version=1, fingerprint=fp)]})
    assert resp.status_code == 200, resp.text
    conn = psycopg2.connect(live_db, cursor_factory=RealDictCursor)
    c = conn.cursor()
    c.execute("SELECT status, settlement_status, settlement_authority, freeze_id,"
              " freeze_table, reserved_amount, physical_split_snapshot"
              "  FROM geo_douyin_post_tasks ORDER BY id DESC LIMIT 1")
    task = dict(c.fetchone())
    conn.close()
    assert task["settlement_status"] == "frozen", task
    assert task["settlement_authority"] == "direct_freeze", task
    assert task["freeze_id"] and task["freeze_table"], task
    assert int(task["reserved_amount"]) == 390, task


# ---------------------------------------------------------------------------
# 2. 反向对照 —— 证明闸真的在挡
# ---------------------------------------------------------------------------

def test_non_uuid_item_request_id_is_not_silently_accepted(client, seeded):
    """🔴 反向:发回**旧形态**的 `item-<slot>`。

    它不许变成 500(那正是返工前的病),也不许被当成合法 uuid 存进去。
    换句话说:合同必须在**入口**就把它认出来,而不是让库去抛。
    """
    preview = _preview(client, seeded).json()
    fp = preview["items"][0]["production_price_fingerprint"]
    item = _frontend_item(seeded["slot_key"], slot_version=1, fingerprint=fp)
    item["item_request_id"] = "item-" + seeded["slot_key"]      # 返工前的形态
    resp = client.post("/api/geo-douyin/batches", json={
        "request_id": str(uuid.uuid4()), "quote_id": seeded["quote_id"],
        "contract_revision_id": seeded["revision_id"],
        "expected_total_price_points": preview["total_price_points"],
        "items": [item]})
    assert resp.status_code < 500, (
        "非 uuid 的 item_request_id 打穿到库层 500 —— 入口没有守住形态:\n"
        + str(resp.text)[:800])
    assert resp.status_code in (400, 422), (
        "非 uuid 的 item_request_id 被放行(HTTP " + str(resp.status_code) + ")—— "
        "闸不存在,前面那条『2xx』就只是碰巧:\n" + str(resp.text)[:800])


def test_stale_slot_version_is_rejected_with_zero_side_effect(client, seeded, live_db):
    """🔴 反向:槽位乐观锁。报一个过期版本必须 409,且**零创建零冻结**。"""
    preview = _preview(client, seeded).json()
    fp = preview["items"][0]["production_price_fingerprint"]
    conn = psycopg2.connect(live_db, cursor_factory=RealDictCursor)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) AS n FROM geo_douyin_post_tasks")
    before = int(dict(c.fetchone())["n"])
    conn.close()

    resp = client.post("/api/geo-douyin/batches", json={
        "request_id": str(uuid.uuid4()), "quote_id": seeded["quote_id"],
        "contract_revision_id": seeded["revision_id"],
        "expected_total_price_points": preview["total_price_points"],
        # 槽位真实版本是 1;报 7 = "我看到的是第 7 版",服务端必须说不
        "items": [_frontend_item(seeded["slot_key"], slot_version=7, fingerprint=fp)]})
    assert resp.status_code == 409, resp.text

    conn = psycopg2.connect(live_db, cursor_factory=RealDictCursor)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) AS n FROM geo_douyin_post_tasks")
    after = int(dict(c.fetchone())["n"])
    conn.close()
    assert after == before, "CAS 被拒却仍然创建了任务 —— 『整项零创建』不成立"


def test_slot_scope_mismatch_is_rejected(client, seeded):
    """🔴 反向:P0-08 的越权面。声明的合同修订与槽位所属不一致必须被拒。"""
    preview = _preview(client, seeded).json()
    fp = preview["items"][0]["production_price_fingerprint"]
    resp = client.post("/api/geo-douyin/batches", json={
        "request_id": str(uuid.uuid4()),
        "quote_id": seeded["quote_id"],
        "contract_revision_id": int(seeded["revision_id"]) + 10_000,
        "expected_total_price_points": preview["total_price_points"],
        "items": [_frontend_item(seeded["slot_key"], slot_version=1, fingerprint=fp)]})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "SLOT_SCOPE_MISMATCH", resp.text


# ---------------------------------------------------------------------------
# 3. 真 HTTP **回放**判据(第 3 棒 · Review 附加项)
# ---------------------------------------------------------------------------

def _counts(dsn) -> dict:
    conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    try:
        c = conn.cursor()
        out = {}
        for name, sql in (
            ("tasks", "SELECT count(*) AS n FROM geo_douyin_post_tasks"),
            ("posts", "SELECT count(*) AS n FROM geo_douyin_posts"),
            ("batches", "SELECT count(*) AS n FROM geo_douyin_production_batches"),
            ("freezes", "SELECT count(*) AS n FROM point_freezes"),
        ):
            c.execute(sql)
            out[name] = int(dict(c.fetchone())["n"])
        return out
    finally:
        conn.close()


def test_same_request_id_replays_the_same_nonempty_batch_with_zero_new_rows(
        client, seeded, live_db):
    """同 `request_id` 二发 ⇒ 同一个**非空** batch_id + 四张表零新增。

    🔴 两半缺一不可:
      · 只验"零新增"→ 回放返回一个空 batch_id 也算过(前端拿不到批次,
        表现是提交完跳不过去);
      · 只验"同一个 id"→ 空 == 空 也成立。
    所以断言是「**非空**且相同」,并且四张表逐张比计数。
    """
    preview = _preview(client, seeded).json()
    fp = preview["items"][0]["production_price_fingerprint"]
    payload = {
        "request_id": str(uuid.uuid4()), "quote_id": seeded["quote_id"],
        "contract_revision_id": seeded["revision_id"],
        "expected_total_price_points": preview["total_price_points"],
        "items": [_frontend_item(seeded["slot_key"], slot_version=1, fingerprint=fp)]}

    first = client.post("/api/geo-douyin/batches", json=payload)
    assert first.status_code == 200, first.text
    batch_id = first.json().get("batch_id")
    assert batch_id, first.text

    before = _counts(live_db)
    second = client.post("/api/geo-douyin/batches", json=payload)
    _assert_no_5xx(second, "回放提交")
    assert second.status_code == 200, second.text
    assert second.json().get("batch_id"), (
        "回放返回了空 batch_id —— 幂等记录里没有结果可回放"
        "(`response_json` 没人写):" + second.text[:400])
    assert second.json()["batch_id"] == batch_id, (
        "回放拿到的不是同一个批次:" + second.text[:400])
    after = _counts(live_db)
    assert after == before, ("回放产生了新增行(不是零副作用):" + str(before)
                            + " → " + str(after))


def test_the_replay_criterion_can_see_an_empty_response_json(client, seeded, live_db):
    """判别力自证:把 `response_json` 抹空之后,上面那条**必须**失效。

    🔴 没有这一条,「回放返回同一个 batch_id」可能只是因为
       两次都返回了空值。这里人为制造那个状态并断言它**被看见** ——
       即回放确实拿到空 batch_id,于是上一条的断言会红。
    """
    preview = _preview(client, seeded).json()
    fp = preview["items"][0]["production_price_fingerprint"]
    rid = str(uuid.uuid4())
    payload = {
        "request_id": rid, "quote_id": seeded["quote_id"],
        "contract_revision_id": seeded["revision_id"],
        "expected_total_price_points": preview["total_price_points"],
        "items": [_frontend_item(seeded["slot_key"], slot_version=1, fingerprint=fp)]}
    assert client.post("/api/geo-douyin/batches", json=payload).status_code == 200

    conn = psycopg2.connect(live_db, cursor_factory=RealDictCursor)
    conn.autocommit = True
    conn.cursor().execute(
        "UPDATE publish_idempotency_keys SET response_json = '{}'::jsonb"
        " WHERE request_id = %s",
        (rid,))
    conn.close()

    replay = client.post("/api/geo-douyin/batches", json=payload)
    assert replay.status_code == 200, replay.text
    assert not replay.json().get("batch_id"), (
        "`response_json` 已被写成空对象,回放却仍给出了 batch_id —— "
        "说明上一条判据打的不是回放结果这条链路,它没有判别力:" + replay.text[:400])


def test_batch_state_over_http_is_the_projection_not_the_header_column(
        client, seeded, live_db):
    """🔴 [第 3 棒 · 我自己的变异存活] 批次对外状态必须来自**逐项投影**。

    第一版判据只打纯函数 `project_batch_status` —— 于是把 handler 里那一行
    改回读表头(`head['status']`)时,变异**存活**:锁盖住了函数,没盖住出口。
    这条打真 HTTP:先把表头那一列改成一个不可能的值,响应里**不许**出现它。
    """
    preview = _preview(client, seeded).json()
    fp = preview["items"][0]["production_price_fingerprint"]
    created = client.post("/api/geo-douyin/batches", json={
        "request_id": str(uuid.uuid4()), "quote_id": seeded["quote_id"],
        "contract_revision_id": seeded["revision_id"],
        "expected_total_price_points": preview["total_price_points"],
        "items": [_frontend_item(seeded["slot_key"], slot_version=1, fingerprint=fp)]})
    assert created.status_code == 200, created.text
    batch_id = created.json()["batch_id"]

    conn = psycopg2.connect(live_db, cursor_factory=RealDictCursor)
    conn.autocommit = True
    conn.cursor().execute(
        # 🔴 用一个**合法但与事实相反**的值(表头有 CHECK,塞不进乱码):
        #    逐项全是 queued,表头却写 'failed'。读表头 ⇒ 界面说整批失败了。
        "UPDATE geo_douyin_production_batches SET status = 'failed'"
        " WHERE batch_id = %s", (batch_id,))
    conn.close()

    got = client.get("/api/geo-douyin/batches/" + batch_id)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["state"] != "failed", (
        "对外状态直接读了表头那一列 —— 它建行后全仓无人更新,"
        "批次做完了界面上也永远是旧值:" + str(body)[:300])
    assert body["state"] == "accepted", ("全部 queued 的批次应投影成 accepted:"
                                         + str(body)[:300])
    assert body.get("record_state") == "failed", (
        "表头那一列应作为审计留痕原样下发(record_state):" + str(body)[:300])

    # 逐项推进一条 ⇒ 混合态必须落到 processing(不能还说"已接受")
    conn = psycopg2.connect(live_db, cursor_factory=RealDictCursor)
    conn.autocommit = True
    conn.cursor().execute(
        "UPDATE geo_douyin_post_tasks SET status='ready' WHERE production_batch_id = %s"
        "   AND id = (SELECT min(id) FROM geo_douyin_post_tasks"
        "              WHERE production_batch_id = %s)", (batch_id, batch_id))
    conn.close()
    body2 = client.get("/api/geo-douyin/batches/" + batch_id).json()
    assert body2["state"] in ("processing", "completed"), body2
