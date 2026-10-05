"""
回归测试(判别锁)· FIX 批第2轮 · db/geo_plan_tasks_db.py

- source-inspection 为主:直接读源码断言修复标志,修复被回退则测试失败。
- 不 import server.py,不依赖真实 DB 连接。
- 针对 GEO-R8-CAN-001(mark_status 终态 CAS 守卫)提供判别断言。
- GEO-R5-CAN-007 已 skip(needs-manual-fund-review),不在此断言修复,仅记录现状。
"""
import re
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

TARGET = Path(__file__).resolve().parents[2] / "db" / "geo_plan_tasks_db.py"
SRC = TARGET.read_text(encoding="utf-8")


def _mark_status_body() -> str:
    """截取 mark_status 函数体到下一个顶层 def 之前。"""
    start = SRC.index("def mark_status(")
    # 找到下一个顶层 def(行首 def)
    rest = SRC[start + 1:]
    m = re.search(r"\n(?:def |# =====)", rest)
    end = (start + 1 + m.start()) if m else len(SRC)
    return SRC[start:end]


# ---------- GEO-R8-CAN-001: 终态 compare-and-set 守卫 ----------

def test_r8_cas_guard_marker_present():
    """修复标记注释存在(回退即失败)。"""
    assert "[GEO-R8-CAN-001]" in SRC, "缺少 GEO-R8-CAN-001 修复标记"


def test_r8_mark_status_terminal_cas_where_clause():
    """终态 UPDATE 必须带 CAS 守卫:源态 NOT IN 四个终态。"""
    body = _mark_status_body()
    # 断言存在防止终态互相覆盖的 WHERE 守卫
    assert "status NOT IN" in body, "mark_status 缺少终态 CAS 守卫(status NOT IN ...)"
    # 四个终态都必须在守卫集合里
    for st in ("done", "failed", "cancelled", "timeout"):
        assert re.search(
            r"status NOT IN \([^)]*'%s'" % st, body
        ) or ("'%s'" % st) in body.split("status NOT IN", 1)[1][:120], (
            f"CAS 守卫集合缺少终态 {st}"
        )


def test_r8_where_guards_all_transitions_unconditional():
    """[Deploy-CTO NO-GO finding 5 返工 2026-07-12] CAS 守卫【无条件】加(所有出终态转移)。

    旧设计只在 is_terminal 分支加守卫 → cancelled→running(非终态目标)漏防会复活任务。
    终态是吸收态:守卫必须无条件在 where 里,禁止任何终态→其他态转移。
    """
    body = _mark_status_body()
    # 守卫必须无条件加在 where(不在 if is_terminal 分支内)
    assert 'where = "WHERE id = %s AND status NOT IN' in body, (
        "CAS 守卫必须无条件加在 where(终态=吸收态·防 cancelled→running 复活)"
    )
    # 反向:不得再把守卫仅放在 is_terminal 分支(那会漏防非终态目标的复活)
    assert not re.search(r"if\s+is_terminal:\s*\n\s*where\s*\+?=", body), (
        "守卫不得再仅在 is_terminal 分支内(否则 cancelled→running 仍可复活)"
    )


def test_r8_mark_status_returns_changed_bool():
    """mark_status 返回是否真正命中(bool),供调用方跳过后续副作用。"""
    body = _mark_status_body()
    assert re.search(r"->\s*bool", body), "mark_status 应声明返回 bool"
    assert "cur.rowcount" in body, "mark_status 应基于 rowcount 判定是否改动"
    assert re.search(r"return\s+changed", body), "mark_status 应返回 changed"


def test_r8_skip_is_logged_not_silent():
    """终态被 CAS 拦截时要 warning 记录,不静默降级。"""
    body = _mark_status_body()
    assert "SKIPPED" in body and "logger.warning" in body, (
        "CAS 拦截应有 warning 日志,不能静默"
    )


def test_r8_import_and_signature_intact():
    """模块可被 py_compile 解析后,函数签名与常量存在(不依赖 DB)。"""
    assert "_TERMINAL_STATUSES" in SRC, "应抽出终态常量 _TERMINAL_STATUSES"
    # 确认 running 分支仍无条件(不被 CAS 误伤)
    assert 'if status == "running":' in SRC


# ---------- GEO-R5-CAN-007: 记录 skip 现状(未修复,交人工) ----------

def test_r5_recovery_paths_unchanged_documented():
    """mark_zombie / sweep_server_restart 仍为直接 UPDATE(未接 freeze 释放)。

    该项按纪律 skip(needs-manual-fund-review):在 DB 层释放 freeze 属资金/守恒逻辑。
    本测试仅锁定现状,若未来有人在此路径接入 freeze 释放,应改由基金评审覆盖测试。
    """
    assert "def mark_zombie(" in SRC and "def sweep_server_restart(" in SRC
    # 现状:恢复路径不 import release_freeze(资金操作留人工)
    assert "release_freeze" not in SRC, (
        "如需在 DB 层释放 freeze,须走人工基金评审并补充守恒测试"
    )
