"""
调度控制面单测(WORKERS=4 · SPEC §7 · FF4)

leader 门控 + Redis 降级(无外部依赖);consume 的 2d(unwrap raw · 独立 identity)+ 2e(done 只在
业务结束后)用注入 fake db/job 验证。
"""
import functools
import sys
import types

from services import sched_control as sctl


def test_consume_skips_when_not_leader(monkeypatch):
    import services.cron_gate as cg
    monkeypatch.setattr(cg, "_active", False)
    assert sctl.consume_commands_once() == {"skipped": "not_leader"}


def test_report_status_noop_when_not_leader(monkeypatch):
    import services.cron_gate as cg
    monkeypatch.setattr(cg, "_active", False)
    assert sctl.report_status_once() is None


def test_read_status_degraded_when_redis_none(monkeypatch):
    fake = types.ModuleType("cache.redis_client")
    fake.get_redis = lambda: None
    monkeypatch.setitem(sys.modules, "cache.redis_client", fake)
    r = sctl.read_status()
    assert r["degraded"] is True and r["running"] is False and r["jobs"] == []


def test_read_status_degraded_when_heartbeat_missing(monkeypatch):
    class _R:
        def get(self, k):
            return None
    fake = types.ModuleType("cache.redis_client")
    fake.get_redis = lambda: _R()
    monkeypatch.setitem(sys.modules, "cache.redis_client", fake)
    r = sctl.read_status()
    assert r["degraded"] is True and r["reason"] == "cron_heartbeat_missing"


def _stub_claim_db(monkeypatch, row):
    """fake get_db:claim UPDATE...RETURNING 返回一次 row,之后 None;rowcount=0(reaper 无死命令)。"""
    class _C:
        def __init__(self):
            self._row = row
            self._served = False
            self.rowcount = 0          # [FF10] 供 _reap_stale_commands 读(本测无死命令)
        def cursor(self):
            return self
        def execute(self, q, p=None):
            pass
        def fetchone(self):
            if not self._served:
                self._served = True
                return self._row
            return None
        def commit(self):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    mod = types.ModuleType("db.connection")
    mod.get_db = lambda: _C()
    monkeypatch.setitem(sys.modules, "db.connection", mod)


def test_consume_runs_unwrapped_raw_and_finishes_after(monkeypatch):
    """2d:执行 job 的 __wrapped__(raw · 不落 cron 时间桶);2e:finish 只在 raw 完成之后。"""
    import services.cron_gate as cg
    monkeypatch.setattr(cg, "_active", True)                 # leader
    _stub_claim_db(monkeypatch, ("cmd1", "jobX"))

    trace = {"body": 0, "finish_at_body": None, "finish_status": None}

    def raw_body(*a):
        trace["body"] += 1
        return "ok"

    @functools.wraps(raw_body)
    def time_bucket_wrapper(*a):
        raise AssertionError("必须调 __wrapped__(raw),不能走时间桶 wrapper")
    # functools.wraps 已设 __wrapped__=raw_body

    class _FakeJob:
        func = time_bucket_wrapper
        args = ()

    monkeypatch.setattr(sctl, "_find_job", lambda jn: (object(), _FakeJob()))
    monkeypatch.setattr(sctl, "_heartbeat_command", lambda *a, **k: None)
    monkeypatch.setattr(
        sctl, "_finish_command",
        lambda cid, tok, status, result, err: trace.update(
            finish_at_body=trace["body"], finish_status=status),
    )

    r = sctl.consume_commands_once()
    assert r == {"processed": 1, "status": "done", "reaped": 0}
    assert trace["body"] == 1                    # 执行了 raw(2d 独立 identity)
    assert trace["finish_at_body"] == 1          # finish 在 body 之后(2e done 只在业务结束后)
    assert trace["finish_status"] == "done"


def test_consume_unknown_job_marks_failed(monkeypatch):
    import services.cron_gate as cg
    monkeypatch.setattr(cg, "_active", True)
    _stub_claim_db(monkeypatch, ("cmd2", "ghost_job"))
    monkeypatch.setattr(sctl, "_find_job", lambda jn: (None, None))
    seen = {}
    monkeypatch.setattr(sctl, "_finish_command",
                        lambda cid, tok, status, result, err: seen.update(status=status, err=err))
    r = sctl.consume_commands_once()
    assert r["processed"] == 1 and r["status"] == "failed"
    assert seen["status"] == "failed" and "unknown job" in seen["err"]


