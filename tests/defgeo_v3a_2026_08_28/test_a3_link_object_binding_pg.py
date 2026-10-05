"""【A-3 = Codex 三审 P1-8 + P2-8】客户链接重签没有绑定用户看到的对象。

Codex 原文的两半
----------------
① GET 面板已经下发 ``objectRef``,但 ``ReissueRequest`` 和前端 DTO/POST 只有
   ``brandId+kind``;POST 再跑一次 ``canonical_quote_id()`` 取**最新**报价。
   GET 之后新增 quote ⇒ 用户看的是 A,POST 换掉的是 B 的 token。
   🔴 C-4 那一轮修掉的是「两条 ORDER BY」,这里坏的是**两个时刻** ——
      同一个取数口不代表同一个答案。
② expired ``quote_proposal`` 的文案逐字写着「点一下就能重新签发」,
   而 ``REISSUABLE_KINDS`` 只有 portal、UI 只在 ``reissuable=true`` 时显示按钮
   ⇒ **文案许诺了一条不存在的路径**。

另有 P2-8:凭据轮换只有 actor 非空才落审计,而 defgeo 这条路径**一个都没传**
⇒ audit_logs 里这条轮换是空白的。

修法(见 customer_links.reissue_link / assist_api / 前端 DTO)
-----------------------------------------------------------
· POST 必传 ``objectRef``(**必填不是可选**:可选等于把旧的不安全路径留着,
  而留着的那条不会有判据在守);
· 比对两层:进函数快速失败一次 + 把同一个谓词作为 ``in_tx_guard`` 交给
  ``generate_client_token``,**在它失效旧 token 的那个事务里**再确认一次;
· 漂移 ⇒ typed 409 ``SNAPSHOT_CHANGED``(语义逐字同义,不新造错误码);
· ``quote_proposal`` 接现役 ``extend_session`` ⇒ 文案与能力一致;
· 轮换传 actor/username/request_id ⇒ 审计不再空白。
"""
from __future__ import annotations

import uuid

import psycopg2
import pytest

pytestmark = pytest.mark.integration


def _conn(dsn):
    import psycopg2.extras

    c = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)
    c.autocommit = True
    c.cursor().execute("SET search_path = public")
    return c


def _bind_db(dsn):
    """把生产代码的连接池指到本条判据这把一次性库上。"""
    import db.connection as dbconn

    dbconn.DATABASE_URL = dsn
    dbconn._pool = None


def _seed_brand_with_quote(cur, *, owner=9310):
    cur.execute(
        "INSERT INTO users (id, username, display_name, password_hash, email, is_active) "
        "VALUES (%s,%s,%s,'x',%s,1) ON CONFLICT (id) DO NOTHING",
        (owner, "v3a_a3_%d" % owner, "v3a_a3", "v3a_a3_%d@example.com" % owner))
    cur.execute("INSERT INTO brands (name, owner_user_id) VALUES (%s,%s) RETURNING id",
                ("V3A_A3_" + uuid.uuid4().hex[:6], owner))
    brand_id = int(cur.fetchone()["id"])
    return brand_id, owner


def _new_quote(cur, brand_id, created_at):
    cur.execute("INSERT INTO quotes (brand_id, created_at) VALUES (%s,%s) RETURNING id",
                (brand_id, created_at))
    return int(cur.fetchone()["id"])


def _new_token(cur, quote_id, token, *, is_active=1, expires_at="2099-01-01"):
    cur.execute(
        "INSERT INTO client_access_tokens (quote_id, token, is_active, expires_at) "
        "VALUES (%s,%s,%s,%s)", (quote_id, token, is_active, expires_at))


def _new_session(cur, brand_id, quote_id, token, *, status="selecting",
                 expires_at="2099-01-01 00:00:00"):
    cur.execute(
        "INSERT INTO keyword_selection_sessions "
        "(token, quote_id, brand_id, keywords_snapshot, expires_at, status) "
        "VALUES (%s,%s,%s,'[]',%s,%s) RETURNING id",
        (token, quote_id, brand_id, expires_at, status))
    return int(cur.fetchone()["id"])


@pytest.fixture()
def links_world(chain_db):
    dsn = chain_db("a3links")
    _bind_db(dsn)
    conn = _conn(dsn)
    try:
        cur = conn.cursor()
        brand_id, owner = _seed_brand_with_quote(cur)
        quote_a = _new_quote(cur, brand_id, "2026-08-01 10:00:00")
        _new_token(cur, quote_a, "V3ATOKA" + uuid.uuid4().hex[:8].upper())
    finally:
        conn.close()
    return {"dsn": dsn, "brand_id": brand_id, "owner": owner, "quote_a": quote_a}


