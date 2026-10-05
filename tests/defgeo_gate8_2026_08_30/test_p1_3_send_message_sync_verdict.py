"""【fof8 P1-3】同步守卫说"拒",`send_message` 必须三处零增量。

被测缺陷(Codex fof8 §2 P1-3,坐标 `server.py:2487-2511`)
--------------------------------------------------------
异步 `publish()` 返 `None`(总线不可用)时,代码构造本地 seq 并调
`set_snapshot_sync()`,**却把它的三态返回值整个丢掉**;即使返回 `SYNC_SET_REJECTED`,
仍然 `_remember_local()` 并 `dispatch()`。而 `:2495` 更是在**同步裁定之前**
就先写了一次本地。

后果:Redis 的同步守卫已经判定"这条事件陈旧/乱序"(它手上有更新的 seq'd 快照),
生产路径还是把它当本地真相**存下来并推给 WS** —— 终态页可能倒退回"处理中"。

判据形态
--------
**真 Redis · 真 Lua 裁定**。`SYNC_SET_REJECTED` 不是打桩打出来的:
先真发一条 `publish`(Redis 里于是有 seq'd 快照),再发一条**非终态**直投 ——
守卫 Lua 按它自己的规则拒。**唯一被模拟的是"异步总线不可用"**
(`publish` 返 None),那是真实环境条件,而不是被测判定本身。

三处零增量各一条,外加**配对的必须不命中**:同一条路径在 `ACCEPTED` 时
三处都必须增量 —— 否则"什么都不做"也能让上面三条全绿。
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration


class _Spy:
    """dispatch 观察点。记下每一次投递。"""

    def __init__(self):
        self.calls = []

    async def __call__(self, session_id, message):
        self.calls.append((session_id, dict(message)))


async def _publish_none(*a, **k):
    """模拟异步总线不可用(断路器打开 / async 客户端坏)—— 真实可达的环境条件。"""
    return None


def _snapshot_three(manager, sid):
    local = manager._local_status.get(sid)
    return (None if local is None else dict(local),
            manager._local_seq.get(sid),
            None)


@pytest.fixture()
def bus(live_redis, monkeypatch):
    """真 Redis + 真 manager;dispatch 换成观察点;异步 publish 置为不可用。"""
    import cache.progress_bus as pb
    from server import manager

    spy = _Spy()
    monkeypatch.setattr(manager, "dispatch", spy)
    return {"pb": pb, "manager": manager, "spy": spy, "monkeypatch": monkeypatch}


@pytest.mark.asyncio
async def test_sync_rejected_leaves_cache_queue_and_dispatch_untouched(bus):
    """🔴 主锁:`publish=None` × 同步守卫 `REJECTED` ⇒ 缓存 / 队列 / dispatch **三处零增量**。"""
    pb, manager, spy, mp = bus["pb"], bus["manager"], bus["spy"], bus["monkeypatch"]
    sid = "p13_rej_" + uuid.uuid4().hex[:8]

    # ① 先真发一条 —— Redis 里于是有 seq'd 快照,后面那条非终态直投会被守卫**真的**拒
    seq = await pb.publish(sid, {"type": "progress", "stage": "collecting", "progress": 40})
    assert seq and seq > 0, "前置 publish 没成功,后面拒不掉 —— 判据不可用:%r" % (seq,)
    probe = pb.set_snapshot_sync(sid, {"type": "progress", "progress": 1})
    assert probe == pb.SYNC_SET_REJECTED, \
        "守卫没拒 —— 这一幕没造出来,后面三条会是零分母:%r" % (probe,)

    # ② 现在把异步总线打成不可用,走进那条"本地兜底"分支
    mp.setattr(pb, "publish", _publish_none)
    before_local, before_seq, _ = _snapshot_three(manager, sid)
    before_dispatch = len(spy.calls)

    await manager.send_message(sid, {"type": "progress", "stage": "collecting", "progress": 1})

    after_local, after_seq, _ = _snapshot_three(manager, sid)
    assert after_local == before_local, \
        "缓存被陈旧事件改了(守卫已经拒过它):%r -> %r" % (before_local, after_local)
    assert after_seq == before_seq, \
        "本地 seq 队列被推进了:%r -> %r" % (before_seq, after_seq)
    assert len(spy.calls) == before_dispatch, \
        "被守卫拒的事件仍然投给了 WS:%r" % (spy.calls[before_dispatch:],)


@pytest.mark.asyncio
async def test_sync_accepted_still_updates_all_three(bus):
    """配对的必须不命中:同一条路径在 `ACCEPTED` 时三处都必须增量。

    没有这条,把 `send_message` 写成"总线不可用就直接 return"也能让主锁全绿 ——
    那是把兜底整个删掉,不是修守卫。
    """
    pb, manager, spy, mp = bus["pb"], bus["manager"], bus["spy"], bus["monkeypatch"]
    sid = "p13_acc_" + uuid.uuid4().hex[:8]          # 全新 sid:Redis 里没有 seq'd 快照

    mp.setattr(pb, "publish", _publish_none)
    before_local, before_seq, _ = _snapshot_three(manager, sid)
    before_dispatch = len(spy.calls)

    await manager.send_message(sid, {"type": "progress", "stage": "collecting", "progress": 7})

    after_local, after_seq, _ = _snapshot_three(manager, sid)
    assert after_local is not None and after_local != before_local, \
        "总线不可用 + 守卫接受时本地兜底没写 —— 兜底被删掉了,不是修好了"
    assert after_seq == (before_seq or 0) + 1, \
        "本地 seq 没推进:%r -> %r" % (before_seq, after_seq)
    assert len(spy.calls) == before_dispatch + 1, "接受的事件没投出去"
    assert spy.calls[-1][1].get("seq") == after_seq, \
        "投出去那份的 seq 与本地队列对不上:%r" % (spy.calls[-1][1],)


@pytest.mark.asyncio
async def test_nothing_is_written_before_the_sync_verdict(bus):
    """`:2495` 那次**过早**的本地写:裁定出来之前一个字都不许落。

    与主锁分开是因为它们抓的是**两个**动作:主锁抓"拒了还留",这条抓"还没问就写"。
    观察点:在 `set_snapshot_sync` 被调用的**那一刻**,本地必须仍是原样。
    """
    pb, manager, spy, mp = bus["pb"], bus["manager"], bus["spy"], bus["monkeypatch"]
    sid = "p13_early_" + uuid.uuid4().hex[:8]

    seq = await pb.publish(sid, {"type": "progress", "stage": "collecting", "progress": 40})
    assert seq and seq > 0, "前置 publish 没成功 —— 判据不可用"
    baseline = dict(manager._local_status.get(sid) or {})

    seen = {}
    real_sync = pb.set_snapshot_sync

    def _watching_sync(session_id, msg):
        # 裁定发生的那一刻,本地应当还没被这条消息动过
        seen["local_at_verdict"] = dict(manager._local_status.get(session_id) or {})
        return real_sync(session_id, msg)

    mp.setattr(pb, "publish", _publish_none)
    mp.setattr(pb, "set_snapshot_sync", _watching_sync)

    await manager.send_message(sid, {"type": "progress", "stage": "collecting", "progress": 1})

    assert "local_at_verdict" in seen, "同步守卫压根没被调用 —— 判据不可用(零分母)"
    assert seen["local_at_verdict"] == baseline, \
        "同步裁定之前就把消息写进本地了:%r(裁定前应当仍是 %r)" % (
            seen["local_at_verdict"], baseline)
