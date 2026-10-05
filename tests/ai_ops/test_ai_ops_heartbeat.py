"""Runner 心跳(P1-B)测试 · db.ai_ops_db.upsert_heartbeat / get_runner_status + overview.runner。

覆盖四态判定:无心跳→None、新鲜→online、超窗→offline;overview 携带真实 runner。
DB 集成用例声明 clean_ai_ops + 真实测试库(conftest)。
"""
import types

import pytest

import api.ai_ops_api as ai_ops_api
from db import ai_ops_db as aiops_db

ADMIN = {"id": 1, "is_admin": True}


def _req(user):
    return types.SimpleNamespace(state=types.SimpleNamespace(user=user))


# ==========================================
# get_runner_status 三态
# ==========================================

def test_runner_status_none_when_no_heartbeat(clean_ai_ops):
    """从没心跳 → None(前端显示"未接入")。"""
    assert aiops_db.get_runner_status() is None


def test_upsert_then_online(clean_ai_ops):
    aiops_db.upsert_heartbeat(
        "runner-test-1", host="vm-a", version="abc123",
        env_enabled=False, codex_available=True, ssh_runner_enabled=False,
    )
    status = aiops_db.get_runner_status()
    assert status is not None
    assert status["worker_id"] == "runner-test-1"
    assert status["online"] is True          # 刚写入,age≈0,在窗口内
    assert status["env_enabled"] is False     # env 关也心跳:在线但未授权执行
    assert status["codex_available"] is True
    assert status["host"] == "vm-a"


def test_upsert_is_idempotent_single_row(clean_ai_ops):
    """同 worker_id 重复上报只更新同一行,不堆积。"""
    aiops_db.upsert_heartbeat("runner-test-1", env_enabled=False)
    aiops_db.upsert_heartbeat("runner-test-1", env_enabled=True, codex_available=True)
    status = aiops_db.get_runner_status()
    assert status["env_enabled"] is True      # 第二次覆盖
    assert status["codex_available"] is True


def test_stale_heartbeat_is_offline(clean_ai_ops, pg_conn):
    """last_seen_at 超出 stale 窗口 → online=False(离线)。"""
    aiops_db.upsert_heartbeat("runner-test-1", env_enabled=True)
    # 把心跳时间人为拨到 300 秒前
    cur = pg_conn.cursor()
    cur.execute(
        "UPDATE ai_ops_worker_heartbeats SET last_seen_at = NOW() - INTERVAL '300 seconds' "
        "WHERE worker_id = %s",
        ("runner-test-1",),
    )
    pg_conn.commit()
    status = aiops_db.get_runner_status(stale_seconds=120)
    assert status is not None
    assert status["online"] is False
    assert status["age_seconds"] >= 120


def test_latest_heartbeat_wins_with_multiple_runners(clean_ai_ops, pg_conn):
    """多 Runner 时 get_runner_status 取 last_seen_at 最新的一行。"""
    aiops_db.upsert_heartbeat("runner-old", env_enabled=False)
    cur = pg_conn.cursor()
    cur.execute(
        "UPDATE ai_ops_worker_heartbeats SET last_seen_at = NOW() - INTERVAL '600 seconds' "
        "WHERE worker_id = %s",
        ("runner-old",),
    )
    pg_conn.commit()
    aiops_db.upsert_heartbeat("runner-new", env_enabled=True)
    status = aiops_db.get_runner_status()
    assert status["worker_id"] == "runner-new"


# ==========================================
# overview 携带 runner
# ==========================================

@pytest.mark.asyncio
async def test_overview_runner_none_without_heartbeat(clean_ai_ops):
    result = await ai_ops_api.api_overview(_req(ADMIN))
    assert "runner" in result
    assert result["runner"] is None
    assert result["health"]["runner_online"] is False


@pytest.mark.asyncio
async def test_overview_runner_reflects_heartbeat(clean_ai_ops):
    aiops_db.upsert_heartbeat("runner-test-1", env_enabled=False, codex_available=True)
    result = await ai_ops_api.api_overview(_req(ADMIN))
    assert result["runner"] is not None
    assert result["runner"]["worker_id"] == "runner-test-1"
    assert result["runner"]["online"] is True
    assert result["health"]["runner_online"] is True


# ==========================================
# 心跳间隔校验 + daemon 线程(复审 P2/P3)
# ==========================================

def test_heartbeat_interval_default_and_clamp(monkeypatch):
    from services.ai_ops import worker as w
    monkeypatch.delenv("AI_OPS_HEARTBEAT_SECONDS", raising=False)
    assert w._heartbeat_interval() == 45          # 默认
    monkeypatch.setenv("AI_OPS_HEARTBEAT_SECONDS", "abc")
    assert w._heartbeat_interval() == 45          # 非法值不抛异常,回落默认
    monkeypatch.setenv("AI_OPS_HEARTBEAT_SECONDS", "300")
    assert w._heartbeat_interval() == 60          # clamp 上限:interval*2 ≤ stale 窗口 120s
    monkeypatch.setenv("AI_OPS_HEARTBEAT_SECONDS", "1")
    assert w._heartbeat_interval() == 5           # clamp 下限
    monkeypatch.setenv("AI_OPS_HEARTBEAT_SECONDS", "30")
    assert w._heartbeat_interval() == 30          # 合法值原样


def test_heartbeat_thread_fires_immediately(monkeypatch):
    """daemon 心跳线程启动即发第一次(不等 interval),主循环阻塞也不影响它。"""
    import threading as _threading
    from services.ai_ops import worker as w

    fired = _threading.Event()
    monkeypatch.setattr(w, "emit_heartbeat", lambda cfg: fired.set())
    monkeypatch.setenv("AI_OPS_HEARTBEAT_SECONDS", "60")  # interval 拉大,只验首发
    t = w.start_heartbeat_thread(w.WorkerConfig())
    assert t.daemon is True
    assert fired.wait(timeout=5), "心跳线程启动后 5s 内未发出首个心跳"
