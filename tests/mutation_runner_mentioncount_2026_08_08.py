#!/usr/bin/env python3
"""变异验证 · 汇总计数接线包(WO_MENTION_COUNT_NOT_RECOMPUTED 2026-08-08)。

每条变异都对应"这个修复的某一部分被撤掉/写歪"的具体形态,
跑一遍被它覆盖的锁,**必须变红**;红了才说明那条锁有判别力。

🔴 Windows 三坑已规避(上一单的教训,别再踩):
  1. **按字节读写**(`read_bytes/write_bytes`)—— `read_text/write_text` 在 Windows 上
     是 LF→CRLF 单向阀,"原样复原"会把整个文件行尾翻一遍(上次因此改坏了受保护文件);
     复原后逐字节断言。
  2. 子进程一律 `PYTHONDONTWRITEBYTECODE=1` —— `__pycache__` 按 (mtime,size) 判失效,
     等字节变异会让子进程继续跑变异版 .pyc,表现成"复原后基线仍红"。
  3. 变异存活先分诊「锁弱 vs 变异是空操作」,别直接判锁弱。

用法:
    TEST_DATABASE_URL=... python tests/mutation_runner_mentioncount_2026_08_08.py
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOCKS = "tests/test_mention_count_recompute_2026_08_08.py"

DECISION = "services/diagnosis_identity_decision.py"
REBUILD = "scripts/rebuild_parenthetical_false_mentions_2026_08_06.py"
BACKFILL = "scripts/backfill_mention_count_2026_08_08.py"


# (编号, 说明, 目标文件, 原文, 替换, 应当变红的用例 -k 表达式)
MUTATIONS: list[tuple[str, str, str, str, str, str]] = [
    (
        "M01",
        "删掉人工确认链的接线(**本 bug 的原形态** · 生产就是这样)",
        DECISION,
        '        totals_delta = apply_recomputed_totals(ai, aggregates)',
        '        ai["dimension_stats"] = aggregates["dimension_stats"]\n'
        '        totals_delta = {"detected_count": {"before": ai.get("detected_count"),'
        ' "after": ai.get("detected_count")},'
        ' "overall_mention_rate": {"before": ai.get("overall_mention_rate"),'
        ' "after": ai.get("overall_mention_rate")}}',
        "confirm_one_cell or persisted_count_equals or customer_facing",
    ),
    (
        "M02",
        "rate 沿用旧值(只刷分子不刷分母派生值 → 报告里计数涨了、比率没涨,自相矛盾)",
        DECISION,
        '    ai["overall_mention_rate"] = rate_after',
        '    ai["overall_mention_rate"] = rate_before if rate_before is not None else rate_after',
        "refreshes_the_rate",
    ),
    (
        "M03",
        "在旧值上 +1 而不是重新派生(旧值离谱时会把错误放大)",
        DECISION,
        '    detected_after = int(totals.get("detected", 0) or 0)',
        '    detected_after = int(ai.get("detected_count") or 0) + 1',
        "seed_was_wrong or noop_on_an_already_consistent",
    ),
    (
        "M04",
        "存量重建路径不接线(541/563「被提及 17 次」的隐形级报告就是这么来的)",
        REBUILD,
        '    totals_delta = apply_recomputed_totals(ai, aggregates)',
        '    ai["dimension_stats"] = aggregates["dimension_stats"]\n'
        '    totals_delta = {"detected_count": {"before": ai.get("detected_count"),'
        ' "after": ai.get("detected_count")},'
        ' "overall_mention_rate": {"before": ai.get("overall_mention_rate"),'
        ' "after": ai.get("overall_mention_rate")}}',
        "rebuild_path",
    ),
    (
        "M05",
        "不刷 DB 列镜像(工单没列到的第三处陈旧点 · regen 会拿列值把 JSON 冲回去)",
        DECISION,
        '               SET ai_detected_count = %s,\n'
        '                   ai_mention_rate = COALESCE(%s, ai_mention_rate)\n'
        '             WHERE id = %s\n',
        '               SET ai_detected_count = ai_detected_count,\n'
        '                   ai_mention_rate = ai_mention_rate\n'
        '             WHERE id = %s AND %s IS NOT NULL AND %s IS NULL\n',
        "column_mirror",
    ),
    (
        "M06",
        "重建路径不刷列镜像(两道墙只拆一道 = 白修)",
        REBUILD,
        '            "UPDATE diagnosis_records SET ai_detected_count=%s,"\n'
        '            " ai_mention_rate=COALESCE(%s, ai_mention_rate) WHERE id=%s",\n'
        '            (\n'
        '                int(totals_delta["detected_count"]["after"]),\n'
        '                _rate_or_none(totals_delta),  # 无分母 → None → 保留原列值,绝不写 0\n'
        '                diagnosis_id,\n'
        '            ),',
        '            "UPDATE diagnosis_records SET ai_detected_count=ai_detected_count,"\n'
        '            " ai_mention_rate=ai_mention_rate WHERE id=%s AND %s IS NOT NULL",\n'
        '            (\n'
        '                diagnosis_id,\n'
        '                int(totals_delta["detected_count"]["after"]),\n'
        '            ),',
        "rebuild_path_also_refreshes",
    ),
    (
        "M07",
        "分母改口径(拿确定分母 total 冒充 total_tests · §3.1 红线)",
        DECISION,
        '        total_tests = int(ai.get("total_tests") or 0)',
        '        total_tests = int((aggregates.get("totals") or {}).get("total") or 0)',
        "refreshes_the_rate",
    ),
    (
        "M08",
        "回填脚本偷偷改单元格(§3.3 人工已决格不许被机器推翻)",
        BACKFILL,
        '    aggregates = _aggregates(ai, identity)\n    totals_delta = apply_recomputed_totals(ai, aggregates)',
        '    for _it in ai.get("detail_table") or []:\n'
        '        for _c in (_it.get("results") or {}).values():\n'
        '            if isinstance(_c, dict) and _c.get("brand_verdict") == "NO":\n'
        '                _c["brand_verdict"] = "YES"\n'
        '    aggregates = _aggregates(ai, identity)\n'
        '    totals_delta = apply_recomputed_totals(ai, aggregates)',
        "backfill_apply",
    ),
    (
        "M09",
        "回填脚本恢复默认 ids 名单(§4.2 防「顺手全跑」)",
        BACKFILL,
        '    parser.add_argument("--ids", required=True, help="逗号分隔;**必传**,没有默认名单")',
        '    DEFAULT_IDS = (501, 505, 529, 541, 545, 547, 563)\n'
        '    parser.add_argument("--ids", default=",".join(str(i) for i in DEFAULT_IDS))',
        "no_default_id_list",
    ),
    (
        "M10",
        "dry-run 也写库(§4.2 先看后写)",
        BACKFILL,
        '    if not apply:\n        return result',
        '    if not apply:\n        apply = True',
        "backfill_dry_run",
    ),
    (
        "M12",
        "无分母时把 rate 写成 0(凭空把客户推荐率抹成 0% · 比不刷新更坏)",
        DECISION,
        '    if total_tests > 0:\n'
        '        rate_after = round(detected_after / total_tests * 100, 1)\n'
        '        ai["overall_mention_rate"] = rate_after\n'
        '    else:\n'
        '        rate_after = rate_before',
        '    rate_after = round(detected_after / total_tests * 100, 1) if total_tests > 0 else 0\n'
        '    ai["overall_mention_rate"] = rate_after',
        "missing_denominator",
    ),
    (
        "M13",
        "列镜像丢掉 COALESCE(无分母时把列里的 rate 冲成 0)",
        DECISION,
        '                   ai_mention_rate = COALESCE(%s, ai_mention_rate)',
        '                   ai_mention_rate = COALESCE(%s, 0)',
        "missing_denominator",
    ),
    (
        "M11",
        "把派生点搬回各自实现(§3.2 禁止第二套统计 · 元判据应当抓到)",
        REBUILD,
        '    totals_delta = apply_recomputed_totals(ai, aggregates)',
        '    ai["dimension_stats"] = aggregates["dimension_stats"]\n'
        '    ai["detected_count"] = int((aggregates.get("totals") or {}).get("detected") or 0)\n'
        '    ai["overall_mention_rate"] = 0\n'
        '    totals_delta = {"detected_count": {"before": 0, "after": ai["detected_count"]},'
        ' "overall_mention_rate": {"before": 0, "after": 0}}',
        "exactly_one_place",
    ),
]


def _run(expr: str) -> tuple[bool, str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # 坑 2
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", LOCKS, "-q", "--no-header",
         "-p", "no:randomly", "-k", expr],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, timeout=1800,
    )
    return proc.returncode == 0, (proc.stdout or "")[-400:]


def main() -> int:
    if not os.environ.get("TEST_DATABASE_URL"):
        print("[ABORT] 需要 TEST_DATABASE_URL")
        return 2

    originals = {
        rel: (ROOT / rel).read_bytes() for rel in {m[2] for m in MUTATIONS}
    }

    covered = " or ".join(sorted({m[5] for m in MUTATIONS}))
    green, tail = _run(covered)
    if not green:
        print("[ABORT] 基线就是红的,变异结论无意义\n" + tail)
        return 2
    print(f"✅ 基线绿({covered.count('or') + 1} 组被覆盖的用例)\n")

    killed, survived = 0, []
    for tag, desc, rel, old, new, expr in MUTATIONS:
        path = ROOT / rel
        text = originals[rel].decode("utf-8")
        if text.count(old) != 1:
            print(f"⚠️  {tag} 锚点命中 {text.count(old)} 次(预期 1)—— 变异未应用,记作存活")
            survived.append((tag, desc, "锚点未唯一命中"))
            continue
        path.write_bytes(text.replace(old, new, 1).encode("utf-8"))  # 坑 1
        try:
            ok, tail = _run(expr)
        finally:
            path.write_bytes(originals[rel])
            assert path.read_bytes() == originals[rel], f"{rel} 复原失败"
        if ok:
            survived.append((tag, desc, tail))
            print(f"❌ {tag} **存活**:{desc}")
        else:
            killed += 1
            print(f"✅ {tag} 被杀:{desc}")

    for rel, blob in originals.items():
        assert (ROOT / rel).read_bytes() == blob, f"{rel} 未复原"

    green, tail = _run(covered)
    print(f"\n变异 {killed} 杀 / {len(survived)} 存活 · 复原后基线{'绿' if green else '红'}")
    if survived:
        print("\n存活明细(先分诊:锁弱 vs 变异是空操作):")
        for tag, desc, info in survived:
            print(f"  {tag} {desc}\n    {info[:200]}")
    return 0 if (not survived and green) else 1


if __name__ == "__main__":
    raise SystemExit(main())
