import json
from pathlib import Path

import psycopg2


def test_gate_probe_sql_is_read_only_and_covers_required_checks():
    from scripts.geo_flywheel_gate_probe import PROBES, emit_sql

    names = {probe.name for probe in PROBES}
    assert "required_columns_exist" in names
    assert "reference_only_not_purchasable" in names
    assert "citation_raw_id_coverage" in names
    assert "one_active_writing_strategy_per_industry" in names

    sql = emit_sql().upper()
    forbidden = ["INSERT ", "UPDATE ", "DELETE ", "DROP ", "TRUNCATE ", "ALTER "]
    for token in forbidden:
        assert token not in sql


def test_gate_probe_required_checks_count_only_missing_items():
    from scripts.geo_flywheel_gate_probe import PROBES

    probes = {probe.name: probe for probe in PROBES}
    tables_sql = probes["required_tables_exist"].sql
    columns_sql = probes["required_columns_exist"].sql

    assert "COUNT(*)::int AS missing_count" not in tables_sql
    assert "COUNT(*)::int AS missing_count" not in columns_sql
    assert "COUNT(*) FILTER (WHERE tables.table_name IS NULL)::int AS missing_count" in tables_sql
    assert "COUNT(*) FILTER (WHERE columns.column_name IS NULL)::int AS missing_count" in columns_sql


def test_gate_probe_reports_sql_errors_without_stopping(monkeypatch):
    from scripts import geo_flywheel_gate_probe as probe_module

    class FakeCursor:
        def __init__(self):
            self.current_sql = ""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql):
            self.current_sql = sql
            if "FROM media_entity_score_snapshots" in sql:
                raise psycopg2.ProgrammingError("relation does not exist")

        def fetchall(self):
            if "COUNT(*)::int AS missing_count" in self.current_sql:
                return [{"missing_count": 0, "missing": []}]
            if "bad_count" in self.current_sql:
                return [{"bad_count": 0}]
            if "invalid_rank_count" in self.current_sql:
                return [{"raw_count": 0, "answer_cited_count": 0, "invalid_rank_count": 0}]
            if "missing_raw_id_count" in self.current_sql:
                return [{"citation_count": 0, "missing_raw_id_count": 0, "dangling_raw_id_count": 0}]
            return []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def set_session(self, **_kwargs):
            return None

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(probe_module.psycopg2, "connect", lambda *_a, **_k: FakeConnection())

    report = probe_module.run_probes("postgresql://unit-test")

    errored = [r for r in report["results"] if r["name"] == "reference_only_not_purchasable"][0]
    assert report["status"] == "fail"
    assert errored["status"] == "fail"
    assert "relation does not exist" in errored["error"]
    assert len(report["results"]) == len(probe_module.PROBES)


def test_calibration_analyzer_counts_cjk_markers_and_shared_hosts(tmp_path: Path):
    from scripts.analyze_geo_flywheel_calibration import collect_records, summarize

    payload = {
        "engine": "qwen",
        "answer": "据携程介绍[2]，来源[1]显示亲子酒店更适合。代码 arr[3] 不应算引用。",
        "search_results": [
            {"url": "https://mp.weixin.qq.com/s/demo", "title": "公众号"},
            {"url": "https://www.sohu.com/a/demo", "title": "搜狐号"},
            {"url": "https://example.com/a", "title": "普通来源"},
        ],
    }
    data_file = tmp_path / "qwen_sample.json"
    data_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    summary = summarize(collect_records([tmp_path]))

    assert summary["record_count"] >= 1
    assert summary["engine_summary"]["qwen"]["answers_with_markers"] >= 1
    assert summary["engine_summary"]["qwen"]["answers_with_cjk_adjacent_markers"] >= 1
    assert summary["marker_count"] >= 2
    assert summary["cjk_adjacent_marker_count"] >= 2
    assert summary["code_index_like_count"] >= 1
    shared_hosts = dict(summary["shared_host_domains"])
    assert shared_hosts["mp.weixin.qq.com"] == 1
    assert shared_hosts["sohu.com"] == 1