# ══════════════════════════════════════════════════════════════════════════
# ① 对象漂移 —— 工单点名的那一发
# ══════════════════════════════════════════════════════════════════════════
def test_a3_reissue_refuses_when_the_object_drifted_after_the_panel_was_read(links_world):
    """🔴 靶心:GET 之后新增一份报价 ⇒ POST 必须 **drift**,不许轮换。

    这一条打的是**两个时刻**之间的窗口:面板读到 A,她还没点之前系统里多了 B,
    她点下去 —— 旧实现会去换 B 的 token(A 还是坏的,客户手上好好的 B 被作废)。
    """
    from services.defensive_geo import customer_links as cl

    panel_ref = {"kind": "quote", "id": links_world["quote_a"]}

    conn = _conn(links_world["dsn"])
    try:
        cur = conn.cursor()
        # 面板读完之后才出现的那一份(created_at 更新 ⇒ 它成了 canonical)
        quote_b = _new_quote(cur, links_world["brand_id"], "2026-08-27 10:00:00")
        _new_token(cur, quote_b, "V3ATOKB" + uuid.uuid4().hex[:8].upper())
        cur.execute("SELECT token, is_active FROM client_access_tokens "
                    "WHERE quote_id=%s", (quote_b,))
        before = dict(cur.fetchone())
    finally:
        conn.close()

    with pytest.raises(cl.CustomerLinkObjectDrifted) as err:
        cl.reissue_link(kind="customer_portal", brand_id=links_world["brand_id"],
                        brand_name="V3A", expected_object_ref=panel_ref)
    assert str(links_world["quote_a"]) in str(err.value.expected), err.value.expected

    # 🔴 反向自证:**一个 token 都没被动过**。
    #    只断言"抛了异常"是不够的 —— 先失效再抛也会让那条断言绿,
    #    而那时客户手上的链接已经废了。
    conn = _conn(links_world["dsn"])
    try:
        cur = conn.cursor()
        cur.execute("SELECT token, is_active FROM client_access_tokens "
                    "WHERE quote_id=%s", (quote_b,))
        after = dict(cur.fetchone())
        assert after["is_active"] == before["is_active"] and after["token"] == before["token"], (
            "对象漂移时报了错,却已经把另一个对象的 token 换掉了:%r → %r"
            % (before, after))
    finally:
        conn.close()


def test_a3_reissue_rotates_when_the_object_did_not_drift(links_world):
    """配对的必须不命中:对象**没漂**时,重签必须照常轮换成功。

    少了它,一个「永远 drift」的实现也能让上面那条绿 —— 而那样按钮就永远点不动了。
    """
    from services.defensive_geo import customer_links as cl

    ref = {"kind": "quote", "id": links_world["quote_a"]}
    out = cl.reissue_link(kind="customer_portal", brand_id=links_world["brand_id"],
                          brand_name="V3A", expected_object_ref=ref)
    assert out is not None and out["objectRef"] == ref, out
    assert out["url"].startswith("/portal/"), out

    conn = _conn(links_world["dsn"])
    try:
        cur = conn.cursor()
        cur.execute("SELECT count(*) AS c FROM client_access_tokens "
                    "WHERE quote_id=%s AND is_active=1", (links_world["quote_a"],))
        assert cur.fetchone()["c"] == 1, "轮换之后该有且只有一个活 token"
    finally:
        conn.close()


def test_a3_the_in_transaction_guard_is_what_the_rotation_primitive_gets(links_world):
    """🔴 CAS 必须落在**轮换那个事务里**,不能只在进函数时比一次。

    只比第一层的话,「比完 → 开事务 → 失效旧 token」之间仍然有窗口,
    而那正是本条 finding 的形状(只是更窄)。这一条把钩子真的被调用这件事钉住:
    替身在事务里被调用一次,且此刻**旧 token 还是活的** —— 证明它跑在失效动作之前。
    """
    from db import monitoring_db as mdb
    from services.defensive_geo import customer_links as cl

    seen = {}
    real = mdb.generate_client_token

    def _spy(*a, **kw):
        guard = kw.get("in_tx_guard")
        seen["guard_passed"] = guard is not None

        def _wrapped(cursor):
            cursor.execute("SELECT count(*) AS c FROM client_access_tokens "
                           "WHERE quote_id=%s AND is_active=1", (kw.get("quote_id"),))
            row = cursor.fetchone()
            seen["active_at_guard_time"] = int(
                row["c"] if isinstance(row, dict) else row[0])
            return guard(cursor)

        kw["in_tx_guard"] = _wrapped
        return real(*a, **kw)

    mdb.generate_client_token = _spy
    try:
        cl.reissue_link(kind="customer_portal", brand_id=links_world["brand_id"],
                        brand_name="V3A",
                        expected_object_ref={"kind": "quote", "id": links_world["quote_a"]})
    finally:
        mdb.generate_client_token = real

    assert seen.get("guard_passed") is True, (
        "轮换原语没有拿到事务内的 CAS 钩子 —— 只剩进函数时那一次比对,窗口还开着")
    assert seen.get("active_at_guard_time") == 1, (
        "钩子被调用时旧 token 已经不是活的(%r)—— 说明它跑在失效动作**之后**,"
        "那就不是 CAS 了" % (seen.get("active_at_guard_time"),))


