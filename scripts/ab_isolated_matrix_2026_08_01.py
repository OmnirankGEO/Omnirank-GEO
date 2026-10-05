"""隔离层逐文件 A/B 矩阵(裁定 2026-08-01 升格为交付硬要求)。

用法::

    A_DB=postgresql://... B_DB=postgresql://... B_ROOT=<基线 worktree 路径> \\
        python scripts/ab_isolated_matrix_2026_08_01.py

**为什么必须逐文件单跑**(两次血的教训,同一份交付里各栽一次):
  · lock6(span 级 AI 修复):组合跑两侧都显示 5 红,我据此判成"全是既有污染"——
    单跑才现:基线 20 passed / 我方 1 failed。**是我的真回归**。
  · t1(问句比例):组合跑 220 passed / 5 failed 把它盖住了 ——
    单跑才现:基线 30 passed / 我方 1 failed。**又是我的真回归**。

集合层相同的红数会互相掩盖:两侧都红 N 条 ≠ 两侧红的是同一批。
所以判"有没有引入回归"**只能**在隔离层比,一个文件一个文件比。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

A_ROOT = Path(__file__).resolve().parent.parent
B_ROOT = Path(os.environ.get("B_ROOT", r"C:\AI-Test\_aireview_base_24c46669"))
A_DB = os.environ.get("A_DB") or os.environ.get("TEST_DATABASE_URL", "")
B_DB = os.environ.get("B_DB", "")

#: 本次交付**波及到的既有 suite**(我改过其断言、或改动的代码被它们覆盖)。
#: 新增的自有 suite 基线上不存在,单列在下面 NEW_ONLY。
TOUCHED = (
    "tests/test_publish_gate_tristate_2026_07_31.py",
    "tests/test_p07_publish_gate_trio.py",
    "tests/test_article_review_gate_reason_code_layering.py",
    "tests/test_c4_review_autopilot_2026_07_27.py",
    "tests/test_p3a_batch_review_2026_08_01.py",
    "tests/test_span_level_ai_repair_2026_07_30.py",
    "tests/test_legal_prohibition_single_source.py",
    "tests/test_fallback_title_form_2026_08_01.py",
    "tests/test_title_batch_dedupe_2026_07_31.py",
    "tests/test_title_question_and_length_2026_07_29.py",
)

NEW_ONLY = (
    "tests/test_ai_review_flow_fusion_2026_08_01.py",
    "tests/test_title_naturalness_2026_08_01.py",
    "tests/test_flywheel_closeout_2026_08_01.py",
    "tests/test_strategy_binding_ai_2026_08_01.py",
)


def run_one(root: Path, db: str, test_file: str) -> tuple[int, int, list[str]]:
    """单文件单跑。返回 (passed, failed+error, 失败用例名)。"""
    if not (root / test_file).exists():
        return (-1, -1, [])
    env = {**os.environ, "TEST_DATABASE_URL": db, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", test_file, "-q",
         "-p", "no:randomly", "-p", "no:cacheprovider", "--tb=no"],
        cwd=root, capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env,
    )
    out = proc.stdout + proc.stderr
    passed = sum(int(m) for m in re.findall(r"(\d+) passed", out))
    bad = sum(int(m) for m in re.findall(r"(\d+) failed", out)) + \
        sum(int(m) for m in re.findall(r"(\d+) error", out))
    names = [n.split("::")[-1] for n in re.findall(r"^(?:FAILED|ERROR) (\S+)", out, re.M)]
    return passed, bad, names


def main() -> int:
    if not A_DB or not B_DB:
        print("需要 A_DB 与 B_DB(两个独立一次性库,库名含 test)")
        return 2

    print("=" * 100)
    print(f"A(HEAD) = {A_ROOT}")
    print(f"B(基线) = {B_ROOT}")
    print("=" * 100)
    print(f"{'suite':<58}{'B 基线':>16}{'A 本包':>16}  判定")
    print("-" * 100)

    regressions: list[str] = []
    for test_file in TOUCHED:
        bp, bb, bn = run_one(B_ROOT, B_DB, test_file)
        ap, ab, an = run_one(A_ROOT, A_DB, test_file)
        short = test_file.replace("tests/", "")
        if bp < 0:
            verdict = "基线无此文件(新增)"
        elif ab > bb or (set(an) - set(bn)):
            verdict = f"🔴 回归 +{sorted(set(an) - set(bn))}"
            regressions.append(f"{short}: {sorted(set(an) - set(bn))}")
        elif bb > ab:
            verdict = "✅ 我方更好(修掉了基线的红)"
        else:
            verdict = "✅ 同"
        print(f"{short:<58}{f'{bp}p/{bb}f':>16}{f'{ap}p/{ab}f':>16}  {verdict}")

    print("-" * 100)
    for test_file in NEW_ONLY:
        ap, ab, an = run_one(A_ROOT, A_DB, test_file)
        short = test_file.replace("tests/", "")
        if ab:
            regressions.append(f"{short}: {an}")
        print(f"{short:<58}{'(新增)':>16}{f'{ap}p/{ab}f':>16}  "
              f"{'✅' if not ab else '🔴 ' + str(an)}")

    print("=" * 100)
    if regressions:
        print("🔴 隔离层发现回归:")
        for r in regressions:
            print("   ", r)
        return 1
    print("✅ 隔离层逐文件比对:零回归")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
