"""第 4 棒 · P0-7(Owner 2026-08-18 裁决)· 两道闸的**真 HTTP** 判据 + 一条负向锁。

Owner 裁决原文(见 `CODEX_GEOIMG_R2_AUDIT_2026-08-18.md` §Owner 裁决):

  ① legacy `/short-video/publish` 加**向前闸**:新调用必须带非空 `geo_post_id`
     (存量数据与既有在途单不动,不做回溯修改);
     **Owner 2026-08-19 定案收窄**:闸**只封图文 lane**(`article_type=3`),
     手动上传视频直发(`geo_post_id: null`)不受闸,现役路径零变化;
  ② 浏览器自报(`/publish-result` 与 extension `PUBLISH_PROGRESS`)**降级为线索**:
     可记录、可展示为「待核实」,**不得**直接写 publish_records success /
     不作为发布终态事实;终态只能由 managed 链(worker + provider 回执)落。

## 判据形状

- 闸的判据必须打**真 HTTP**,而且必须证明"拒的是这一条" —— 只断言 4xx 不算数,
  那样把「缺 media_ids」也一并算成闸生效了。所以每一条都配**同一份 payload
  只差一个字段**的对照:两边拿到的拒绝**理由不同**,闸才算真的在这一格上;
- 降级的判据必须落到**库**:响应说「待核实」是廉价的,真正要证的是
  `published_url` / `published_at` / 作品终态**一个字都没被写**。
"""
from __future__ import annotations

import json
import os
import pathlib
import re
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
#: 🔴 [第 7 棒 · R7 (3)] 读 `publish_records` 的**结构锚**。
#:
#:    上一版这个正则里的 ``\b`` 是**真的 0x08 退格字节**(改写脚本把两字符转义
#:    写成了字节),于是它要求"publish_records 前后各跟一个退格符" —— 源码里
#:    永远不会出现,锁**恒绿**。下面配了毒化自证,它再变成死正则会当场红。
READS_PUBLISH_RECORDS = re.compile(
    r"(?i)\b(from|into|update|join)\s+publish_records\b")

#: 图文的**交付单元身份键**(见 `publication_stage_projection.unit_key`)。
#: 浏览器自报那一支只要拿不到这几个,就归不到具体某一篇图文头上。
IMAGE_NOTE_IDENTITY_KEYS = ("delivery_slot_key", "source_geo_post_id",
                            "geo_douyin_posts", "geo_post_id")


#: SQL 里"互相独立的一段"的边界:UNION 分支 与 EXISTS 子查询。
#: 两种都要切 —— 现役投影层两种写法都有。
BRANCH_BOUNDARY = re.compile(
    r"(?i)\bUNION\b|\bEXISTS\s*\(")


def _identity_keys_in(sql_branch):
    return sorted(k for k in IMAGE_NOTE_IDENTITY_KEYS
                  if re.search(r"\b" + k + r"\b", sql_branch))


def _scan_files(scope):
    out = []
    for path in scope:
        files = sorted(path.rglob("*.py")) if path.is_dir() else [path]
        out += [f for f in files if "__pycache__" not in str(f) and f.exists()]
    return out


#: 源码里把一个常量拼进 SQL 的写法:`""" + NAME + """`。
#: 抽常量会**把一个 SQL 分支切成两个字面量**,而结构锚是按字面量分段的 ——
#: 不先把拼接缝合回去,分支的一半(SELECT 列)与另一半(FROM 子句)就落到
#: 两段里,毒化变异从此杀不动。第 7 棒的变异 R10 实测正是这样存活的:
#: 我在 (5) 里抽常量,顺手在 (3) 的锁上开了个洞。
_SPLICE = re.compile(r'"{3}\s*\+\s*[A-Za-z_][A-Za-z0-9_]*\s*\+\s*"{3}')


def _sql_literals(text):
    """取源码里的 SQL 字面量(三引号块),**先把常量拼接缝合回去**。

    锁**字面量**而不是整个文件:整文件扫会把几百行外一个无关的
    `geo_douyin_posts` 算成"publish_records 分支携了身份键" —— 那是假红。
    """
    text = _SPLICE.sub(" /*spliced*/ ", text)
    return (re.findall(r'"{3}(.*?)"{3}', text, flags=re.S)
            + re.findall(r"'{3}(.*?)'{3}", text, flags=re.S))


