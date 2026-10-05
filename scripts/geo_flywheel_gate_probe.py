"""Read-only deploy gate probes for the GEO placement flywheel.

This script is intentionally read-only. It checks schema readiness and the
most important shadow-safety invariants before Deploy-CTO runs rebuild jobs.

Usage:
  python scripts/geo_flywheel_gate_probe.py --json
  python scripts/geo_flywheel_gate_probe.py --sql-only
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from typing import Any, Iterable

import psycopg2
from psycopg2.extras import RealDictCursor


@dataclass(frozen=True)
class Probe:
    name: str
    severity: str
    sql: str
    pass_when: str
    description: str


PROBES: tuple[Probe, ...] = (
    Probe(
        name="required_tables_exist",
        severity="fail",
        pass_when="missing_count == 0",
        description="All flywheel and research-monitor tables needed by rebuild exist.",
        sql="""
        WITH required(table_name) AS (
            VALUES
              ('geo_research_raw'),
              ('geo_research_round'),
              ('geo_research_article_citations'),
              ('geo_research_articles'),
              ('geo_research_source_signals'),
              ('geo_media_entities'),
              ('geo_media_inventory_mappings'),
              ('media_entity_score_snapshots'),
              ('writing_strategy_versions')
        )
        SELECT COUNT(*) FILTER (WHERE tables.table_name IS NULL)::int AS missing_count,
               COALESCE(jsonb_agg(required.table_name)
                        FILTER (WHERE tables.table_name IS NULL), '[]'::jsonb) AS missing
          FROM required
          LEFT JOIN information_schema.tables tables
            ON tables.table_schema = 'public'
           AND tables.table_name = required.table_name
        """,
    ),
    Probe(
        name="required_columns_exist",
        severity="fail",
        pass_when="missing_count == 0",
        description="Columns introduced or consumed by the provenance flywheel are present.",
        sql="""
        WITH required(table_name, column_name) AS (
            VALUES
              ('geo_research_raw', 'is_answer_cited'),
              ('geo_research_raw', 'adoption_rank'),
              ('geo_research_article_citations', 'raw_id'),
              ('geo_research_source_signals', 'signal_tier'),
              ('geo_research_source_signals', 'balanced_weight'),
              ('geo_research_source_signals', 'total_sources_in_answer'),
              ('geo_research_source_signals', 'metadata'),
              ('media_entity_score_snapshots', 'reference_status'),
              ('media_entity_score_snapshots', 'is_purchasable'),
              ('writing_strategy_versions', 'status'),
              ('writing_strategy_versions', 'strategy_version')
        )
        SELECT COUNT(*) FILTER (WHERE columns.column_name IS NULL)::int AS missing_count,
               COALESCE(jsonb_agg(required.table_name || '.' || required.column_name)
                        FILTER (WHERE columns.column_name IS NULL), '[]'::jsonb) AS missing
          FROM required
          LEFT JOIN information_schema.columns columns
            ON columns.table_schema = 'public'
           AND columns.table_name = required.table_name
           AND columns.column_name = required.column_name
        """,
    ),
    Probe(
        name="reference_only_not_purchasable",
        severity="fail",
        pass_when="bad_count == 0",
        description="Fail-closed media entities must not become purchasable without verified inventory.",
        sql="""
        SELECT COUNT(*)::int AS bad_count
          FROM media_entity_score_snapshots
         WHERE reference_status = 'reference_only'
           AND is_purchasable IS TRUE
        """,
    ),
    Probe(
        name="one_active_writing_strategy_per_industry",
        severity="fail",
        pass_when="bad_count == 0",
        description="Only one active writing strategy may exist per industry.",
        sql="""
        WITH dupes AS (
            SELECT industry_key, COUNT(*)::int AS active_count
              FROM writing_strategy_versions
             WHERE status = 'active'
             GROUP BY industry_key
            HAVING COUNT(*) > 1
        )
        SELECT COUNT(*)::int AS bad_count,
               COALESCE(jsonb_agg(jsonb_build_object(
                   'industry_key', industry_key,
                   'active_count', active_count
               )), '[]'::jsonb) AS examples
          FROM dupes
        """,
    ),
    Probe(
        name="raw_adoption_columns_have_consistent_values",
        severity="warn",
        pass_when="invalid_rank_count == 0",
        description="Rows marked as answer-cited should not carry invalid adoption_rank values.",
        sql="""
        SELECT COUNT(*)::int AS raw_count,
               COUNT(*) FILTER (WHERE is_answer_cited IS TRUE)::int AS answer_cited_count,
               COUNT(*) FILTER (
                   WHERE is_answer_cited IS TRUE
                     AND (adoption_rank IS NULL OR adoption_rank <= 0)
               )::int AS invalid_rank_count
          FROM geo_research_raw
        """,
    ),
    Probe(
        name="citation_raw_id_coverage",
        severity="warn",
        pass_when="missing_raw_id_count == 0 and dangling_raw_id_count == 0",
        description="Article citations should keep raw_id so rebuild can join article quality gates.",
        sql="""
        SELECT COUNT(*)::int AS citation_count,
               COUNT(*) FILTER (WHERE citation.raw_id IS NULL)::int AS missing_raw_id_count,
               COUNT(*) FILTER (
                   WHERE citation.raw_id IS NOT NULL
                     AND raw.id IS NULL
               )::int AS dangling_raw_id_count
          FROM geo_research_article_citations citation
          LEFT JOIN geo_research_raw raw ON raw.id = citation.raw_id
        """,
    ),
    Probe(
        name="completed_rounds_not_zero_signal",
        severity="warn",
        pass_when="bad_count == 0",
        description="Completed research rounds with zero article signal should be investigated before rebuild.",
        sql="""
        SELECT COUNT(*)::int AS bad_count
          FROM geo_research_round
         WHERE status = 'completed'
           AND COALESCE(
                 CASE
                   WHEN (summary_json->>'total_articles_seen') ~ '^[0-9]+$'
                   THEN (summary_json->>'total_articles_seen')::int
                   ELSE 0
                 END,
                 0
               ) = 0
        """,
    ),
    Probe(
        name="source_signal_tier_distribution",
        severity="info",
        pass_when="informational",
        description="Current shadow source signal distribution after rebuild.",
        sql="""
        SELECT signal_tier,
               COUNT(*)::int AS row_count,
               ROUND(SUM(balanced_weight)::numeric, 4)::float AS total_balanced_weight
          FROM geo_research_source_signals
         GROUP BY signal_tier
         ORDER BY row_count DESC
        """,
    ),
)


def _database_url(cli_value: str = "") -> str:
    return (
        cli_value
        or os.getenv("DATABASE_URL", "")
        or os.getenv("TEST_DATABASE_URL", "")
    )


def _first_int(row: dict[str, Any], key: str) -> int:
    value = row.get(key)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _status_for_probe(probe: Probe, rows: list[dict[str, Any]]) -> str:
    first = rows[0] if rows else {}
    if probe.name in {"required_tables_exist", "required_columns_exist"}:
        return "pass" if _first_int(first, "missing_count") == 0 else "fail"
    if probe.name == "reference_only_not_purchasable":
        return "pass" if _first_int(first, "bad_count") == 0 else "fail"
    if probe.name == "one_active_writing_strategy_per_industry":
        return "pass" if _first_int(first, "bad_count") == 0 else "fail"
    if probe.name == "raw_adoption_columns_have_consistent_values":
        return "pass" if _first_int(first, "invalid_rank_count") == 0 else "warn"
    if probe.name == "citation_raw_id_coverage":
        missing = _first_int(first, "missing_raw_id_count")
        dangling = _first_int(first, "dangling_raw_id_count")
        return "pass" if missing == 0 and dangling == 0 else "warn"
    if probe.name == "completed_rounds_not_zero_signal":
        return "pass" if _first_int(first, "bad_count") == 0 else "warn"
    return "info"


def emit_sql(probes: Iterable[Probe] = PROBES) -> str:
    chunks: list[str] = []
    for probe in probes:
        chunks.append(f"-- {probe.name} [{probe.severity}]")
        chunks.append(probe.sql.strip())
        chunks.append("")
    return "\n".join(chunks).strip() + "\n"


def run_probes(database_url: str) -> dict[str, Any]:
    if not database_url:
        raise SystemExit("DATABASE_URL or TEST_DATABASE_URL is required unless --sql-only is used")

    results: list[dict[str, Any]] = []
    with psycopg2.connect(database_url, cursor_factory=RealDictCursor) as conn:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor() as cur:
            for probe in PROBES:
                try:
                    cur.execute(probe.sql)
                    rows = [dict(row) for row in cur.fetchall()]
                    status = _status_for_probe(probe, rows)
                    error = ""
                except psycopg2.Error as exc:
                    rows = []
                    status = "fail"
                    error = str(exc).strip()
                results.append({
                    "name": probe.name,
                    "severity": probe.severity,
                    "status": status,
                    "pass_when": probe.pass_when,
                    "description": probe.description,
                    "rows": rows,
                    "error": error,
                })
    return {
        "status": "fail" if any(r["status"] == "fail" for r in results) else "pass",
        "mode": "read_only",
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default="")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--sql-only", action="store_true")
    args = parser.parse_args()

    if args.sql_only:
        print(emit_sql(), end="")
        return 0

    payload = run_probes(_database_url(args.database_url))
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"status={payload['status']} mode={payload['mode']}")
        for result in payload["results"]:
            print(f"{result['status'].upper():5} {result['name']} - {result['description']}")
    return 1 if payload["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
