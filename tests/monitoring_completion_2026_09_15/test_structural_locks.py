# -*- coding: utf-8 -*-
"""WO_224-c1 结构锁 —— 分母不是工单点名的那几处,是三条腿扫出来的全集。

Review 2026-09-15 补的钉法:
  · 每个建 `monitoring_tasks` 的写入点都要带 `platform_count`
    (抹掉任一路径 ⇒ 走 `<=0` 的出声分支 ⇒ 必须有锁接住);
  · 「出声」那行本身也是锁目标(把它删掉要红)。
"""
from __future__ import annotations

import ast
import io
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _production_files():
    out = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT,
                         capture_output=True, text=True).stdout.split()
    return [f for f in out if not f.startswith(("tests/", "scripts/"))]


#: 🔴 `db/monitoring_db.py:7509` 的那条 INSERT 是**归档恢复**路径:
#:   它按 `t.get("platform_count", 4)` 把历史行原样写回(那个 4 是历史事实的兜底,
#:   不是新任务的派发宽度)。恢复的是**已归档的旧任务**,不是新建,
#:   所以不在「四条建 task 路径」的分母里 —— 但它**必须具名豁免**,
#:   否则下一个人会以为分母只有四处,而它是第五个写入点。
ARCHIVE_RESTORE_SITE = "db/monitoring_db.py"


def test_every_task_creation_path_records_the_dispatch_width():
    """🔴 四条建 task 的路径都必须传 `planned_platform_count`。

    准入的 ③ 档(没派发台账时退到计数)完全靠这个数;
    任一路径不传 ⇒ `platform_count` 落默认 0 ⇒ 那一档退化成恒真。
    分母用**路径腿**枚举,不是照抄工单点名的那四处。
    """
    callers = []
    for f in _production_files():
        src = io.open(ROOT / f, encoding="utf-8", errors="replace").read()
        if "create_monitoring_task" not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:                      # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name != "create_monitoring_task":
                continue
            kwargs = {k.arg for k in (node.keywords or [])}
            callers.append((f, node.lineno, "planned_platform_count" in kwargs))

    assert len(callers) >= 4, "分母取错了:只数到 %d 个建 task 的调用点" % len(callers)
    missing = [(f, ln) for f, ln, ok in callers if not ok]
    assert not missing, (
        "这些建 task 的路径没记派发宽度 ⇒ 完成准入的计数那一档在它们身上恒真:%s" % missing)


def test_the_only_other_task_insert_is_the_archive_restore():
    """🔴 `INSERT INTO monitoring_tasks` 的**字面量腿**:除 `create_monitoring_task`
    之外只许有归档恢复那一条,且必须在具名豁免里。

    多出第五个写入点 = 有人绕开了单点建任务,那条路径上派发宽度没人记。
    """
    pat = re.compile(r"(?<![_\w.])INSERT\s+INTO\s+monitoring_tasks\b", re.I)
    sites = []
    for f in _production_files():
        src = io.open(ROOT / f, encoding="utf-8", errors="replace").read()
        for m in pat.finditer(src):
            sites.append((f, src[:m.start()].count(chr(10)) + 1))

    assert len(sites) == 2, "`INSERT INTO monitoring_tasks` 的处数变了:%s" % sites
    files = sorted({f for f, _ in sites})
    assert files == [ARCHIVE_RESTORE_SITE], (
        "出现了 `db/monitoring_db.py` 之外的建表点 —— 单点被绕开了:%s" % sites)


def test_the_width_unknown_branch_still_says_so():
    """🔴 「出声」那行本身是锁目标。

    毒法:把 `_completion_evidence` 里 `width <= 0` 那支的 warning 删掉。
    删了以后「宽度没记下来」与「宽度满足了」在数据上分不开,
    而所有行为判据照样绿 —— 这正是「仪器缺一条腿却读起来很干净」。

    本条**执行**那条分支并读 logger,不是 grep 源码
    (源码切片不执行分支,`if False:` 留着文本照样绿 —— WO_221 的 Pc 就是这么活的)。
    """
    import logging

    import db.monitoring_db as M

    calls = []

    class _Cur:
        def execute(self, sql, params=None):
            calls.append(sql)

        def fetchall(self):
            # 第一问:结果行(非空,才走得到宽度那一档);第二问:派发台账(空)
            if "monitoring_results" in calls[-1]:
                return [{"platform": "dashscope", "n": 1}]
            return []

        def fetchone(self):
            return {"platform_count": 0}

    logger = logging.getLogger("GEO-Monitoring")
    records = []

    class _Catch(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Catch()
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        out = M._completion_evidence(_Cur(), 424242)
    finally:
        logger.removeHandler(handler)

    assert out["ok"] is True and out["reason"] == "width_unknown", out
    assert any("424242" in m and "platform_count" in m for m in records), (
        "宽度未知那一档放行了却**没出声** —— 读起来像判过了:%s" % records)
