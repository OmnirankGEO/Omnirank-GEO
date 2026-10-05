"""
Phase 4 PLAN 01 · geo_plan_tasks 表 DB 层单测

依赖: 真实 DB (docker-compose 跑的 omnirank-db),因为本层就是 SQL 交互.
不 mock,所有 test 自建自清.

10 test 对齐 PLAN 01 Task 1 <behavior>:
  1. test_create_task_happy_path
  2. test_find_running_for_brand_exists
  3. test_find_running_for_brand_none
  4. test_count_running_for_user
  5. test_update_progress_updates_heartbeat
  6. test_mark_status_done_sets_done_at
  7. test_mark_zombie_threshold
  8. test_sweep_server_restart
  9. test_get_task_rbac (用户 A 创建,用户 B 查返 None)
  10. test_list_tasks_filters

运行:
  docker exec omnirank-ai pytest tests/test_geo_plan_tasks_db.py -x -v
"""
from __future__ import annotations

import os
import sys
import time
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import pytest
except ImportError:
    # 兜底 stub (和 test_managed_campaign.py 一致)
    class _PytestStub:
        @staticmethod
        def skip(msg): raise Exception(f'SKIP: {msg}')
        @staticmethod
        def fail(msg): raise AssertionError(msg)
        class fixture:
            def __init__(self, *a, **kw): pass
            def __call__(self, fn): return fn
        class raises:
            def __init__(self, exc): self.exc = exc
            def __enter__(self): return self
            def __exit__(self, t, v, tb):
                if t is None or not issubclass(t, self.exc):
                    raise AssertionError(f'expected {self.exc.__name__}, got {t}')
                return True
    pytest = _PytestStub()

# 项目根目录
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# =========================================================================
# Fixtures / 工具
# =========================================================================

# 测试专用 user_id (不和真实用户冲突)
# 2026-04-20 · 约定 9_000_0xx 段给 Phase 4 单测用,避免和生产数据碰撞
_TEST_USER_A = 9_000_001
_TEST_USER_B = 9_000_002
_TEST_BRAND_A = 9_000_001
_TEST_BRAND_B = 9_000_002


def _get_conn():
    """单独 import 避免模块级 load DB."""
    from db.connection import get_connection
    return get_connection()


