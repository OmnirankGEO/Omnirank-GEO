"""【门八第三发现】confirm 成功之后,进度端点必须**立刻**看得见这次体检。

被测缺陷
--------
防御体检的 confirm 五腿提交成功 = run 真的存在、算力真的冻着。但真正开跑要等
cron 执行器下一轮(生产 20s)。在那之前 ``manager.get_task_status`` 一片空,
``/api/diagnosis/session/{sid}/status`` 回 ``{"found": false}``,
进度页据此挂红色横幅「未找到此诊断任务。可能已过期或无权查看,请返回重新发起。」

她**刚付过钱**。而"重新发起"意味着再冻一笔 —— 这是资金相邻的误导,
不是普通文案问题。

legacy 发起路径十几个月前就用同一手(``server.py`` 注释原话:
「防前端 /status 拿到 found:False」),防御链这一侧一直没有。

判据形态
--------
**真 PG × 真 HTTP confirm × 真 /status 端点函数**。

为什么不能只断言 ``manager.get_task_status(sid) is not None``:那证明的是
"我写了一格快照",证明不了"用户那一发 /status 会因此拿到 found:true" ——
中间还隔着归属校验(``_ws_authorize_session``)与 ``{"found": True, **status}``
那一层。所以最后一跳打的是**端点函数本身**,归属也**不 monkeypatch**:
它按真 ``diagnosis_runs.session_id`` 查,查得到才放行。

每条正向断言都配一条**必须不命中**:
  · confirm 之前同一把 sid 必须 ``found:false``(否则"之后 found:true"是恒真);
  · 同一租户、同样授权、但**没写过快照**的另一把 sid 必须 ``found:false``
    (否则端点可能是无条件 found:true)。
"""
from __future__ import annotations

import uuid

import psycopg2
import psycopg2.extras
import pytest

from .conftest import confirm_once, redis_down

pytestmark = pytest.mark.integration

TENANT = 8471
BRAND_NAME_PREFIX = "GATE8_"
PLAN_PATH = "/api/defensive-geo/question-plans/preview"
PREVIEW_PATH = "/api/defensive-geo/run-previews"


def _conn(dsn):
    c = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)
    c.autocommit = True
    c.cursor().execute("SET search_path = public")
    return c


def _bind_db(dsn):
    import db.connection as dbconn

    dbconn.DATABASE_URL = dsn
    dbconn._pool = None


def _seed(cur) -> int:
    """一个用户 + 一个品牌 + 一条 0 算力的价目行。

    0 算力是刻意的:本条判据要测的是「跑起来之前那一格快照」,不是计价。
    真实计价要一整套钱包/冻结夹具,与本缺陷无关 —— 那是 wp2 那个包在管的。
    """
    cur.execute(
        "INSERT INTO users (id, username, display_name, password_hash, email, is_active) "
        "VALUES (%s,%s,%s,'x',%s,1) ON CONFLICT (id) DO NOTHING",
        (TENANT, "gate8_owner", "gate8", "gate8@example.com"))
    cur.execute("INSERT INTO brands (name, owner_user_id) VALUES (%s,%s) RETURNING id",
                (BRAND_NAME_PREFIX + uuid.uuid4().hex[:6], TENANT))
    brand_id = int(cur.fetchone()["id"])
    cur.execute(
        "INSERT INTO feature_pricing (feature_code, feature_name, cost_points, "
        "  requires_paid_points, is_active) "
        "VALUES ('geo_diagnosis','GEO 体检',0,false,true) "
        "ON CONFLICT (feature_code) DO UPDATE SET cost_points=0, "
        "  requires_paid_points=false, is_active=true")
    return brand_id


@pytest.fixture()
def env(chain_db, live_redis):
    dsn = chain_db("queued")
    _bind_db(dsn)
    conn = _conn(dsn)
    brand_id = _seed(conn.cursor())

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.defensive_geo_api import router

    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):        # noqa: ANN001
        request.state.user = {"user_id": TENANT, "is_admin": False}
        return await call_next(request)

    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)
    yield {"dsn": dsn, "conn": conn, "brand_id": brand_id, "client": client}
    conn.close()


@pytest.fixture(autouse=True)
def _allow_brand(monkeypatch):
    import auth.brand_access as ba

    monkeypatch.setattr(ba, "require_brand_access", lambda *a, **k: None)


