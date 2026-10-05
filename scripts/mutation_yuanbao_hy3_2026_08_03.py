#!/usr/bin/env python3
"""[工单 2026-08-03] 变异自检:证明元宝血缘/错误可见性那组锁真有判别力。

先跑基线要求全绿;再逐个注入"最可能被改回去"的变异,要求锁转红。
退出码:0=全杀 · 1=有存活。
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
TEST = "tests/test_yuanbao_hy3_model_and_error_visibility_2026_08_03.py"
ENV = {**os.environ, "TEST_DATABASE_URL": "postgresql://noconnect:noconnect@127.0.0.1:5499/geo_agentscope_test"}

LINEAGE = ROOT / "services/ai_surface_monitoring/lineage.py"
MDB = ROOT / "db/monitoring_db.py"
BM = ROOT / "tools/monitoring/batch_monitor.py"
ADAPTER = ROOT / "services/ai_surface_monitoring/adapters/openai_compat.py"
PRICE = ROOT / "tools/llm_call_tracker.py"
RESOLVER = ROOT / "services/brand_identity_resolver.py"
PLATPERF = ROOT / "frontend/src/features/publicReportPremium/components/sections/PlatformPerformance.tsx"
TEST2 = "tests/test_registry_name_correction_review_2026_08_03.py"


def run() -> bool:
    r = subprocess.run([sys.executable, "-m", "pytest", TEST, "-q"],
                       cwd=ROOT, env=ENV, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode == 0


def run_resolver() -> bool:
    r = subprocess.run([sys.executable, "-m", "pytest", TEST2, "-q"],
                       cwd=ROOT, env=ENV, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode == 0


def run_ui() -> bool:
    base = ROOT / "frontend/node_modules/.bin"
    exe = base / ("playwright.cmd" if sys.platform == "win32" else "playwright")
    if not exe.exists():
        raise RuntimeError(f"playwright 不存在:{exe}")
    r = subprocess.run([str(exe), "test", "--config", "playwright.report-hierarchy.config.ts"],
                       cwd=ROOT / "frontend", capture_output=True, text=True,
                       encoding="utf-8", errors="replace", shell=False)
    return r.returncode == 0


MUTATIONS = [
    ("M1 模型回退到 hy3-preview(8/31 下线的那个)", LINEAGE,
     'default_model_key="hy3",', 'default_model_key="hy3-preview",',
     "锁 test_lineage_uses_hy3_not_preview 该红"),

    ("M2 换模型时顺手关掉联网检索", LINEAGE,
     "default_search_enabled=True,       # 实测 2026-08-03",
     "default_search_enabled=False,      # 实测 2026-08-03",
     "锁 test_lineage_uses_hy3_not_preview 的 search 断言该红"),

    ("M3 漏改第 4 处血缘(monitoring_db 成本映射)", MDB,
     '"yuanbao": ("tencent_tokenhub", "hy3"),',
     '"yuanbao": ("tencent_tokenhub", "hy3-preview"),',
     "锁 test_all_four_lineage_sites_agree_on_model 该红"),

    ("M4 漏改执行层表面映射(batch_monitor)", BM,
     '"yuanbao": ("tencent_tokenhub", "hy3", "ai_search", "tencent_tokenhub"),',
     '"yuanbao": ("tencent_tokenhub", "hy3-preview", "ai_search", "tencent_tokenhub"),',
     "同上锁该红"),

    ("M5 删掉旧价目条目(历史行算价会失真)", PRICE,
     '    ("tencent_tokenhub", "hy3-preview"): {"input": 0.001, "output": 0.004, "cache_hit": 0.00025},',
     '',
     "锁 test_pricing_keeps_both_models 该红"),

    ("M6 错误体又被吞回去(不打码不分类,直接 str)", ADAPTER,
     "    text = _SECRETISH_RE.sub(\"<redacted>\", text)",
     "    text = text",
     "锁 test_redaction_keeps_reason_and_drops_credentials 该红"),

    ("M7 计费类错误被降级成普通供应商错误", ADAPTER,
     "    if status_code == 402 or any(",
     "    if False and any(",
     "锁 test_billing_error_is_classified_as_needing_human 该红"),

    # ── 工单 ② 工商名称矫正 → 待确认 ──
    ("M8 拆掉「矫正样本进待确认」的闸(退回直接丢分)", RESOLVER,
     "            verification.verdict is BrandVerdict.NO\n            and window_spans",
     "            False\n            and window_spans",
     "锁 test_real_samples_go_to_review_instead_of_losing_points 该红"),

    ("M9 闸放宽成「只要有矫正话术就收」(丢掉窗口条件)", RESOLVER,
     "            and detect_registry_name_correction(answer, self.identity)",
     "            or detect_registry_name_correction(answer, self.identity)",
     "反向锁 test_ordinary_absent_answer_still_counts_as_no 该红(真没提到的被误收进待确认)"),

    # ── 工单 ③ 问句层级 ──
    ("M10 问句标题退回原来的弱层级(text-xs/font-medium)", PLATPERF,
     'className="min-w-0 flex-1 break-words text-sm font-semibold leading-6 text-slate-900 sm:text-[15px]"',
     'className="min-w-0 flex-1 break-words text-xs font-medium text-slate-700"',
     "UI 锁1「问句必须比正文更重更大」该红"),

    ("M11 去掉问句与答案之间的分隔线", PLATPERF,
     'className="mt-2.5 border-t border-slate-200 pt-2.5"',
     'className="mt-2.5 pt-2.5"',
     "UI 锁2「有可见分隔线」该红"),
]


def main() -> int:
    print("=== 基线(未变异)必须全绿:血缘/错误可见性 + 矫正判定 + UI 层级 ===")
    b1, b2, b3 = run(), run_resolver(), run_ui()
    print(f"  血缘+错误 {'✅' if b1 else '🔴'} · 矫正判定 {'✅' if b2 else '🔴'} · UI 层级 {'✅' if b3 else '🔴'}")
    if not (b1 and b2 and b3):
        print("🔴 基线不绿,中止 —— 后面的红说明不了任何事")
        return 1

    survived = []
    runner_of = {RESOLVER: run_resolver, PLATPERF: run_ui}
    for name, path, old, new, why in MUTATIONS:
        runner = runner_of.get(path, run)
        src = path.read_text(encoding="utf-8")
        if old not in src:
            print(f"🔴 {name}:锚点串找不到(实现改过?)→ 变异无效,当作失败")
            survived.append(name)
            continue
        backup = tempfile.mktemp(suffix=".bak")
        shutil.copyfile(path, backup)
        try:
            path.write_text(src.replace(old, new, 1), encoding="utf-8")
            if runner():
                print(f"🔴 {name} 存活 —— {why}")
                survived.append(name)
            else:
                print(f"✅ {name} 被抓到 —— {why}")
        finally:
            shutil.copyfile(backup, path)
            os.unlink(backup)

    print()
    if survived:
        print(f"🔴 {len(survived)}/{len(MUTATIONS)} 存活:{survived}")
        return 1
    print(f"✅ {len(MUTATIONS)}/{len(MUTATIONS)} 变异全杀")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
