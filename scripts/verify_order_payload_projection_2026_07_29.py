"""订单类端点字段投影 · 变异判别力证据(2026-07-29)

对应要求:「变异:任一端点改成原样返回 jsonb → 转红」。

## 验的是【行为】
  改产品源码 → 起 FastAPI TestClient 真打端点 → 数真 FAILED/ERROR → git checkout 还原;
  变异后先跑 import 冒烟(语法红不算判别力);FAILED 与 ERROR 两类都统计。
  零 `assert "xxx" in src`。

## 三条变异
  OP-1 `_serialize_order` 非 admin 分支直接返回整行  → 服务商拿到 margin_cents /
       seller_cost_basis_cents / downstream_markup_bps → 必红
  OP-2 `_serialize_order` 把 admin 专属那段无条件塞进去(等价"忘了判 is_admin")→ 必红
  OP-3 `get_order_visible` 把 is_admin 恒真 → 详情/导出两条端点必红

前置:需要 dealer_resale_test 专用 PG16(见 tests/dealer_inventory_resale/conftest.py 的
      硬性守卫)。环境变量:
        TEST_DATABASE_URL=DEALER_RESALE_TEST_DATABASE_URL=
            postgresql://dealer_test:dealer_test_pw@127.0.0.1:55545/dealer_resale_test
        ALLOW_DESTRUCTIVE_TEST_DB=1
      连不上就如实判失败 —— SKIP 不是 PASS。
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

LOCKS = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "tests/dealer_inventory_resale/test_order_payload_projection.py"]

RESALE = "services/dealer_inventory_resale.py"

NONADMIN_BRANCH_OLD = """    else:
        is_seller = int(actor_user_id) == int(row["seller_user_id"])
        data["direction"] = "sale" if is_seller else "purchase"
        data["refund_handling"] = "PLATFORM_MANAGED\""""
NONADMIN_BRANCH_NEW = """    else:
        data = dict(row)   # [mutation] 原样回吐整行"""

ADMIN_GUARD_OLD = """    if is_admin:
        data.update({
            "seller_cost_basis_cents": int(row["seller_cost_basis_cents"]),"""
ADMIN_GUARD_NEW = """    if True:
        data.update({
            "seller_cost_basis_cents": int(row["seller_cost_basis_cents"]),"""

VISIBLE_OLD = """    return _serialize_order(cur, order, actor_user_id=actor_user_id, is_admin=is_admin)"""
VISIBLE_NEW = """    return _serialize_order(cur, order, actor_user_id=actor_user_id, is_admin=True)"""


@dataclass
class Case:
    key: str
    what: str
    old: str
    new: str


CASES = [
    Case("OP-1", "_serialize_order 非 admin 分支原样返回整行(要求:转红)",
         NONADMIN_BRANCH_OLD, NONADMIN_BRANCH_NEW),
    Case("OP-2", "admin 专属字段段忘了判 is_admin(要求:转红)",
         ADMIN_GUARD_OLD, ADMIN_GUARD_NEW),
    Case("OP-3", "get_order_visible 把 is_admin 恒真(要求:转红)",
         VISIBLE_OLD, VISIBLE_NEW),
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


def assert_clean_worktree() -> bool:
    out = subprocess.run(["git", "status", "--porcelain", "--", RESALE],
                         cwd=ROOT, capture_output=True, text=True)
    if out.stdout.strip():
        print(f"🔴 {RESALE} 有未提交改动 —— 变异靠 git checkout 还原,会把它冲掉。请先 commit。")
        return False
    return True


def apply_case(case: Case) -> int:
    path = ROOT / RESALE
    text = path.read_text(encoding="utf-8")
    if case.old not in text:
        print(f"[{case.key}] [ANCHOR-MISS] {RESALE}")
        return -1
    try:
        path.write_text(text.replace(case.old, case.new, 1), encoding="utf-8")
        smoke = run([sys.executable, "-c", "import services.dealer_inventory_resale"])
        if smoke.returncode != 0:
            print(f"[{case.key}] [SMOKE-FAIL] 变异后 import 不过 —— 这种红不算判别力")
            return -1
        out = run(LOCKS)
        return red_count(out.stdout + out.stderr)
    finally:
        subprocess.run(["git", "checkout", "--", RESALE], cwd=ROOT, check=False)


def main() -> int:
    if not assert_clean_worktree():
        return 2

    out = run(LOCKS)
    blob = out.stdout + out.stderr
    reds, greens = red_count(blob), green_count(blob)
    print(f"基线(未变异): passed={greens} reds={reds}")
    if reds or greens == 0:
        print("基线就不干净 —— 变异结果不作数(0 passed 时的红是环境红,不是判别力)")
        print(blob[-3000:])
        return 1

    print("\n变异:")
    bad = []
    for case in CASES:
        got = apply_case(case)
        if got <= 0:
            bad.append(case)
            print(f"[{case.key}] [UNEXPECTED] reds={got}  {case.what}")
            continue
        print(f"[{case.key}] [AS-EXPECTED] reds={got}  {case.what}")

    print("\n" + "=" * 74)
    if bad:
        print("未达预期:" + " / ".join(c.key for c in bad))
        return 1
    print("任一端点改成原样回吐订单行/快照 → 判别锁全部转红。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
