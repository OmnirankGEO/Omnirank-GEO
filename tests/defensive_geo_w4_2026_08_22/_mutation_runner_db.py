"""库层反向变异 —— 变异打在**迁移文件**上,不打在活库当前状态上。

🔴 为什么必须打迁移文件(本仓 2026-08-21 记过)
----------------------------------------------
conftest 的 session fixture 每次都 ``DROP SCHEMA`` + 重放迁移。
如果变异打在活库(比如手动 ``ALTER TABLE ... DROP CONSTRAINT``),
下一次 fixture 重放会把变异**悄悄还原**,于是判据全绿 ——
看起来像"判据没有区分力",实际是变异根本没活到被测那一刻。

所以:改迁移 SQL → 重放 → 跑判据 → 原样写回。
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUITE = "tests/defensive_geo_w4_2026_08_22/test_migration_046_pg.py"
MIGRATION = "db/migration_046_defgeo_monitoring_lineage_2026_08_22.sql"

MUTATIONS = [
    {
        "id": "DBMUT-01",
        "why": "承重① 摘掉 UNIQUE(plan_cell_id, ordinal):同一格可以有两条第 1 次尝试",
        "old": "            ADD CONSTRAINT uq_defgeo_attempt_cell_ordinal\n            UNIQUE (plan_cell_id, attempt_ordinal);",
        "new": "            ADD CONSTRAINT uq_defgeo_attempt_cell_ordinal\n            UNIQUE (attempt_id);",
        "expect_red": ["test_duplicate_ordinal_on_same_cell_is_rejected"],
    },
    {
        "id": "DBMUT-02",
        "why": "承重② 摘掉终态不可改写触发器:MON-03 只剩应用层 CAS",
        "old": "CREATE TRIGGER trg_defgeo_attempt_terminal_immutable\n    BEFORE UPDATE ON defgeo_monitoring_attempts\n    FOR EACH ROW EXECUTE FUNCTION defgeo_attempt_terminal_immutable();",
        "new": "-- [MUTATION] trigger removed",
        "expect_red": ["test_terminal_row_cannot_be_updated"],
    },
    {
        "id": "DBMUT-03",
        "why": "承重③ 摘掉 partial unique:同一格可以有两个在飞 attempt",
        # [索引守卫加固二单 2026-08-24] 锚跟到表绑定守卫的建索引分支。
        "old": "        CREATE UNIQUE INDEX uq_defgeo_attempt_single_inflight ON public.defgeo_monitoring_attempts (plan_cell_id) WHERE terminal_state IS NULL;",
        "new": "        NULL;  -- [MUTATION] partial unique removed",
        "expect_red": ["test_two_inflight_attempts_on_one_cell_are_rejected"],
    },
    {
        "id": "DBMUT-04",
        "why": "§13.1 合并态 CHECK 摘掉 —— 列宽已放宽,这条 CHECK 是真承重",
        "old": "            CHECK (support_level IN\n                   ('corroborated','inferred','unsupported','unknown'));",
        "new": "            CHECK (support_level IS NOT NULL);",
        "expect_red": ["test_merged_support_level_cannot_even_be_stored"],
    },
    {
        "id": "DBMUT-05",
        "why": "POR-15 快照原地改状态触发器摘掉",
        "old": "CREATE TRIGGER trg_defgeo_snapshot_no_inplace_state_change\n    BEFORE UPDATE ON defgeo_report_snapshots\n    FOR EACH ROW EXECUTE FUNCTION defgeo_snapshot_no_inplace_state_change();",
        "new": "-- [MUTATION] snapshot trigger removed",
        "expect_red": ["test_snapshot_inplace_state_change_is_rejected"],
    },
    {
        "id": "DBMUT-06",
        "why": "§13.4 六维 CHECK 放宽成 >=1:五维也能存进来",
        "old": "            CHECK (jsonb_typeof(signals) = 'array' AND jsonb_array_length(signals) = 6);",
        "new": "            CHECK (jsonb_typeof(signals) = 'array' AND jsonb_array_length(signals) >= 1);",
        "expect_red": ["test_renewal_requires_exactly_six_signals"],
    },
    {
        "id": "DBMUT-07",
        "why": "§9.6 小榜冻结理由的钱形态 CHECK 摘掉",
        "old": "            CHECK (public_text !~ '[0-9]\\s*(算力|元|块钱)' AND public_text !~ '[¥$]\\s*[0-9]');",
        "new": "            CHECK (length(public_text) >= 0);",
        "expect_red": ["test_business_numbers_cannot_be_stored"],
    },
    {
        "id": "DBMUT-08",
        "why": "MON-11:policy_skipped 零 provider 调用的 CHECK 摘掉",
        "old": "            CHECK (terminal_state <> 'policy_skipped' OR provider_called = FALSE);",
        "new": "            CHECK (terminal_state IS NOT NULL OR TRUE);",
        "expect_red": ["test_policy_skipped_must_not_claim_a_provider_call"],
    },
    {
        "id": "DBMUT-09",
        "why": "§13.2 下游:不可比却推荐续费的 CHECK 摘掉",
        "old": "            CHECK (comparability_level <> 'none' OR level = 'insufficient_basis');",
        "new": "            CHECK (comparability_level IS NOT NULL);",
        "expect_red": ["test_non_comparable_cannot_recommend_renewal"],
    },
    {
        "id": "DBMUT-10",
        "why": "体内塞一条 DML —— 零 DML 普查必须抓到(prestart 无条件重放)",
        # [索引守卫加固二单 2026-08-24] 锚跟到表绑定守卫的锚注释。
        "old": "-- @index-guard idx_defgeo_attempt_cell ON defgeo_monitoring_attempts plain",
        "new": "INSERT INTO defgeo_monitoring_attempts (attempt_id) VALUES ('x');\n-- @index-guard idx_defgeo_attempt_cell ON defgeo_monitoring_attempts plain",
        "expect_red": ["test_migration_body_has_zero_dml"],
    },
]


def _read(p: Path) -> str:
    with io.open(p, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(p: Path, t: str) -> None:
    with io.open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write(t)


def main() -> int:
    path = ROOT / MIGRATION
    original = _read(path)
    results = []
    for mut in MUTATIONS:
        if mut["old"] not in original:
            results.append((mut["id"], "ANCHOR-MISS", "锚点找不到 —— 这一发没打出去"))
            continue
        if original.count(mut["old"]) != 1:
            results.append((mut["id"], "ANCHOR-AMBIGUOUS",
                            f"锚点命中 {original.count(mut['old'])} 处"))
            continue
        try:
            _write(path, original.replace(mut["old"], mut["new"], 1))
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", SUITE, "-q", "--no-header",
                 "-p", "no:cacheprovider", "--tb=no"],
                cwd=ROOT, capture_output=True, text=True,
                encoding="utf-8", errors="replace")
            out = (proc.stdout or "") + (proc.stderr or "")
            if proc.returncode == 0:
                results.append((mut["id"], "SURVIVED", "判据全绿 —— 这条约束没人验"))
            else:
                missing = [n for n in mut["expect_red"] if n not in out]
                results.append((mut["id"],
                                "RED-BUT-WRONG-NODE" if missing else "KILLED",
                                f"点名判据没红:{missing}" if missing else ""))
        finally:
            _write(path, original)
            assert _read(path) == original, "迁移文件还原失败"

    print("\n" + "=" * 78)
    print("库层反向变异(窗D · 迁移 046 承重约束)")
    print("=" * 78)
    bad = 0
    for mid, verdict, detail in results:
        if verdict != "KILLED":
            bad += 1
        why = next(m["why"] for m in MUTATIONS if m["id"] == mid)
        print(f"{'OK ' if verdict == 'KILLED' else '!! '}{mid}  {verdict:<20} {why}")
        if detail:
            print(f"      {detail}")
    print("-" * 78)
    print(f"KILLED {len(results) - bad}/{len(results)}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
