"""窗D 反向变异逐发跑器。

规矩(本仓记过的坑,逐条避开):

* **不用 ``git checkout`` 撤销** —— 那会连带撤掉工作树里别的改动。
  这里先把原文读进内存,跑完**原样写回**,并逐字节校验还原成功。
* 变异必须**语法合法、语义精确** —— 整段替成废代码只是 blunt kill,
  证明不了"判据在守那一行"。
* 每发都声明 ``expect_red_nodes``:**必须转红的判据**。
  只看"有没有失败"不够 —— 失败在别处说明变异打偏了。
* Windows 读改写一律 ``newline=""`` + utf-8,防 LF→CRLF 静默翻转。

用法::

    TEST_DATABASE_URL=... python tests/defensive_geo_w4_2026_08_22/_mutation_runner.py
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUITE = "tests/defensive_geo_w4_2026_08_22/test_wp7_lineage_and_denominators.py"

MUTATIONS: list[dict] = [
    {
        "id": "MUT-01",
        "why": "身份 hash 只吃前 3 个字段 —— 其余字段变了 id 不变",
        "file": "services/defensive_geo/monitoring/lineage.py",
        "old": "    for f in fields:\n        parts.append(f.encode(\"utf-8\"))",
        "new": "    for f in fields[:3]:\n        parts.append(f.encode(\"utf-8\"))",
        "expect_red": ["test_every_formula_field_changes_the_id",
                       "test_every_attempt_field_changes_the_id"],
    },
    {
        "id": "MUT-02",
        "why": "零分母返回 0.0 —— 报告上会写成 0%,而真相是没测到",
        "file": "services/defensive_geo/monitoring/denominators.py",
        "old": "        return Ratio(numerator_label, denominator_key, unit_of(denominator_key),\n                     0, 0, None, \"no_denominator\")",
        "new": "        return Ratio(numerator_label, denominator_key, unit_of(denominator_key),\n                     0, 0, 0.0, \"measured\")",
        "expect_red": ["test_zero_denominator_is_never_zero_percent"],
    },
    {
        "id": "MUT-03",
        "why": "主覆盖率分子改成 mapped(2)—— MET-32 要的是 verified(1)",
        "file": "services/defensive_geo/monitoring/attribution.py",
        "old": "        denominator_key=\"source_attribution_eligible_claims\",\n        numerator=len(verified),",
        "new": "        denominator_key=\"source_attribution_eligible_claims\",\n        numerator=len(mapped),",
        "expect_red": ["test_met32_fixture_gives_one_over_ten_and_one_over_two"],
    },
    {
        "id": "MUT-04",
        "why": "R-1 反转:内容类低置信冲突默认转人工而不是交 AI",
        "file": "services/defensive_geo/monitoring/defensive_projection.py",
        "old": "    return \"ai_adjudicated\"",
        "new": "    return \"pending_review\"",
        "expect_red": ["test_content_conflict_defaults_to_ai_not_human"],
    },
    {
        "id": "MUT-05",
        "why": "不可比时仍然给出涨跌结论(§13.2 下游)",
        "file": "services/defensive_geo/monitoring/renewal.py",
        "old": "    if comparability_level == \"none\":\n        level = \"insufficient_basis\"\n    else:",
        "new": "    if False:\n        level = \"insufficient_basis\"\n    else:",
        "expect_red": ["test_non_comparable_cannot_conclude_improvement"],
    },
    {
        "id": "MUT-06",
        "why": "新门误伤 legacy —— §19 变异 140 的另一半",
        "file": "services/defensive_geo/monitoring/enrollment.py",
        "old": "    if not is_v2_enrolled:",
        "new": "    if False:",
        "expect_red": ["test_legacy_is_never_blocked_by_the_new_gate"],
    },
    {
        "id": "MUT-07",
        "why": "R-4 反转:发现冲突就把客户确认过的事实抹掉",
        "file": "services/defensive_geo/monitoring/source_consistency.py",
        "old": "        # R-4:永远原样带出,不因冲突消失。\n        \"acceptedValue\": accepted_value,",
        "new": "        # R-4:永远原样带出,不因冲突消失。\n        \"acceptedValue\": None if state == \"conflicting\" else accepted_value,",
        "expect_red": ["test_accepted_fact_survives_a_conflict"],
    },
    {
        "id": "MUT-08",
        "why": "推荐 positive set 放宽成 issubset —— mentioned_only 可以偷渡进来",
        "file": "services/defensive_geo/monitoring/scenario_coverage.py",
        "old": "    if positive != signed:",
        "new": "    if not signed.issubset(positive):",
        "expect_red": ["test_smuggling_mentioned_only_into_recommendation_is_refused"],
    },
    {
        "id": "MUT-09",
        "why": "POR-15:允许同 hash 原地改状态",
        "file": "services/defensive_geo/monitoring/snapshot.py",
        "old": "    if existing_hash == new_hash and existing_state != new_state:",
        "new": "    if False:",
        "expect_red": ["test_in_place_state_change_on_same_hash_is_refused"],
    },
    {
        "id": "MUT-10",
        "why": "MON-10:fallback 成功后只保留 canonical 的 error,首发失败被抹掉",
        "file": "services/defensive_geo/monitoring/attempt_ledger.py",
        "old": "            for a in attempts if a.error_code",
        "new": "            for a in attempts if a.error_code and a is canonical",
        "expect_red": ["test_fallback_success_keeps_the_first_attempt_error"],
    },
    {
        "id": "MUT-11",
        "why": "空串冒充 null —— service_projection_id='' 与 null 撞成同一格",
        "file": "services/defensive_geo/monitoring/lineage.py",
        "old": "            if not v:\n                raise LineageIdentityError(",
        "new": "            if False:\n                raise LineageIdentityError(",
        "expect_red": ["test_null_and_empty_string_are_not_the_same"],
    },
    {
        "id": "MUT-12",
        "why": "§0.5.1-7 去混淆门摘掉:现役 question_key(裸 64 hex)可以传进来",
        "file": "services/defensive_geo/monitoring/lineage.py",
        "old": "    if _LEGACY_QUESTION_KEY_SHAPE.match(value):",
        "new": "    if False:",
        "expect_red": ["test_legacy_question_key_shape_is_refused"],
    },
    {
        "id": "MUT-13",
        "why": "资金 sink 接线锁自证:往续费模块引入 billing",
        "file": "services/defensive_geo/monitoring/renewal.py",
        "old": "from typing import Any, Mapping, NamedTuple, Sequence",
        "new": "from typing import Any, Mapping, NamedTuple, Sequence\nfrom middleware import billing  # noqa: F401  [MUTATION]",
        "expect_red": ["test_renewal_module_imports_no_funding_sink"],
    },
    {
        "id": "MUT-14",
        "why": "MET-31 反转:出现冲突就强制降成 unsupported",
        "file": "services/defensive_geo/monitoring/defensive_projection.py",
        "old": "    if support_level in FORBIDDEN_MERGED_SUPPORT_LEVELS:",
        "new": "    if has_conflict and support_level == 'corroborated':\n        raise DefensiveProjectionError('conflict downgrade [MUTATION]')\n    if support_level in FORBIDDEN_MERGED_SUPPORT_LEVELS:",
        "expect_red": ["test_corroborated_with_conflict_is_legal"],
    },
    {
        "id": "MUT-15",
        "why": "冻结项少一项仍能算出 hash(MON-06 重建前提破)",
        "file": "services/defensive_geo/monitoring/snapshot.py",
        "old": "    for f in FROZEN_FIELDS:\n        parts.append(f.encode(\"utf-8\"))",
        "new": "    for f in FROZEN_FIELDS[:4]:\n        parts.append(f.encode(\"utf-8\"))",
        "expect_red": ["test_every_frozen_field_changes_the_hash"],
    },
]


def _read(path: Path) -> str:
    with io.open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(path: Path, text: str) -> None:
    with io.open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _run_suite() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", SUITE, "-q", "--no-header",
         "-p", "no:cacheprovider", "--tb=no"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    results: list[tuple[str, str, str]] = []
    for mut in MUTATIONS:
        path = ROOT / mut["file"]
        original = _read(path)
        if mut["old"] not in original:
            results.append((mut["id"], "ANCHOR-MISS",
                            f"变异锚点在 {mut['file']} 找不到 —— 这一发没打出去"))
            continue
        occurrences = original.count(mut["old"])
        if occurrences != 1:
            results.append((mut["id"], "ANCHOR-AMBIGUOUS",
                            f"锚点命中 {occurrences} 处,变异会打到多个位置"))
            continue
        try:
            _write(path, original.replace(mut["old"], mut["new"], 1))
            rc, out = _run_suite()
            if rc == 0:
                results.append((mut["id"], "SURVIVED",
                                "判据全绿 —— 这一行没有任何判据在守"))
                continue
            missing = [n for n in mut["expect_red"] if n not in out]
            if missing:
                results.append((mut["id"], "RED-BUT-WRONG-NODE",
                                f"红了但点名的判据没红:{missing}"))
            else:
                failed = out.strip().splitlines()[-1] if out.strip() else ""
                results.append((mut["id"], "KILLED", failed))
        finally:
            _write(path, original)
            assert _read(path) == original, f"{mut['file']} 还原失败"

    print("\n" + "=" * 78)
    print("反向变异逐发结果(窗D · WP7 纯域)")
    print("=" * 78)
    bad = 0
    for mid, verdict, detail in results:
        flag = "OK " if verdict == "KILLED" else "!! "
        if verdict != "KILLED":
            bad += 1
        why = next(m["why"] for m in MUTATIONS if m["id"] == mid)
        print(f"{flag}{mid}  {verdict:<20} {why}")
        if verdict != "KILLED":
            print(f"      {detail}")
    print("-" * 78)
    print(f"KILLED {len(results) - bad}/{len(results)}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
