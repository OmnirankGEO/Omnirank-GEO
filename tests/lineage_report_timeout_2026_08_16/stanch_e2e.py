"""[P0 血缘三报 2026-08-16] 止血层**真库**端到端(工单 §2 判据)。

不是 mock:直连生产形状库(pg_dump 夹具 · 生产 92% 规模),走真实
`_load_rows` → `_bounded_report_txn` → psycopg2 → PG。

成对判据:
  1. 正常查询    → 必须成功 **且 < 5s**(工单根治判据)
  2. pg_sleep 注入 + 30s 闸 → 必须抛 ReportComputeTimeout(超时被掐)
  3. 同样注入但**把闸拆掉**(设成 10min)→ 必须**不抛**、且真的慢下来
     ← 反向对照。没有它,第 2 条可能只是"SQL 语法错了"也会抛。
  4. 超时响应必须是 degraded 且计数为 None,**不是 0**
"""
from __future__ import annotations

import os
import sys
import time
import functools

def _repo_root() -> str:
    """向上找 `.git` 锚定仓根。

    🔴 原来写 `dirname(dirname(__file__))` —— 那是脚本还待在 `.probe/` 时的层数。
    入库到 `tests/lineage_report_timeout_2026_08_16/` 后只剥到 `tests/`,
    于是从**任何** cwd 跑都是 `No module named 'services'` / 打开 `tests/services/...`。
    交付单里的数字出自搬家**前**那份,交付树上这份根本跑不起来 —— Review 抓到的就是这个。
    锚定 `.git` 后再搬一次家也不会复发。
    注:worktree 里 `.git` 是**文件**不是目录,必须用 `exists` 不能用 `isdir`。
    """
    d = os.path.dirname(os.path.abspath(__file__))
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            raise SystemExit("找不到仓根(一路向上都没有 .git)")
        d = parent


REPO = _repo_root()
# 判据可用性第 0 关:REPO 错了后面全是「找不到模块」这类误导性失败,先响亮地炸掉。
if not os.path.isfile(os.path.join(REPO, "services", "article_structure_analysis.py")):
    raise SystemExit(f"仓根判定错误:{REPO} 下没有 services/article_structure_analysis.py")
sys.path.insert(0, REPO)

PROD_SHAPE_URL = "postgresql://geo_admin:lineagefix@127.0.0.1:15499/geo_agentscope"
os.environ["DATABASE_URL"] = PROD_SHAPE_URL
os.environ.setdefault("TEST_DATABASE_URL", PROD_SHAPE_URL)

import services.article_structure_analysis as asa  # noqa: E402
from services.article_structure_analysis import ReportComputeTimeout  # noqa: E402

# 让 db.connection 用上面这个 URL(模块加载时会缓存)
import db.connection as dbconn  # noqa: E402
dbconn.DATABASE_URL = PROD_SHAPE_URL

FAILS: list[str] = []


print = functools.partial(__builtins__["print"] if isinstance(__builtins__, dict) else __builtins__.print, flush=True)


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  ✅ " if ok else "  🔴 ") + name + (f"  · {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    print("=== 1. 正常查询:必须成功且 <5s(改写后) ===")
    for scope, industry in (("ALL", "general"), ("IND", "装修建材")):
        t0 = time.monotonic()
        _key, _vals, rows, grade = asa._load_rows(industry, limit=300, min_chars=500,
                                                  oss_backfill_cap=0)
        dt = time.monotonic() - t0
        check(f"{scope} 查询成功 · {len(rows)} 行 · grade={grade}", len(rows) >= 0)
        check(f"{scope} 耗时 {dt*1000:.0f}ms < 5000ms", dt < 5.0, f"{dt*1000:.0f}ms")

    print("\n=== 2. pg_sleep 注入 + 默认 30s 闸:必须被掐 ===")
    original = asa.ARTICLE_STRUCTURE_ALL_SQL
    # 注入:让主查询必然睡够久(不改任何判定逻辑,只是塞一个必然慢的条件)
    slow = original.replace(
        "  FROM geo_research_articles article\n",
        "  FROM geo_research_articles article\n  CROSS JOIN (SELECT pg_sleep(0.02)) AS _p0_slow\n",
        1,
    )
    if slow == original:
        check("pg_sleep 注入锚点命中", False, "锚点没命中 —— 本节判据不可用")
        return 1
    check("pg_sleep 注入锚点命中", True)

    asa.ARTICLE_STRUCTURE_ALL_SQL = slow
    try:
        os.environ[asa.REPORT_STATEMENT_TIMEOUT_ENV] = "3000"   # 3s 闸,别让探针跑满
        t0 = time.monotonic()
        try:
            asa._load_rows("general", limit=300, min_chars=500, oss_backfill_cap=0)
            check("闸生效:抛 ReportComputeTimeout", False, "居然没抛 —— 止血层没起作用")
        except ReportComputeTimeout as exc:
            dt = time.monotonic() - t0
            check("闸生效:抛 ReportComputeTimeout", True,
                  f"{dt*1000:.0f}ms 被掐(闸 {exc.timeout_ms}ms)")
            check("确实是被闸掐的(耗时≈闸值,不是查询自己快)", dt < 15.0, f"{dt:.1f}s")

        print("\n=== 3. 反向对照:把闸放大到 120s → 必须不抛 ===")
        os.environ[asa.REPORT_STATEMENT_TIMEOUT_ENV] = "120000"
        t0 = time.monotonic()
        try:
            asa._load_rows("general", limit=300, min_chars=500, oss_backfill_cap=0)
            dt = time.monotonic() - t0
            check("放大闸后不再被掐(证明第 2 条是闸的功劳,不是 SQL 报错)",
                  dt > 3.0, f"{dt:.1f}s(pg_sleep(0.02) 真的睡满了)")
        except ReportComputeTimeout:
            check("放大闸后不再被掐", False, "仍抛超时 —— 说明第 2 条不是闸的功劳")
    finally:
        asa.ARTICLE_STRUCTURE_ALL_SQL = original
        os.environ.pop(asa.REPORT_STATEMENT_TIMEOUT_ENV, None)

    print("\n=== 4. 超时响应必须 degraded 且计数为 None(不是 0) ===")
    from api.writing_style_flywheel_api import _report_timeout_payload
    payload = _report_timeout_payload(ReportComputeTimeout("article_structure_all", 30000))
    check("degraded=True", payload.get("degraded") is True)
    check("status='degraded'", payload.get("status") == "degraded")
    for k in ("loaded", "groups", "feature_lift", "engine_count", "legacy_lineage"):
        check(f"{k} 是 None 而不是 0/空(0 会被前端渲染成「真的没有」)",
              payload.get(k, "MISSING") is None, repr(payload.get(k)))
    check("空态原因标成 compute_timeout", payload.get("empty_state_reason") == "compute_timeout")
    check("文案明说「不是没有数据」", "不是" in str(payload.get("empty_state_message")))

    print()
    if FAILS:
        print(f"🔴 止血端到端未全绿,失败 {len(FAILS)} 条:{FAILS}")
        return 1
    print("✅ 止血端到端全绿(含拆闸反向对照)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
