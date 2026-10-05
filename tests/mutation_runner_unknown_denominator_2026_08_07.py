"""变异检验 · 出现率分母「身份待确认」明示 · 2026-08-07。

跑法(需 throwaway PG,与锁套件同一个):
    TEST_DATABASE_URL=postgresql://... python tests/mutation_runner_unknown_denominator_2026_08_07.py

判据 = python 锁套件 **+** 前端门禁(node)一起跑:披露横跨后端归类、DTO 映射、
两个渲染面。只跑一侧会漏掉"改了后端让前端静默降级"这类。

🔴 自坏防线:
  1. 每跑前清 `__pycache__`,子进程 `-B` + `PYTHONDONTWRITEBYTECODE=1`(缓存会把
     「已杀死」误报成「存活」· 单向偏差);
  2. 锚点命中数必须恰为 1,否则 ANCHOR_BAD 整体失败;
  3. 退出码 5 单独报 NO_TESTS,不算杀死;
  4. **全 ASCII 标记 + stdout 切 UTF-8** —— Windows GBK 控制台会被中文/emoji 打崩,
     「我这儿能跑」不是判据(复审 2026-08-06 实测)。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent.parent
PY_SUITE = "tests/test_unknown_denominator_disclosure_2026_08_07.py"
JS_GATE = "scripts/test-unknown-denominator-disclosure.mjs"

PRESENT = "services/public_report_presentation.py"
REVIEW = "services/diagnosis_identity_review.py"
PLATFORM = "frontend/src/features/publicReportPremium/components/sections/PlatformPerformance.tsx"
MAPDTO = "frontend/src/features/publicReportPremium/transport/mapDto.ts"
CELL = "frontend/src/pages/Diagnosis/components/BrandVerdictCell.tsx"

MUTATIONS: list[tuple[str, str, str, str, str]] = [
    ("U01", "[边界1] 把待确认算进分母(改统计口径 -- 那半边还挂 Owner)",
     PRESENT,
     "            if _is_identity_pending(result):\n"
     "                stats[\"identity_pending\"] += 1\n"
     "            else:\n"
     "                stats[\"errors\"] += 1\n",
     "            stats[\"valid\"] += 1\n            stats[\"competitive_valid\"] += 1\n"),

    ("U02", "待确认又被说成「部分失败」(改口回退)",
     PRESENT,
     "        elif stats[\"identity_pending\"]:\n",
     "        elif False:\n"),

    ("U03", "[边界4] 0 时给 0 而不是 None -> 563 报告多出「0 次待确认」",
     PRESENT,
     '            "identityPendingSamples": stats["identity_pending"] or None,',
     '            "identityPendingSamples": stats["identity_pending"],'),

    ("U04", "计数判据另立一套(verdict==UNKNOWN 就算)-- 说的 N 不等于扣掉的 N",
     REVIEW,
     "    return classify_cell_state(\n"
     "        cell, identity=None, allow_local_rejudge=False\n"
     "    ) == STATE_PENDING_IDENTITY\n",
     "    return str(cell.get(\"brand_verdict\") or \"\").upper() == \"UNKNOWN\"\n"),

    ("U05", "计数判据恒真(把真失败也说成待确认)",
     REVIEW,
     "def is_identity_pending_cell(cell: Any) -> bool:\n",
     "def is_identity_pending_cell(cell: Any) -> bool:\n    return True\n"),

    ("U06", "[边界4] C 端明示块改成无条件渲染(563 多一句废话)",
     PLATFORM,
     "                    {pendingDisclosureTotal > 0 && (\n",
     "                    {true && (\n"),

    ("U07", "[复审] 映射层漏校验新字段 -> 整段平台数据静默降级",
     MAPDTO,
     "        && isOptionalCount(value.identityPendingSamples)\n",
     "\n"),

    ("U08", "[边界4] 代理端去确认按钮改成无条件渲染",
     CELL,
     "                    {pendingTotal > 0 && (\n"
     "                        <Button\n"
     "                            variant=\"outline\"\n",
     "                    {true && (\n"
     "                        <Button\n"
     "                            variant=\"outline\"\n"),

    ("U09", "代理端明示只说格数不说分母(退回旧版信息量)",
     CELL,
     "                            已确认 {definiteTotal} 格 · 出现率按这 {definiteTotal} 格算 ·\n"
     "                            另 {pendingTotal} 格未计入\n",
     "                            有几格待确认\n"),
]


def _purge() -> None:
    for path in ROOT.rglob("__pycache__"):
        shutil.rmtree(path, ignore_errors=True)


def _run() -> str:
    _purge()
    py = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-q", "-x", "-p", "no:randomly",
         "-p", "no:cacheprovider", PY_SUITE],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if py.returncode == 5:
        return "NO_TESTS"
    if py.returncode != 0:
        return "KILLED"
    js = subprocess.run(
        ["node", JS_GATE], cwd=str(ROOT / "frontend"),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return "KILLED" if js.returncode != 0 else "SURVIVED"


def main() -> int:
    if not os.getenv("TEST_DATABASE_URL"):
        print("[ERR ] 需要 TEST_DATABASE_URL")
        return 2
    print("---- 基线自检:未变异时 python 锁 + 前端门禁必须全绿 ----")
    if _run() != "SURVIVED":
        print("[ERR ] 基线不是绿的 -> 变异结果无意义")
        return 2
    print("[OK  ] 基线全绿\n")

    results = []
    for code, note, rel, anchor, repl in MUTATIONS:
        target = ROOT / rel
        original = target.read_text(encoding="utf-8")
        hits = original.count(anchor)
        if hits != 1:
            results.append((code, "ANCHOR_BAD", note))
            print(f"{code}  [BAD ] ANCHOR_BAD  {note}(命中 {hits} 次,应为 1)")
            continue
        try:
            with open(target, "w", encoding="utf-8", newline="") as fh:
                fh.write(original.replace(anchor, repl))
            verdict = _run()
        finally:
            with open(target, "w", encoding="utf-8", newline="") as fh:
                fh.write(original)
        icon = {"KILLED": "[KILL]", "SURVIVED": "[LIVE]", "NO_TESTS": "[NONE]"}[verdict]
        results.append((code, verdict, note))
        print(f"{code}  {icon} {verdict:9} {note}")

    _purge()
    killed = sum(1 for _, v, _ in results if v == "KILLED")
    print(f"\n变异 {len(results)} 条 · 杀死 {killed} · 存活/异常 {len(results) - killed}")
    bad = [(c, v, n) for c, v, n in results if v != "KILLED"]
    if bad:
        print("[FAIL] 下列变异未被任何锁抓住:")
        for c, v, n in bad:
            print(f"   {c} [{v}] {n}")
        return 1
    print("[PASS] 全部杀死")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
