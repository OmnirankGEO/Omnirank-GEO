#!/usr/bin/env python3
"""[打包商品下架 2026-08-10] 变异 runner。

每条变异 = 把本包一处真改动改回改前的样子,期望对应锁转红。
纯前端那几条 python 套件够不到 —— **不当免检**,由
`frontend/scripts/test-delist-entries.mjs` 覆盖,在此显式声明为预期存活。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = pathlib.Path(__file__).resolve().parents[2]
SUITES = ["tests/delist_sku_2026_08_10/"]

MUTANTS: list[tuple[str, str, str, str, str]] = [
    ("M1", "db/wallet_db.py",
     "        ('monitor_single',    '监测单次检测',         130,  1.0,  False),\n",
     "        ('monitor_single',    '监测单次检测',         130,  1.0,  False),\n"
     "        ('monitor_month_10',  '监测月包10词条',     39000, 150.0, False),\n",
     "把下架商品加回种子 → 新环境会以 is_active=true 复活"),

    ("M2", "services/organization_contract.py",
     '    "scheduled_monitoring": "monitoring.run",\n',
     '    "scheduled_monitoring": "monitoring.run",\n    "rank_alert": "monitoring.configure",\n',
     "权限映射又指向已下架商品(死代码)"),

    ("M3", "api/wallet_api.py",
     "    try:\n        result = await deduct_points(user[\"user_id\"], req.feature_code)\n    except ValueError as exc:",
     "    if True:\n        result = await deduct_points(user[\"user_id\"], req.feature_code)\n    if False:\n        exc = None",
     "退回改前:ValueError 不捕 → 下架商品打成 500"),

    ("M4", "api/wallet_api.py",
     '            detail="该功能商品当前不可用（已下架或不存在），本次未扣任何算力。"\n'
     '                   "如需该能力，请在价目表中选择在售功能，或联系平台。",',
     '            detail="该功能商品当前不可用（已下架或不存在），本次未扣任何算力。",',
     "拒绝文案去掉出口 → 用户被堵死在原地(总册 §13.5)"),

    ("M5", "db/migration_manifest.py",
     '    "scripts/migration_v3_3_managed_campaign.sql",\n',
     "",
     "🔴 反注册托管迁移 = 新环境建不出托管定价行 = 事实废弃(Owner 禁做项)"),

    # ── 纯前端(python 套件够不到)· 预期存活 · 由 mjs 锁覆盖 ──────────
    ("F1", "frontend/src/config/managedEntryGate.ts",
     "export const MANAGED_ENTRY_ENABLED = false;",
     "export const MANAGED_ENTRY_ENABLED = true;",
     "翻开托管入口总闸(纯前端 · 预期存活)"),
    ("F2", "frontend/src/pages/Pricing/PricingPage.tsx",
     "  'monitor_month_10',\n  'rank_alert',\n",
     "",
     "价目表不再隐藏下架商品(纯前端 · 预期存活)"),
]

EXPECTED_SURVIVORS = {"F1", "F2"}


def run() -> tuple[bool, str]:
    r = subprocess.run([sys.executable, "-m", "pytest", *SUITES, "-q"],
                       cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    tail = (r.stdout or "").strip().splitlines()
    return r.returncode == 0, (tail[-1] if tail else "")


def main() -> int:
    ok, line = run()
    print(f"[基线] {'全绿' if ok else '有红'} · {line}")
    if not ok:
        print("❌ 基线不绿,变异无意义")
        return 2

    killed, survived = [], []
    for code, rel, old, new, why in MUTANTS:
        p = ROOT / rel
        orig = p.read_text(encoding="utf-8")
        if old not in orig:
            print(f"  {code:<3} ⚠️  锚点找不到 · {rel}")
            survived.append((code, why, "锚点缺失"))
            continue
        try:
            p.write_text(orig.replace(old, new, 1), encoding="utf-8", newline="")
            passed, tail = run()
            (survived.append((code, why, tail)) if passed else killed.append(code))
            print(f"  {code:<3} {'🔴 存活' if passed else '✅ 被杀'} · {why}")
        finally:
            p.write_text(orig, encoding="utf-8", newline="")

    print(f"\n变异 {len(killed)}/{len(MUTANTS)} 被杀")
    hard = [c for c, _w, _t in survived if c not in EXPECTED_SURVIVORS]
    if survived:
        print("存活分诊:")
        for c, w, t in survived:
            tag = "(已声明:纯前端,mjs 锁覆盖)" if c in EXPECTED_SURVIVORS else "🔴 非预期"
            print(f"  {c} {tag}: {w}")
    if hard:
        print(f"❌ MUTATION_FAIL —— 非预期存活: {hard}")
        return 1
    print("✅ MUTATION_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