PROD_SCHEMA = pathlib.Path(os.getenv(
    "GEOIMG_PROD_SCHEMA_SQL", r"C:/AI-Test/.deploy_toolkit/_geoimg_prodschema_20260817.sql"))
DSN = os.getenv("TEST_DATABASE_URL")
OWNER_UID = 4444


#: (1) 图文合同链本体:这里**一处都不得**读 publish_records。
IMAGE_NOTE_CHAIN_SCOPE = [
    REPO / "services" / "geo_douyin",
    REPO / "api" / "geo_douyin_api.py",
    REPO / "api" / "geo_image_note_api.py",
    REPO / "db" / "geo_douyin_db.py",
]

#: (2) 投影层 + 发布历史统一视图:这里**允许**读 publish_records(浏览器自发确实
#:     是四条发布链之一),但那一支**不得携图文身份键**。
PROJECTION_AND_HISTORY_SCOPE = [
    REPO / "services" / "publication_stage_sources.py",
    REPO / "services" / "publication_stage_projection.py",
    REPO / "services" / "publication_stage_adapters.py",
    REPO / "api" / "meijiehezi_api.py",
    REPO / "db" / "meijiehezi_db.py",
]


def _admin_dsn() -> str:
    return DSN.rsplit("/", 1)[0] + "/postgres"


@pytest.fixture(scope="module")
def live_db():
    if not DSN or not PROD_SCHEMA.is_file():
        pytest.skip("需要 TEST_DATABASE_URL 与生产 schema 夹具")
    name = "geoimg_r3fence_" + uuid.uuid4().hex[:8]
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


@pytest.fixture()
def seeded(live_db):
    conn = _conn(live_db)
    c = conn.cursor()
    c.execute("SET search_path = public")
    c.execute("INSERT INTO users (id, username, display_name, password_hash, email) "
              "VALUES (%s,%s,%s,'x',%s) ON CONFLICT (id) DO NOTHING",
              (OWNER_UID, "owner_fence", "owner_fence", "fence@example.com"))
    c.execute("INSERT INTO user_wallets (user_id, paid_points, bonus_points, frozen_points) "
              "VALUES (%s, 1000000, 0, 0) ON CONFLICT (user_id) DO UPDATE "
              "SET paid_points = 1000000", (OWNER_UID,))
    c.execute("INSERT INTO brands (name, owner_user_id) VALUES (%s,%s) RETURNING id",
              ("闸品牌_" + uuid.uuid4().hex[:6], OWNER_UID))
    brand_id = int(c.fetchone()["id"])
    c.execute(
        "INSERT INTO geo_douyin_posts (brand_id, created_by, keyword, content_type, status,"
        " tenant_owner_user_id, payer_user_id, actor_user_id, title, body_text,"
        " hashtags, cards, oss_keys) VALUES (%s,%s,'关键词','image_post','ready',"
        " %s,%s,%s,%s,%s,'[]'::jsonb,'[]'::jsonb,%s::jsonb) RETURNING id",
        (brand_id, OWNER_UID, OWNER_UID, OWNER_UID, OWNER_UID, "标题", "正文",
         json.dumps(["a.png"])))
    post_id = int(c.fetchone()["id"])
    conn.close()
    return {"dsn": live_db, "brand_id": brand_id, "post_id": post_id}


def _client(router):
    testclient = pytest.importorskip("fastapi.testclient")
    from fastapi import FastAPI, Request as FastAPIRequest

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


# ═══════════════════════════════════════════════════════════════════
# ① legacy `/short-video/publish` 向前闸
# ═══════════════════════════════════════════════════════════════════

def _svideo_payload(**over):
    body = {
        "title": "一条测试标题", "content": "正文", "keyword": "kw",
        "media_ids": [9001], "media_names": ["账号A"],
        "article_type": 3, "video_url": "", "image_urls": ["https://x/a.png"],
        "cover_image": "", "customer_name": "客户", "remark": "",
        "brand_id": None, "geo_post_id": None, "cost_points": [1], "cost_yuan": [],
    }
    body.update(over)
    return body


