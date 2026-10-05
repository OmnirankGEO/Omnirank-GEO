"""[WO-METASO-GATE-2026-08-04] 秘塔业务级失败闸 · 变异自检。

三关缺一即判无效变异:
  1. 锚点唯一命中(命中 0 或 >1 = 变异没打在预期位置);
  2. 落盘后文件真变了;
  3. **至少一个预期用例转红**。零转红 = 这条锁在这个 fixture 下抓不到本 bug。

🔴 文件读写一律走 bytes:`Path.read_text`/`write_text` 在 Windows 上会翻换行,
   "读原文再写回"这个本该恒等的还原操作会把整个文件从 LF 翻成 CRLF。

跑法: TEST_DATABASE_URL=... python tests/mutation_runner_metaso_gate_2026_08_04.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

HEALTH = "tools/metaso_health.py"
CA = "tools/competition_analyzer.py"
KVS = "tools/keyword_value_scorer.py"
MS = "tools/search/metaso_search.py"

PY_TESTS = "tests/test_metaso_business_failure_gate_2026_08_04.py"

# (编号, 说明, 文件, 原文, 替换, 预期转红的用例名片段)
MUTATIONS: list[tuple[str, str, str, str, str, list[str]]] = [
    # ---------------- 判据本身 ----------------
    ("H1", "🔴 errCode 检查失效(退回本 bug 的形态:200+errCode 当成功)",
     HEALTH,
     '    code = result.get("errCode", result.get("errcode"))\n',
     '    code = None\n',
     ["test_metaso_result_error_truth_table",
      "test_guard_falls_back_on_errcode_body",
      "test_errcode_body_becomes_error_and_exhausts_failover"]),

    ("H2", "🔴 判据恒返回 None(全放行 = 等于没有闸)",
     HEALTH,
     '    if result is None:\n        return "空响应(None)"\n',
     '    return None\n    if result is None:\n        return "空响应(None)"\n',
     ["test_metaso_result_error_truth_table",
      "test_truth_table_has_both_polarities",
      "test_guard_falls_back_on_errcode_body"]),

    ("H3", "🔴 判据恒返回失败(全拦 —— 正常响应也被当断血,反向对照必须抓到)",
     HEALTH,
     '    if result is None:\n        return "空响应(None)"\n',
     '    return "MUTANT-always-fail"\n    if result is None:\n        return "空响应(None)"\n',
     ["test_metaso_result_error_truth_table",
      "test_truth_table_has_both_polarities",
      "test_guard_does_not_fall_back_on_normal_empty",
      "test_normal_empty_results_never_alert",
      "test_normal_body_unchanged_and_success_true"]),

    ("H4", "信封完整的空结果也判失败(会把冷门词的真·零竞争误当故障兜底)",
     HEALTH,
     '        if not any(k in result for k in _ENVELOPE_KEYS):\n',
     '        if True:\n',
     ["test_metaso_result_error_truth_table",
      "test_guard_does_not_fall_back_on_normal_empty",
      "test_normal_empty_results_never_alert"]),

    ("H5", "缺 webpages 键不判失败(响应体不是搜索结果形状也放行)",
     HEALTH,
     '    if pages is None:\n        return "响应体缺 webpages 键(不是搜索结果形状)"\n',
     '    if pages is None:\n        pass\n',
     ["test_metaso_result_error_truth_table"]),

    # ---------------- 告警计数 ----------------
    ("A1", "阈值 off-by-one(第 N 次不响,要等第 N+1 次)",
     HEALTH,
     '            if not _state["alerting"] and n >= METASO_CONSECUTIVE_ALERT_THRESHOLD:\n',
     '            if not _state["alerting"] and n > METASO_CONSECUTIVE_ALERT_THRESHOLD:\n',
     ["test_alert_fires_only_at_threshold"]),

    ("A2", "🔴 去重失效(每次失败都拉一次 = 刷屏)",
     HEALTH,
     '            if not _state["alerting"] and n >= METASO_CONSECUTIVE_ALERT_THRESHOLD:\n',
     '            if n >= METASO_CONSECUTIVE_ALERT_THRESHOLD:\n',
     ["test_alert_deduped_no_spam"]),

    ("A3", "成功不清零连续计数(抖动也会攒够阈值误报)",
     HEALTH,
     '            _state["consecutive"] = 0\n            if _state["alerting"]:\n',
     '            pass\n            if _state["alerting"]:\n',
     ["test_success_resets_consecutive_counter"]),

    ("A4", "恢复不收告警(修好了还一直挂着)",
     HEALTH,
     '                _state["alerting"] = False\n                action = "resolve"\n',
     '                action = None\n',
     ["test_recovery_alerts_once_then_silent"]),

    ("A5", "告警阈值调成 1(单次抖动就报)",
     HEALTH,
     "METASO_CONSECUTIVE_ALERT_THRESHOLD = 20\n",
     "METASO_CONSECUTIVE_ALERT_THRESHOLD = 1\n",
     ["test_threshold_value_is_pinned"]),

    # ---------------- ① search_metaso 出口 ----------------
    ("C1", "🔴 断血仍记 success=True(假绿的正主)",
     CA,
     '                            tracker.record(success=False, error_msg=f"业务级失败: {biz_err}"[:200])\n',
     '                            tracker.record(success=True)\n',
     ["test_errcode_body_becomes_error_and_exhausts_failover"]),

    ("C2", "🔴 断血不 raise(failover 不触发,断血账号不切备用 —— 本 bug 的第二层)",
     CA,
     '                            raise RuntimeError(f"metaso 业务级失败: {biz_err}")  # raise → failover 换下一个账号\n',
     '                            return data\n',
     ["test_errcode_body_becomes_error_and_exhausts_failover"]),

    ("C3", "业务判据整体旁路(直接返回 body)",
     CA,
     '                        biz_err = metaso_result_error(data)\n',
     '                        biz_err = None\n',
     ["test_errcode_body_becomes_error_and_exhausts_failover"]),

    # ---------------- ② 守卫 ----------------
    ("K1", "🔴 守卫退回只认 \"error\" 键(2026-08-04 事故当天那个判据)",
     KVS,
     '            unusable = metaso_result_error(result)\n',
     '            unusable = "error" in result\n',
     ["test_guard_falls_back_on_errcode_body"]),

    ("K2", "兜底不留痕(事后分不清「真零竞争」和「测不到」)",
     KVS,
     '                    "fallback_reason": unusable,\n',
     '',
     ["test_guard_falls_back_on_errcode_body"]),

    ("K3", "兜底值写死 1(等于没兜底 —— 仍然是地板值进报价)",
     KVS,
     '                smart_default = estimate_competition_from_keyword(kw)\n',
     '                smart_default = 1\n',
     ["test_guard_falls_back_on_errcode_body"]),

    # ---------------- ① metaso_search(:113/:117) ----------------
    ("M1", "🔴 断血不返回 Error(把 errCode body 当搜索结果交出去)",
     MS,
     '            if biz_err:\n                return ToolResponse(\n'
     '                    content=[{"type": "text", "text": f"Error: 秘塔业务级失败 - {biz_err}"}]\n'
     '                )\n',
     '            if False:\n                pass\n',
     ["test_metaso_search_errcode_returns_error"]),

    ("M2", "🔴 tracker 退回只判 status_code(:113 那一行的原形态)",
     MS,
     '                tracker.record(\n                    success=biz_err is None,\n',
     '                tracker.record(\n                    success=response.status_code == 200,\n',
     ["test_metaso_search_errcode_returns_error"]),
]


def read_src(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def write_src(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def run_locks() -> tuple[int, str]:
    env = dict(os.environ)
    env.setdefault("TEST_DATABASE_URL",
                   "postgresql://geo_test:geo_test@127.0.0.1:5432/geo_test")
    p = subprocess.run(
        [sys.executable, "-m", "pytest", PY_TESTS, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=REPO, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
    )
    return (1 if p.returncode else 0), (p.stdout or "") + (p.stderr or "")


def red_markers(output: str) -> set[str]:
    marks: set[str] = set()
    for line in output.splitlines():
        if line.startswith("FAILED ") and "::" in line:
            marks.add(line.split("::")[-1].split()[0].split("[")[0])
        if line.startswith("ERROR ") and "::" in line:
            marks.add(line.split("::")[-1].split()[0].split("[")[0])
    return marks


def main() -> int:
    rc, out = run_locks()
    if rc != 0:
        print("基线不绿,先修基线:\n" + out[-3000:])
        return 2
    print("基线 GREEN\n" + "=" * 72)

    killed, survived = 0, []
    for mid, desc, rel, old, new, expect in MUTATIONS:
        path = REPO / rel
        original = read_src(path)
        hits = original.count(old)
        if hits != 1:
            print(f"[{mid}] ❌ 锚点命中 {hits} 次(需恰好 1 次): {desc}")
            survived.append((mid, desc, f"锚点命中 {hits} 次"))
            continue
        mutated = original.replace(old, new, 1)
        if mutated == original:
            print(f"[{mid}] ❌ 落盘后文件没变(no-op): {desc}")
            survived.append((mid, desc, "文件未改变"))
            continue

        write_src(path, mutated)
        try:
            m_rc, m_out = run_locks()
        finally:
            write_src(path, original)

        reds = red_markers(m_out)
        hit = sorted(reds & set(expect))
        if m_rc == 0:
            print(f"[{mid}] ❌ 存活(零转红): {desc}")
            survived.append((mid, desc, "零转红"))
        elif not hit:
            print(f"[{mid}] ⚠️ 转红但不是预期那些: {desc}\n"
                  f"        预期 {expect} / 实际 {sorted(reds)}")
            survived.append((mid, desc, f"红的是 {sorted(reds)}"))
        else:
            killed += 1
            print(f"[{mid}] ✅ 被杀 (含预期 {hit}): {desc}")

    print("=" * 72)
    print(f"变异结果: {killed}/{len(MUTATIONS)} 被杀")
    if survived:
        print("存活/无效变异:")
        for mid, desc, why in survived:
            print(f"  - {mid} [{why}] {desc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
