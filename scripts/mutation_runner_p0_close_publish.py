#!/usr/bin/env python
"""P0-1(关入口)+ P1-3(存在性)+ fix-of-fix P1-A/B/C 的**撕锁自证**。

═══════════════════════════════════════════════════════════════════════
🔴 [fix-of-fix ⑤ 2026-08-23] 期望/实际红集合做**精确匹配**
═══════════════════════════════════════════════════════════════════════
之前每发变异的期望写成 ``any("test_02" in r for r in red)`` —— 那只回答
"该红的红了没",回答不了"**还红了别的没有**"。一发变异如果顺带打红了三条
不相干的判据,旧写法照样报"✅ 杀死"。而那三条红恰恰是最该看的东西:
要么是判据之间有耦合,要么是这发变异的作用面比我以为的大。

现在:每发变异声明一个**精确集合**,实测集合与它逐项相等才算通过;
少一条 = 该红的没红,多一条 = 有溢出。两种都 FAIL,并分别列出。

纪律(与本仓前几轮同源):
 · 先跑一发不变异的基线;基线不是全绿,后面全部作废;
 · 变异语法合法、语义精确;
 · 还原用备份字节回写并**逐字节核对**,不用 ``git checkout``。

跑法(仓库根)::

    TEST_DATABASE_URL=postgresql://geo_admin:testpw@localhost:55475/geo_defgeo_w3c_test \\
        python scripts/mutation_runner_p0_close_publish.py
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
PUBLISH = ROOT / "api" / "defensive_publish_api.py"
MONITOR = ROOT / "api" / "defensive_monitoring_api.py"
COPY = ROOT / "services" / "defensive_geo" / "copy_registry.py"
APP = ROOT / "frontend" / "src" / "App.tsx"
MIG040 = ROOT / "db" / "migration_040_defgeo_question_plans_2026_08_21.sql"

PYTEST_TARGETS = (
    "tests/defensive_geo_w3_2026_08_21/test_p0_close_publish_entry_pg.py",
    "tests/defensive_geo_w3_2026_08_21/test_fixoffix_p1abc_pg.py",
    "tests/defensive_geo_w3_2026_08_21/test_migration_conrelid_scope_pg.py",
)
GATE2 = "scripts/test-defgeo-gate2-fixes.mjs"

DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://geo_admin:testpw@localhost:55475/geo_defgeo_w3c_test",
)

P0 = "test_p0_close_publish_entry_pg.py"
FX = "test_fixoffix_p1abc_pg.py"
MG = "test_migration_conrelid_scope_pg.py"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


def run_criteria() -> tuple[set[str], int, int]:
    """跑判据,回 (**归一化红集合**, 绿数, 退出码)。"""
    env = dict(os.environ, TEST_DATABASE_URL=DB_URL, PYTHONIOENCODING="utf-8")
    red: set[str] = set()
    green = 0
    exit_code = 0

    p = subprocess.run(
        [sys.executable, "-m", "pytest", *PYTEST_TARGETS, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=ROOT, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    out = (p.stdout or "") + (p.stderr or "")
    for line in out.splitlines():
        # 🔴 token 必须长得像 node id(``<path>.py`` 或 ``<path>.py::<用例>``)。
        #    只写 ``\S+`` 会把 pytest 的 **caplog 行**
        #    ``ERROR    GEO-DefGeoPublishAPI:defensive_publish_api.py:1211`` 也收进红集合 ——
        #    精确匹配一上线就把这个解析缺陷照出来了(旧的 any() 写法永远看不见)。
        m = re.match(r"^(FAILED|ERROR)\s+([\w./" + "\\\\" + r"\-]+\.py(?:::\S+)?)$",
                     line.strip())
        if m:
            # 归一到 "<文件名>::<用例>" —— 去掉目录前缀,保留参数化后缀
            red.add("py::" + m.group(2).replace("\\", "/").split("/")[-1])
    m = re.search(r"(\d+) passed", out)
    green += int(m.group(1)) if m else 0
    exit_code = exit_code or p.returncode

    p2 = subprocess.run(
        ["node", GATE2], cwd=ROOT / "frontend", env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    out2 = (p2.stdout or "") + (p2.stderr or "")
    for line in out2.splitlines():
        if "\U0001F534" not in line:
            continue
        if not line.startswith(" "):
            continue                      # 汇总行(顶格),不是单条判据
        name = line.split("\U0001F534", 1)[1].split("—", 1)[0].strip()
        red.add("fe::" + name)
    green += out2.count("✅")
    exit_code = exit_code or p2.returncode

    return red, green, exit_code


MUTATIONS = [
    # ── P0-1 ────────────────────────────────────────────────────────────
    {
        # 🔴 [包E 2026-08-24] 方向**反过来**了:执行侧接线完成后入口已重开,
        #    所以「把开关拨掉」现在是 True → False。判据也随之翻面
        #    (test_00b 现在同时钉住「开关」与「它的前提条件」两端)。
        "id": "MUT-01", "file": PUBLISH,
        "desc": "把入口开关拨回 False(= 执行侧接完了却继续关着客户入口)",
        "from": "_CUSTOMER_PUBLISH_ENTRY_OPEN = True",
        "to": "_CUSTOMER_PUBLISH_ENTRY_OPEN = False",
        "expect": {
            f"py::{P0}::test_00b_entry_is_open_only_with_real_executor_wiring",
            f"py::{P0}::test_01_preview_not_closed",
            f"py::{P0}::test_02_confirm_not_closed",
            f"py::{P0}::test_03_retry_child_not_closed",
            f"py::{P0}::test_04_rejected_calls_freeze_nothing",
        },
        # 🔴 我第一版还把两条 P1-A 反向判据列进来了,实测它们**没红** ——
        #    预期错了不是锁弱:那两条自己 monkeypatch 把开关钉成 False
        #    (它们要的就是"关闸状态下换 key/换 hash 仍 403"),
        #    所以模块常量被拨成 True 对它们没有影响。这是对的行为。
        "expect_note": "P1-A 两条反向判据自钉开关,不受本发影响",
    },
    {
        "id": "MUT-02", "file": PUBLISH,
        "desc": "只把 confirm 的关闭闸摘掉",
        "from": "    if replay is not None:\n        return replay\n    _assert_customer_publish_entry_open()",
        "to": "    if replay is not None:\n        return replay",
        "expect": {
            f"py::{P0}::test_00_every_money_moving_endpoint_is_closed",
            f"py::{P0}::test_02_confirm_not_closed",
            f"py::{FX}::test_p1a_reverse_different_key_is_still_closed",
            f"py::{FX}::test_p1a_reverse_different_hash_is_still_closed",
            f"py::{FX}::test_p1a_guard_still_precedes_every_funding_call",
            # test_04 逐条调用**三个**已关端点并断言各自返回关闭信封;
            # confirm 一旦不再关,它必然跟着红。第一版漏了它 —— 精确匹配抓出来的。
            f"py::{P0}::test_04_rejected_calls_freeze_nothing",
        },
    },
    {
        "id": "MUT-03", "file": PUBLISH,
        "desc": "把 retry-child 的关闭闸摘掉(终审单没点名、我多关的那条)",
        "from": "    _assert_customer_publish_entry_open()\n    tenant = _tenant(request)\n    from db.connection import get_connection\n\n    conn = get_connection()\n    try:\n        cur = conn.cursor()\n        parent = _store.get_command(",
        "to": "    tenant = _tenant(request)\n    from db.connection import get_connection\n\n    conn = get_connection()\n    try:\n        cur = conn.cursor()\n        parent = _store.get_command(",
        "expect": {
            f"py::{P0}::test_00_every_money_moving_endpoint_is_closed",
            f"py::{P0}::test_03_retry_child_not_closed",
            f"py::{P0}::test_04_rejected_calls_freeze_nothing",
        },
    },
    {
        "id": "MUT-04", "file": COPY,
        "desc": "发布关闭文案里把钱那半句删掉",
        "from": '"publish_entry_closed": "发布这一步还没有开放，暂时不能确认；没有扣除任何算力。",',
        "to": '"publish_entry_closed": "发布这一步还没有开放，暂时不能确认。",',
        "expect": {
            f"py::{P0}::test_01_preview_not_closed",
            f"py::{P0}::test_02_confirm_not_closed",
            f"py::{P0}::test_03_retry_child_not_closed",
            f"py::{P0}::test_04_rejected_calls_freeze_nothing",
        },
    },
    {
        "id": "MUT-05", "file": PUBLISH,
        "desc": "改用 501(终审单明确禁止的解法)",
        "from": '    "PUBLISH_ENTRY_CLOSED": (403, False),',
        "to": '    "PUBLISH_ENTRY_CLOSED": (501, False),',
        "expect": {
            f"py::{P0}::test_01_preview_not_closed",
            f"py::{P0}::test_02_confirm_not_closed",
            f"py::{P0}::test_03_retry_child_not_closed",
            f"py::{P0}::test_04_rejected_calls_freeze_nothing",
            f"py::{FX}::test_p1a_reverse_different_key_is_still_closed",
            f"py::{FX}::test_p1a_reverse_different_hash_is_still_closed",
        },
    },
    {
        "id": "MUT-06", "file": PUBLISH,
        "desc": "顺手把不动钱的 cancel 也关掉(越权关闭)",
        "from": '    """§15.7 cancel:只对 open/unconsumed 做 CAS。',
        "to": '    """§15.7 cancel:只对 open/unconsumed 做 CAS。\n\n    MUT-06\n    """\n    _assert_customer_publish_entry_open()\n    _dummy = """',
        "expect": {f"py::{P0}::test_00_every_money_moving_endpoint_is_closed"},
    },
    # ── P1-3 存在性 ─────────────────────────────────────────────────────
    {
        "id": "MUT-07", "file": MONITOR,
        "desc": "P1-3:把存在性闸摘掉(退回改动前)",
        "from": '            cur.execute(\n                "SELECT 1 FROM public.monitoring_tasks WHERE id=%s AND brand_id=%s",\n                (int(task_id), int(brand_id)))\n            if cur.fetchone() is None:\n                raise _safe_error("NOT_FOUND")\n\n',
        "to": "",
        # 🔴 进度端点被 P1-B 关着 ⇒ test_10/test_11 走重开哨兵 skip。
        #    撕锁时正是这一发暴露了"存在性闸在这次关闭里没人守了",
        #    于是补了 test_p1b_existence_gate_survives_the_closure(临时开闸专打那道闸)。
        "expect": {f"py::{FX}::test_p1b_existence_gate_survives_the_closure"},
    },
    {
        "id": "MUT-08", "file": MONITOR,
        "desc": "P1-3:存在性闸改成一律 404(误杀正常场景)",
        "from": '            if cur.fetchone() is None:\n                raise _safe_error("NOT_FOUND")',
        "to": '            if True:\n                raise _safe_error("NOT_FOUND")',
        "expect": {
            f"py::{FX}::test_p1c_unplanned_cells_do_not_dilute_progress",
            f"py::{FX}::test_p1c_reverse_all_planned_is_unchanged",
        },
    },
    # ── 前端 ────────────────────────────────────────────────────────────
    {
        "id": "MUT-09", "file": APP,
        # [包E] 路由已恢复 ⇒ 变异方向改成"再摘掉一条"。
        "desc": "前端:把恢复的客户入口路由再摘掉一条",
        "from": '                            <Route path="defensive-geo/publish/commands/:commandId" element={<ProtectedRoute requiredModule="writing"><DefgeoPublishCommandStatus /></ProtectedRoute>} />\n',
        "to": "",
        "expect": {"fe::两条客户入口路由已恢复到 App.tsx(结构锚)"},
    },
    {
        "id": "MUT-10", "file": APP,
        "desc": "前端:把该保留的 admin 路由也摘掉",
        "from": '                            <Route path="admin/defensive-geo-settlement-review" element={<ProtectedRoute requiredModule="users"><DefgeoSettlementReviewQueue /></ProtectedRoute>} />\n',
        "to": "",
        "expect": {"fe::admin 只读队列路由仍在(重开没有误伤运维入口)"},
    },
    # ── fix-of-fix P1-A ─────────────────────────────────────────────────
    {
        "id": "MUT-11", "file": PUBLISH,
        "desc": "P1-A:把关闸前的只读重放整段摘掉(退回 P0-1 那一版)",
        "from": "    replay = _consumed_replay_or_none(\n        snapshot_id=snapshot_id, tenant=tenant,\n        expected_hash=body.expected_hash, expected_version=body.expected_version,\n        idempotency_key=idempotency_key,\n    )\n    if replay is not None:\n        return replay\n",
        "to": "",
        "expect": {f"py::{FX}::test_p1a_consumed_replay_survives_the_closed_gate"},
    },
    {
        "id": "MUT-12", "file": PUBLISH,
        "desc": "P1-A:重放不再核对 Idempotency-Key(换键也放行)",
        "from": '        if str(cmd.get("idempotency_key") or "") != idempotency_key:\n            return None',
        "to": '        if False:\n            return None',
        "expect": {f"py::{FX}::test_p1a_reverse_different_key_is_still_closed"},
    },
    {
        "id": "MUT-13", "file": PUBLISH,
        "desc": "P1-A:重放不再核对 hash(换 hash 也放行)",
        "from": "        if str(row[\"canonical_hash\"]) != expected_hash:\n            return None",
        "to": "        if False:\n            return None",
        "expect": {f"py::{FX}::test_p1a_reverse_different_hash_is_still_closed"},
    },
    {
        "id": "MUT-14", "file": PUBLISH,
        "desc": "P1-A:重放里偷偷加一次写(证明『只读』是被守着的)",
        "from": "        cmd = _store.get_command(\n            cur, publish_command_id=str(consumed_command_id), tenant_owner_id=tenant)",
        "to": "        cur.execute(\"UPDATE point_freezes SET reason='mut14' WHERE false\")\n        cmd = _store.get_command(\n            cur, publish_command_id=str(consumed_command_id), tenant_owner_id=tenant)",
        "expect": {f"py::{FX}::test_p1a_replay_helper_is_structurally_read_only"},
    },
    # ── fix-of-fix P1-B / P1-C ──────────────────────────────────────────
    # 🔴 [包F ② 2026-08-23] MUT-15 / MUT-16 **换靶**,不是删掉。
    #
    # 一期这两发打的是「进度入口关着」:MUT-15 把 `_MONITORING_PROGRESS_OPEN`
    # 拨回 True,MUT-16 往不涉钱的关闭文案里塞一句钱。包F ② 把闸撤了
    # (前置条件"账本没接线"已由包F ① 消除),这两个靶子在源码里不存在了 ——
    # 变异跑起来会 `from` 找不到而报基础设施红。
    #
    # 但**不能**就这么把两发删掉:那等于这一格从"有两发在守"变成"零发在守",
    # 而且没有任何判据会因此变红(本仓记过:锁被静默退役)。
    # 所以换成打**新状态**的两发 —— 靶子从"闸关着"变成"闸开着而它下面
    # 那两道真正承重的东西还在":存在性闸 + 账本接线。
    {
        "id": "MUT-15", "file": MONITOR,
        "desc": "包F②:把重开后的存在性闸摘掉(不存在的 task 又开始编造零进度)",
        "from": '            if cur.fetchone() is None:\n                raise _safe_error("NOT_FOUND")',
        "to": '            if False:\n                raise _safe_error("NOT_FOUND")',
        "expect": {
            f"py::{FX}::test_p1b_existence_gate_is_permanent",
        },
    },
    {
        "id": "MUT-16", "file": "db/monitoring_db.py",
        "desc": "包F②:把接线点①(claim 时 open attempt)摘掉 —— 账本又变成死函数",
        "from": "        open_for_claim(cur, dict(row))",
        "to": "        pass  # open_for_claim(cur, dict(row))",
        "expect": {
            "py::tests/defensive_geo_pkgf_2026_08_23/"
            "test_run_ledger_wiring.py::test_live_chain_call_sites_match_expected",
        },
    },
    {
        "id": "MUT-17", "file": MONITOR,
        "desc": "P1-C:把 is_planned 过滤摘掉(未计划的格又进分母)",
        "from": '"WHERE task_id=%s AND brand_id=%s AND is_planned = TRUE",',
        "to": '"WHERE task_id=%s AND brand_id=%s",',
        "expect": {f"py::{FX}::test_p1c_unplanned_cells_do_not_dilute_progress"},
    },
    # ── Codex 终签 P1:约束守卫作用域 ───────────────────────────────────
    {
        "id": "MUT-18", "file": MIG040,
        "desc": "把 chk_defgeo_qplan_mode 的 conrelid 绑定改回 conname-only(终签单点名的那一发)",
        # 🔴 这一段**必须用 Write/Edit 维护,禁用 heredoc**:本轮又一次把
        #    Python 字面量里的 \n 写成了真换行,文件当场 SyntaxError。
        #    本仓这条已经栽到第五次了。
        "from": ("                    WHERE conname='chk_defgeo_qplan_mode'\n"
                 "                      AND conrelid='defgeo_question_plans'::regclass) THEN"),
        "to": "                    WHERE conname='chk_defgeo_qplan_mode') THEN",
        "expect": {
            f"py::{MG}::test_no_unscoped_constraint_guard_remains",
            f"py::{MG}::test_decoy_same_named_constraint_does_not_block_the_real_one",
            f"py::{MG}::test_without_decoy_behaviour_is_unchanged",
        },
    },
    {
        "id": "MUT-19", "file": MIG040,
        "desc": "把 exact readiness 的期望定义改窄一格(证明它比的是定义不是存在)",
        "from": "'chk_defgeo_qplan_mode', 'public.defgeo_question_plans'",
        "to": "'chk_defgeo_qplan_mode', 'public.defgeo_question_plans_TYPO'",
        "expect": {
            f"py::{MG}::test_decoy_same_named_constraint_does_not_block_the_real_one",
            f"py::{MG}::test_without_decoy_behaviour_is_unchanged",
        },
    },
]