# ══════════════════════════════════════════════════════════════════════════
# ② 文案与能力一致 —— quote_proposal 现在真的能重签
# ══════════════════════════════════════════════════════════════════════════
def test_a3_the_expired_quote_proposal_copy_no_longer_promises_a_missing_button():
    """🔴 文案说「点一下就能重新签发」,那这一类就必须**真的**可重签。

    这一条把「文案」与「能力」绑在一起:两边任意一边改了而另一边没跟,当场红。
    (Codex 原话:UI 只在 reissuable=true 时显示按钮,因此根本没有可点的按钮。)
    """
    from services.defensive_geo.copy_registry import try_user_label
    from services.defensive_geo.customer_links import REISSUABLE_KINDS

    text = try_user_label("reason", "customer_link_expired") or ""
    assert "重新签发" in text, (
        "过期文案被改了(%r)—— 如果不再许诺重签,这条判据要跟着改;"
        "但**不要**只改一边" % text)
    assert "quote_proposal" in REISSUABLE_KINDS, (
        "过期文案仍然写着「点一下就能重新签发」,而 quote_proposal 不在可重签集合里"
        " —— 文案许诺了一条不存在的路径:UI 只在 reissuable=true 时才显示按钮")


def test_a3_reissuing_an_expired_quote_proposal_actually_extends_the_session(links_world):
    """🔴 而且「能重签」得是**真的**:过期的选词会话必须被延期并恢复可用。

    接的是现役 ``POST /api/keyword-selection/{token}/extend`` 的那两个 db 原语
    (延 7 天 + 过期恢复 selecting),不另写一份延期逻辑。
    """
    from services.defensive_geo import customer_links as cl

    token = "v3a-sess-" + uuid.uuid4().hex[:8]
    conn = _conn(links_world["dsn"])
    try:
        cur = conn.cursor()
        _new_session(cur, links_world["brand_id"], links_world["quote_a"], token,
                     status="expired", expires_at="2020-01-01 00:00:00")
    finally:
        conn.close()

    out = cl.reissue_link(
        kind="quote_proposal", brand_id=links_world["brand_id"], brand_name="V3A",
        expected_object_ref={"kind": "keyword_selection_session", "token": token})
    assert out is not None and out["url"] == "/s/" + token, out

    conn = _conn(links_world["dsn"])
    try:
        cur = conn.cursor()
        cur.execute("SELECT status, expires_at FROM keyword_selection_sessions "
                    "WHERE token=%s", (token,))
        row = dict(cur.fetchone())
        assert row["status"] == "selecting", (
            "过期会话没有被恢复成 selecting:%r —— 那按钮点了等于没点" % row)
        assert str(row["expires_at"]) > "2026-", "过期时间没有被延后:%r" % row
    finally:
        conn.close()


def test_a3_a_drifted_quote_proposal_is_refused_too(links_world):
    """quote_proposal 这一类同样受对象绑定约束(不是只有门户那一类才 CAS)。"""
    from services.defensive_geo import customer_links as cl

    # 🔴 `keyword_selection_sessions.quote_id` 是 **UNIQUE**(生产 schema 实核)——
    #    一个报价只有一个会话。所以"面板之后又出现了一个更新的会话"这一幕,
    #    在真 schema 上必须靠**第二份报价**造出来,不能硬塞两条同 quote 的会话。
    #    (这不是绕开约束,这就是生产上那一幕真实的形状。)
    conn = _conn(links_world["dsn"])
    try:
        cur = conn.cursor()
        _new_session(cur, links_world["brand_id"], links_world["quote_a"],
                     "v3a-old-" + uuid.uuid4().hex[:8])
        quote_b = _new_quote(cur, links_world["brand_id"], "2026-08-27 10:00:00")
        _new_session(cur, links_world["brand_id"], quote_b,
                     "v3a-new-" + uuid.uuid4().hex[:8])
    finally:
        conn.close()

    with pytest.raises(cl.CustomerLinkObjectDrifted):
        cl.reissue_link(
            kind="quote_proposal", brand_id=links_world["brand_id"], brand_name="V3A",
            expected_object_ref={"kind": "keyword_selection_session",
                                 "token": "v3a-a-token-nobody-has"})


