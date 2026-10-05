"""最小验证包 · 写作链 T5 变异判别力(2026-07-29 拆包后从归属包分出)

原脚本 `scripts/verify_false_green_fixes_2026_07_29.py` 里的 **FG-2** 一组验的是
`services/topic_generation_reservation.py`,随 T5 一起拆到本包。归属包那份只留
FG-1(pending 保护)与 FR(奖励资金路径)。

## 验的是【行为】,不是源码串断言
  - 全脚本没有一处 `assert "xxx" in src`;
  - 改产品源码 → 跑真 pytest → 数真 FAILED/ERROR → `git checkout` 还原;
  - 变异后先跑 import 冒烟:import 都过不去的红是语法红,不算判别力;
  - FAILED 与 ERROR 两类都统计 —— ERROR 不是 PASS。

## FG-2 的来历
交付时自曝:假 cursor 按 SQL 前缀分派后**自己重写了一份语义**,产品的 WHERE 怎么改
它都照自己那套跑 → 三条 WHERE 变异全不转红。已把假 cursor 改成**解析真实 SET/WHERE
子句再执行**(`_predicates`/`_matches`),本脚本就是那次修复"现在真的能转红"的证据。

用法:  python scripts/verify_false_green_fixes_writing_2026_07_29.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

T5_LOCKS = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "tests/test_topic_generation_reservation_2026_07_28.py"]

RESERVATION = "services/topic_generation_reservation.py"


@dataclass
class Case:
    key: str
    what: str
    old: str
    new: str
    expect_red: bool


CASES = [
    Case(
        "FG2-C1", "clear 的 WHERE 丢掉 generation_request_id 条件(要求:转红)",
        "                WHERE quote_id=%s\n                  AND generation_request_id=%s",
        "                WHERE quote_id=%s\n                  AND %s IS NOT NULL",
        expect_red=True,
    ),
]


def run(cmd):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def red_count(output: str) -> int:
    """FAILED 与 ERROR 两类都算。"""
    summary = ""
    for line in reversed(output.strip().splitlines()):
        if re.search(r"\b\d+ (?:failed|passed|error|errors|skipped)\b", line) and " in " in line:
            summary = line
            break
    total = 0
    for pattern in (r"\b(\d+) failed\b", r"\b(\d+) errors?\b"):
        match = re.search(pattern, summary)
        if match:
            total += int(match.group(1))
    total += len(re.findall(r"^(?:FAILED|ERROR) ", output, re.MULTILINE))
    return total


def green_count(output: str) -> int:
    match = re.search(r"\b(\d+) passed\b", output)
    return int(match.group(1)) if match else 0


def apply_case(case: Case) -> int:
    path = ROOT / RESERVATION
    text = path.read_text(encoding="utf-8")
    if case.old not in text:
        print(f"[{case.key}] [ANCHOR-MISS] {RESERVATION}")
        return -1
    try:
        path.write_text(text.replace(case.old, case.new, 1), encoding="utf-8")
        smoke = run([sys.executable, "-c", "import services.topic_generation_reservation"])
        if smoke.returncode != 0:
            print(f"[{case.key}] [SMOKE-FAIL] 变异后 import 不过 —— 这种红不算判别力")
            return -1
        out = run(T5_LOCKS)
        return red_count(out.stdout + out.stderr)
    finally:
        subprocess.run(["git", "checkout", "--", RESERVATION], cwd=ROOT, check=False)


def main() -> int:
    out = run(T5_LOCKS)
    blob = out.stdout + out.stderr
    reds, greens = red_count(blob), green_count(blob)
    print(f"基线(未变异)T5 锁: passed={greens} reds={reds}")
    if reds or greens == 0:
        print("基线就不干净 —— 变异结果不作数(0 passed 时的红是环境红,不是判别力)")
        return 1

    print("\n变异:")
    bad = []
    for case in CASES:
        got = apply_case(case)
        if got < 0:
            bad.append(case)
            continue
        ok = (got > 0) == case.expect_red
        print(f"[{case.key}] {'[AS-EXPECTED]' if ok else '[UNEXPECTED]'} "
              f"reds={got} expect_red={case.expect_red}  {case.what}")
        if not ok:
            bad.append(case)

    print("\n" + "=" * 74)
    if bad:
        print(f"未达预期 {len(bad)} 条:" + " / ".join(c.key for c in bad))
        return 1
    print("假 cursor 改成解析真实 SET/WHERE 之后,WHERE 变异确实转红。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
