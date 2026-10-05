"""变异 runner:把实现故意改坏,确认锁真的会红。

全绿不证明锁有判别力 —— 正反两侧同时失效时锁也会全绿。每个变异必须**至少杀掉一条**锁;
一个杀不掉的变异 = 那条实现逻辑目前没有任何锁在看着它。

用法(worktree 根目录):
    python tests/mutation_runner_pubfacts_2026_08_09.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# 🔴 Windows 默认终端是 GBK,脚本里的 ✅/🔴 会抛 UnicodeEncodeError ——
# Review 2026-08-09 实测:按交付命令直接跑会崩,只有额外设 PYTHONIOENCODING=utf-8 才复现结果。
# 交付命令必须能原样跑通,所以在脚本内自己钉死编码,不依赖调用方环境。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001  老 Python / 非 TextIO 时跳过
        pass

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "services" / "publication_outcome_facts.py"
TESTS = "tests/test_publication_outcome_facts_2026_08_09.py"

# 🔴 接线类变异要改的是**别的文件**(部署清单 / 调度),不是被测模块本身。
# 只在被测模块里做变异,永远验不出「这包有没有接进部署链」—— 那正是 Codex 抓到的 P0。
MANIFEST = ROOT / "db" / "migration_manifest.py"
SCHEDULER = ROOT / "api" / "scheduler.py"

# (说明, 原文, 替换)  —— 每条都必须能杀掉至少一条锁
# (说明, 原文, 替换[, 目标文件])
MUTATIONS: list[tuple] = [
    (
        "去掉「引用早于发布」的守卫(Review P0-2 那个洞)",
        "if tested_at is None or tested_at < published_at or tested_at >= window_end:",
        "if tested_at is None or tested_at >= window_end:",
    ),
    (
        "窗口右端改成闭区间(7天窗和14天窗会重叠计数)",
        "or tested_at >= window_end:",
        "or tested_at > window_end:",
    ),
    (
        "把「没记录引用」也算成可观察(把没看见当失败)",
        'if status.startswith("valid"):',
        'if status.startswith("valid") or status == "empty":',
    ),
    (
        "坏 JSON 混进可观察",
        'elif status in {"invalid_json", "not_array"}:\n            bucket["citations_unparsable"] += 1',
        'elif status in {"invalid_json", "not_array"}:\n            bucket["tests_observable"] += 1',
    ),
    (
        "🔴 迁移从部署清单里摘掉(整包上线即惰性)",
        '    "db/migration_032_publication_outcome_facts_2026_08_09.sql",\n',
        "",
        MANIFEST,
    ),
    (
        "🔴 builder 从调度里摘掉(表建出来永远是空的)",
        "from services.publication_outcome_facts import build_publication_facts",
        "build_publication_facts = lambda **k: {}",
        SCHEDULER,
    ),
    (
        "🔴 重跑不再清理过期行(published→rejected 的旧战果永远留着)",
        'cur.execute(f"DELETE FROM {OUTCOME_TABLE} WHERE metric_version = %s",',
        'cur.execute(f"SELECT 1 FROM {OUTCOME_TABLE} WHERE metric_version = %s",',
    ),
    (
        "🔴 清理不按 metric_version 限定(把历史口径一起端了)",
        'f"DELETE FROM {OUTCOME_TABLE} WHERE metric_version = %s",\n                        (FACTS_METRIC_VERSION,)',
        'f"DELETE FROM {OUTCOME_TABLE}",\n                        ()',
    ),
    (
        "🔴 先插后删(等于没删)",
        'cur.execute(f"DELETE FROM {ATTEMPT_TABLE} WHERE metric_version = %s",\n                        (FACTS_METRIC_VERSION,))\n            summary["stale_rows_deleted"] += int(getattr(cur, "rowcount", 0) or 0)\n            for params in attempt_rows:',
        "for params in attempt_rows:",
    ),
    (
        "🔴 文章级分母改回只从 outcome 表数(零监测文章人间蒸发)",
        "FROM pop p\n          LEFT JOIN {OUTCOME_TABLE} o",
        "FROM pop p\n          JOIN {OUTCOME_TABLE} o",
    ),
    (
        "first_cited_at 取最后一次而不是最早",
        'if bucket["first_cited_at"] is None or tested_at < bucket["first_cited_at"]:',
        'if bucket["first_cited_at"] is None or tested_at > bucket["first_cited_at"]:',
    ),
    (
        "窗口完整性恒为 True(拿偏小的分母算率)",
        "window_complete = clock >= window_end",
        "window_complete = True",
    ),
    (
        "分组丢掉 platform(合并掉引擎差异)",
        'key = (str(row.get("keyword") or ""), str(row.get("platform") or ""))',
        'key = (str(row.get("keyword") or ""), "all")',
    ),
    (
        "rank 用数组下标覆盖真实 rank 字段",
        "rank = int(rank_raw) if rank_raw is not None else position",
        "rank = position",
    ),
    (
        "读取侧把 gate1 的分母换成 tests_total(把没看见当失败)",
        '"gate1_url_cited": ("tests_observable"',
        '"gate1_url_cited": ("tests_total"',
    ),
    (
        "文章级查询不过滤未走完的窗口(拿偏小的分母算率)",
        "AND o.window_days = %s\n                AND o.window_complete",
        "AND o.window_days = %s",
    ),
    (
        "文章级人口不过滤未走完的窗口(刚发的文章拉低分母)",
        "AND a.published_at + make_interval(days => %s) <= NOW()",
        "AND TRUE",
    ),
    (
        "文章级分母改成全部文章(把没测过的算成失败)",
        "COUNT(*) FILTER (WHERE observable > 0)   AS articles_observable",
        "COUNT(*)                                 AS articles_observable",
    ),
    (
        "🔴 零监测文章从人口里蒸发(COALESCE 去掉 → LEFT JOIN 未命中变 NULL)",
        "COALESCE(SUM(o.tests_observable), 0) AS observable",
        "SUM(o.tests_observable) AS observable",
    ),
]


def _run_pytest() -> tuple[bool, str]:
    env = dict(os.environ)
    if not env.get("TEST_DATABASE_URL"):
        raise SystemExit("先设 TEST_DATABASE_URL(指向名字里带 test 的测试库)")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TESTS, "-q", "--no-header", "-x", "--tb=no"],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    return proc.returncode == 0, (proc.stdout or "")[-400:]


def main() -> int:
    # 🔴 newline="" 保原始行尾。默认模式会把 LF 写成 CRLF(Windows),
    #    本仓 core.autocrlf=false 且仓库存 LF → 跑一次变异就把整个文件的行尾污染了,
    #    diff 里 9986 行全变(实际改动只有 35 行)。第一版就是这么翻的车。
    originals = {f: f.read_text(encoding="utf-8", newline="") for f in (TARGET, MANIFEST, SCHEDULER)}

    ok, tail = _run_pytest()
    if not ok:
        print("基线就是红的,先修基线再跑变异:\n" + tail)
        return 2
    print("基线 ✅ 全绿\n")

    survived: list[str] = []
    try:
        for idx, mutation in enumerate(MUTATIONS, start=1):
            label, old, new = mutation[0], mutation[1], mutation[2]
            target = mutation[3] if len(mutation) > 3 else TARGET
            base = originals[target]
            if old not in base:
                print(f"[{idx:2}] 🔴 变异锚点找不到(实现已漂移): {label}")
                survived.append(f"{label}(锚点失效)")
                continue
            target.write_text(base.replace(old, new, 1), encoding="utf-8", newline="")
            killed, _ = _run_pytest()
            # killed=False 表示 pytest 失败 = 锁抓到了变异 = 好
            if killed:
                print(f"[{idx:2}] 🔴 存活(没有锁看着它): {label}")
                survived.append(label)
            else:
                print(f"[{idx:2}] ✅ 被杀: {label}")
    finally:
        for f, text in originals.items():
            f.write_text(text, encoding="utf-8", newline="")

    restored_ok, _ = _run_pytest()
    print(f"\n还原后基线: {'✅ 全绿' if restored_ok else '🔴 没还原干净'}")
    print(f"变异 {len(MUTATIONS)} 个 · 被杀 {len(MUTATIONS) - len(survived)} · 存活 {len(survived)}")
    for item in survived:
        print(f"  存活: {item}")
    return 0 if (not survived and restored_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
