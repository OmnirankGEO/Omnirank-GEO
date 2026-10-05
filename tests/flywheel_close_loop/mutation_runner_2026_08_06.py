"""变异测试跑手(WO_DELIVERY_FLYWHEEL_CLOSURE §4.5「锁+变异严格全红」)。

对每条变异:改源码 → 跑指定测试 → **必须变红** → 还原。
任何一条变异跑出绿,说明对应的锁抓不到它 = 那条锁是假的。

用法:  TEST_DATABASE_URL=... python tests/flywheel_close_loop/mutation_runner_2026_08_06.py

🔴 文件读写一律 newline="" —— 否则在 Windows 上会把整个源文件的行尾改成 CRLF,
   变异跑完"还原"出来的文件和原文件字节不同(2026-08-05 踩过,10 个源文件被写成 CRLF)。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# (名字, 相对路径, 原文片段, 变异片段, 必须变红的测试)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    (
        "M1 实体匹配放宽成恒相等(跨实体串味)",
        "services/strict_article_outcomes.py",
        "            return identity_map.get(int(brand_id)) or brand_id",
        "            return 'ANY'",
        "tests/flywheel_close_loop/test_attribution_ledger_2026_08_06.py",
    ),
    (
        "M2 实体映射被忽略(回到永远归不上)",
        "services/strict_article_outcomes.py",
        "    identity_map = {int(k): str(v) for k, v in (identity_by_brand or {}).items() if v}",
        "    identity_map = {}",
        "tests/flywheel_close_loop/test_attribution_ledger_2026_08_06.py::test_identity_map_bridges_split_brand_ids",
    ),
    (
        "M3 URL 归一化不剥 tracking 参数",
        "services/strict_article_outcomes.py",
        '            if lowered.startswith("utm_") or lowered in TRACKING_KEYS:',
        "            if False:",
        "tests/flywheel_close_loop/test_attribution_ledger_2026_08_06.py::test_event_carries_normalized_url_and_domain",
    ),
    (
        "M4 落库参数缺字段也硬凑一行",
        "services/article_attribution_ledger.py",
        "    if result_id is None or not source or source_id is None:\n        return None",
        "    if False:\n        return None",
        "tests/flywheel_close_loop/test_attribution_ledger_2026_08_06.py::test_row_params_drops_incomplete_events",
    ),
    (
        "M5 零行报警闸恒绿(工单点名的那种假探针)",
        "services/flywheel_heartbeat.py",
        '        elif int(freshness.get("total_rows") or 0) <= 0:',
        "        elif False:",
        "tests/flywheel_close_loop/test_attribution_ledger_2026_08_06.py::test_ledger_silence_gate_fires_and_clears",
    ),
    (
        "M6 静默阈值被改(48h → 480h)",
        "services/flywheel_heartbeat.py",
        "LEDGER_SILENCE_ALERT_SECONDS = 48 * 3600.0",
        "LEDGER_SILENCE_ALERT_SECONDS = 480 * 3600.0",
        "tests/flywheel_close_loop/test_attribution_ledger_2026_08_06.py",
    ),
    (
        "M7 归因同步排到回写之后(每天读到昨天的账本)",
        "api/scheduler.py",
        "                trigger=CronTrigger(hour=4, minute=5, timezone=BEIJING_TZ),\n                id=\"article_attribution_sync\",",
        "                trigger=CronTrigger(hour=4, minute=50, timezone=BEIJING_TZ),\n                id=\"article_attribution_sync\",",
        "tests/flywheel_close_loop/test_attribution_ledger_2026_08_06.py::test_attribution_sync_job_registered_and_ordered_before_backfill",
    ),
    (
        "M8 飞轮回写不读账本(账本建了但没接线)",
        "services/writing_outcome_backfill.py",
        "        from services.article_attribution_ledger import citation_counts_for_articles",
        "        citation_counts_for_articles = lambda *a, **k: {}",
        "tests/flywheel_close_loop/test_attribution_ledger_2026_08_06.py::test_writing_backfill_reads_attribution_ledger",
    ),
    (
        "M9 错字判据去掉形近条件(误伤真实业务词)",
        "services/keyword_hygiene.py",
        "            return ch_place in _CONFUSABLE.get(ch_token, set())",
        "            return True",
        "tests/flywheel_close_loop/test_keyword_hygiene_2026_08_06.py",
    ),
    (
        # 🔴 这条变异第一版写成「非重叠 2/3 字分块」,结果**存活**了 ——
        #    因为「东菀」恰好落在偶数偏移,非重叠切分照样切得到。变异不忠实于真实 bug,
        #    等于给自己发了一张假的及格证。现在改成复刻真实的原始实现(贪婪 3 字非重叠)。
        "M10 候选切分退回真实原始 bug(re.findall 贪婪非重叠)",
        "services/keyword_hygiene.py",
        "    runs = re.findall(r\"[一-鿿]+\", str(text or \"\"))\n    for run in runs:\n        for size in (2, 3):\n            for start in range(0, len(run) - size + 1):\n                yield run[start:start + size]",
        "    for token in re.findall(r\"[一-鿿]{2,3}\", str(text or \"\")):\n        yield token",
        "tests/flywheel_close_loop/test_keyword_hygiene_2026_08_06.py",
    ),
    (
        "M10b 候选切分退回非重叠分块(偏移敏感的弱化版)",
        "services/keyword_hygiene.py",
        "        for size in (2, 3):\n            for start in range(0, len(run) - size + 1):\n                yield run[start:start + size]",
        "        for size in (2, 3):\n            for start in range(0, len(run) - size + 1, size):\n                yield run[start:start + size]",
        "tests/flywheel_close_loop/test_keyword_hygiene_2026_08_06.py",
    ),
    (
        "M11 错字改成只提示不阻断",
        "services/keyword_hygiene.py",
        "            blocking.append({\n                \"rule\": \"place_typo\",",
        "            advisory.append({\n                \"rule\": \"place_typo\",",
        "tests/flywheel_close_loop/test_keyword_hygiene_2026_08_06.py::test_typo_blocks_but_too_few_only_advises",
    ),
    (
        "M12 词数不足改成阻断(无效警告)",
        "services/keyword_hygiene.py",
        "        advisory.append({\n            \"rule\": \"too_few_keywords\",",
        "        blocking.append({\n            \"rule\": \"too_few_keywords\",",
        "tests/flywheel_close_loop/test_keyword_hygiene_2026_08_06.py::test_typo_blocks_but_too_few_only_advises",
    ),
    (
        # [开源 E3 · WO_323 G3a · 2026-10-02] 原锚(报价加词入口)随端点删;改指写作大厅补词入口(同样是付费配置面)
        "M13 摘掉一个付费入口的闸(留降级出口绕过)",
        "server.py",
        "        _hygiene_gate_keywords([kw], quote_id=quote_id)",
        "        pass  # gate removed",
        "tests/flywheel_close_loop/test_keyword_hygiene_2026_08_06.py::test_every_confirmed_keyword_insert_site_is_gated",
    ),
    (
        "M17 无快照的行被算进主指标(伪造证据等级)",
        "services/strict_article_outcomes.py",
        "    proven = [e for e in events if e.get(\"body_proof\", True)]",
        "    proven = list(events)",
        "tests/flywheel_close_loop/test_attribution_ledger_2026_08_06.py::test_missing_snapshot_is_recorded_but_never_counted_as_proven",
    ),
    (
        "M18 默认口径被放宽成永远降级(严格闸消失)",
        "services/strict_article_outcomes.py",
        "            if not include_unverified_body:\n                quality[\"publication_snapshot_missing\"] += 1\n                continue",
        "            if False:\n                quality[\"publication_snapshot_missing\"] += 1\n                continue",
        "tests/flywheel_close_loop/test_attribution_ledger_2026_08_06.py::test_missing_snapshot_is_dropped_by_default",
    ),
    (
        "M19 飞轮被引计数不再过滤 body_proof(主指标被污染)",
        "services/article_attribution_ledger.py",
        "                   AND body_proof",
        "                   AND TRUE",
        "tests/flywheel_close_loop/test_attribution_ledger_2026_08_06.py::test_flywheel_metric_filters_on_body_proof",
    ),
    (
        "M14 渠道分级恒定结论(假分级)",
        "services/channel_effectiveness_report.py",
        "    if our_published < MIN_SAMPLE_FOR_VERDICT:",
        "    if True:",
        "tests/flywheel_close_loop/test_channel_effectiveness_2026_08_06.py",
    ),
    (
        "M15 池子存在感被忽略(会误杀 cnblogs 那类域)",
        "services/channel_effectiveness_report.py",
        "    pool_present = pool_articles >= POOL_PRESENT_MIN_ARTICLES",
        "    pool_present = False",
        "tests/flywheel_close_loop/test_channel_effectiveness_2026_08_06.py",
    ),
    (
        "M16 流水线路径偷偷改成阻断",
        "db/diagnosis_db.py",
        "        from services.keyword_hygiene import check_keywords as _kw_check",
        "        from services.keyword_hygiene import assert_keywords_clean as _kw_check",
        "tests/flywheel_close_loop/test_keyword_hygiene_2026_08_06.py::test_pipeline_writer_records_but_does_not_block",
    ),
]


def _run(target: str) -> bool:
    """True = 绿。

    🔴🔴 `PYTHONDONTWRITEBYTECODE=1` 不是洁癖,是**判据正确性的前提**:
       变异 runner 在秒级内反复改写同一个 .py,CPython 的 .pyc 失效判断靠 mtime(秒级),
       会**复用变异前的字节码** —— 于是"变异跑出绿"其实是在跑原始代码,被记成 SURVIVED。
       实测踩过:M10 第一轮报存活,清掉 __pycache__ 后行为就对了。
       注意这个偏差是**单向**的:它只会把"已杀死"误报成"存活",不会把存活误报成杀死。
    """
    import os

    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", target, "-q", "--no-header", "-x",
         "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env,
    )
    return proc.returncode == 0


def main() -> int:
    print("=" * 78)
    print("第 0 步:基线必须全绿 —— 基线就红的话,后面每条变异都会'红',但那是假红")
    print("=" * 78)
    baseline_targets = sorted({m[4].split("::")[0] for m in MUTATIONS})
    for target in baseline_targets:
        ok = _run(target)
        print(f"  {'✅' if ok else '🔴'} baseline {target}")
        if not ok:
            print("基线红,变异测试无判别力,中止。")
            return 2

    killed, survived = 0, []
    print()
    print("=" * 78)
    print(f"第 1 步:逐条变异({len(MUTATIONS)} 条),每条**必须打红**对应的锁")
    print("=" * 78)
    for name, rel, original, mutated, target in MUTATIONS:
        path = ROOT / rel
        source = path.read_text(encoding="utf-8", newline="")
        if original not in source:
            print(f"  🔴 {name}: 变异锚点没命中源码 —— 变异本身是坏的,不算杀死")
            survived.append(f"{name}(锚点失效)")
            continue
        assert source.count(original) >= 1
        path.write_text(source.replace(original, mutated, 1), encoding="utf-8", newline="")
        try:
            still_green = _run(target)
        finally:
            path.write_text(source, encoding="utf-8", newline="")
        if still_green:
            print(f"  🔴 SURVIVED  {name}  → 锁抓不到它")
            survived.append(name)
        else:
            print(f"  ✅ KILLED    {name}")
            killed += 1

    print()
    print("=" * 78)
    print(f"结果:{len(MUTATIONS)} 条变异 · 杀死 {killed} · 存活 {len(survived)}")
    if survived:
        for s in survived:
            print(f"  存活:{s}")
        return 1
    print("全部杀死。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
