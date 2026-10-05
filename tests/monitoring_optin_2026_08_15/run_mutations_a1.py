# -*- coding: utf-8 -*-
"""W1 系列成对反向:拆掉闸/锁,对应用例必须转红。恒真锁 = 等于没写。

🔴 合并说明(2026-08-16):本文件原来有 5 条变异,其中 3 条打的是
`get_active_client_keywords`(加闸版)。裁定取**删除版** ⇒ 那 3 条的靶子不存在了,
不能留着 —— 靶子不存在的变异会以"锚点不存在"的形式失败,那种红跟"判据没守住"
是两回事,混在一起看就分不出「跑没跑起来」和「过没过」。
所以按合并后的现实重写:留下仍成立的 2 条(M2a/M2b),
换上打**新尺子**的 3 条(M5/M6/M7)。

🔴 Windows:读写两处都必须 newline=""(否则整文件 LF→CRLF,diff 假爆炸)。
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MDB = ROOT / "db" / "monitoring_db.py"
WIRING = ROOT / "tests" / "monitoring_optin_2026_08_15" / "test_optin_wiring.py"
TEST = "tests/monitoring_optin_2026_08_15/test_optin_wiring.py"

MUTATIONS = [
    # ── 从 parity 分支捞回来、合并后仍成立的两条(守 W3 升级)──
    ("M2a 展示口径退回『只看列不看订阅』(拿掉 EXISTS)", MDB,
     "                        AND EXISTS (" + chr(10), "                        AND (" + chr(10),
     "test_W3_hardcoded_false_is_gone"),
    ("M2b EXISTS 不按 keyword_source 限定(被同号 confirmed 订阅冒名)", MDB,
     "                               AND kms.keyword_source = 'extra'" + chr(10), "",
     "test_W3_hardcoded_false_is_gone"),

    # ── 打合并后新尺子的三条 ──
    ("M5 每日取词的 extra 臂拆掉 opt-in 闸(dispatch 站点无闸)", MDB,
     "              AND ek.is_monitored = TRUE" + chr(10), "",
     "test_W1b_dispatch_sites_are_gated_or_explicitly_exempt"),
    ("M6 尺子退回只认 FROM(看不见 JOIN 形态的取词口)", WIRING,
     'r"(?:FROM|JOIN)\\s+(?:public\\.)?extra_keywords"',
     'r"FROM\\s+(?:public\\.)?extra_keywords"',
     "test_W1f_join_form_is_visible_to_the_ruler"),
    ("M7 已归类函数里偷加第 2 处取词 SQL(组合没变,只有处数变)", MDB,
     "def get_keyword_by_id(",
     "def _sneaky_second_site_in_a_classified_func():\n"
     "    return \"SELECT 1 FROM extra_keywords\"\n\n\ndef get_keyword_by_id(",
     "test_W1e_site_counts_are_pinned"),
]


def run(node):
    r = subprocess.run([sys.executable, "-m", "pytest", f"{TEST}::{node}", "-q", "-p", "no:warnings"],
                       cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=dict(os.environ))
    return r.returncode


fails = []
for name, path, old, new, node in MUTATIONS:
    src = path.read_text(encoding="utf-8", newline="")
    if old not in src:
        print(f"FAIL  {name}\n      锚点不存在,变异无效(这条判据这次根本没被验证过)")
        fails.append(name)
        continue
    baseline = run(node)
    path.write_text(src.replace(old, new, 1), encoding="utf-8", newline="")
    try:
        mutated = run(node)
    finally:
        path.write_text(src, encoding="utf-8", newline="")
    restored = run(node)
    ok = (baseline == 0 and mutated != 0 and restored == 0)
    print(f"{'PASS' if ok else 'FAIL'}  {name}\n"
          f"      基线={baseline}(须0) 变异后={mutated}(须≠0) 还原后={restored}(须0)  -> {node}")
    if not ok:
        fails.append(name)

print("\n=== 变异结论 ===", "全部转红" if not fails else f"未转红:{fails}")
sys.exit(1 if fails else 0)
