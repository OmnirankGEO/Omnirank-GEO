"""【A-2 = Codex fix-of-fix2 P1-4】延期端点跨租户越权 —— 真 HTTP × 真 PG16。

被测缺陷
--------
``POST /api/keyword-selection/{token}/extend`` 没有对象归属校验。客户分享链接里的
token 是明文的:任何 quote-enabled 的登录用户拿到别人租户的 token,就能把那个
会话延期 —— 并把一个 ``expired`` 会话拉回 ``selecting``,**重新对客户开放选词**。

🔴 为什么必须打真 HTTP + 真库
-----------------------------
本仓记过「裸符号名 = 把 import 当调用」和「验标记清零 ≠ 验接线」:
源码里出现一行 ``_require_session_owner_access(request, token)`` 证明不了
它在真请求里跑到了。所以这一组从 HTTP 进,断言落在**库里的行**上。

三条断言彼此独立
----------------
① 跨租户 ⇒ 403(与现有 owner 检查同码);
② 拒绝后那一行**逐字段不变**(不是"看起来没变" —— 拿拒绝前后的整行快照比);
③ 同租户 ⇒ 200 且 ``expires_at`` 真的往后走了。
只有①的话,"先写了再报 403" 也会绿;只有③的话,把校验删掉也会绿。
"""
from __future__ import annotations

import uuid

import psycopg2
import psycopg2.extras
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

EXTEND_PATH = "/api/keyword-selection/%s/extend"


def _conn(dsn):
    c = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)
    c.autocommit = True
    c.cursor().execute("SET search_path = public")
    return c


def _bind_db(dsn):
    import db.connection as dbconn

    dbconn.DATABASE_URL = dsn
    dbconn._pool = None


def _user(cur, uid, name):
    cur.execute(
        "INSERT INTO users (id, username, display_name, password_hash, email, is_active) "
        "VALUES (%s,%s,%s,'x',%s,1) ON CONFLICT (id) DO NOTHING",
        (uid, name, name, "%s@example.com" % name))


def _seed(cur):
    owner, attacker = 9601, 9602
    _user(cur, owner, "v5a_owner")
    _user(cur, attacker, "v5a_attacker")
    cur.execute("INSERT INTO brands (name, owner_user_id) VALUES (%s,%s) RETURNING id",
                ("V5A_A2_" + uuid.uuid4().hex[:6], owner))
    brand_id = int(cur.fetchone()["id"])
    cur.execute("INSERT INTO quotes (brand_id, created_at) VALUES (%s, NOW()) RETURNING id",
                (brand_id,))
    quote_id = int(cur.fetchone()["id"])
    token = "V5ATOK" + uuid.uuid4().hex[:8].upper()
    cur.execute(
        # 🔴 keywords_snapshot 是 NOT NULL 且**没有默认值**(生产 dump 实核) ——
        #    漏了它这条 INSERT 直接炸,判据会因为"夹具建不起来"而红,
        #    那种红与"缺陷仍在"长得不一样,但一样会浪费一轮。
        "INSERT INTO keyword_selection_sessions "
        "  (brand_id, quote_id, token, status, expires_at, created_by, keywords_snapshot) "
        "VALUES (%s,%s,%s,'expired','2020-01-01 00:00:00',%s,'[]') RETURNING id",
        (brand_id, quote_id, token, owner))
    return {"brand_id": brand_id, "quote_id": quote_id, "token": token,
            "owner": owner, "attacker": attacker}


def _row(cur, token):
    cur.execute("SELECT * FROM keyword_selection_sessions WHERE token=%s", (token,))
    return dict(cur.fetchone())


@pytest.fixture()
def env(chain_db):
    dsn = chain_db("a2extend")
    _bind_db(dsn)
    admin = _conn(dsn)
    ctx = _seed(admin.cursor())

    from api import selection_api

    app = FastAPI()
    app.include_router(selection_api.router)

    @app.middleware("http")
    async def _inject(request: Request, call_next):           # noqa: ANN001
        who = request.headers.get("X-Test-Identity", "owner")
        request.state.user = {
            "owner": {"user_id": ctx["owner"], "username": "v5a_owner", "is_admin": False},
            "attacker": {"user_id": ctx["attacker"], "username": "v5a_attacker",
                         "is_admin": False},
            "admin": {"user_id": ctx["owner"], "username": "v5a_admin", "is_admin": True},
        }[who]
        return await call_next(request)

    with TestClient(app, raise_server_exceptions=False) as client:
        yield {"client": client, "admin": admin, "cur": admin.cursor(), **ctx}
    admin.close()