@pytest.fixture()
def svideo_client(live_db, monkeypatch):
    import services.meijiehezi.config as mcfg

    monkeypatch.setattr(mcfg, "SVIDEO_PUBLISH_ENABLED", True, raising=False)
    import api.meijiehezi_api as mapi

    return _client(mapi.router)


def test_new_call_without_geo_post_id_is_refused_by_the_forward_gate(svideo_client, seeded):
    """**图文 lane**(`article_type=3`)缺 `geo_post_id` 的新调用必须被这道闸拒掉,
    而且是**零副作用**。

    🔴 `article_type=3` 在这里是**显式的作用域前提**,不是"payload 恰好这么写"——
       Owner 2026-08-19 定案闸只封图文 lane,所以这一条与下面那条视频直发的
       放行对照**成对**才有意义:少了任何一半,"只封图文"都验不出来
       (全封与全不封各自都能让单独一条变绿)。
    """
    resp = svideo_client.post("/api/meijiehezi/short-video/publish",
                              json=_svideo_payload(article_type=3, geo_post_id=None))
    assert resp.status_code == 400, (str(resp.status_code) + " " + resp.text[:400])
    detail = resp.json().get("detail")
    assert isinstance(detail, dict) and detail.get("code") == "GEO_POST_ID_REQUIRED", (
        "拒是拒了,但不是被向前闸拒的 —— 只断言 4xx 会把别的拒绝也算成闸生效:"
        + resp.text[:400])

    # 零副作用:闸在扣费/建单之前,库里不许留下任何订单
    left = _row(seeded["dsn"], "SELECT count(*) AS n FROM mhz_publish_orders"
                               " WHERE user_id=%s", (OWNER_UID,))
    assert int(left["n"]) == 0, ("被闸拦下的调用留下了订单:" + str(left))


def test_geo_post_id_is_not_an_identity_credential(svideo_client, seeded):
    """[第 5 棒 · Codex R5 P0-3 · **不变式搬家 ⇒ 改断言不退役**]

    上一版这里断言的是「带了 `geo_post_id` 就该放行」。那条断言**是错的**,
    而且错在最要命的地方:`geo_post_id` **不是身份凭证**。

      · 带上它并不改变这个端点的形状 —— 标题 / 正文 / 图片 / 账号清单仍然
        全部来自客户端,服务端还照着这些内容**重铸一张草稿**;
      · 于是「看起来来自 managed 作品」的提交,发出去的可以是完全另一份内容,
        而 managed 链那一整套(冻结版本 / 素材身份三方核对 / 外发前广告法闸 /
        逐项冻结结算)一条都盖不到它身上。

    所以图文 lane 的 legacy 入口**直接拒绝**,并指向新 command。
    本条断言的是「带着自带内容的 type-3 提交必被拒,且拒的理由是这一条」。
    """
    resp = svideo_client.post(
        "/api/meijiehezi/short-video/publish",
        json=_svideo_payload(article_type=3, geo_post_id=seeded["post_id"]))
    assert resp.status_code == 400, (str(resp.status_code) + " " + resp.text[:400])
    detail = resp.json().get("detail")
    assert isinstance(detail, dict) and detail.get("code") == "IMAGE_NOTE_LEGACY_ENTRY_CLOSED", (
        "带 geo_post_id 的 type-3 仍从 legacy 入口发了出去 —— 旁路没封:"
        + resp.text[:500])
    assert detail.get("use_endpoint") == "/api/meijiehezi/image-notes/publish-batch", (
        "拒了但没指向新入口 —— 用户会以为图文发不了了:" + resp.text[:400])

    # 零副作用:拒在扣费/建单之前
    left = _row(seeded["dsn"], "SELECT count(*) AS n FROM mhz_publish_orders"
                               " WHERE user_id=%s", (OWNER_UID,))
    assert int(left["n"]) == 0, ("被拒的调用留下了订单:" + str(left))