# ══════════════════════════════════════════════════════════════════════════
# ③ P2-8 —— 轮换要落 actor/request 审计
# ══════════════════════════════════════════════════════════════════════════
def test_a3_the_rotation_carries_actor_and_request_for_the_audit(links_world):
    """🔴 P2-8:``generate_client_token`` 只有 actor 非空才写 audit_logs,
    而 defgeo 这条路径以前**一个都没传** ⇒ 这条轮换在审计里是空白的。

    这一条不去断言 audit 表里有几行(那是轮换原语自己的判据面),
    它钉的是**本调用方真的把身份交出去了** —— 缺的就是这一手。
    """
    from db import monitoring_db as mdb
    from services.defensive_geo import customer_links as cl

    captured = {}
    real = mdb.generate_client_token

    def _spy(*a, **kw):
        captured.update(kw)
        return real(*a, **kw)

    mdb.generate_client_token = _spy
    try:
        cl.reissue_link(kind="customer_portal", brand_id=links_world["brand_id"],
                        brand_name="V3A", actor_user_id=links_world["owner"],
                        actor_username="v3a_a3", request_id="req-v3a-1",
                        expected_object_ref={"kind": "quote", "id": links_world["quote_a"]})
    finally:
        mdb.generate_client_token = real

    assert captured.get("actor_user_id") == links_world["owner"], captured
    assert captured.get("actor_username") == "v3a_a3", captured
    assert captured.get("request_id") == "req-v3a-1", captured
    assert captured.get("reason"), "没有给出轮换原因 —— 审计行会说不清这次是谁为什么换的"


# ══════════════════════════════════════════════════════════════════════════
# ④ 结构:契约两端都得带上 objectRef
# ══════════════════════════════════════════════════════════════════════════
def test_a3_the_reissue_request_requires_the_object_ref():
    """POST 的 DTO 上 ``objectRef`` 必须是**必填**。

    可选的话,旧的那条"服务端自己取最新"的路径就原样留着了 ——
    而留着的那条不会有任何判据在守(本仓「漏掉的那一项不会让任何判据变红」)。
    """
    import pydantic

    from api.defensive_geo_assist_api import ReissueRequest

    with pytest.raises(pydantic.ValidationError):
        ReissueRequest.model_validate({"brandId": 1, "kind": "customer_portal"})

    ok = ReissueRequest.model_validate({"brandId": 1, "kind": "customer_portal",
                                        "objectRef": {"kind": "quote", "id": 7}})
    # 🔴 [工单 V5-A · P2-2 之后] ``object_ref`` 从裸 ``dict`` 收成了**判别式
    #    typed model**(kind=quote → id:int / kind=keyword_selection_session → token:str),
    #    所以这里比的是 ``model_dump()`` 而不是那个 dict 本身。
    #    本条要守的**不变量没变**:objectRef 必填,而且它带回来的就是面板下发的那个对象。
    #    变的只是表示形态 —— 重构改变了这个谓词该打在哪一层,判据跟着挪,
    #    而不是反过来逼代码为了判据保留旧形状。
    #    并且比 ``model_dump()`` 比原来**更严**:它顺带证明了 id 被规范成了 int。
    assert ok.object_ref.model_dump() == {"kind": "quote", "id": 7}
    assert isinstance(ok.object_ref.model_dump()["id"], int)


def test_a3_the_frontend_dto_and_caller_carry_the_object_ref():
    """前端 DTO 与调用点同步 —— 服务端发了、前端丢了,等于没修。

    源码级断言:本环境 ``node_modules`` 里**没有** typescript
    (``npx tsc`` 会以"请先安装"退出,而本仓记过「缺 typescript 时 rc=0」是假绿),
    所以这里不假装做了类型检查,只机械核三件事:
    DTO 声明了这一格 / api 函数签名收它 / 唯一调用点真的传了它。
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    api_ts = (root / "frontend/src/lib/defensiveGeoAssistApi.ts").read_text(encoding="utf-8")
    panel = (root / "frontend/src/components/defensiveGeo/CustomerLinksPanel.tsx"
             ).read_text(encoding="utf-8")

    assert re.search(r"objectRef:\s*\{\s*kind:", api_ts), (
        "CustomerLink DTO 里没有 objectRef —— 服务端发了、前端丢了")
    assert re.search(r"reissueCustomerLink\([^)]*objectRef", api_ts, re.S), (
        "reissueCustomerLink 的签名没收 objectRef")
    assert "objectRef" in api_ts.split("customer-links/reissue")[1][:200], (
        "POST body 里没带 objectRef")
    assert "link.objectRef" in panel, (
        "面板调用点没有把**这一行显示的那个对象**传回去")
