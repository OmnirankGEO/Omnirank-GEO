# -*- coding: utf-8 -*-
"""#10(audit · 2026-06-10):诊断原子防重。

根治 start_diagnosis 的 check→freeze→占位 三段跨连接非原子的 93ms 双击双扣
(prod point_freezes 16/17 同品牌 93ms 双冻结实锤)+ 5min 窗口 < 诊断时长(p90=208s/max=1104s)
导致慢诊断重试再扣 650。做法:pg_advisory_xact_lock 按 brand 串行 + check+占位 INSERT 同事务原子
+ 窗口 30min + freeze 失败回删占位防孤儿。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / "server.py").read_text(encoding="utf-8")


def _start_diagnosis_block() -> str:
    i = SERVER.find("async def start_diagnosis(")
    assert i >= 0, "未找到 start_diagnosis"
    nxt_async = SERVER.find("\nasync def ", i + 10)
    nxt_route = SERVER.find("\n@app.", i + 10)
    ends = [x for x in (nxt_async, nxt_route) if x > 0]
    return SERVER[i:(min(ends) if ends else len(SERVER))]


def test_atomic_slot_advisory_lock():
    """slot 用 pg_advisory_xact_lock(hashtext) 串行化同 brand 并发 · key 带独特前缀防冲突。"""
    blk = _start_diagnosis_block()
    assert "_acquire_diagnosis_slot" in blk, "应有原子 slot 函数"
    assert "pg_advisory_xact_lock(hashtext(" in blk, "需 advisory xact lock 串行化"
    assert "geo_diag_dedup:" in blk, "advisory lock key 须用独特前缀防与现有 hashtext 锁冲突"


def test_check_and_insert_same_transaction():
    """临界区顺序:lock → check(未完成占位)→ 占位 INSERT → commit(同事务原子 · commit 释放锁)。"""
    blk = _start_diagnosis_block()
    slot = blk[blk.find("_acquire_diagnosis_slot"):]
    i_lock = slot.find("pg_advisory_xact_lock")
    i_check = slot.find("total_score IS NULL")
    i_insert = slot.find("INSERT INTO diagnosis_records")
    i_commit = slot.find("_conn.commit()")
    assert 0 < i_lock < i_check < i_insert < i_commit, "须 lock→check→占位 INSERT→commit 同事务原子"


def test_dedup_window_extended_to_30min():
    """窗口 5min → 30min(覆盖诊断 p90=208s / max=1104s 慢诊断,防重试再扣)。"""
    blk = _start_diagnosis_block()
    assert "30 minutes" in blk, "dedup 窗口须 ≥30min"
    assert "5 minutes" not in blk, "旧 5min 窗口须移除"


def test_old_nonatomic_paths_removed():
    """旧独立 _check_running_diagnosis / _insert_placeholder(跨连接非原子)须删除。"""
    assert "_check_running_diagnosis" not in SERVER, "旧独立 check(跨连接)须删"
    assert "_insert_placeholder" not in SERVER, "旧独立占位 INSERT(跨连接)须删"


def test_freeze_failure_deletes_placeholder():
    """freeze 失败(余额不足 402 / fail-closed 503)两分支都回删占位,防孤儿在 30min 窗口挡重试。"""
    blk = _start_diagnosis_block()
    assert blk.count("_delete_diagnosis_placeholder") >= 2, "freeze 的 402 与 503 两分支都须回删占位"


def test_placeholder_delete_only_unfinished():
    """回删 helper 只删未完成占位(total_score IS NULL),绝不误删已出分的正式诊断行。"""
    i = SERVER.find("def _delete_diagnosis_placeholder")
    assert i >= 0, "缺回删 helper"
    blk = SERVER[i:i + 700]
    assert "DELETE FROM diagnosis_records WHERE session_id = %s AND total_score IS NULL" in blk, \
        "回删须带 total_score IS NULL 守卫(防误删正式诊断)"


def test_fail_open_not_block_user():
    """抢锁/占位异常 fail-open(不挡用户诊断 · 永远不中断对话铁律)。"""
    blk = _start_diagnosis_block()
    assert "fail-open" in blk, "异常分支须标注 fail-open 语义"


def test_session_id_before_slot():
    """session_id 生成须在 slot 之前(占位 INSERT 需要它)。"""
    blk = _start_diagnosis_block()
    i_sid = blk.find("session_id = \"\".join(")
    i_slot = blk.find("_acquire_diagnosis_slot")
    assert 0 < i_sid < i_slot, "session_id 须在原子 slot 之前生成"


def test_failed_diagnosis_deletes_placeholder():
    """诊断启动后失败(run_diagnosis_task except 分支)须回删占位,防孤儿锁死同 brand 30min(红队 P1)。"""
    i = SERVER.find("async def run_diagnosis_task(")
    assert i >= 0, "未找到 run_diagnosis_task"
    j = SERVER.find("\nasync def ", i + 10)
    blk = SERVER[i:(j if j > 0 else len(SERVER))]
    assert "_delete_diagnosis_placeholder" in blk, "run_diagnosis_task 失败分支须回删占位(防孤儿)"
    i_rel = blk.find("release_freeze")
    i_del = blk.find("_delete_diagnosis_placeholder")
    assert 0 < i_rel < i_del, "回删占位须在 release_freeze 之后(失败分支)"