def test_idempotent_replay_of_an_inflight_order_is_not_touched_by_the_gate(
        svideo_client, seeded, monkeypatch):
    """存量/在途不动:命中幂等缓存的重放**先于**闸返回,老单不受新闸影响。

    Owner 裁决里"存量数据与既有在途单不动"这半句,落到代码里就是这一条 ——
    闸必须放在幂等命中**之后**。放前面的话,新闸会把上线前发出去的
    在途单的重试一并打死。
    """
    import db.meijiehezi_db as mdb

    cached = {"status": "success", "message": "这是上线前那笔在途单的缓存响应"}
    monkeypatch.setattr(mdb, "check_idempotency", lambda *a, **k: cached)
    resp = svideo_client.post(
        "/api/meijiehezi/short-video/publish",
        json=_svideo_payload(geo_post_id=None, request_id=str(uuid.uuid4())))
    assert resp.status_code == 200, resp.text[:400]
    assert resp.json().get("message") == cached["message"], (
        "在途单的幂等重放被新闸拦下了 —— 违反『存量与在途不动』:" + resp.text[:400])


def test_video_direct_publish_without_geo_post_id_is_untouched_by_the_gate(
        svideo_client, seeded):
    """回归对照(Owner 2026-08-19 定案的另一半):`article_type≠3` + `geo_post_id` 为空
    ⇒ **必须放行**,现役「手动上传视频直发」路径零变化。

    🔴 断言不是"没报 GEO_POST_ID_REQUIRED"就完 —— 那句话在 500 里也成立。
       这里断言它**恰好落到下一道真实业务闸**(视频直发没传视频地址),
       证明请求确实**穿过**了向前闸,而不是死在别处。
    """
    resp = svideo_client.post(
        "/api/meijiehezi/short-video/publish",
        json=_svideo_payload(article_type=1, geo_post_id=None,
                             image_urls=[], video_url=""))
    assert resp.status_code == 400, (str(resp.status_code) + " " + resp.text[:400])
    assert "GEO_POST_ID_REQUIRED" not in resp.text, (
        "视频直发被向前闸拦了 —— 闸的作用域超出了图文 lane,现役路径被改坏:"
        + resp.text[:400])
    assert resp.json().get("detail") == "请先上传视频", (
        "没落到视频直发的下一道业务闸 —— 说明它是被别的东西挡下的,"
        "『穿过了向前闸』这件事没被证明:" + resp.text[:400])


def test_forward_gate_scope_is_a_single_named_constant():
    """作用域必须是**一个具名常量**,不是散在 handler 里的 if。

    这条不是风格洁癖:Owner 若改判"只封图文旁路",改的应当是一处常量,
    而不是去 handler 里重新读一遍逻辑。交付单把它列为发车前确认项,
    确认动作落在这个常量上。
    """
    import api.meijiehezi_api as mapi

    assert hasattr(mapi, "LEGACY_SVIDEO_FORWARD_GATE_ARTICLE_TYPES"), (
        "向前闸的作用域没有具名常量")
    scope = mapi.LEGACY_SVIDEO_FORWARD_GATE_ARTICLE_TYPES
    assert scope is None or isinstance(scope, (set, frozenset)), scope
    # 🔴 Owner 2026-08-19 定案值。改它 = 改 Owner 裁过的业务范围,
    #    必须先有新裁决 —— 所以这里钉死,不是"看看类型对不对"。
    assert scope == {3}, (
        "向前闸作用域不是 Owner 2026-08-19 定案的 {3}(只封图文 lane):" + str(scope))


# ═══════════════════════════════════════════════════════════════════
# ② 浏览器自报 `/publish-result` 降级为线索
# ═══════════════════════════════════════════════════════════════════

