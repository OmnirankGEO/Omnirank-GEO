# -*- coding: utf-8 -*-
"""parity 成对反向:把闸/分支拆掉,对应用例必须转红。

🔴 Windows:读写两处都 newline=""。
"""
import os, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRV = ROOT / "server.py"
MDB = ROOT / "db" / "monitoring_db.py"
D = "tests/manual_keyword_parity_2026_08_16"

MUT = [
    ("MP1 拆掉 renew 里新建订阅那段(还原孤儿生产线)", SRV,
     "            create_keyword_monitor_subscription_with_cursor(",
     "            _removed_by_mutation = lambda **kw: None\n            _removed_by_mutation(",
     f"{D}/test_p11_renew_identity.py::test_P11_renew_confirmed_creates_subscription_and_keeps_identity"),
    ("MP2 拆掉 fail-closed 的 raise(退回『标了没建』)", SRV,
     '                raise RuntimeError(\n                    f"监测计费主体无法确认',
     '                billing_user_id = 0  # mutation\n                _unused = (\n                    f"监测计费主体无法确认',
     f"{D}/test_p11_renew_identity.py::test_P11_failclosed_rolls_back_everything"),
    ("MP3 拆掉 K3 守卫(手动词续费也重置服务期)", SRV,
     '            if _src != "extra" and row.get("quote_id") is not None:',
     '            if row.get("quote_id") is not None:',
     f"{D}/test_p11_renew_identity.py::test_P11_K3_extra_renew_does_not_touch_service_start_date"),
    ("MP4 拆掉每日取词的 extra 臂(手动词又不跑了)", MDB,
     "            JOIN extra_keywords ek ON ek.id = s.keyword_id",
     "            JOIN extra_keywords ek ON ek.id = s.keyword_id AND FALSE",
     f"{D}/test_p02_dispatch_parity.py::test_P02_both_sources_dispatched_when_quote_paid"),
    ("MP5 extra 臂不认 opt-in 开关(未开的手动词也被拉去跑)", MDB,
     "              AND ek.is_monitored = TRUE",
     "              AND TRUE",
     f"{D}/test_p03_switch_identity.py::test_P03_switch_off_means_not_dispatched"),
    # 🔴 第一版 MP6 改的是 CREATE 的列表 —— 没转红,因为带 source 的索引**已经存在**,
    #    `IF NOT EXISTS` 让整句变成 no-op,而 DROP 那句还在。变异必须复现**原病史本身**:
    #    重建只按 keyword_id 的旧索引、且不再 DROP。
    # ── Review 2026-08-16 R-1:P0-1 的旗舰修复原来零锁 ──
    #   实测:把任一臂的 source 闸换成 AND TRUE,全仓测试照样全绿(锁天然瞎)。
    #   下面两条是那两行的**唯一**判据,拆掉必须转红。
    ("MP7 合同臂拆掉 source 闸(会 JOIN 到同 id 的手动词)", MDB,
     "              AND s.keyword_source = 'confirmed'" + chr(10),
     "              AND TRUE" + chr(10),
     f"{D}/test_p01b_dispatch_id_collision.py::test_P01b_no_cross_table_keyword_swap"),
    ("MP8 手动臂拆掉 source 闸(会 JOIN 到同 id 的合同词)", MDB,
     "              AND s.keyword_source = 'extra'" + chr(10),
     "              AND TRUE" + chr(10),
     f"{D}/test_p01b_dispatch_id_collision.py::test_P01b_no_cross_table_keyword_swap"),
    ("MP6 自愈 DDL 退回原病史(重建 keyword_id 唯一索引 · 不再 DROP)", MDB,
     '                cursor.execute("DROP INDEX IF EXISTS uniq_kms_keyword_active")',
     '                cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS uniq_kms_keyword_active'
     " ON keyword_monitor_subscriptions(keyword_id)"
     " WHERE status IN ('active', 'paused_low_balance')\")",
     f"{D}/test_p01_id_collision.py::test_P01b_selfhealing_ddl_does_not_undo_the_migration"),
]


def run(node):
    r = subprocess.run([sys.executable, "-m", "pytest", node, "-q", "-p", "no:warnings"],
                       cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=dict(os.environ))
    return r.returncode


fails = []
for name, path, old, new, node in MUT:
    src = path.read_text(encoding="utf-8", newline="")
    if old not in src:
        print(f"FAIL  {name}\n      锚点不存在,变异无效(判据没被验证过)")
        fails.append(name)
        continue
    base = run(node)
    path.write_text(src.replace(old, new, 1), encoding="utf-8", newline="")
    try:
        mut = run(node)
    finally:
        path.write_text(src, encoding="utf-8", newline="")
    back = run(node)
    ok = (base == 0 and mut != 0 and back == 0)
    print(f"{'PASS' if ok else 'FAIL'}  {name}\n"
          f"      基线={base}(须0) 变异后={mut}(须≠0) 还原后={back}(须0)")
    if not ok:
        fails.append(name)

print("\n=== parity 变异结论 ===", "全部转红" if not fails else f"未转红:{fails}")
sys.exit(1 if fails else 0)
