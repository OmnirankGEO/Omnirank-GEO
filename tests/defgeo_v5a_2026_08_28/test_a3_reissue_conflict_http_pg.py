"""【A-3 = Codex fix-of-fix2 P1-5】面板读完后会话被推进 ⇒ 重签必须 typed 409。

被测缺陷
--------
``customer_links_reissue`` 只捕 ``CustomerLinkObjectDrifted``。面板读出来之后
会话被并发推进到 ``confirmed``,``extend_selection_session_atomically`` 返
``status_not_extendable`` ⇒ 域层抛 ``CustomerLinkNotExtendable`` ⇒ 没人接
⇒ 由 route class 兜成 typed **500 INTERNAL_ERROR**。

我的证伪补一句准确的话:它**不是裸 500 文本**(本包挂了 ``TypedErrorRoute``,
信封形状是对的),坏的是**码和出口**。500 对用户的意思是「我们这边坏了,
稍后再试」—— 而会话不会自己退回选词态,再试一万次都一样。
一句确定性错误的建议,比一个难看的错误页更贵。

判据从 HTTP 进,断言落在 **status_code + 信封里的 code/nextAction** 上:
读源码只能证明 except 子句里写了那个名字,证不了真请求里翻出来的是 409。
"""
from __future__ import annotations

import uuid

import psycopg2
import psycopg2.extras
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

REISSUE_PATH = "/api/defensive-geo/customer-links/reissue"


def _conn(dsn):
    c = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)
    c.autocommit = True
    c.cursor().execute("SET search_path = public")
    return c


def _bind_db(dsn):
    import db.connection as dbconn

    dbconn.DATABASE_URL = dsn
    dbconn._pool = None


def _seed(cur):
    owner = 9701
    cur.execute(
        "INSERT INTO users (id, username, display_name, password_hash, email, is_active) "
        "VALUES (%s,%s,%s,'x',%s,1) ON CONFLICT (id) DO NOTHING",
        (owner, "v5a_a3_owner", "v5a", "v5a_a3@example.com"))
    cur.execute("INSERT INTO brands (name, owner_user_id) VALUES (%s,%s) RETURNING id",
                ("V5A_A3_" + uuid.uuid4().hex[:6], owner))
    brand_id = int(cur.fetchone()["id"])
    cur.execute("INSERT INTO quotes (brand_id, created_at) VALUES (%s, NOW()) RETURNING id",
                (brand_id,))
    quote_id = int(cur.fetchone()["id"])
    token = "V5AA3" + uuid.uuid4().hex[:8].upper()
    cur.execute(
        # keywords_snapshot:NOT NULL 无默认(生产 dump 实核)
        "INSERT INTO keyword_selection_sessions "
        "  (brand_id, quote_id, token, status, expires_at, created_by, keywords_snapshot) "
        "VALUES (%s,%s,%s,'expired','2020-01-01 00:00:00',%s,'[]')",
        (brand_id, quote_id, token, owner))
    return {"brand_id": brand_id, "quote_id": quote_id, "token": token, "owner": owner}


@pytest.fixture()
def env(chain_db):
    dsn = chain_db("a3reissue")
    _bind_db(dsn)
    admin = _conn(dsn)
    ctx = _seed(admin.cursor())

    from api import defensive_geo_assist_api as assist

    app = FastAPI()
    app.include_router(assist.router)

    @app.middleware("http")
    async def _inject(request: Request, call_next):           # noqa: ANN001
        request.state.user = {"user_id": ctx["owner"], "username": "v5a_a3_owner",
                              "is_admin": True}
        request.state.request_id = "v5a-a3-req"
        return await call_next(request)

    with TestClient(app, raise_server_exceptions=False) as client:
        yield {"client": client, "admin": admin, "cur": admin.cursor(), **ctx}
    admin.close()


def _reissue(env, *, status_to_set=None):
    if status_to_set:
        env["cur"].execute(
            "UPDATE keyword_selection_sessions SET status=%s WHERE token=%s",
            (status_to_set, env["token"]))
    return env["client"].post(REISSUE_PATH, json={
        "brandId": env["brand_id"], "kind": "quote_proposal",
        "objectRef": {"kind": "keyword_selection_session", "token": env["token"]},
    })


