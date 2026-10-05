"""【P2-RUNTIME-1】Redis 失联时,陈旧的进程内 queued 不许遮住权威。

被测缺陷(Codex fof8 §7.4 报,机制我自己复核过)
-----------------------------------------------
`ConnectionManager._set_status` 原来**无条件**先写 `_local_status` 再调 Redis,
而 `set_snapshot_sync` 把守卫 Lua 的 accepted/rejected **整个丢掉**(只报"Redis 可达")。
cron 进程推进只更新 Redis;web 进程那份 `queued` 没有任何写者会更新它。
Redis 一失联,`get_task_status` 回退到那份 queued,端点 `if status:` 直接早返 ⇒
权威 DB(终态 / 还活着的 run)整个走不到 ⇒ **用户可能永远看到「正在初始化」**。

🔴 这个前提是我自己的 G15 引进来的:防御体检 confirm 跑在 web 进程,真正推进它的是
   cron 进程。legacy 不受影响(它 `create_task` 在同一进程,本地会被推前)。

判据形态
--------
**真 Redis(宿主可连)× 真 PG × 真两进程**。

· "cron 进程"是**真的另一个 python 进程**(`subprocess` 跑仓库自己的 `cache.progress_bus`)
  —— 要证的正是"另一个进程推进时,本进程的本地兜底不会跟着动"。
· "Redis 失联"用**真连接失败**模拟(指到死端口 + 重置客户端),
  **不 monkeypatch `_get_sync_redis`**:那样会把"只有这一扇门"变成我的假设而不是判据
  (见 `test_redis_has_exactly_one_door`)。
· 🔴 **本文件不许 import fakeredis**。容器镜像里 fakeredis/lupa 都缺,
  一旦沾上,进 18 包分母后会以「importorskip ⇒ 整包 skip」的形状消失在六数里。
  真 Redis 连不上就 `pytest.fail`(判据不可用),**不 skip**。
"""
from __future__ import annotations

import asyncio
import ast
import inspect
import json
import logging
import os
import pathlib
import subprocess
import sys
import time
import uuid


import psycopg2
import psycopg2.extras
import pytest

from .conftest import (
    confirm_once, make_client, redis_down, redis_target, redis_up,
    seed_owner_brand_pricing,
)

pytestmark = pytest.mark.integration

REPO = pathlib.Path(__file__).resolve().parents[2]

TENANT = 8481
OTHER = 8482


def _bind_db(dsn):
    import db.connection as dbconn

    dbconn.DATABASE_URL = dsn
    dbconn._pool = None


def _conn(dsn):
    c = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)
    c.autocommit = True
    c.cursor().execute("SET search_path = public")
    return c


@pytest.fixture()
def env(chain_db, live_redis):
    dsn = chain_db("p2rt1")
    _bind_db(dsn)
    conn = _conn(dsn)
    with conn.cursor() as cur:
        brand_id = seed_owner_brand_pricing(cur, TENANT, "p2rt1")
        cur.execute(
            "INSERT INTO users (id, username, display_name, password_hash, email, is_active) "
            "VALUES (%s,%s,%s,'x',%s,1) ON CONFLICT (id) DO NOTHING",
            (OTHER, "p2rt1_other", "other", "p2rt1o@example.com"))
    yield {"dsn": dsn, "conn": conn, "brand_id": brand_id,
           "client": make_client(TENANT)}
    conn.close()


def _new_run(env, *, alive: bool) -> str:
    """库里造一条真 run 行(归属 = TENANT)。`alive=True` ⇒ `finished_at IS NULL`。"""
    sid = "defgeo_run_" + uuid.uuid4().hex[:10]
    with env["conn"].cursor() as cur:
        cur.execute(
            # freeze_task_ref 是 NOT NULL(生产 dump 实核)
            "INSERT INTO diagnosis_runs (run_token, session_id, owner_user_id, brand_id,"
            # freeze_id/freeze_backend 不是装饰:`chk_freeze_handle` 要求 paid 行进
            # running 前必须持有句柄(库级不变式)。少给这两列夹具当场 CheckViolation。
            " client_request_id, billing_mode, run_status, freeze_task_ref, finished_at,"
            " freeze_id, freeze_backend) "
            "VALUES (%s,%s,%s,%s,%s,'paid',%s,%s,%s,1,'legacy')",
            (sid.replace("defgeo_", ""), sid, TENANT, env["brand_id"],
             "creq-" + uuid.uuid4().hex[:8],
             "running" if alive else "committed",
             "diag_" + sid, None if alive else "2026-01-01 00:00:00"))
    return sid


