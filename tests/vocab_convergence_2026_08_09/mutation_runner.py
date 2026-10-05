#!/usr/bin/env python3
"""变异 runner · 证明 tests/vocab_convergence_2026_08_09 的锁有判别力。

每条变异把一处实现改坏,锁必须转红;**存活 = 锁弱或该处是空操作**,两者都要分诊,
不许直接归因成"锁弱"了事(本仓 M16 存活实为空操作)。

中断安全四层(踩过的坑):
  finally 恢复 / SIGINT+SIGTERM / atexit / 落盘备份 + 下一轮残留检测拒跑

用法:
    TEST_DATABASE_URL=... python tests/vocab_convergence_2026_08_09/mutation_runner.py
"""
from __future__ import annotations

import atexit
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKUP_DIR = Path(__file__).resolve().parent / ".mutation_backup"
TEST_PATH = "tests/vocab_convergence_2026_08_09/"

# (编号, 文件, 原文, 变异后, 说明, 期望转红的锁名片段)
MUTATIONS = [
    ("M1", "tools/media_vocab_normalize.py",
     '    "综合全国": "全国",\n    "海外": "全球",',
     '    "海外": "全球",',
     "删掉「综合全国→全国」这组同义词",
     "test_area_synonyms_are_merged"),

    ("M2", "tools/media_vocab_normalize.py",
     '    return AREA_SYNONYMS.get(trimmed, trimmed)',
     '    return AREA_SYNONYMS.get(trimmed, "全国")',
     "非同义值也被改成「全国」(过度归一)",
     "test_area_non_synonyms_are_untouched"),

    ("M3", "tools/media_vocab_normalize.py",
     'LISTING_SLOTS: frozenset[str] = frozenset({"套餐系列", "十元专区", "最新秒杀"})',
     'LISTING_SLOTS: frozenset[str] = frozenset({"套餐系列", "最新秒杀"})',
     "词表漏掉「十元专区」",
     # 🔴 不能指望 test_listing_slots_are_detected —— 它的 parametrize 参数取自
     #    LISTING_SLOTS 本身,词表删小了那条用例直接不存在(实测)。用字面量那条。
     "test_listing_slot_vocabulary_is_exactly_these_three"),

    ("M4", "tools/media_vocab_normalize.py",
     '    return str(value).strip() in LISTING_SLOTS',
     '    return True',
     "把所有类目都当成价格档(误杀真体裁)",
     "test_real_categories_are_not_listing_slots"),

    ("M5", "db/meijiehezi_db.py",
     '    normalize_media_row(media)\n    cursor.execute("""\n        INSERT INTO mhz_media (',
     '    cursor.execute("""\n        INSERT INTO mhz_media (',
     "🔴 接线拆掉:归一函数还在,但 _upsert_media 不调它了(本仓第五例的形态)",
     "test_wiring_meijiehezi_upsert_media_writes_canonical_area"),

    ("M6", "db/meijiehezi_db.py",
     '        ON CONFLICT (id) DO UPDATE SET\n            listing_slot = EXCLUDED.listing_slot,',
     '        ON CONFLICT (id) DO UPDATE SET',
     "ON CONFLICT 分支不更新 listing_slot(存量行档位永远停在第一次)",
     "test_wiring_upsert_media_conflict_update_also_carries_slot"),

    ("M7", "db/meijiehezi_db.py",
     '    normalize_wemedia_row(media)',
     '    pass',
     "自媒体侧接线拆掉",
     "test_wiring_wemedia_upsert_writes_canonical_province"),

    ("M8", "db/publish_db.py",
     '                detect_listing_slot(m.get("resource_type_name"), m.get("category")),',
     '                None,',
     "🔴 第 3 个写入点恒写 None(只在 meijiehezi_db 落闸就是这个后果)",
     "test_wiring_publish_db_bulk_upsert_carries_slot"),

    ("M9", "db/publish_db.py",
     '              AND resource_type_name <> ALL(%s)\n            ORDER BY resource_type_name\n        """, (sorted(LISTING_SLOTS),))',
     '            ORDER BY resource_type_name\n        """, ())',
     "分类下拉不再排除价格档",
     "test_category_dropdown_excludes_listing_slots"),

    ("M10", "db/migration_031_media_listing_slot_2026_08_09.sql",
     "CHECK (listing_slot IS NULL OR listing_slot IN ('套餐系列', '十元专区', '最新秒杀'));",
     "CHECK (TRUE);",
     "CHECK 放行任意档位值(新档位静默混入)",
     "test_migration_check_rejects_unknown_slot"),

    ("M11", "db/migration_031_media_listing_slot_2026_08_09.sql",
     # [索引守卫加固二单 2026-08-24] 锚跟到表绑定守卫的建索引分支。
     "        CREATE INDEX idx_mhz_media_listing_slot ON public.mhz_media (listing_slot) WHERE listing_slot IS NOT NULL;",
     "",
     "迁移不建索引",
     "test_migration_created_column_check_and_index"),

    ("M12", "db/rollback_031_media_listing_slot_2026_08_09.sql",
     "ALTER TABLE mhz_media DROP COLUMN IF EXISTS listing_slot;",
     "",
     "回滚脚本不删列(回滚不干净)",
     "test_rollback_script_removes_everything"),

    ("M13", "scripts/backfill_media_vocab_2026_08_09.py",
     '        sys.exit(2)',
     '        pass',
     "🔴 回填闸自检不再拒跑(先回填后落闸 = 白干)",
     "test_backfill_refuses_to_run_when_gate_missing"),

    # ── 收尾补的四条(2026-08-09 二次交付)──
    ("M15", "db/publish_db.py",
     '                category = EXCLUDED.category,\n                listing_slot = EXCLUDED.listing_slot,',
     '                category = EXCLUDED.category,',
     "🔴 upsert_media 的 ON CONFLICT 不更新 listing_slot(生产同步绝大多数走这条)",
     "test_wiring_publish_db_upsert_media_conflict_updates_slot"),

    ("M16", "db/publish_db.py",
     '                    our_price_points = EXCLUDED.our_price_points,\n                    listing_slot = EXCLUDED.listing_slot,',
     '                    our_price_points = EXCLUDED.our_price_points,',
     "🔴 bulk_upsert_media 的 ON CONFLICT 不更新 listing_slot",
     "test_wiring_publish_db_bulk_upsert_conflict_updates_slot"),

    ("M17", "db/meijiehezi_db.py",
     '            ("mhz_media", "listing_slot", "TEXT"),',
     '',
     "🔴 init_mhz_tables 不再自愈补 listing_slot(新库/旧表上同步整批挂)",
     "test_init_mhz_tables_self_heals_listing_slot"),

    # M14 只禁第 1 条检查;第 2 条(elif)必须单独有变异,否则它是否有判别力无人知道。
    ("M18", "scripts/backfill_media_vocab_2026_08_09.py",
     '        elif "listing_slot" not in _isrc.split("INSERT INTO mhz_media", 1)[-1][:1200]:',
     '        elif False:',
     "🔴 闸自检不再查「认了档位却没写进 INSERT 列清单」",
     "test_backfill_gate_covers_the_fifth_write_point"),
]


