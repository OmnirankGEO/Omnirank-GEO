"""变异自检 · 监测平台授权口径包(R1-R4)· 2026-08-04

规矩(踩过的坑,写成代码不靠记):
  1. 每条锁都要有一个**能杀死它**的变异。杀不掉 = 锁是摆设,或者测试用例打偏了。
  2. 变异必须**先证明自己改到了东西** —— 找不到 old 串就报 NO-OP(不是 SURVIVED),
     否则"没改到"会被读成"锁很强"。
  3. 跑完必须逐文件比对 sha256 复原,防止把变异烤进交付代码。
  4. 全程不碰 git(上个包 git stash 撞上后台 runner,改动被写回丢失 + 变异被烤进交付)。

跑法(不在 pytest 收集范围内,手动跑):
    MONITORING_SCHEDULER_PG_TEST_URL=... python tests/_mutation_selfcheck_platauth_20260804.py
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

STRUCT = "tests/test_monitoring_platform_authority_2026_08_04.py"
DIAG = "tests/test_diagnosis_report_fix_2026_07_26.py"
RESOLVER = "tests/geo_monitoring_content_loop/test_brand_identity_resolver.py"

# (编号, 说明, 目标文件, old, new, 期望转红的测试选择)
MUTATIONS = [
    # ---------------- R1 ----------------
    (
        "M1", "R1 helper 恒返 None(等于没继承)",
        "db/monitoring_db.py",
        "    verified_quote_id = _resolve_delivery_quote_id(",
        "    return None\n    verified_quote_id = _resolve_delivery_quote_id(",
        f"{STRUCT}::test_acceptance_end_to_end_real_postgres",
    ),
    (
        "M2", "R1 去掉 quote 归属核验(直接信 client_id)",
        "db/monitoring_db.py",
        "    verified_quote_id = _resolve_delivery_quote_id(\n        client_id=client_id, brand_id=brand_id\n    )",
        "    verified_quote_id = int(client_id) if client_id and str(client_id).isdigit() else None",
        f"{STRUCT}::test_acceptance_end_to_end_real_postgres",
    ),
    (
        "M3", "R1 不接线:add_keyword 仍抄产品默认",
        "db/monitoring_db.py",
        "        platforms = resolve_extra_keyword_platforms(\n            brand_id=resolved_brand_id, client_id=client_id_compat\n        )",
        "        platforms = None",
        f"{STRUCT}::test_acceptance_end_to_end_real_postgres",
    ),
    # ---------------- R2 ----------------
    (
        "M4", "R2 cells 层回退到商品版本常量(数值相同·纯接线回归)",
        "db/monitoring_db.py",
        "                if platform in MONITORING_RUN_CELL_PLATFORMS and platform not in entitlement:",
        "                if platform in MONITORING_CLASSIC4_ORDER and platform not in entitlement:",
        f"{STRUCT}::test_r2_both_layers_read_the_same_run_cell_whitelist",
    ),
    (
        "M5", "R2 常量偷偷扩到五路(CHECK 没跟着改)",
        "config/ai_engines.py",
        '    "kimi",\n    "doubao",\n)',
        '    "kimi",\n    "doubao",\n    "yuanbao",\n)',
        f"{STRUCT}::test_r2_constant_agrees_with_the_db_check_it_claims_to_mirror",
    ),
    (
        "M6", "R2 去掉裁 eligible 的账本面钳制",
        "tools/monitoring/batch_monitor.py",
        "            if platform not in MONITORING_RUN_CELL_PLATFORMS:\n                skipped_ledger.append(platform)\n                continue",
        "            pass",
        f"{DIAG}::test_p0_2_every_default_monitoring_platform_is_actually_collectable "
        f"{RESOLVER}::test_daily_monitoring_filters_non_collectable_platforms",
    ),
    (
        "M7", "R2 守卫错误不再点名到词",
        "db/monitoring_db.py",
        '                    f"keyword_id={keyword_id} source={source} "',
        '                    ""',
        f"{STRUCT}::test_r2_guard_stays_fail_closed_and_names_the_keyword_and_platform "
        f"{STRUCT}::test_acceptance_end_to_end_real_postgres",
    ),
    (
        "M8", "R2 守卫从 raise 降级成 log(fail-closed 被削)",
        "db/monitoring_db.py",
        "            if excess:\n                raise MonitoringCellConflict(",
        "            if excess:\n                print(\n                    MonitoringCellConflict(",
        f"{STRUCT}::test_r2_guard_stays_fail_closed_and_names_the_keyword_and_platform",
    ),
    # ---------------- R3 ----------------
    (
        "M9", "R3 迁移不登记 manifest(建了但永远不跑)",
        "db/migration_manifest.py",
        '    "scripts/migration_monitoring_extra_keyword_entitlement_2026_08_04.sql",',
        "",
        f"{STRUCT}::test_r3_migration_is_registered_in_the_manifest",
    ),
    (
        "M10", "R3 迁移动了列默认值(会把两槽做成起不来)",
        "scripts/migration_monitoring_extra_keyword_entitlement_2026_08_04.sql",
        "SET LOCAL search_path = pg_catalog, public;",
        "SET LOCAL search_path = pg_catalog, public;\nALTER TABLE public.extra_keywords\n    ALTER COLUMN platforms SET DEFAULT 'dashscope,deepseek,kimi,doubao';",
        f"{STRUCT}::test_r3_migration_must_not_touch_column_defaults",
    ),
    (
        "M11", "R3 迁移去掉归属核验(会改到别人 quote 的行)",
        "scripts/migration_monitoring_extra_keyword_entitlement_2026_08_04.sql",
        "       AND q.brand_id = e.brand_id       -- 归属核验:brand 对不上一律不动",
        "",
        f"{STRUCT}::test_acceptance_end_to_end_real_postgres",
    ),
    # ---------------- R4 ----------------
    (
        "M12", "R4 去掉连接串脱敏",
        "api/monitoring_api.py",
        "    for pattern in _PLAN_ERROR_REDACT_PATTERNS:\n        summary = pattern.sub(\"[redacted]\", summary)",
        "    pass",
        f"{STRUCT}::test_r4_plan_error_summary_carries_the_cause_and_redacts_credentials",
    ),
    (
        "M13", "R4 去掉截断",
        "api/monitoring_api.py",
        'return " ".join(summary.split())[:_PLAN_ERROR_SUMMARY_MAX]',
        'return " ".join(summary.split())',
        f"{STRUCT}::test_r4_plan_error_summary_carries_the_cause_and_redacts_credentials",
    ),
    (
        "M14", "R4 摘要没接进 SSE error 事件(死函数)",
        "api/monitoring_api.py",
        "'plan_error': plan_error_summary",
        "'plan_error_unused': plan_error_summary",
        f"{STRUCT}::test_r4_sse_error_event_actually_carries_plan_error",
    ),
]

TOUCHED = sorted({m[2] for m in MUTATIONS})


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_tests(selection: str) -> bool:
    """True = 全绿。"""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "--tb=no",
         *selection.split()],
        cwd=ROOT, capture_output=True, text=True,
    )
    return result.returncode == 0


def main() -> int:
    if not os.environ.get("MONITORING_SCHEDULER_PG_TEST_URL"):
        print("!! MONITORING_SCHEDULER_PG_TEST_URL 未设 —— 端到端那条会被 skip,"
              "变异会假存活。拒绝在这种条件下出结论。")
        return 2

    backup_dir = Path(tempfile.mkdtemp(prefix="platauth_mut_"))
    baseline = {}
    for rel in TOUCHED:
        src = ROOT / rel
        dst = backup_dir / rel.replace("/", "__")
        shutil.copy2(src, dst)
        baseline[rel] = sha256(src)

    # 反向对照:没有变异时必须**全绿**。恒红的锁和恒真的锁一样废。
    all_selection = f"{STRUCT} {DIAG} {RESOLVER}"
    print("== 基线(无变异)必须全绿 ==")
    # DIAG 里有两条与本包无关的既有红(p0_3 文案 / protected_files),按名字排除。
    baseline_sel = (
        f"{STRUCT} {RESOLVER} "
        f"{DIAG}::test_p0_2_every_default_monitoring_platform_is_actually_collectable "
        f"{DIAG}::test_sold_matrix_minus_executable_surface_is_declared_not_silent"
    )
    if not run_tests(baseline_sel):
        print("!! 基线就是红的,变异结果全部不可信。停。")
        shutil.rmtree(backup_dir)
        return 3
    print("   基线全绿 ✅")

    killed, survived, noop = [], [], []
    for tag, desc, rel, old, new, selection in MUTATIONS:
        path = ROOT / rel
        original = path.read_text(encoding="utf-8", newline="")
        if original.count(old) != 1:
            noop.append(f"{tag} {desc} —— 锚点命中 {original.count(old)} 次(应为 1)")
            print(f"[{tag}] NO-OP ❌ 锚点没唯一命中,这条变异什么都没证明")
            continue
        # 🔴 newline="" 读写:Windows 上用默认模式会把整个文件行尾翻转,
        #    那既不是变异也不是复原,是把 CRLF 烤进交付代码。
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(original.replace(old, new, 1))
        try:
            green = run_tests(selection)
        finally:
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(original)
        if green:
            survived.append(f"{tag} {desc}")
            print(f"[{tag}] SURVIVED ❌ {desc}")
        else:
            killed.append(tag)
            print(f"[{tag}] killed ✅ {desc}")

    print("\n== 复原核验(sha256 逐文件) ==")
    drift = [rel for rel in TOUCHED if sha256(ROOT / rel) != baseline[rel]]
    for rel in TOUCHED:
        mark = "❌ 漂了" if rel in drift else "✅"
        print(f"   {mark} {rel}")
    shutil.rmtree(backup_dir)

    print(f"\n结果: killed {len(killed)}/{len(MUTATIONS)} · survived {len(survived)} · no-op {len(noop)}")
    for item in survived + noop:
        print(f"   - {item}")
    return 0 if (not survived and not noop and not drift) else 1


if __name__ == "__main__":
    raise SystemExit(main())