# ══════════════════════════════════════════════════════════════════════════
# 00 · 判据活性:路由真的挂上了,夹具真的摆成了「可延期」那一态
# ══════════════════════════════════════════════════════════════════════════
def test_00_fixture_is_in_the_extendable_state(env) -> None:
    before = _row(env["cur"], env["token"])
    assert before["status"] == "expired", (
        "夹具不在 expired 态,那么「拒绝后没写入」这件事可能只是因为**本来就写不进去**")
    r = env["client"].post(EXTEND_PATH % env["token"], headers={"X-Test-Identity": "owner"})
    assert r.status_code != 404, "路由没挂上 —— 后面全是假绿(路径:%s)" % (EXTEND_PATH % "…")


# ══════════════════════════════════════════════════════════════════════════
# 01 · 跨租户必须被拒 + 一行都不许写
# ══════════════════════════════════════════════════════════════════════════
def test_01_cross_tenant_is_rejected(env) -> None:
    r = env["client"].post(EXTEND_PATH % env["token"],
                           headers={"X-Test-Identity": "attacker"})
    assert r.status_code in (403, 404), (
        "别的租户拿着这个 token 得到了 %d —— 全局 JWT 只说明「能用报价模块」,"
        "证明不了「拥有这个 session」" % r.status_code)
    assert r.status_code == 403, (
        "现有 owner 检查对「存在但不是你的」返 403;这里返了 %d,与同类端点不同码"
        % r.status_code)


def test_02_rejected_request_writes_nothing(env) -> None:
    """拒绝后整行**逐字段**不变。

    🔴 只看 ``expires_at`` 不够:延期原语还会写 ``status`` 与 ``updated_at``。
       拿整行快照比,漏字段这件事就不可能发生。
    """
    before = _row(env["cur"], env["token"])
    env["client"].post(EXTEND_PATH % env["token"], headers={"X-Test-Identity": "attacker"})
    after = _row(env["cur"], env["token"])
    diff = {k: (before[k], after[k]) for k in before if before[k] != after.get(k)}
    assert not diff, "越权请求被拒了,但库里那一行还是被改了:%r" % diff


def test_03_expires_at_unchanged_after_rejection(env) -> None:
    """工单点名的那一格单独再钉一次 —— 一红就直接指到"她的链接被别人续了命"。"""
    before = _row(env["cur"], env["token"])["expires_at"]
    env["client"].post(EXTEND_PATH % env["token"], headers={"X-Test-Identity": "attacker"})
    assert _row(env["cur"], env["token"])["expires_at"] == before


# ══════════════════════════════════════════════════════════════════════════
# 04 · 配对的必须不命中:自己人照常能延期
# ══════════════════════════════════════════════════════════════════════════
def test_04_owner_can_still_extend(env) -> None:
    """没有这一条,把端点改成"永远 403"也会让上面三条全绿。"""
    before = _row(env["cur"], env["token"])
    r = env["client"].post(EXTEND_PATH % env["token"], headers={"X-Test-Identity": "owner"})
    assert r.status_code == 200, "品牌归属者延期被拒了:%d %s" % (r.status_code, r.text[:200])
    after = _row(env["cur"], env["token"])
    assert str(after["expires_at"]) > str(before["expires_at"]), \
        "返了 200 但 expires_at 没往后走:%r → %r" % (before["expires_at"], after["expires_at"])
    assert after["status"] == "selecting", \
        "expired 延期后应当回到 selecting,实得 %r" % after["status"]


def test_05_admin_can_extend(env) -> None:
    """admin 全权 —— 与现有 ``_require_session_owner_access`` 的口径一致,不另立一套。"""
    r = env["client"].post(EXTEND_PATH % env["token"], headers={"X-Test-Identity": "admin"})
    assert r.status_code == 200, "admin 延期被拒了:%d %s" % (r.status_code, r.text[:200])


# ══════════════════════════════════════════════════════════════════════════
# 06 · 方向锁不许因为加了归属校验而丢
# ══════════════════════════════════════════════════════════════════════════
def test_06_confirmed_session_still_cannot_be_extended(env) -> None:
    """V4-A 的状态机方向锁是**存量**:归属校验加在它前面,不许把它顶掉。

    归属校验先跑 ⇒ 自己人也照样撞到 409,而不是"有权限就能把 confirmed 拉回去"。
    """
    env["cur"].execute("UPDATE keyword_selection_sessions SET status='confirmed' WHERE token=%s",
                       (env["token"],))
    before = _row(env["cur"], env["token"])
    r = env["client"].post(EXTEND_PATH % env["token"], headers={"X-Test-Identity": "owner"})
    assert r.status_code == 409, \
        "已确认的会话被延期成功了(%d)—— 商业状态被倒回去了" % r.status_code
    after = _row(env["cur"], env["token"])
    assert after["status"] == "confirmed", "状态被改成了 %r" % after["status"]
    assert after["expires_at"] == before["expires_at"], "被拒了但 expires_at 还是动了"