# ══════════════════════════════════════════════════════════════════════════
# 00 · 判据活性:这条路走得通(不然 409 那条可能是别的原因红/绿)
# ══════════════════════════════════════════════════════════════════════════
def test_00_reissue_works_on_an_extendable_session(env) -> None:
    """配对的必须不命中:``expired`` 的会话重签**应当成功**。

    没有这一条的话,把端点改成"永远 409"也会让下面那条绿。
    """
    r = _reissue(env)
    assert r.status_code == 200, "可延期的会话重签失败了:%d %s" % (r.status_code, r.text[:300])
    body = r.json()
    assert body["link"]["objectRef"]["token"] == env["token"], \
        "重签换的不是同一个对象:%r" % body["link"]["objectRef"]
    env["cur"].execute("SELECT status FROM keyword_selection_sessions WHERE token=%s",
                       (env["token"],))
    assert env["cur"].fetchone()["status"] == "selecting", "延期后没回到选词态"


# ══════════════════════════════════════════════════════════════════════════
# 01 · 本条 finding:推进到 confirmed 之后必须 typed 409,不是 500
# ══════════════════════════════════════════════════════════════════════════
def test_01_confirmed_session_reissue_is_typed_conflict(env) -> None:
    r = _reissue(env, status_to_set="confirmed")
    assert r.status_code != 500, (
        "会话被推进到 confirmed 后重签得到 500 —— 那句话是「我们这边坏了,稍后再试」,"
        "可会话不会自己退回选词态,再试一万次都一样。这是**她这一侧**的状态问题。")
    assert r.status_code == 409, "期望 409(你看到的那一份已经变了),实得 %d" % r.status_code

    detail = r.json().get("detail")
    assert isinstance(detail, dict), "响应体不是 typed 信封:%r" % r.json()
    assert detail.get("code") == "SNAPSHOT_CHANGED", \
        "错误码是 %r(期望复用 SNAPSHOT_CHANGED,不新造码)" % detail.get("code")
    assert detail.get("publicExplanation"), "没有给用户能读的那句话"
    nxt = detail.get("nextAction") or {}
    assert nxt.get("kind") in ("new_preview", "refresh"), \
        "没有给可执行的下一步(nextAction=%r)—— 任何阻塞必须自带解决方案" % nxt
    assert nxt.get("label"), "nextAction 没有人话标签"


def test_02_conflict_writes_nothing(env) -> None:
    """拒绝 = **一行不写**。原语在方向锁那一支是 rollback,这里从 HTTP 侧再证一次。"""
    env["cur"].execute("UPDATE keyword_selection_sessions SET status='confirmed' WHERE token=%s",
                       (env["token"],))
    env["cur"].execute("SELECT * FROM keyword_selection_sessions WHERE token=%s", (env["token"],))
    before = dict(env["cur"].fetchone())
    _reissue(env)
    env["cur"].execute("SELECT * FROM keyword_selection_sessions WHERE token=%s", (env["token"],))
    after = dict(env["cur"].fetchone())
    diff = {k: (before[k], after[k]) for k in before if before[k] != after.get(k)}
    assert not diff, "被 409 拒了,库里那一行还是被改了:%r" % diff


@pytest.mark.parametrize("status", [
    "confirmed", "pending_payment", "active", "payment_overdue",
    "pricing_pending_review", "business_lines_submitted",
])
def test_03_every_non_reversible_status_yields_409(env, status) -> None:
    """方向锁**逐档跑满**,不是只跑 confirmed。

    手抄的名单掉一档不会让任何判据变红,而掉的那一档就是可以被回退的那一档。
    (名单与现役 ``_check_expired_locked`` 的逐字比对在
     ``tests/defgeo_v4a_2026_08_28`` 里,这里跑的是**行为**。)
    """
    r = _reissue(env, status_to_set=status)
    assert r.status_code == 409, \
        "status=%s 的会话重签得到 %d(期望 409)" % (status, r.status_code)
    assert (r.json().get("detail") or {}).get("code") == "SNAPSHOT_CHANGED"


# ══════════════════════════════════════════════════════════════════════════
# 04 · drift 那一支不许被这次合并出口改坏
# ══════════════════════════════════════════════════════════════════════════
def test_04_object_drift_still_returns_the_same_typed_conflict(env) -> None:
    """两个异常收进同一个 except,drift 的行为必须逐字不变(V3-A 存量)。"""
    r = env["client"].post(REISSUE_PATH, json={
        "brandId": env["brand_id"], "kind": "quote_proposal",
        "objectRef": {"kind": "keyword_selection_session", "token": "NOT-THE-ONE"},
    })
    assert r.status_code == 409, "对象漂移不再是 409 了:%d" % r.status_code
    assert (r.json().get("detail") or {}).get("code") == "SNAPSHOT_CHANGED"