def test_consume_reaps_stale_claimed_no_rerun(monkeypatch):
    """[FF10 P0-1] 死 claimed 命令(心跳>90s)→ 回收转 retry_pending · **不复活执行**(无 pending → processed 0)。"""
    import services.cron_gate as cg
    monkeypatch.setattr(cg, "_active", True)

    class _C:
        def __init__(self):
            self.rowcount = 2          # reaper UPDATE 命中 2 条死命令
        def cursor(self):
            return self
        def execute(self, q, p=None):
            pass
        def fetchone(self):
            return None                # 无 pending 可领
        def commit(self):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    mod = types.ModuleType("db.connection")
    mod.get_db = lambda: _C()
    monkeypatch.setitem(sys.modules, "db.connection", mod)
    # 死命令绝不复活执行:_find_job 一旦被调即失败
    monkeypatch.setattr(sctl, "_find_job",
                        lambda jn: (_ for _ in ()).throw(AssertionError("死 claimed 命令不得复活执行")))

    r = sctl.consume_commands_once()
    assert r["processed"] == 0 and r["reaped"] == 2


def test_consume_async_manual_runs_to_completion(monkeypatch):
    """[FF11 P1-3] 手动触发 async job:必须 await 到**真正结束**(执行=1·结果真实·无 never-awaited),
    不是 raw(*) 返回未 await 的 coroutine 就假标 done。"""
    import services.cron_gate as cg
    monkeypatch.setattr(cg, "_active", True)
    _stub_claim_db(monkeypatch, ("cmdA", "asyncJob"))
    ran = {"n": 0}

    async def araw(*a):
        ran["n"] += 1
        return "async-done"

    class _FakeJob:
        func = araw            # 无 __wrapped__ → raw=araw(async coroutine function)
        args = ()

    monkeypatch.setattr(sctl, "_find_job", lambda jn: (object(), _FakeJob()))
    monkeypatch.setattr(sctl, "_heartbeat_command", lambda *a, **k: None)
    seen = {}
    monkeypatch.setattr(sctl, "_finish_command",
                        lambda cid, tok, status, result, err: seen.update(status=status, result=result, err=err))

    r = sctl.consume_commands_once()
    assert r["processed"] == 1 and r["status"] == "done"
    assert ran["n"] == 1                                   # async body 真正执行一次(非假完成)
    assert seen["status"] == "done" and seen["result"]
    assert "async-done" in str(seen["result"])             # 结果是真实返回值
    assert "coroutine" not in str(seen["result"]).lower()  # 不是 <coroutine object ...>(never-awaited 的证据)


# ---------------- [FF12 P0-2] cron 部署硬门:当前 leader 判定(不看历史日志)----------------

def _stub_redis(monkeypatch, leader, status):
    class _R:
        def get(self, k):
            return {"sched:leader": leader, "sched:status": status}.get(k)
    fake = types.ModuleType("cache.redis_client")
    fake.get_redis = lambda: _R()
    monkeypatch.setitem(sys.modules, "cache.redis_client", fake)


def test_verify_is_current_leader_true(monkeypatch):
    import json as _j
    _stub_redis(monkeypatch, leader="hostA-123-abcdef",
                status=_j.dumps({"host": "hostA", "running": True}))
    assert sctl.verify_is_current_leader("hostA") is True


def test_verify_is_current_leader_false_when_owner_not_me(monkeypatch):
    """[FF12 判别] sched:leader 是**别的容器** → False → 部署 abort + 自动恢复旧 cron(新 cron 未真接管)。"""
    import json as _j
    _stub_redis(monkeypatch, leader="hostB-999-zzz",
                status=_j.dumps({"host": "hostB", "running": True}))
    assert sctl.verify_is_current_leader("hostA") is False


def test_verify_is_current_leader_false_when_status_stale_or_mismatch(monkeypatch):
    import json as _j
    # 心跳 host 不符(leader 键对但 status 是别人/陈旧)→ False
    _stub_redis(monkeypatch, leader="hostA-1-x", status=_j.dumps({"host": "hostB", "running": True}))
    assert sctl.verify_is_current_leader("hostA") is False
    # 无 sched:status(心跳缺失/未写)→ False
    _stub_redis(monkeypatch, leader="hostA-1-x", status=None)
    assert sctl.verify_is_current_leader("hostA") is False
    # running=false → False
    _stub_redis(monkeypatch, leader="hostA-1-x", status=_j.dumps({"host": "hostA", "running": False}))
    assert sctl.verify_is_current_leader("hostA") is False


def test_verify_is_current_leader_false_when_redis_none(monkeypatch):
    fake = types.ModuleType("cache.redis_client")
    fake.get_redis = lambda: None
    monkeypatch.setitem(sys.modules, "cache.redis_client", fake)
    assert sctl.verify_is_current_leader("hostA") is False   # fail-closed


# ---------------- [FF14/FF15 P1-5/P0] outcome_unknown 受控处置(单事务原子)----------------

