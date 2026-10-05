"""[WO-PUB-ZOMBIE-2026-08-04] 变异自检。

每个变异都要过三关,任何一关不过就当**无效变异**报红:
  1. 锚点唯一命中(找不到 / 命中多处 → 变异根本没打在预期位置);
  2. 落盘后文件真的变了(防"改了个寂寞");
  3. **至少一个预期用例转红**。零转红 = 这条锁在这个 fixture 下抓不到本 bug,
     哪怕它天天绿也是废的(撤一行但断言仍被别的路径命中 = 无效变异)。

跑法:  python tests/publish_zombie_2026_08_04/mutation_runner.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TESTS = "tests/publish_zombie_2026_08_04/"

SVC = "services/publish_orphan_settlement.py"
DB = "db/meijiehezi_db.py"
API = "api/meijiehezi_api.py"
SCHED = "api/scheduler.py"

# (编号, 说明, 文件, 原文, 替换, 预期转红的用例名片段)
MUTATIONS: list[tuple[str, str, str, str, str, list[str]]] = [
    # ---------------- R1b 三分支 ----------------
    ("M1", "禁退区拆掉:外部有单未被认领也照退",
     SVC,
     '        if row.get("claimed_by") is None:\n            return ACTION_MANUAL_REVIEW\n',
     '        if False:\n            return ACTION_MANUAL_REVIEW\n',
     ["test_r1b_external_found_unclaimed_must_not_refund",
      "test_r1b_any_unclaimed_row_wins",
      "test_sweep_must_not_refund_when_external_unclaimed"]),

    ("M2", "新鲜度闸对「解析不了的时间戳」改成放行(fail-open)",
     SVC,
     "    age = sync_age_minutes(raw, now=now)\n    if age is None:\n        return False\n",
     "    age = sync_age_minutes(raw, now=now)\n    if age is None:\n        return True\n",
     ["test_freshness_gate_cells"]),

    ("M2b", "新鲜度闸超龄也放行",
     SVC,
     "    return age <= float(max_age_minutes)\n",
     "    return True\n",
     ["test_freshness_gate_cells", "test_sweep_skips_entire_round_when_sync_stale"]),

    ("M3", "判别力自检永远通过(镜像不全也照比对)",
     SVC,
     "    if hits != checked:\n",
     "    if False:\n",
     ["test_discriminating_check_false_when_mirror_incomplete",
      "test_sweep_skips_round_when_mirror_not_discriminating"]),

    ("M3b", "判别力自检:没有样本时默认放行",
     SVC,
     "    if checked <= 0:\n",
     "    if False and checked <= 0:\n",
     ["test_discriminating_check_false_with_no_sample"]),

    ("M4", "🔴 候选 SQL 去掉「只收无单号」的红线",
     DB,
     "              AND (i.mhz_order_id IS NULL OR i.mhz_order_id = '')\n"
     "              AND COALESCE(i.media_type, 'mhz') <> 'svideo'\n",
     "              AND COALESCE(i.media_type, 'mhz') <> 'svideo'\n",
     ["test_r2_candidate_cells", "test_sweep_never_touches_items_with_order_id"]),

    ("M5", "候选 SQL 去掉超时阈值(刚下的单也清)",
     DB,
     "              AND o.created_at < NOW() - INTERVAL '{hours} hours'\n",
     "              AND TRUE\n",
     ["test_r2_candidate_cells"]),

    ("M6", "候选 SQL 去掉短视频排除(抢别人的 item)",
     DB,
     "              AND COALESCE(i.media_type, 'mhz') <> 'svideo'\n",
     "              AND TRUE\n",
     ["test_r2_candidate_cells"]),

    ("M7", "清扫器忽略新鲜度结论,照退不误",
     SVC,
     '            result.skipped_reason = f"{cfg_key} 同步不新鲜"\n            continue\n',
     '            result.skipped_reason = f"{cfg_key} 同步不新鲜"\n',
     ["test_sweep_skips_entire_round_when_sync_stale"]),

    ("M8", "转人工分支改成照样退款",
     SVC,
     "        if action == ACTION_MANUAL_REVIEW:\n",
     "        if False:\n",
     ["test_sweep_must_not_refund_when_external_unclaimed"]),

    # ---------------- R3 去重判据(工单点名的变异锚点) ----------------
    ("M9", "🔴 去重判据改回 NOT IN(...) 反向定义 —— 无单号 pending 又被拦",
     DB,
     "              AND (\n"
     "                    i.status = 'published'\n"
     "                 OR i.mhz_order_id IS NOT NULL AND i.mhz_order_id <> ''\n"
     "                 OR i.status <> 'pending'\n"
     "              )\n",
     "              AND TRUE\n",
     ["test_r3_dedupe_cells"]),

    ("M10", "理由码恒为 IN_FLIGHT(published 又被说成「进行中」)",
     DB,
     '                "reason_code": (BLOCK_REASON_ALREADY_PUBLISHED if r["has_published"]\n'
     "                                else BLOCK_REASON_IN_FLIGHT),\n",
     '                "reason_code": BLOCK_REASON_IN_FLIGHT,\n',
     ["test_r3_dedupe_cells", "test_r3_published_still_blocks_even_when_ancient",
      "test_r3_published_wins_over_inflight_on_same_media"]),

    ("M9b", "提交时那道闸不放行陈年僵尸(锁只是挪了个位置)",
     DB,
     "              AND NOT (\n"
     "                    i.status = 'pending'\n"
     "                AND (i.mhz_order_id IS NULL OR i.mhz_order_id = '')\n"
     "                AND o.created_at < NOW() - INTERVAL "
     "'{int(ORPHAN_AGE_HOURS_FOR_DEDUPE)} hours'\n"
     "              )\n",
     "              AND TRUE\n",
     ["test_r3_submit_time_dedupe_also_exempts_old_zombies"]),

    ("M9c", "陈年判据去掉时间下限 → 同批在途也被当僵尸放过(386 型重复下单又能发生)",
     DB,
     "                AND o.created_at < NOW() - INTERVAL "
     "'{int(ORPHAN_AGE_HOURS_FOR_DEDUPE)} hours'\n",
     "                AND TRUE\n",
     ["test_r3_submit_time_dedupe_also_exempts_old_zombies"]),

    # ---------------- R1 fail-closed ----------------
    ("M11", "🔴 拿掉空单号守卫 —— 又能写出无单号的 submitted",
     DB,
     '    if not (mhz_order_id and str(mhz_order_id).strip()):\n',
     '    if False:\n',
     ["test_r1_submitted_guard_cells"]),

    # ---------------- R4 文案 ----------------
    ("M12", "published 又说成「正在发布中」",
     API,
     '        parts.append(_fmt(published, "这几篇已经发过对应媒体了："))\n',
     '        parts.append(_fmt(published, "这几篇正在发布中，请勿重复提交："))\n',
     ["test_r4_message_cells"]),

    ("M13", "超出条数恢复静默截断",
     API,
     "        if len(labels) > _DUP_LIST_MAX:\n",
     "        if False:\n",
     ["test_r4_overflow_is_not_silent"]),

    ("M14", "标题恢复硬截",
     API,
     '    head = max(1, (limit * 2) // 3)\n    tail = max(1, limit - head)\n'
     '    return f"{text[:head]}…{text[-tail:]}"\n',
     "    return text[:15]\n",
     ["test_r4_title_not_cut_mid_sentence"]),

    # ---------------- 接线 ----------------
    ("M15", "单发链路拿掉空单号收口(又会留下 pending 僵尸)",
     API,
     "                        if not update_order_item_submitted(item[\"id\"], item_sn):\n",
     "                        if False and not update_order_item_submitted(item[\"id\"], item_sn):\n",
     ["test_r1_all_submitted_sinks_settle_on_empty_sn"]),

    ("M15b", "单发链路把收口分支整个删掉(裸调用)",
     API,
     '                        if not update_order_item_submitted(item["id"], item_sn):\n',
     '                        update_order_item_submitted(item["id"], item_sn)\n'
     '                        if False:\n',
     ["test_r1_all_submitted_sinks_settle_on_empty_sn"]),

    ("M15c", "自媒体批量链路把收口条件 and 掉",
     API,
     '                            if not update_order_item_submitted(oi["id"], item_sn):\n'
     '                                # 🔴 [WO-PUB-ZOMBIE] 同软文段:没拿到单号不许留 pending。\n',
     '                            if False and not update_order_item_submitted(oi["id"], item_sn):\n'
     '                                # 🔴 [WO-PUB-ZOMBIE] 同软文段:没拿到单号不许留 pending。\n',
     ["test_r1_all_submitted_sinks_settle_on_empty_sn"]),

    ("M15d", "调度器重试链路把收口条件 and 掉",
     SCHED,
     '                                    if not update_order_item_submitted(it["id"], item_sn):\n'
     '                                        # 🔴 [WO-PUB-ZOMBIE] 重试投出去了但没拿到单号 —— 不许回 pending\n',
     '                                    if False and not update_order_item_submitted(it["id"], item_sn):\n'
     '                                        # 🔴 [WO-PUB-ZOMBIE] 重试投出去了但没拿到单号 —— 不许回 pending\n',
     ["test_r1_all_submitted_sinks_settle_on_empty_sn"]),

    ("M15e", "豁免标记被滥用:给真需要收口的 sink 贴免检条(守卫数掉下地板)",
     API,
     '                        if not update_order_item_submitted(item["id"], item_sn):\n',
     '                        update_order_item_submitted(item["id"], item_sn)  '
     '# WO-PUB-ZOMBIE-sn-nonempty\n                        if False:\n',
     ["test_r1_all_submitted_sinks_settle_on_empty_sn"]),

    ("M16", "清扫器 job 没注册(建了不跑)",
     SCHED,
     "                          'interval', minutes=15, id='mhz_orphan_zombie_sweep', replace_existing=True)\n",
     "                          'interval', minutes=15, id='NOT_REGISTERED', replace_existing=True)\n",
     ["test_new_functions_have_real_call_sites"]),

    ("M17", "退款不走既有幂等键(自造第二条资金路径的形状)",
     SVC,
     '                refund_key=f"item:{item_id}",\n',
     '                refund_key=f"orphan:{item_id}",\n',
     ["test_no_second_refund_path", "test_sweep_refunds_when_external_absent",
      "test_sweep_refunds_when_external_claimed_by_other_item"]),
]


def read_src(path: Path) -> str:
    """🔴 一律走 bytes 手动解码 —— **不能用 `Path.read_text`/`write_text`**。

    那对函数在 Windows 上会做换行翻译:读时 `\\r\\n`→`\\n`、写时 `\\n`→`\\r\\n`。
    于是"读原文 → 写回原文"这个本该是恒等的还原操作,会把整个文件从 LF 翻成 CRLF
    —— 表现成 `git diff` 里几千行全变、真正的改动被淹掉。
    (`read_text(newline=...)` 是 3.13+,生产是 3.12,不能用。)
    """
    return path.read_bytes().decode("utf-8")


def write_src(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def run_pytest() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TESTS, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def failed_names(output: str) -> set[str]:
    names = set()
    for line in output.splitlines():
        if line.startswith("FAILED ") or line.startswith("ERROR "):
            part = line.split("::")[-1].split()[0] if "::" in line else ""
            if part:
                names.add(part)
    return names


def main() -> int:
    rc, out = run_pytest()
    if rc != 0:
        print("基线不绿,先修基线再跑变异:\n" + out[-3000:])
        return 2
    print(f"基线 GREEN\n{'=' * 72}")

    killed, survived = 0, []
    for mid, desc, rel, old, new, expect in MUTATIONS:
        path = REPO / rel
        original = read_src(path)

        hits = original.count(old)
        if hits != 1:
            print(f"[{mid}] ❌ 锚点命中 {hits} 次(需恰好 1 次)—— 变异没打在预期位置: {desc}")
            survived.append((mid, desc, f"锚点命中 {hits} 次"))
            continue

        mutated = original.replace(old, new, 1)
        if mutated == original:
            print(f"[{mid}] ❌ 落盘后文件没变(no-op 变异): {desc}")
            survived.append((mid, desc, "文件未改变"))
            continue

        write_src(path, mutated)
        try:
            m_rc, m_out = run_pytest()
        finally:
            write_src(path, original)

        reds = failed_names(m_out)
        hit_expected = sorted(reds & set(expect))
        if m_rc == 0:
            print(f"[{mid}] ❌ 存活(零用例转红 = 这条锁抓不到本 bug): {desc}")
            survived.append((mid, desc, "零转红"))
        elif not hit_expected:
            print(f"[{mid}] ⚠️ 转红但**不是预期那些**(锁可能打偏): {desc}\n"
                  f"        预期 {expect}\n        实际 {sorted(reds)}")
            survived.append((mid, desc, f"红的是 {sorted(reds)}"))
        else:
            killed += 1
            print(f"[{mid}] ✅ 被杀 ({len(reds)} 红,含预期 {hit_expected}): {desc}")

    print("=" * 72)
    print(f"变异结果: {killed}/{len(MUTATIONS)} 被杀")
    if survived:
        print("存活/无效变异:")
        for mid, desc, why in survived:
            print(f"  - {mid} [{why}] {desc}")
        return 1

    rc2, _ = run_pytest()
    print("还原后复跑基线:", "GREEN" if rc2 == 0 else "RED(文件未正确还原!)")
    return 0 if rc2 == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
