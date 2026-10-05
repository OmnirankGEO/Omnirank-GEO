"""变异检验 · 改名冲突检查按 (name, owner_user_id)(WO 快修 2026-08-08)。

自坏防线:清 `__pycache__` + `-p no:cacheprovider` · 锚点命中 ≠1 报 ANCHOR_BAD ·
退出码 5 单独报 NO_TESTS · 写回用原始 bytes 逐字还原。

跑法(需 TEST_DATABASE_URL 指向 throwaway PG):
    python tests/mutation_runner_brand_rename_scope_2026_08_08.py
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
LOCK_SUITE = "tests/test_brand_rename_conflict_scope_2026_08_08.py"
API = "api/brand_api.py"
SUITE_FILE = "tests/test_brand_rename_conflict_scope_2026_08_08.py"

MUTATIONS: list[tuple[str, str, str, str, str]] = [
    (
        "M01", "整条改回全局预查(= bug 原样:别人用过的名字挡住新用户)",
        API,
        '                    "SELECT id FROM brands "\n'
        '                    "WHERE name = %s AND owner_user_id = %s AND is_deleted = false "\n'
        '                    "  AND id <> %s LIMIT 1",\n'
        '                    (name, user_id, brand_id),\n',
        '                    "SELECT id FROM brands WHERE name = %s AND id <> %s LIMIT 1",\n'
        '                    (name, brand_id),\n',
    ),
    (
        "M02", "只去掉 owner 收窄(保留 is_deleted —— 半修)",
        API,
        '"WHERE name = %s AND owner_user_id = %s AND is_deleted = false "',
        '"WHERE name = %s AND %s IS NOT NULL AND is_deleted = false "',
    ),
    (
        "M03", "只去掉 is_deleted(软删的名字又变成冲突)",
        API,
        '"WHERE name = %s AND owner_user_id = %s AND is_deleted = false "',
        '"WHERE name = %s AND owner_user_id = %s AND %s IS NOT NULL "',
    ),
    (
        "M04", "owner 参数写死(不再是当前登录用户)",
        API,
        '                    (name, user_id, brand_id),\n',
        '                    (name, 1, brand_id),\n',
    ),
    (
        "M05", "去掉 id <> 自己(改成自己现在的名字都算冲突)",
        API,
        '"  AND id <> %s LIMIT 1"',
        '"  AND id <> -1 LIMIT 1"',
    ),
    (
        "M06", "预查恒不命中(等于把冲突保护整个删掉 —— UPDATE 会撞 unique index 抛 500)",
        API,
        '                name_conflict = cur.fetchone()\n',
        '                name_conflict = None\n',
    ),
    # ── 判据自身的判别力 ────────────────────────────────────────────────
    (
        # 夹具是"先造再删"两步(见套件里的注释),所以 DROP 那一行出现两次;
        # 变异要打的是**造完之后那一次删**,锚点必须带上前一行才唯一。
        "M07", "夹具造了全局唯一约束却不删(测的就不是生产那个约束了)",
        SUITE_FILE,
        'ALTER TABLE brands ADD CONSTRAINT brands_name_key UNIQUE (name);\n'
        'ALTER TABLE brands DROP CONSTRAINT IF EXISTS brands_name_key;\n',
        'ALTER TABLE brands ADD CONSTRAINT brands_name_key UNIQUE (name);\n',
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