class _Req:
    def __init__(self, user):
        class _S:
            pass

        self.state = _S()
        self.state.user = user
        self.state.organization_identity = None


def _status(sid, user_id=TENANT):
    from server import get_diagnosis_session_status

    return get_diagnosis_session_status(sid, _Req({"user_id": user_id, "is_admin": False}))


def _cron_process_publish(sid, payload):
    """**另一个真进程**用仓库自己的 progress_bus 把进度写进 Redis。"""
    code = (
        "import asyncio,sys,json\n"
        "sys.path.insert(0, r'%s')\n"
        "import cache.progress_bus as pb\n"
        "pb._REDIS_HOST=%r; pb._REDIS_PORT=%d\n"
        "pb._sync_redis=None; pb._async_redis=None\n"
        "seq=asyncio.run(pb.publish(%r, json.loads(%r)))\n"
        "print('SEQ=%%s' %% seq)\n"
    ) % (str(REPO), redis_target().host, redis_target().port, sid, json.dumps(payload))
    out = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True, timeout=120)
    assert "SEQ=" in out.stdout, "cron 进程没写成功:%s\n%s" % (out.stdout, out.stderr)
    assert "SEQ=None" not in out.stdout, "cron 进程连不上 Redis(SEQ=None)—— 判据不可用"
    return out.stdout.strip()