def _headers(idem=None):
    h = {"X-Test-Tenant": str(TENANT)}
    if idem:
        h["Idempotency-Key"] = idem
    return h


def _confirm_once(env):
    """真 HTTP:题单 → preview → confirm。返回 confirm 的 JSON。"""
    client = env["client"]
    plan = client.post(PLAN_PATH, json={
        "clientRequestId": "creq-" + uuid.uuid4().hex[:10],
        "brandId": env["brand_id"], "profileRevisionId": "prof-1",
        "mode": "defensive",
        "questions": [{"text": "这个牌子靠谱吗", "modeSide": "defensive",
                       "familyKey": "identity_check", "brandExposure": "named"}],
    }, headers=_headers())
    assert plan.status_code == 200, "题单没签出来:" + plan.text
    plan = plan.json()

    prev = client.post(PREVIEW_PATH, json={
        "questionPlanId": plan["planId"], "questionPlanRevision": plan["planRevision"],
        "profileRevisionId": "prof-1", "platformKeys": ["deepseek"],
    }, headers=_headers(idem="idem-" + uuid.uuid4().hex[:10]))
    assert prev.status_code == 200, "preview 没签出来:" + prev.text
    prev = prev.json()

    r = client.post(f"{PREVIEW_PATH}/{prev['previewId']}/confirm",
                    json={"expectedHash": prev["canonicalHash"]},
                    headers=_headers(idem="idem-" + uuid.uuid4().hex[:10]))
    assert r.status_code == 200, "confirm 没成功:" + r.text
    return r.json()


class _Req:
    """够 ``get_diagnosis_session_status`` 用的最小 Request 替身。

    只喂它真正读的三样:``state.user`` / ``state.organization_identity`` /
    ``state.client_brand_ids``。归属**不 monkeypatch** —— 那正是要验的东西。
    """

    def __init__(self, user):
        class _S:
            pass

        self.state = _S()
        self.state.user = user
        self.state.organization_identity = None


def _status(sid, user_id=TENANT, admin=False):
    from server import get_diagnosis_session_status

    return get_diagnosis_session_status(sid, _Req({"user_id": user_id, "is_admin": admin}))


def _calls_to(module_src: str, name: str) -> int:
    """源码里对 ``name`` 的**调用**次数(AST,不是数字符串)。

    数字符串会把 ``from server import mark_session_queued`` 这一行也算进去 ——
    于是"把调用改成 pass"这一发注毒仍然绿(实测踩过)。接线锁要数的是调用点。
    """
    import ast

    n = 0
    for node in ast.walk(ast.parse(module_src)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)                 and node.func.id == name:
            n += 1
    return n


def test_the_write_outlet_is_single(env):
    """接线正样本:legacy 与防御链写的必须是**同一个出口**,而且都真的调了它。

    两边各拼一份 payload 的话,哪天改了措辞只会改一处,而漂掉的那一份没人守。
    """
    import inspect

    import server

    assert callable(getattr(server, "mark_session_queued", None)),         "server.mark_session_queued 不在 —— 单点写出口没了"
    server_src = inspect.getsource(server)
    assert _calls_to(server_src, "mark_session_queued") >= 1,         "server.py 里只有定义没有调用 —— legacy 发起那一处没接上"
    # 单点:payload 只许在函数体里拼一次
    assert server_src.count('"stage": "queued"') == 1,         "queued 那一格的 payload 在 server.py 里出现了不止一次 —— 又拼了第二份"

    from api import defensive_geo_api

    assert _calls_to(inspect.getsource(defensive_geo_api), "mark_session_queued") >= 1,         "防御链 confirm 没有**调用**这个写出口(只 import 不调用 = 没接线)"


