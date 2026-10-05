"""
B2 选主锁单测(WORKERS=4 · SPEC §2.1/D3)· 全仓第一处 Lua

- fail-CLOSED(Redis 故障 → acquire None)任何环境可跑(monkeypatch get_redis→None)。
- Lua 语义(三键原子获取 / owner-CAS 续租/释放 / epoch INCR / D3 互斥)用 fakeredis 打桩,
  容器内 `pip install -q fakeredis` 后可跑(fakeredis 或其 Lua 不可用则 skip)。
"""
import pytest

from cache import leader_lock as ll


# ---------------- fail-CLOSED(核心 · 无 Redis 依赖)----------------

def test_acquire_fail_closed_when_redis_down(monkeypatch):
    monkeypatch.setattr(ll, "get_redis", lambda: None)
    lock = ll.LeaderLock(ttl_seconds=60)
    assert lock.acquire() is None          # 现 acquire_lock fail-OPEN;这里刻意 fail-CLOSED
    assert lock.is_leader() is False


def test_renew_fail_closed_when_redis_down(monkeypatch):
    lock = ll.LeaderLock(ttl_seconds=60)
    lock.token = "tok"                      # 假装曾是 leader
    monkeypatch.setattr(ll, "get_redis", lambda: None)
    assert lock.renew() is False            # Redis 不可用 → 视为丢租(停止派发)


def test_read_epoch_none_when_redis_down(monkeypatch):
    monkeypatch.setattr(ll, "get_redis", lambda: None)
    assert ll.read_epoch() is None          # job fencing 拿不到 epoch → 只能靠 DB 幂等键(§2.2)


# ---------------- Lua 语义:fakeredis 打桩 ----------------

def _fake_redis(monkeypatch):
    try:
        import fakeredis
    except Exception:
        pytest.skip("fakeredis 未安装(容器内 pip install fakeredis 后可跑)")
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    # 探测 Lua 支持(fakeredis 需 lupa)
    try:
        client.register_script("return 1")(keys=[], args=[])
    except Exception as e:
        pytest.skip(f"fakeredis Lua 不可用({e}) · 需 lupa")
    monkeypatch.setattr(ll, "get_redis", lambda: client)
    return client


def test_acquire_holds_three_keys_and_bumps_epoch(monkeypatch):
    client = _fake_redis(monkeypatch)
    lock = ll.LeaderLock(ttl_seconds=60)
    epoch = lock.acquire()
    assert epoch == 1                                  # 首次任期 epoch=1
    assert lock.is_leader() is True
    for k in ll.DEFAULT_LEADER_KEYS:                   # 三把键都被本 token 持有
        assert client.get(k) == lock.token
    assert ll.read_epoch(client) == 1


def test_second_leader_blocked_then_succeeds_after_release(monkeypatch):
    client = _fake_redis(monkeypatch)
    a = ll.LeaderLock(ttl_seconds=60)
    b = ll.LeaderLock(ttl_seconds=60)
    assert a.acquire() == 1
    assert b.acquire() is None                         # 互斥:三键被 a 持有 → b 拿不到
    assert a.renew() is True                           # a 仍是 owner
    a.release()
    for k in ll.DEFAULT_LEADER_KEYS:
        assert client.get(k) is None                   # owner-CAS 释放三键
    assert b.acquire() == 2                             # 让位后 b 成为 leader · epoch 递增到 2


def test_d3_mutual_exclusion_old_scheduler_lock_blocks_new(monkeypatch):
    """D3 回滚互斥:旧 web-scheduler 用 SET NX 抢 scheduler:lock → 新 cron 三键获取失败。"""
    client = _fake_redis(monkeypatch)
    # 模拟旧代码 acquire_lock("scheduler:lock") = SET NX(值=holder)
    client.set("scheduler:lock", "old-web-worker-44", nx=True, ex=60)
    lock = ll.LeaderLock(ttl_seconds=60)
    assert lock.acquire() is None                      # 任一键被他人持有 → all-or-nothing 失败
    # 旧键仍归旧持有者 · 未被新 token 覆盖
    assert client.get("scheduler:lock") == "old-web-worker-44"
    assert client.get("sched:leader") is None


def test_renew_returns_false_after_ownership_lost(monkeypatch):
    client = _fake_redis(monkeypatch)
    lock = ll.LeaderLock(ttl_seconds=60)
    assert lock.acquire() == 1
    # 模拟一把键被别人夺走(过期后他人 SET)
    client.set("research-monitor-scheduler:lock", "someone-else")
    assert lock.renew() is False                       # 丢租 → 放弃 leadership
    assert lock.is_leader() is False