# ══════════════════════════════════════════════════════════════════════
# 判据可用性:先证判据本身能用
# ══════════════════════════════════════════════════════════════════════
def test_redis_has_exactly_one_door(live_redis):
    """`_get_sync_redis()` 是同步读写**唯一**的门 —— 所以"指死端口"确实等于"失联"。

    不是废话:哪天有人在 `get_snapshot_sync` 里另开一条连接,我的失联模拟就只关了半扇门,
    而下面几条会因此**假绿**。
    """
    import cache.progress_bus as pb

    for fn in (pb.get_snapshot_sync, pb.set_snapshot_sync):
        calls = {n.func.id for n in ast.walk(ast.parse(inspect.getsource(fn)))
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "_get_sync_redis" in calls, \
            "%s 不再走 _get_sync_redis —— 失联模拟已失效" % fn.__name__


def test_tristate_values_are_distinguishable(live_redis):
    """C3:三态**三种都真的构造得出来**,且两两不等。

    只断"两个常量不相等"是空的:那只证明我写了两个不同的字面量。
    """
    import cache.progress_bus as pb

    sid = "p2rt1_tri_" + uuid.uuid4().hex[:8]
    accepted = pb.set_snapshot_sync(sid, {"stage": "queued", "progress": 0})
    assert accepted == pb.SYNC_SET_ACCEPTED, "第一次写应被接受,实得 %r" % (accepted,)

    asyncio.run(pb.publish(sid, {"stage": "collecting", "progress": 40}))
    rejected = pb.set_snapshot_sync(sid, {"stage": "queued", "progress": 0})
    assert rejected == pb.SYNC_SET_REJECTED, \
        "publish 接管后迟到的 queued 必须被守卫拒,实得 %r" % (rejected,)

    redis_down()
    unavailable = pb.set_snapshot_sync(sid, {"stage": "queued", "progress": 0})
    assert unavailable is None, "Redis 不可达必须回 None,实得 %r" % (unavailable,)

    assert pb.SYNC_SET_ACCEPTED != pb.SYNC_SET_REJECTED


# ══════════════════════════════════════════════════════════════════════
# C2:被守卫拒绝 ⇒ 不许写进程内兜底
# ══════════════════════════════════════════════════════════════════════
def test_rejected_write_does_not_poison_the_local_fallback(live_redis):
    """C2:Redis 说"我手上有更新的",本进程就没资格把 queued 留在兜底里。

    改前:`_set_status` 先无条件写本地再问 Redis ⇒ 被拒的 queued 照样落进兜底,
    Redis 一失联它就是用户看到的唯一答案。
    """
    import cache.progress_bus as pb
    from server import manager

    sid = "p2rt1_rej_" + uuid.uuid4().hex[:8]
    asyncio.run(pb.publish(sid, {"stage": "collecting", "progress": 40}))   # publish 接管
    manager._set_status(sid, {"type": "progress", "stage": "queued",
                              "progress": 0, "done": False})
    local = manager._local_status.get(sid)
    assert local is None or local.get("stage") != "queued", \
        "被守卫拒的 queued 落进了进程内兜底:%r" % (local,)


# ══════════════════════════════════════════════════════════════════════
# C1a:防御链真形态 —— web 写 queued,cron **另一个进程**推进,Redis 失联
# ══════════════════════════════════════════════════════════════════════
def test_defensive_queued_never_becomes_a_stale_local_answer(env):
    """C1a:`driven_in_process=False` ⇒ 本进程压根不留兜底 ⇒ 没有可陈旧的东西。

    这是修在**源头**的那一刀:web 进程不再为一个自己不会推进的 session 写兜底。
    """
    from server import manager, mark_session_queued

    sid = _new_run(env, alive=True)
    mark_session_queued(sid, driven_in_process=False)

    assert manager._local_status.get(sid) is None, \
        "web 进程给一个 cron 才会推进的 session 留了兜底 —— Redis 一失联它就永远是 queued"
    # Redis 里**有**(跨进程权威),所以正常态下用户看得到 queued
    assert (manager.get_task_status(sid) or {}).get("stage") == "queued", \
        "Redis 里也没写 —— G15 那条保证被改没了"

    _cron_process_publish(sid, {"stage": "collecting", "progress": 40, "message": "正在问问题"})
    redis_down()

    out = _status(sid)
    assert out.get("stage") != "queued", \
        "Redis 失联后又回到了 queued —— 用户会永远看到「正在初始化」:%r" % (out,)
    assert out.get("found") is True, "run 明明活着,不该回 found:false:%r" % (out,)


# ══════════════════════════════════════════════════════════════════════
# C1b:陈旧的本地兜底(legacy 形态也可能出现)不许遮住权威
# ══════════════════════════════════════════════════════════════════════
def test_expired_local_snapshot_stops_shadowing_the_authority(env):
    """C1b:本地兜底**超过信任时长**之后就不许再早返。

    C1a 只证"防御链不再制造陈旧兜底";这条证"万一别处还有陈旧兜底(legacy 进程卡死、
    多 worker 残留),端点也不会被它挡住"。时间戳直接推老,不 sleep 120 秒。
    """
    from server import LOCAL_SNAPSHOT_TRUST_SECONDS, manager, mark_session_queued

    sid = _new_run(env, alive=True)
    mark_session_queued(sid, driven_in_process=True)      # legacy 形态:留兜底
    assert manager._local_status.get(sid) is not None, "兜底没写进去,后面是空断言"

    redis_down()
    # 还新鲜 ⇒ 允许早返(不改这一格的行为)
    assert manager.snapshot_is_authoritative(sid) is True
    assert _status(sid).get("stage") == "queued"

    # 推老到信任窗口之外
    manager._local_status_at[sid] = time.time() - (LOCAL_SNAPSHOT_TRUST_SECONDS + 30)
    assert manager.snapshot_is_authoritative(sid) is False
    out = _status(sid)
    assert out.get("stage") != "queued", \
        "陈旧兜底仍然遮住了权威 DB:%r" % (out,)
    assert out.get("found") is True, "run 还活着,不该回 found:false:%r" % (out,)


# ══════════════════════════════════════════════════════════════════════
# C1c:Codex P2-5 的动态复现场景逐值复刻
# ══════════════════════════════════════════════════════════════════════
def test_queued_must_not_roll_back_a_progressed_local_snapshot(env):
    """C1c:本地已经是 `running/progress=37/seq=9` 时,一发 queued **不许**把它盖回去。

    Codex P2-5 的定向反例逐值复刻(它直接调真 `ConnectionManager` / `mark_session_queued`,
    观察到 `local_after.stage == queued`、且 Redis 失联后 `poll_after_bus_loss.stage == queued`,
    **即使调用前本地是 running/progress=37/seq=9**)。

    🔴 我核 C1a/C1b 时发现它们盖不住这一格:
      · C1a 从**空**本地态出发(证的是"防御链压根不写本地");
      · C1b 也从 queued 出发(证的是"陈旧兜底不许早返");
      · C2 的前置进度在 **Redis** 里(救它的是守卫 Lua 的 REJECTED),不在本地。
    而本地单调那条原来只挡"非终态盖终态" —— `running/progress=37` 不是终态,
    于是 queued 照样盖得下去。Redis 若同时不可达(返 None,不是 REJECTED),
    就没有任何东西拦它。**这是真缝,当场补**(见 `_set_status` 的本地 seq 守卫)。
    """
    from server import manager

    sid = _new_run(env, alive=True)
    progressed = {"type": "progress", "stage": "collecting", "progress": 37,
                  "message": "正在问第 3 批问题", "done": False, "seq": 9}
    manager._remember_local(sid, progressed)

    # Redis 不可达 ⇒ set_snapshot_sync 返 None(**不是** REJECTED),守卫 Lua 帮不上忙
    redis_down()
    manager._set_status(sid, {"type": "progress", "stage": "queued",
                              "progress": 0, "done": False}, local_fallback=True)

    local_after = manager._local_status.get(sid)
    assert local_after is not None, "本地态被整个抹掉了"
    assert local_after.get("stage") != "queued", \
        f"queued 把本地的 running/progress=37 盖回去了:{local_after}"
    assert local_after.get("progress") == 37, f"进度被回退:{local_after}"

    out = _status(sid)
    assert out.get("stage") != "queued", \
        f"Redis 失联后 poll 回了 queued —— Codex P2-5 的现象原样复发:{out}"


# ══════════════════════════════════════════════════════════════════════
# C6:Redis 失联 + run 还在跑 ⇒ 不得 found:false
# ══════════════════════════════════════════════════════════════════════
def test_alive_run_never_answers_not_found_when_redis_is_down(env):
    """C6(子审没点名,我加的):**配对的必须不命中**。

    只做"过期 queued 继续查终态"的实现会在这里回 `found:false` —— 而前端拿到
    found:false,退避窗口走完就挂「未找到此诊断任务…请返回重新发起」。
    她刚付过钱,那句话把她推去再冻一笔。门八第三发现治的就是这句话。
    """
    from services.defensive_geo.copy_registry import user_label

    sid = _new_run(env, alive=True)     # run 还活着,库里没有终态,也没写过任何快照
    redis_down()

    out = _status(sid)
    assert out.get("found") is True, "run 还在跑却被告知找不到:%r" % (out,)
    assert out.get("done") is not True, "还没结束就报终态:%r" % (out,)
    assert out.get("message") == user_label("reason", "progress_channel_degraded"), \
        "那句话必须来自 registry(对客文案 SSOT),不是端点现编:%r" % (out.get("message"),)


def test_alive_run_fallback_still_enforces_ownership(env):
    """C5:新开的这条 DB 兜底路径**没有**绕过归属校验(配对的必须不命中)。"""
    from fastapi import HTTPException

    sid = _new_run(env, alive=True)
    redis_down()

    with pytest.raises(HTTPException) as exc:
        _status(sid, user_id=OTHER)
    assert exc.value.status_code == 403, "别人也能读到这条进度:%r" % (exc.value,)


# ══════════════════════════════════════════════════════════════════════
# Review 追加:fail-soft 不许把 bug 吞成静默
# ══════════════════════════════════════════════════════════════════════
def test_confirm_failsoft_is_loud_and_still_commits(env, monkeypatch, caplog):
    """confirm 那个 fail-soft `except` 被触发时:confirm 仍 200,**且留下 warning**。

    起因:`server.py` 原本没有顶层 `import time`,我的时间戳会抛 `NameError`,
    而它正好落在这个 except 里 ⇒ 会静默退化成"根本不写快照",G15 无声回归。
    fail-soft 是对的(不许把已提交已冻结的 confirm 翻成 500),
    但它不能连"出事了"这件事也一起吞掉。
    """
    import server as _server

    def _boom(*a, **k):
        raise RuntimeError("p2rt1-injected")

    monkeypatch.setattr(_server, "mark_session_queued", _boom)

    with caplog.at_level(logging.WARNING):
        r = confirm_once(env["client"], env["brand_id"], TENANT)

    assert r.status_code == 200, "快照写失败把一次已提交已冻结的 confirm 翻成了错误:" + r.text
    warned = [rec for rec in caplog.records
              if rec.levelno >= logging.WARNING and "queued" in rec.getMessage()]
    assert warned, ("fail-soft 分支吞掉了异常且**没有留下任何 warning** —— "
                    "下次谁在里面埋个 NameError,没有任何信号会告诉我们")
