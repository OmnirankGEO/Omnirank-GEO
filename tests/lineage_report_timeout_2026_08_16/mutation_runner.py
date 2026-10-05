"""[P0 血缘三报 2026-08-16] 变异检验:证明新增的锁**真的会杀回归**。

形态 0 → ≠0 → 0:
  基线跑一遍必须 0 failed;每注入一个变异必须 ≥1 failed **且失败的是指定那条锁**;
  还原后必须回到 0 failed(证明是变异造成的,不是环境脏了)。

🔴 只数「失败条数变了」不够 —— 必须核对**失败的是不是号称在打的那条测试**。
   否则任何让 import 炸掉的变异都"杀死"了全部测试,看起来判别力满分,实际零。

🔴 Windows 换行陷阱:读写都必须 newline=""。默认 newline=None 会把 LF 全翻成 CRLF,
   写回去整个文件都变了 —— 变异 runner 在本仓踩过这个坑。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

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
TESTS = "tests/flywheel_integration/test_lineage_report_timeout_2026_08_16.py"
SVC = os.path.join(REPO, "services", "article_structure_analysis.py")


def read(path: str) -> str:
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def run_tests() -> tuple[int, set[str]]:
    """返回 (failed_count, 失败测试名集合)。"""
    env = dict(os.environ)
    env.setdefault("TEST_DATABASE_URL",
                   "postgresql://geo_admin:lineagefix@127.0.0.1:15499/lineage_p0_test")
    env["DATABASE_URL"] = env["TEST_DATABASE_URL"]
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run(
        [sys.executable, "-m", "pytest", TESTS, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    out = (p.stdout or "") + (p.stderr or "")
    # pytest 会把非 ASCII 的 parametrize id 转义成 装... ,与源码里的中文对不上。
    # 不还原的话「打红的不是目标锁」会假红 —— 判据看着有判别力,其实是在比两种编码。
    def _unescape(name: str) -> str:
        return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), name)

    failed = {_unescape(n) for n in re.findall(r"^FAILED\s+\S+::(\S+)", out, re.M)}
    m = re.search(r"(\d+) failed", out)
    n = int(m.group(1)) if m else 0
    if n == 0 and ("error" in out.lower() and "ERRORS" in out):
        n = -1  # collection error:不算"杀死",单独标出
    return n, failed


# (名字, 文件, 原文锚点, 替换成, 期望被打红的测试名前缀)
MUTATIONS = [
    (
        "M1 把 OSS 回读挪回事务内(general 分支)——复现 P0 真形态",
        SVC,
        """                rows.extend(dict(row) for row in cur.fetchall())
        # ↑ 事务已结束、连接已归还 ↓ 下面是网络 I/O,不再持任何表锁
        _backfill_oss_bodies(rows, oss_backfill_cap)""",
        """                rows.extend(dict(row) for row in cur.fetchall())
            _backfill_oss_bodies(rows, oss_backfill_cap)""",
        "test_oss_backfill_runs_only_after_transaction_is_closed[general]",
    ),
    (
        "M2 把 OSS 回读挪回事务内(行业分支)",
        SVC,
        """        rows = _stratified_sample(pool, safe_limit)  # [T2] 四组配额,防对照组被采纳挤空
    # ↑ 同上:事务先关,OSS 回读在事务外
    # [review fix] 行业分支同样贯穿 cap,后台蒸馏不被 40 条稀释
    _backfill_oss_bodies(rows, oss_backfill_cap)""",
        """        rows = _stratified_sample(pool, safe_limit)  # [T2] 四组配额,防对照组被采纳挤空
        _backfill_oss_bodies(rows, oss_backfill_cap)""",
        "test_oss_backfill_runs_only_after_transaction_is_closed[装修建材]",
    ),
    (
        "M3 去掉 AS MATERIALIZED(nested-loop 重扫复发)",
        SVC,
        "article_signal AS MATERIALIZED (",
        "article_signal AS (",
        "test_article_signal_is_materialized",
    ),
    (
        "M4 折叠里把一个 MAX 换成 MIN(报表口径静默改变)",
        SVC,
        "MAX(COALESCE(sig.source_weight, 0)) AS source_weight",
        "MIN(COALESCE(sig.source_weight, 0)) AS source_weight",
        "test_all_collapsed_aggregates_are_max",
    ),
    (
        "M5 只下 statement_timeout(= 工单原方案,拦不住真形态)",
        SVC,
        """        cur.execute("SELECT set_config('idle_in_transaction_session_timeout', %s, true)", (str(idle_ms),))""",
        """        pass  # mutated: 不下 idle 闸""",
        "test_transaction_sets_both_timeouts",
    ),
    (
        "M7 panorama fail-soft 不再留痕(退回 2026-08-16 的伪装态)",
        os.path.join(REPO, "services", "flywheel_panorama.py"),
        """        logger.warning("[panorama] scalar 查询失败(fail-soft 0 · 已标 degraded): %s", str(exc)[:150])
        _mark_degraded("scalar", exc)
        return 0""",
        """        return 0""",
        "test_panorama_marks_degraded_when_queries_fail",
    ),
    (
        "M8 只摘掉 _scalar_opt 的留痕(证明两个来源各自被打到)",
        os.path.join(REPO, "services", "flywheel_panorama.py"),
        """        _mark_degraded("scalar_opt", exc)
        return None""",
        """        return None""",
        "test_panorama_marks_degraded_when_queries_fail",
    ),
    (
        "M6 超时被吞成空结果(fail-soft 吞成 0 的经典病)",
        SVC,
        "            raise ReportComputeTimeout(scope, stmt_ms) from exc",
        "            return  # mutated: 吞掉超时",
        "test_query_canceled_becomes_report_compute_timeout",
    ),
]


def main() -> int:
    base_n, base_failed = run_tests()
    print(f"基线: failed={base_n} {sorted(base_failed) or ''}")
    if base_n != 0:
        print("🔴 基线就不是 0 failed —— 变异检验无意义,先修基线")
        return 1

    ok = True
    for name, path, anchor, repl, expect in MUTATIONS:
        original = read(path)
        if anchor not in original:
            print(f"  🔴 {name}: 锚点未命中 → 变异没注进去(判别力未证明)")
            ok = False
            continue
        try:
            write(path, original.replace(anchor, repl, 1))
            n, failed = run_tests()
            hit = any(f == expect or f.startswith(expect) for f in failed)
            if n == -1:
                print(f"  🔴 {name}: 变异导致收集期报错 → 杀死的是 import 不是锁")
                ok = False
            elif n >= 1 and hit:
                print(f"  ✅ {name}: failed={n},命中目标锁 {expect}")
            elif n >= 1:
                print(f"  🔴 {name}: failed={n} 但打红的不是 {expect},而是 {sorted(failed)}")
                ok = False
            else:
                print(f"  🔴 {name}: failed=0 —— 锁没杀掉这个变异(判据是摆设)")
                ok = False
        finally:
            write(path, original)

    restore_n, restore_failed = run_tests()
    print(f"还原后: failed={restore_n} {sorted(restore_failed) or ''}")
    if restore_n != 0:
        print("🔴 还原后不是 0 failed —— 文件被改坏了")
        return 1
    if not ok:
        print("\n🔴 变异检验未全绿")
        return 1
    print(f"\n✅ 变异检验 {len(MUTATIONS)}/{len(MUTATIONS)} 全杀,形态 0 → ≠0 → 0 成立")
    return 0


if __name__ == "__main__":
    sys.exit(main())