class _FakeConn:
    """fake get_connection 返回的连接:多次 execute · 可配置第 N 次 execute 抛 · 追踪 commit/rollback/参数 ·
    可配置入队 INSERT(第 2 个 execute)的 rowcount(模拟 ON CONFLICT 静默插 0 行)。"""
    def __init__(self, row, raise_on_execute=None, insert_rowcount=1):
        self.row = row
        self.raise_on = raise_on_execute      # 1-based:第几个 execute 抛(模拟 INSERT 失败/中断)
        self.insert_rowcount = insert_rowcount
        self.exec_count = 0
        self.exec_params = []
        self.rowcount = 1
        self.committed = False
        self.rolled_back = False
    def cursor(self):
        return self
    def execute(self, q, p=None):
        self.exec_count += 1
        self.exec_params.append(p)
        if self.raise_on and self.exec_count == self.raise_on:
            raise RuntimeError("INSERT failed / process interrupted")
        if self.exec_count == 2:              # 受控重跑的入队 INSERT · rowcount 决定 FF16 guard
            self.rowcount = self.insert_rowcount
    def fetchone(self):
        return self.row
    def commit(self):
        self.committed = True
    def rollback(self):
        self.rolled_back = True
    def close(self):
        pass


def _inject_conn(monkeypatch, conn):
    mod = types.ModuleType("db.connection")
    mod.get_connection = lambda: conn
    monkeypatch.setitem(sys.modules, "db.connection", mod)


def test_resolve_unknown_confirmed_done_records_audit(monkeypatch):
    conn = _FakeConn(("jobX", "{}"))
    _inject_conn(monkeypatch, conn)
    r = sctl.resolve_unknown_command("cmd1", operator="alice(uid=1)", decision="confirmed_done",
                                     note="已核对:退款已到账")
    assert r["status"] == "success" and r["decision"] == "confirmed_done" and r["resolved_by"] == "alice(uid=1)"
    assert "rerun_command_id" not in r                                  # confirmed_done 不重跑
    assert conn.committed and not conn.rolled_back                      # 提交 · 未回滚
    assert conn.exec_count == 1                                         # 只 UPDATE 一步
    assert "alice" in str(conn.exec_params[0]) and "已核对" in str(conn.exec_params[0])   # operator+证据入审计


def test_resolve_unknown_controlled_rerun_atomic_success(monkeypatch):
    conn = _FakeConn(("jobX", "{}"))
    _inject_conn(monkeypatch, conn)
    r = sctl.resolve_unknown_command("cmd2", operator="bob(uid=2)", decision="controlled_rerun",
                                     note="已核对:退款未发生")
    assert r["status"] == "success" and r.get("rerun_command_id")
    assert conn.committed and not conn.rolled_back                      # 单事务单提交
    assert conn.exec_count == 3        # 原命令 CAS + 入队新命令 INSERT + 记新旧关联 · 同一事务


def test_resolve_unknown_rerun_insert_fail_keeps_outcome_unknown(monkeypatch):
    """[FF15 P0 判别] 新命令 INSERT 失败/进程中断 → **全回滚** → 原命令保持 outcome_unknown(不丢任务)。"""
    conn = _FakeConn(("jobX", "{}"), raise_on_execute=2)   # 第 2 个 execute(入队新命令 INSERT)抛
    _inject_conn(monkeypatch, conn)
    r = sctl.resolve_unknown_command("cmd3", operator="carol(uid=3)", decision="controlled_rerun",
                                     note="核对证据")
    assert r["status"] == "error"                          # 返回失败
    assert conn.rolled_back and not conn.committed         # **回滚原命令 done · 从未提交** → 原命令仍 outcome_unknown


def test_resolve_unknown_rerun_insert_zero_rows_keeps_outcome_unknown(monkeypatch):
    """[FF16 P1 判别] 入队 INSERT 影响 0 行(冲突/静默)→ rowcount!=1 抛 → **全回滚** → 原命令保持 outcome_unknown。
    (防旧 ON CONFLICT DO NOTHING:插 0 行却标原命令 done + 返回不存在的 rerun_command_id = 丢任务)"""
    conn = _FakeConn(("jobX", "{}"), insert_rowcount=0)    # INSERT 不抛但影响 0 行
    _inject_conn(monkeypatch, conn)
    r = sctl.resolve_unknown_command("cmd4", operator="dave(uid=4)", decision="controlled_rerun",
                                     note="核对证据")
    assert r["status"] == "error"                          # 不再假 success
    assert "rerun_command_id" not in r                     # 不返回不存在的 rerun id
    assert conn.rolled_back and not conn.committed         # 全回滚 → 原命令仍 outcome_unknown(不丢任务)


def test_resolve_unknown_rejects_wrong_state(monkeypatch):
    conn = _FakeConn(None)                                 # UPDATE...RETURNING 无命中(非 outcome_unknown)
    _inject_conn(monkeypatch, conn)
    r = sctl.resolve_unknown_command("cmdX", operator="a(uid=1)", decision="confirmed_done", note="x")
    assert r["status"] == "error" and conn.rolled_back and not conn.committed


def test_resolve_unknown_requires_operator_note_valid_decision():
    assert sctl.resolve_unknown_command("c", "", "confirmed_done", "n")["status"] == "error"   # 缺 operator
    assert sctl.resolve_unknown_command("c", "op", "confirmed_done", "")["status"] == "error"  # 缺证据
    assert sctl.resolve_unknown_command("c", "op", "bogus", "n")["status"] == "error"          # 非法 decision