def _backup(rel: str) -> None:
    BACKUP_DIR.mkdir(exist_ok=True)
    dst = BACKUP_DIR / rel.replace("/", "__")
    if not dst.exists():
        shutil.copy2(ROOT / rel, dst)


def _restore_all() -> None:
    if not BACKUP_DIR.exists():
        return
    for f in BACKUP_DIR.iterdir():
        shutil.copy2(f, ROOT / f.name.replace("__", "/"))
        f.unlink()
    try:
        BACKUP_DIR.rmdir()
    except OSError:
        pass


def _guard_leftover() -> None:
    if BACKUP_DIR.exists() and any(BACKUP_DIR.iterdir()):
        print("🔴 上一轮变异有残留(可能被 Ctrl-C 打断),先恢复再跑:")
        for f in BACKUP_DIR.iterdir():
            print("   -", f.name.replace("__", "/"))
        print("   恢复:python tests/vocab_convergence_2026_08_09/mutation_runner.py --restore")
        sys.exit(3)


def _run_tests() -> tuple[bool, str]:
    p = subprocess.run(
        [sys.executable, "-m", "pytest", TEST_PATH, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return p.returncode == 0, (p.stdout or "") + (p.stderr or "")


def _red_tests(out: str) -> set[str]:
    """收集转红的用例名。parametrize 下同名多参数只要**任一**转红即算转红
    (踩过:runner 只取第一条匹配行,[0] PASSED 排在 [True] FAILED 前面 → 误判存活)。"""
    names = set()
    for line in out.splitlines():
        m = re.search(r"^FAILED\s+\S+::(\w+)", line.strip())
        if m:
            names.add(m.group(1))
    return names


def main() -> int:
    if "--restore" in sys.argv:
        _restore_all()
        print("已恢复")
        return 0

    _guard_leftover()
    atexit.register(_restore_all)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *a: (_restore_all(), sys.exit(130)))

    ok, out = _run_tests()
    if not ok:
        print("🔴 基线就不绿,先修基线:\n", out[-3000:])
        return 1
    baseline = out.strip().splitlines()[-1]
    print(f"基线 ✅ {baseline}\n")

    survived = []
    for mid, rel, old, new, desc, expect in MUTATIONS:
        path = ROOT / rel
        src = path.read_text(encoding="utf-8")
        if old not in src:
            print(f"{mid} ⚠️ 锚点失配(实现改过?)· {rel} · {desc}")
            survived.append((mid, desc, "锚点失配"))
            continue
        _backup(rel)
        try:
            path.write_text(src.replace(old, new, 1), encoding="utf-8", newline="")
            passed, out = _run_tests()
            reds = _red_tests(out)
            if passed:
                print(f"{mid} 🔴 存活 · {desc}")
                survived.append((mid, desc, "无锁转红"))
            elif expect in reds:
                print(f"{mid} ✅ 被 {expect} 抓到 · {desc}")
            else:
                print(f"{mid} ⚠️ 转红了但不是预期那条(实际:{sorted(reds)[:3]}) · {desc}")
        finally:
            path.write_text(src, encoding="utf-8", newline="")

    _restore_all()
    print(f"\n{'='*60}\n变异 {len(MUTATIONS)} 条 · 存活 {len(survived)} 条")
    for mid, desc, why in survived:
        print(f"  🔴 {mid} {desc} —— {why}(须分诊:锁弱 vs 空操作)")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