def test_status_is_found_false_when_there_is_no_run_at_all(env):
    """配对的必须不命中(其一):端点**不是**恒真 found:true。

    🔴 [P2-RUNTIME-1 重锚 2026-08-31] 这条原来是「授权过得去但没写过快照的 sid ⇒
       found:false」,用一条 `finished_at IS NULL` 的 run 行做夹具。
       P2-RUNTIME-1 的 L3 之后那种情形**应该**回 `found:true`+降级文案
       (run 还活着却告诉她"找不到",正是门八第三发现要治的病)。
       所以旧断言站到了被修的那一侧 —— 判据跟着被测行为走,不跟着记忆走。

       重锚成:**连 run 行都没有**。这才是"真的不存在"。
       归属用 admin(`authorize_session` 对 admin 直接放行)——否则未知 sid 会先 403,
       那测的是鉴权,不是"存在性"。
    """
    sid = "defgeo_run_" + uuid.uuid4().hex[:10]
    with env["conn"].cursor() as cur:
        cur.execute("SELECT count(*) AS c FROM diagnosis_runs WHERE session_id=%s", (sid,))
        assert cur.fetchone()["c"] == 0, "夹具前提坏了:这个 sid 竟然已经有 run 行"
    out = _status(sid, admin=True)
    assert out["found"] is False, f"什么都没有却拿到了 found:true —— 端点恒真:{out}"


def test_confirm_is_immediately_visible_with_redis_up(env):
    """🔴 G15 主锁 · 臂一(Redis 在):confirm 200 之后**立刻**那一发 /status 就能看到。

    [P2-RUNTIME-1 重锚 2026-08-31] 原来这条断言的是「`manager.get_task_status(sid)`
    非空且 stage=queued」—— 而本机 `_REDIS_HOST="redis"` 是 docker 网名、宿主解析不到,
    于是它**只跑过进程内兜底那条路**,而那正是 P2-RUNTIME-1 说不安全的路。
    重锚成两条:断言**端点给用户的答案**,并把 Redis 在/不在分成两臂各测一次。
    """
    from server import manager

    d = _confirm_once(env)
    sid = d["progressSessionId"]
    assert sid and sid != d["runId"],         f"progressSessionId 必须是另一把键(defgeo_ 前缀):{sid} vs {d['runId']}"

    # 跨进程权威:Redis 里真的有(而不是只在本进程内存里)
    import cache.progress_bus as pb

    assert pb.get_snapshot_sync(sid) is not None,         "Redis 里没有 —— 那这次可见只是本进程兜底,换个 worker 就看不到了"
    # 🔴 防御链**不留**进程内兜底(它不是推进这次 run 的进程)
    assert manager._local_status.get(sid) is None,         "web 进程给一个 cron 才会推进的 session 留了兜底(P2-RUNTIME-1)"

    out = _status(sid)
    assert out["found"] is True, f"端点仍然说找不到:{out}"
    assert out.get("stage") == "queued", f"端点回的不是 queued:{out}"
    assert out.get("done") is False, f"端点把它当成终态了:{out}"


def test_confirm_is_never_answered_not_found_with_redis_down(env):
    """🔴 G15 主锁 · 臂二(Redis 不在):她**仍然不会**被告知"未找到"。

    这一臂才是 G15 真正要的那句保证:不是"快照存在",而是**她不会看到
    「未找到此诊断任务…请返回重新发起」**——她刚付过钱,那句话把她推去再冻一笔。
    Redis 挂着时保证由权威 run 行兑现(P2-RUNTIME-1 的第三支),不由快照兑现。
    """
    from services.defensive_geo.copy_registry import user_label

    redis_down()
    r = confirm_once(env["client"], env["brand_id"], TENANT)
    assert r.status_code == 200, "Redis 挂了把一次 confirm 翻成了错误:" + r.text
    sid = r.json()["progressSessionId"]

    out = _status(sid)
    assert out["found"] is not False, f"Redis 挂着就告诉她找不到 —— G15 那条保证没了:{out}"
    assert out.get("message") == user_label("reason", "progress_channel_degraded"),         f"那句话必须来自 registry(对客文案 SSOT):{out.get('message')!r}"


def test_the_key_written_is_the_one_the_endpoint_checks(env):
    """配对的必须不命中(其二):写进去的键就是端点校验的那把。

    拿 runId(run_token)去查必须**不是** found:true —— 门八第二发现那条缺陷的
    库层对照:两把键不相等,归属查询一条都命中不到。
    """
    d = _confirm_once(env)
    with pytest.raises(Exception) as exc:
        _status(d["runId"])
    # 归属 fail-closed:不存在的 session_id ⇒ 403(不是 found:false,也不是 500)
    assert getattr(exc.value, "status_code", None) == 403, \
        f"拿 run_token 去查竟然不是 403:{exc.value!r}"