def _cleanup_tasks():
    """清理所有测试 user 的 task."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM geo_plan_tasks WHERE user_id IN (%s, %s)",
            (_TEST_USER_A, _TEST_USER_B),
        )
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _setup_once():
    """确保表存在 + 清理旧数据."""
    _cleanup_tasks()


# =========================================================================
# 测试本体
# =========================================================================


class TestGeoPlanTasksDB:
    """DB 层 CRUD + 心跳 + 状态转移 + zombie + RBAC"""

    def setup_method(self, method):
        _setup_once()

    def teardown_method(self, method):
        _cleanup_tasks()

    # ----- 1. create_task -----

    def test_create_task_happy_path(self):
        from db.geo_plan_tasks_db import create_task, get_task_by_id
        task_id = create_task(
            user_id=_TEST_USER_A,
            brand_id=_TEST_BRAND_A,
            params_json={"data_mode": "full"},
            brand_snapshot={"name": "测试品牌", "industry": "餐饮"},
            data_mode="full",
            source="c_end_chat",
            ip="127.0.0.1",
        )
        assert isinstance(task_id, int) and task_id > 0

        row = get_task_by_id(task_id)
        assert row is not None
        assert row["status"] == "queued"
        assert row["progress_percent"] == 0
        assert row["user_id"] == _TEST_USER_A
        assert row["brand_id"] == _TEST_BRAND_A
        assert row["data_mode"] == "full"
        assert row["source"] == "c_end_chat"
        # queued_at 已写,started_at / heartbeat_at / done_at 可能为 NULL (初始状态)
        assert row["queued_at"] is not None

    # ----- 2/3. find_running_for_brand -----

    def test_find_running_for_brand_exists(self):
        from db.geo_plan_tasks_db import create_task, mark_status, find_running_for_brand
        tid = create_task(
            user_id=_TEST_USER_A, brand_id=_TEST_BRAND_A,
            params_json={}, brand_snapshot={}, data_mode="full",
            source="c_end_chat", ip="127.0.0.1",
        )
        # queued 就算 running 集合内
        row = find_running_for_brand(_TEST_BRAND_A)
        assert row is not None
        assert row["id"] == tid

        # 转到 running 仍命中
        mark_status(tid, "running")
        row = find_running_for_brand(_TEST_BRAND_A)
        assert row is not None and row["id"] == tid

    def test_find_running_for_brand_none(self):
        from db.geo_plan_tasks_db import create_task, mark_status, find_running_for_brand
        tid = create_task(
            user_id=_TEST_USER_A, brand_id=_TEST_BRAND_A,
            params_json={}, brand_snapshot={}, data_mode="full",
            source="c_end_chat", ip="127.0.0.1",
        )
        # [v8 同步] queued→done 现被 CAS 拒绝(done 只能从 settling/settlement_pending/settle_conflict)· 用可达终态 cancelled
        mark_status(tid, "cancelled")
        row = find_running_for_brand(_TEST_BRAND_A)
        assert row is None

        row2 = find_running_for_brand(_TEST_BRAND_B)
        assert row2 is None

    # ----- 4. count_running_for_user -----

    def test_count_running_for_user(self):
        from db.geo_plan_tasks_db import create_task, mark_status, count_running_for_user
        ids = []
        for i in range(3):
            ids.append(create_task(
                user_id=_TEST_USER_A,
                brand_id=_TEST_BRAND_A + 100 + i,  # 不同 brand 避免 D1 去重
                params_json={}, brand_snapshot={}, data_mode="full",
                source="c_end_chat", ip="127.0.0.1",
            ))
        done_id = create_task(
            user_id=_TEST_USER_A,
            brand_id=_TEST_BRAND_A + 999,
            params_json={}, brand_snapshot={}, data_mode="full",
            source="c_end_chat", ip="127.0.0.1",
        )
        # [v8 同步] queued→done 被拒 · 用可达终态 cancelled 使其不计入在途
        mark_status(done_id, "cancelled")

        n = count_running_for_user(_TEST_USER_A)
        assert n == 3

    # ----- 5. update_progress 更新 heartbeat -----

    def test_update_progress_updates_heartbeat(self):
        from db.geo_plan_tasks_db import create_task, update_progress, get_task_by_id
        tid = create_task(
            user_id=_TEST_USER_A, brand_id=_TEST_BRAND_A,
            params_json={}, brand_snapshot={}, data_mode="full",
            source="c_end_chat", ip="127.0.0.1",
        )
        before = get_task_by_id(tid)
        old_hb = before["heartbeat_at"]

        time.sleep(1)  # 避免时间精度问题
        update_progress(tid, stage="expand", percent=40, message="已扩词 24 个")
        after = get_task_by_id(tid)
        assert after["progress_stage"] == "expand"
        assert after["progress_percent"] == 40
        assert after["progress_message"] == "已扩词 24 个"
        assert after["heartbeat_at"] is not None
        if old_hb is not None:
            assert after["heartbeat_at"] > old_hb

    # ----- 6. mark_status 'done' 写 done_at -----

    def test_mark_status_done_sets_done_at(self):
        from db.geo_plan_tasks_db import create_task, mark_status, get_task_by_id
        tid = create_task(
            user_id=_TEST_USER_A, brand_id=_TEST_BRAND_A,
            params_json={}, brand_snapshot={}, data_mode="full",
            source="c_end_chat", ip="127.0.0.1",
        )
        # [v8 同步] done 必须经耐久路径 running→settling(落 result)→done(commit 确认口径)· 直接 queued→done 被 CAS 拒
        mark_status(tid, "running")
        mark_status(tid, "settling", result_json={"clusters": 3, "keyword_count": 25})
        mark_status(tid, "done")
        row = get_task_by_id(tid)
        assert row["status"] == "done"
        assert row["done_at"] is not None
        # result_json 应能被反序列化
        rj = row["result_json"]
        if isinstance(rj, str):
            rj = json.loads(rj)
        assert rj["clusters"] == 3

    # ----- 7. mark_zombie 阈值检测 -----

    def test_mark_zombie_threshold(self):
        from db.geo_plan_tasks_db import create_task, mark_status, mark_zombie, get_task_by_id
        # 造一条 heartbeat > 2min 的 running
        old_tid = create_task(
            user_id=_TEST_USER_A, brand_id=_TEST_BRAND_A,
            params_json={}, brand_snapshot={}, data_mode="full",
            source="c_end_chat", ip="127.0.0.1",
        )
        mark_status(old_tid, "running")
        # 造一条最近心跳的 running
        fresh_tid = create_task(
            user_id=_TEST_USER_A, brand_id=_TEST_BRAND_A + 1,
            params_json={}, brand_snapshot={}, data_mode="full",
            source="c_end_chat", ip="127.0.0.1",
        )
        mark_status(fresh_tid, "running")

        # 直接 SQL 把 old_tid 的 heartbeat 拉到 5 分钟前
        conn = _get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE geo_plan_tasks SET heartbeat_at = NOW() - INTERVAL '5 minutes' WHERE id = %s",
                (old_tid,),
            )
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass

        n = mark_zombie(threshold_seconds=120)
        assert n >= 1

        old_row = get_task_by_id(old_tid)
        fresh_row = get_task_by_id(fresh_tid)
        # [v8 同步] mark_zombie 现走【补偿队列】running→refund_pending(不直接 failed · 防丢 freeze)· pending_terminal='failed'
        assert old_row["status"] == "refund_pending"
        assert old_row["error_code"] == "zombie"
        assert fresh_row["status"] == "running"  # 不动

    # ----- 8. sweep_server_restart -----

    def test_sweep_server_restart(self):
        from db.geo_plan_tasks_db import create_task, mark_status, sweep_server_restart, get_task_by_id
        tids = []
        for i in range(3):
            tid = create_task(
                user_id=_TEST_USER_A, brand_id=_TEST_BRAND_A + i,
                params_json={}, brand_snapshot={}, data_mode="full",
                source="c_end_chat", ip="127.0.0.1",
            )
            mark_status(tid, "running")
            tids.append(tid)

        n = sweep_server_restart(grace_seconds=0)   # [v8] 测试内立即清(生产默认 180s 年龄闸防蓝绿误杀活任务)
        assert n >= 3

        for tid in tids:
            row = get_task_by_id(tid)
            # [v8 同步] sweep 现走【补偿队列】running→refund_pending(不直接 failed · freeze 待 reconcile release)
            assert row["status"] == "refund_pending"
            assert row["error_code"] == "server_restart"
            assert row["pending_terminal"] == "failed"

    # ----- 9. RBAC: get_task_by_id 传 user_id -----

    def test_get_task_rbac(self):
        from db.geo_plan_tasks_db import create_task, get_task_by_id
        tid = create_task(
            user_id=_TEST_USER_A, brand_id=_TEST_BRAND_A,
            params_json={}, brand_snapshot={}, data_mode="full",
            source="c_end_chat", ip="127.0.0.1",
        )
        # owner 能查到
        ok = get_task_by_id(tid, user_id=_TEST_USER_A)
        assert ok is not None and ok["id"] == tid
        # 别人查返 None (API 层翻译成 403)
        denied = get_task_by_id(tid, user_id=_TEST_USER_B)
        assert denied is None

    # ----- 10. list_tasks_by_user 过滤 -----

    def test_list_tasks_filters(self):
        from db.geo_plan_tasks_db import create_task, mark_status, list_tasks_by_user
        running_ids = []
        for i in range(2):
            tid = create_task(
                user_id=_TEST_USER_A, brand_id=_TEST_BRAND_A + i,
                params_json={}, brand_snapshot={}, data_mode="full",
                source="c_end_chat", ip="127.0.0.1",
            )
            mark_status(tid, "running")
            running_ids.append(tid)
        done_tid = create_task(
            user_id=_TEST_USER_A, brand_id=_TEST_BRAND_A + 99,
            params_json={}, brand_snapshot={}, data_mode="full",
            source="c_end_chat", ip="127.0.0.1",
        )
        mark_status(done_tid, "done", result_json={"k": "v" * 10000})  # 故意塞大,验精简返回

        # 按 status=running 过滤
        running_only = list_tasks_by_user(_TEST_USER_A, status="running")
        assert len(running_only) == 2
        for r in running_only:
            assert r["status"] == "running"

        # 按 brand_id 过滤
        br0 = list_tasks_by_user(_TEST_USER_A, brand_id=_TEST_BRAND_A)
        assert len(br0) == 1 and br0[0]["id"] == running_ids[0]

        # 精简返回 (不含 result_json 全量,只含 result_summary 或根本不含大字段)
        all_for_user = list_tasks_by_user(_TEST_USER_A, limit=20)
        # 找到 done 那条
        done_rows = [r for r in all_for_user if r["id"] == done_tid]
        assert done_rows, "done 任务应出现在列表里"
        done_row = done_rows[0]
        # result_json 可为 None 或只含摘要字符串 - 不应该是完整的 10K 大 JSON
        raw = done_row.get("result_json")
        summary = done_row.get("result_summary")
        # 两个都存在都可,只要 list 层不返 10K
        if raw is not None:
            # 容忍方案: 如果实现者选择返 result_json,必须是已精简的
            raw_str = json.dumps(raw) if not isinstance(raw, str) else raw
            assert len(raw_str) < 2000, f"list_tasks_by_user 返回的 result_json 太大 ({len(raw_str)} bytes), 应精简"
        # 最低要求: 接口不挂


# =========================================================================
# 独立跑:python tests/test_geo_plan_tasks_db.py
# =========================================================================

if __name__ == "__main__":
    t = TestGeoPlanTasksDB()
    methods = [m for m in dir(t) if m.startswith("test_")]
    for m in methods:
        t.setup_method(m)
        try:
            getattr(t, m)()
            print(f"[PASS] {m}")
        except Exception as e:
            print(f"[FAIL] {m}: {type(e).__name__}: {e}")
        finally:
            t.teardown_method(m)