def main() -> int:
    originals = {f: f.read_bytes() for f in {PUBLISH, MONITOR, COPY, APP, MIG040}}
    print("被测文件:")
    for f, b in sorted(originals.items()):
        print(f"  {f.relative_to(ROOT)} sha={sha(b)}")

    print("\n=== 基线(不变异)===")
    red, green, code = run_criteria()
    print(f"  绿 {green} · 红 {len(red)} · exit={code}")
    if red or code != 0:
        print("🔴 基线就不是全绿,撕锁结果无意义。先修判据。")
        for r in sorted(red):
            print("   " + r)
        return 1

    bad = 0
    for m in MUTATIONS:
        print(f"\n=== {m['id']} · {m['desc']} ===")
        src = originals[m["file"]].decode("utf-8")
        hits = src.count(m["from"])
        if hits != 1:
            print(f"  🔴 变异锚点命中 {hits} 处(必须恰好 1)—— 这一发作废,不当『杀不掉』")
            bad += 1
            continue
        m["file"].write_text(src.replace(m["from"], m["to"], 1), encoding="utf-8", newline="")
        try:
            red, green, code = run_criteria()
        finally:
            m["file"].write_bytes(originals[m["file"]])
            restored = m["file"].read_bytes() == originals[m["file"]]
        if not restored:
            print("  🔴🔴 还原失败!工作树已被污染,立即停手。")
            return 2

        expected: set[str] = m["expect"]
        missing = sorted(expected - red)
        extra = sorted(red - expected)
        print(f"  期望 {len(expected)} 条 · 实测 {len(red)} 条 · 绿 {green}")
        if m.get("expect_note"):
            print(f"  说明:{m['expect_note']}")
        for r in sorted(red):
            print("     " + r)
        if missing:
            print(f"  🔴 **该红没红**({len(missing)}):{missing}")
        if extra:
            print(f"  🔴 **溢出红**({len(extra)}):{extra} —— "
                  "这发变异打到了它不该打到的判据,或判据之间有耦合")
        if not expected and not red:
            print("  ⚠️ 期望为空且实测为空 —— 这一发**当前没有判据在守**,按说明归因")
        elif missing or extra:
            bad += 1
        else:
            print("  ✅ 杀死,且红集合与期望**逐项相等**")

    print("\n还原核对:")
    dirty = False
    for f, b in sorted(originals.items()):
        now = f.read_bytes()
        same = now == b
        dirty = dirty or not same
        print(f"  {f.relative_to(ROOT)} sha={sha(now)} {'(逐字节一致)' if same else '(🔴 不一致)'}")
    if dirty:
        return 2

    print("=" * 64)
    print(f"✅ {len(MUTATIONS)} 发变异全部精确匹配" if bad == 0
          else f"🔴 {bad}/{len(MUTATIONS)} 发未达预期")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
