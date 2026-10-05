"""
§4 诊断并发闸判别测试(WORKERS=4 · cache/diagnosis_slots.py)
============================================================
- redis_down(get_redis→None)任何环境可跑;
- Lua 语义(清过期 + ZCARD + ZADD · 幂等重入 · cap 强制 · 心跳续 · release · count)用 fakeredis 打桩
  (fakeredis 或其 Lua[lupa] 不可用则 skip)。
"""
import pytest

import cache.diagnosis_slots as slots


def _fresh():
    slots._reset_for_test()


# ---------------- redis_down(任何环境)----------------
def test_acquire_redis_down(monkeypatch):
    _fresh()
    monkeypatch.setattr("cache.redis_client.get_redis", lambda: None)
    r = slots.acquire_slot("run_a")
    assert r.redis_down is True and r.acquired is False


def test_renew_redis_down_returns_none(monkeypatch):
    _fresh()
    monkeypatch.setattr("cache.redis_client.get_redis", lambda: None)
    assert slots.renew_slot("run_a") is None


def test_count_redis_down_returns_none(monkeypatch):
    _fresh()
    monkeypatch.setattr("cache.redis_client.get_redis", lambda: None)
    assert slots.count_slots() is None


# ---------------- Lua 语义:fakeredis 打桩 ----------------
def _client_or_skip():
    try:
        import fakeredis
    except Exception:
        pytest.skip("fakeredis 未安装(容器内 pip install fakeredis 后可跑)")
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    try:
        client.register_script("return 1")(keys=[], args=[])
    except Exception as e:
        pytest.skip(f"fakeredis Lua 不可用({e}) · 需 lupa")
    return client


def _use(monkeypatch, client):
    _fresh()
    monkeypatch.setattr("cache.redis_client.get_redis", lambda: client)


def test_acquire_until_cap_then_reject(monkeypatch):
    monkeypatch.setenv("DIAGNOSIS_CONCURRENCY_CAP", "2")
    c = _client_or_skip()
    _use(monkeypatch, c)
    a = slots.acquire_slot("r1", now=1000.0)
    b = slots.acquire_slot("r2", now=1000.0)
    d = slots.acquire_slot("r3", now=1000.0)     # 第 3 个 · cap=2 → 拒
    assert a.acquired and a.current == 1
    assert b.acquired and b.current == 2
    assert d.acquired is False and d.current == 2 and d.redis_down is False


def test_idempotent_reacquire_same_token_no_double_count(monkeypatch):
    monkeypatch.setenv("DIAGNOSIS_CONCURRENCY_CAP", "2")
    c = _client_or_skip()
    _use(monkeypatch, c)
    slots.acquire_slot("r1", now=1000.0)
    again = slots.acquire_slot("r1", now=1000.0)  # 同 token 重入 · 不双计
    assert again.acquired is True and again.current == 1


def test_release_frees_slot(monkeypatch):
    monkeypatch.setenv("DIAGNOSIS_CONCURRENCY_CAP", "1")
    c = _client_or_skip()
    _use(monkeypatch, c)
    assert slots.acquire_slot("r1", now=1000.0).acquired is True
    assert slots.acquire_slot("r2", now=1000.0).acquired is False   # 满
    assert slots.release_slot("r1") is True
    assert slots.acquire_slot("r2", now=1000.0).acquired is True    # 释放后可入


def test_expired_slot_cleared_on_acquire(monkeypatch):
    monkeypatch.setenv("DIAGNOSIS_CONCURRENCY_CAP", "1")
    monkeypatch.setenv("DIAGNOSIS_SLOT_TTL_SECONDS", "180")
    c = _client_or_skip()
    _use(monkeypatch, c)
    slots.acquire_slot("r1", now=1000.0)                 # 过期时刻 1180
    # cap=1 · r1 未过期 → r2 拒
    assert slots.acquire_slot("r2", now=1100.0).acquired is False
    # now 越过 1180 → r1 过期被清 → r2 入
    got = slots.acquire_slot("r2", now=1200.0)
    assert got.acquired is True and got.current == 1


def test_renew_true_then_false_after_release(monkeypatch):
    c = _client_or_skip()
    _use(monkeypatch, c)
    slots.acquire_slot("r1", now=1000.0)
    assert slots.renew_slot("r1", now=1010.0) is True     # 仍在 → 续
    slots.release_slot("r1")
    assert slots.renew_slot("r1", now=1020.0) is False    # 已释放 → 租约失(任务应自我中止)


def test_renew_false_after_expiry(monkeypatch):
    monkeypatch.setenv("DIAGNOSIS_SLOT_TTL_SECONDS", "180")
    c = _client_or_skip()
    _use(monkeypatch, c)
    slots.acquire_slot("r1", now=1000.0)                 # 过期 1180
    assert slots.renew_slot("r1", now=1300.0) is False   # 已过期被清 → False


def test_count_after_ops(monkeypatch):
    monkeypatch.setenv("DIAGNOSIS_CONCURRENCY_CAP", "8")
    c = _client_or_skip()
    _use(monkeypatch, c)
    slots.acquire_slot("r1", now=1000.0)
    slots.acquire_slot("r2", now=1000.0)
    assert slots.count_slots(now=1000.0) == 2
    slots.release_slot("r1")
    assert slots.count_slots(now=1000.0) == 1
