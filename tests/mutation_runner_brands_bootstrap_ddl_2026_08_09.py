"""变异检验 · 建表兜底 DDL 与生产迁移对齐(WO P1 2026-08-09)。

自坏防线:清 `__pycache__` + `-p no:cacheprovider` · 锚点命中 ≠1 报 ANCHOR_BAD ·
退出码 5 单独报 NO_TESTS · 写回用原始 bytes 逐字还原。

跑法(需 TEST_DATABASE_URL 指向 throwaway PG):
    python tests/mutation_runner_brands_bootstrap_ddl_2026_08_09.py
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
LOCK_SUITE = "tests/test_brands_bootstrap_ddl_2026_08_09.py"
DB = "db/diagnosis_db.py"
SUITE = "tests/test_brands_bootstrap_ddl_2026_08_09.py"

MUTATIONS: list[tuple[str, str, str, str, str]] = [
    (
        "M01", "全局 UNIQUE 加回建表语句(= bug 原样:新库撞名直冒 500)",
        DB,
        "            name TEXT NOT NULL,               -- 品牌名（唯一性见 brands_name_owner_key)\n",
        "            name TEXT UNIQUE NOT NULL,        -- 品牌名（唯一)\n",
    ),
    (
        "M02", "不建 partial unique(新库连对的约束都没有 —— 同 owner 重名也拦不住)",
        DB,
        '            "CREATE UNIQUE INDEX IF NOT EXISTS brands_name_owner_key "\n'
        '            "ON brands (name, owner_user_id) WHERE is_deleted = false",\n',
        "",
    ),
    (
        "M03", "partial unique 去掉 owner 维度(退化成全局)",
        DB,
        '            "ON brands (name, owner_user_id) WHERE is_deleted = false",\n',
        '            "ON brands (name) WHERE is_deleted = false",\n',
    ),
    (
        "M04", "partial unique 去掉 is_deleted 条件(软删的名字也占坑)",
        DB,
        '            "ON brands (name, owner_user_id) WHERE is_deleted = false",\n',
        '            "ON brands (name, owner_user_id)",\n',
    ),
    (
        "M05", "不清理老库的全局约束(只救新库,CI/老 staging 照样 500)",
        DB,
        '            "ALTER TABLE brands DROP CONSTRAINT IF EXISTS brands_name_key",\n',
        "",
    ),
    (
        "M06", "把 NOT NULL 也一起删掉(顺手改坏别的)",
        DB,
        "            name TEXT NOT NULL,               -- 品牌名（唯一性见 brands_name_owner_key)\n",
        "            name TEXT,                        -- 品牌名\n",
    ),
    # ── 判据自身的判别力 ────────────────────────────────────────────────
    (
        "M07", "抽 DDL 时不剥 SQL 注释(本包注释里复述了原句 → 判据恒假)",
        SUITE,
        '        (line.split("--")[0].rstrip() if "--" in line else line)\n',
        "        line\n",
    ),
]


def _purge_pycache() -> None:
    for path in ROOT.rglob("__pycache__"):
        shutil.rmtree(path, ignore_errors=True)


def _run_locks() -> str:
    _purge_pycache()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:randomly",
         "-p", "no:cacheprovider", LOCK_SUITE],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if proc.returncode == 5:
        return "NO_TESTS"
    return "KILLED" if proc.returncode != 0 else "SURVIVED"


def main() -> int:
    print("── 基线自检:未变异时锁必须全绿 ──")
    if _run_locks() != "SURVIVED":
        print("[ERR ] 基线就不是绿的 → 变异结果无意义,先修基线")
        return 2
    print("   OK 基线全绿\n")

    killed = bad = 0
    for code, note, rel, anchor, replacement in MUTATIONS:
        target = ROOT / rel
        original = target.read_bytes()
        text = original.decode("utf-8")
        hits = text.count(anchor)
        if hits != 1:
            print(f"{code}  [BAD ] ANCHOR_BAD  {note}(锚点命中 {hits} 次,应为 1)")
            bad += 1
            continue
        try:
            with open(target, "w", encoding="utf-8", newline="") as h:
                h.write(text.replace(anchor, replacement))
            verdict = _run_locks()
        finally:
            target.write_bytes(original)
        if verdict == "KILLED":
            killed += 1
        icon = {"KILLED": "[KILL]", "SURVIVED": "[LIVE]", "NO_TESTS": "[NONE]"}[verdict]
        print(f"{code}  {icon} {verdict:9} {note}")

    total = len(MUTATIONS)
    print(f"\n变异 {total} 条 · KILLED {killed} · ANCHOR_BAD {bad} · 其余 {total - killed - bad}")
    if killed != total:
        print("🔴 有变异没被杀死 —— 先分诊「锁写松了」还是「变异是空操作」")
        return 1
    print("✅ 全部 KILLED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