def test_self_reported_publish_never_becomes_a_terminal_fact(seeded):
    """自报一条"我发出去了 + 这是链接" ⇒ **一个终态字段都不许被写**。

    被写了会怎样:`published_url` 是飞轮反查引用归因的锚,假锚会污染整条归因链;
    作品 `published` 会让用户以为发成功了,而渠道那边可能什么都没有。
    """
    import api.geo_douyin_api as gapi

    client = _client(gapi.router)
    resp = client.post("/api/geo-douyin/publish-result", json={
        "post_id": seeded["post_id"], "order_id": None, "item_ids": [],
        "publish_status": "published",
        "published_url": "https://www.douyin.com/note/伪造的链接",
    })
    assert resp.status_code == 200, resp.text[:400]
    body = resp.json()
    assert body.get("verified") is False, ("自报被当成已核实:" + str(body))

    post = _row(seeded["dsn"], "SELECT status, publish_status, published_url, published_at"
                               " FROM geo_douyin_posts WHERE id=%s", (seeded["post_id"],))
    assert not post["published_url"], (
        "客户端自报的链接被写进了归因锚:" + str(post))
    assert post["published_at"] is None, ("自报写出了发布时间:" + str(post))
    assert post["status"] != "published", ("自报把作品置成了已发布终态:" + str(post))
    assert post["publish_status"] == "self_reported_unverified", (
        "线索没被记录下来 —— 降级不等于丢掉,『可记录、展示为待核实』是裁决的一半:"
        + str(post))

    alert = _row(seeded["dsn"], "SELECT payload FROM ai_ops_alerts WHERE rule_key=%s",
                 ("geo_imgnote_self_reported_publish",))
    assert alert is not None, "自报线索没有留下可查询的记录"
    assert "伪造的链接" in str((alert["payload"] or {}).get("claimed_published_url") or ""), (
        "自报的原始内容没被保留下来供人工核实:" + str(alert))


# ═══════════════════════════════════════════════════════════════════
# ③ 负向锁 · 图文终态链**不得**把 publish_records 当事实源
# ═══════════════════════════════════════════════════════════════════

def test_image_note_terminal_chain_never_reads_publish_records():
    """负向锁:图文合同链的任何一处**都不许**读 `publish_records`。

    🔴 为什么是负向锁而不是改插件后端(WO_273 · 2026-09-23 已整体删除):Owner 裁决第 ② 条点名了
       「不得直接写 publish_records success」。但 `publish_records` 是
       **文章/浏览器插件** lane 的表,全仓有 8+ 处消费者(投放计划核验 /
       发布阶段投影 / 实验登记 / 数据健康 / 已分发计数)。把那条 lane 的
       success 一刀降级,会在**图文域之外**造成 P0 回归。

       本包能给出的、且真实的那一半是:**证明图文这条链从来不从
       `publish_records` 取终态事实** —— 也就是 Codex 担心的那条旁路
       在图文域**不存在**。文章 lane 的降级已在交付单列为未做 + 上抛。

    🔴 这条锁**会**在有人把两条 lane 接起来时转红,那正是它的用途。
    """
    checked, offenders = [], []
    for f in _scan_files(IMAGE_NOTE_CHAIN_SCOPE):
        checked.append(f)
        if READS_PUBLISH_RECORDS.search(f.read_text(encoding="utf-8", errors="ignore")):
            offenders.append(str(f.relative_to(REPO)))
    assert len(checked) >= 8, (
        "分母太小,锁可能扫了个空目录(checked=" + str(len(checked)) + ")")
    assert not offenders, (
        "图文链开始读 publish_records 了 —— 浏览器自报就能反过来决定图文终态:"
        + str(offenders))


def test_the_negative_lock_itself_is_not_a_dead_regex():
    """🔴 **毒化自证**:上一条锁的正则必须真能匹配到东西。

    为什么值得单独一条 —— 上一版它是**恒绿**的:改写脚本把 ``\b`` 写成了
    **真的 0x08 退格字节**,于是正则要求"publish_records 前后各跟一个退格符",
    源码里永远不会出现 ⇒ offenders 恒空 ⇒ 锁恒绿。而我拿它当过裁定依据。

    **一条锁在被当作证据引用之前,必须先亲手注一次毒。**
    """
    assert READS_PUBLISH_RECORDS.search(
        "SELECT 1 FROM publish_records r WHERE r.status = 'success'"), (
        "毒没被抓住 —— 这条负向锁是死的(上一版正是 0x08 退格字节)")
    assert READS_PUBLISH_RECORDS.search("  JOIN publish_records pr ON pr.id = x.id")
    assert READS_PUBLISH_RECORDS.search("UPDATE publish_records SET status='success'")
    # 反向对照:注释/病历里提到表名不算读它,否则锁会被自己的说明文字判红。
    assert not READS_PUBLISH_RECORDS.search("# 我们不读 publish_records 作为终态事实")
    assert not READS_PUBLISH_RECORDS.search("publish_records_backfill(x)")
    # 分母自证:锁真的扫到了文件,不是扫了个不存在的目录。
    assert len(_scan_files(IMAGE_NOTE_CHAIN_SCOPE)) >= 8


