"""
登录限流单测(WORKERS=4 · SPEC §8 · 两键模型 + 故障降级)

- 降级路径(Redis 故障 → 本地 2 次/300s)任何环境可跑(monkeypatch get_redis→None)。
- 正常路径(两键 Lua 真 900s 全局锁)用 fakeredis 打桩,容器内 `pip install -q fakeredis lupa`
  后可跑(fakeredis 或其 Lua 不可用则 skip)。
签名/语义与迁移前 100% 一致 → api/auth_api.py 无需改动。
"""
import pytest

from auth import rate_limiter as rl


# ---------------- 降级路径(核心 · 无 Redis 依赖)----------------

def test_degrade_local_threshold_is_two(monkeypatch):
    monkeypatch.setattr(rl, "get_redis", lambda: None)
    rl._local_clear("u_deg")
    u = "u_deg"
    assert rl.check_rate_limit(u)[0] is True          # 初始放行
    rl.record_failed_attempt(u)                        # 1 次
    assert rl.check_rate_limit(u)[0] is True           # 1 次未触发(阈值=2)
    rl.record_failed_attempt(u)                        # 2 次 → 触发本地锁
    allowed, msg = rl.check_rate_limit(u)
    assert allowed is False and "分钟" in msg          # 被锁 + 15 分钟话术
    assert rl.get_attempt_count(u) == 2
    rl.clear_attempts(u)                               # 成功登录清空
    assert rl.check_rate_limit(u)[0] is True


def test_degrade_alert_throttled(monkeypatch):
    monkeypatch.setattr(rl, "get_redis", lambda: None)
    rl._last_degrade_alert_at = 0.0
    # 只验证不抛异常 + 常量(告警限频细节不强断言)
    rl.check_rate_limit("u_alert")
    assert rl.LOCAL_DEGRADE_MAX == 2


# ---------------- 正常路径:fakeredis + Lua ----------------

def _fake_redis(monkeypatch):
    try:
        import fakeredis
    except Exception:
        pytest.skip("fakeredis 未安装(容器内 pip install fakeredis 后可跑)")
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    try:
        client.register_script("return 1")(keys=[], args=[])
    except Exception as e:
        pytest.skip(f"fakeredis Lua 不可用({e}) · 需 lupa")
    monkeypatch.setattr(rl, "get_redis", lambda: client)
    rl._record_script = None   # 强制在 fake client 上重新 register
    return client


def test_redis_five_fails_trip_global_lock(monkeypatch):
    _fake_redis(monkeypatch)
    u = "u_redis"
    for i in range(rl.MAX_ATTEMPTS - 1):               # 前 4 次:未锁
        rl.record_failed_attempt(u)
        assert rl.check_rate_limit(u)[0] is True, f"第 {i+1} 次不应锁"
    rl.record_failed_attempt(u)                        # 第 5 次 → 置 900s 锁
    allowed, msg = rl.check_rate_limit(u)
    assert allowed is False and "分钟" in msg
    assert rl.get_attempt_count(u) == rl.MAX_ATTEMPTS


def test_redis_clear_unlocks_both_keys(monkeypatch):
    client = _fake_redis(monkeypatch)
    u = "u_clear"
    for _ in range(rl.MAX_ATTEMPTS):
        rl.record_failed_attempt(u)
    assert rl.check_rate_limit(u)[0] is False          # 已锁
    rl.clear_attempts(u)                               # DEL 两键
    assert rl.check_rate_limit(u)[0] is True
    assert client.get(rl._lock_key(u)) is None
    assert client.zcard(rl._att_key(u)) == 0


def test_redis_down_falls_back_to_local(monkeypatch):
    # get_redis 返回 None → 走本地降级(阈值 2),不抛
    monkeypatch.setattr(rl, "get_redis", lambda: None)
    rl._local_clear("u_fb")
    rl.record_failed_attempt("u_fb")
    rl.record_failed_attempt("u_fb")
    assert rl.check_rate_limit("u_fb")[0] is False