def test_publish_records_can_never_carry_an_image_note_identity():
    """投影层 / 发布历史层里的 `publish_records` **不得携图文身份键**。

    🔴 上一版把 scope 圈在 `services/geo_douyin/**`,漏掉了真正危险的那一层:
       **六阶段投影**(`publication_stage_sources`)把四条发布链 UNION 到一起,
       其中就有 `publish_records`;发布历史统一视图(`meijiehezi_api` /
       `meijiehezi_db`)也 UNION 了它。

    🔴 这一层**不能**锁成"不许出现 publish_records" —— 浏览器自发确实是四条
       发布链之一,那是既存且正确的业务。能锁、且必须锁的是**归属**:
       图文的交付单元键是 `delivery_slot_key` / `source_geo_post_id`
       (`publication_stage_projection.unit_key`)。只要 publish_records 那一支拿不到
       这两个键,浏览器自报就**永远落不到某一篇图文头上**,也就成不了它的终态事实。
       现役写法给的正是 `NULL::uuid, NULL::bigint`。

    分段方式:取 SQL 字面量 → 按 **UNION 分支 / EXISTS 子查询**切开。不切的话,
    mhz 那一支(它**应该**带 `source_geo_post_id`)会把同一个字面量里的
    publish_records 分支一并拖红 —— 那是锁形不锁对。实测两种边界都必须切:
    `_PUBLICATIONS_SQL` 是 UNION,而 `PUBLISHED_OCCURRENCE_EXISTS_SQL` 是
    `OR EXISTS (...)`,只切 UNION 时后者整块算一段 ⇒ 假红。
    """
    offenders, branches = [], 0
    for f in _scan_files(PROJECTION_AND_HISTORY_SCOPE):
        for literal in _sql_literals(f.read_text(encoding="utf-8", errors="ignore")):
            for branch in re.split(BRANCH_BOUNDARY, literal):
                if not READS_PUBLISH_RECORDS.search(branch):
                    continue
                branches += 1
                leaked = _identity_keys_in(branch)
                if leaked:
                    offenders.append((str(f.relative_to(REPO)), leaked))
    assert branches >= 4, (
        "分母太小:只扫到 " + str(branches) + " 个读 publish_records 的分支 —— "
        "锁可能没真扫到投影层(零分母的锁和恒绿的锁一样废)")
    assert not offenders, (
        "publish_records 分支开始携图文身份键了 —— 浏览器自报将能归到具体某一篇:"
        + str(offenders))


def test_the_identity_lock_itself_is_not_a_dead_regex():
    """毒化自证 · 上一条:把图文身份键插进 publish_records 分支,必须被逗出来。"""
    poisoned = ("SELECT r.id, gp.delivery_slot_key, i.source_geo_post_id"
                "  FROM publish_records r"
                "  JOIN geo_douyin_posts gp ON gp.id = r.geo_post_id")
    assert READS_PUBLISH_RECORDS.search(poisoned), "毒没被结构锚抓住"
    assert _identity_keys_in(poisoned), "身份锁是死的 —— 插了身份键也不红"
    # 反向对照:现役写法(两个身份键都是硬 NULL)不得被判红。
    clean = ("SELECT %(quote_id)s::int, 'publish_records'::text, r.id::bigint,"
             "       NULL::uuid, NULL::bigint, r.article_id  FROM publish_records r")
    assert not _identity_keys_in(clean), "现役写法被自己的锁判红了"
