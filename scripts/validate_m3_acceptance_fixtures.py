#!/usr/bin/env python3
"""Validate M3 acceptance fixtures against the target database.

This is a preflight guard for staging smoke runs. It catches stale fixture
snapshots before Playwright reaches protected routes and fails with ambiguous
UI assertions.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


DEFAULT_FIXTURES = "scripts/m3_acceptance_fixtures.json"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _json_default(value: Any) -> str:
    return str(value)


def load_fixtures(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def get_conn():
    from db.connection import get_connection

    return get_connection()


def latest_diagnosis_for_brand(cursor, brand_id: int) -> dict[str, Any] | None:
    cursor.execute(
        """
        SELECT id, brand_id, data_completeness_score, created_at
        FROM diagnosis_records
        WHERE brand_id = %s
        ORDER BY id DESC
        LIMIT 1
        """,
        (brand_id,),
    )
    row = cursor.fetchone()
    return dict(row) if row else None


def diagnosis_by_id(cursor, diagnosis_id: int) -> dict[str, Any] | None:
    cursor.execute(
        """
        SELECT id, brand_id, data_completeness_score, created_at
        FROM diagnosis_records
        WHERE id = %s
        """,
        (diagnosis_id,),
    )
    row = cursor.fetchone()
    return dict(row) if row else None


def quote_by_id(cursor, quote_id: int) -> dict[str, Any] | None:
    cursor.execute(
        """
        SELECT id, brand_id, status, monthly_price
        FROM quotes
        WHERE id = %s
        """,
        (quote_id,),
    )
    row = cursor.fetchone()
    return dict(row) if row else None


def add_check(checks: list[dict[str, Any]], name: str, passed: bool, **extra: Any) -> None:
    checks.append({"name": name, "passed": bool(passed), **extra})


def analyze(fixtures: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    conn = get_conn()
    try:
        cursor = conn.cursor()

        low_id = fixtures.get("M3_LOW_COMPLETENESS_BRAND_ID")
        if low_id:
            row = latest_diagnosis_for_brand(cursor, int(low_id))
            score = row.get("data_completeness_score") if row else None
            add_check(
                checks,
                "low_completeness_brand",
                row is not None and score is not None and int(score) < 60,
                brand_id=low_id,
                expected="latest diagnosis data_completeness_score < 60",
                actual_score=score,
                diagnosis_id=row.get("id") if row else None,
            )

        risk_id = fixtures.get("M3_RISK_COMPLETENESS_BRAND_ID")
        if risk_id:
            row = latest_diagnosis_for_brand(cursor, int(risk_id))
            score = row.get("data_completeness_score") if row else None
            add_check(
                checks,
                "risk_completeness_brand",
                row is not None and score is not None and 60 <= int(score) <= 79,
                brand_id=risk_id,
                expected="latest diagnosis data_completeness_score between 60 and 79",
                actual_score=score,
                diagnosis_id=row.get("id") if row else None,
            )

        diagnosis_id = fixtures.get("M3_DIAGNOSIS_ID")
        if diagnosis_id:
            row = diagnosis_by_id(cursor, int(diagnosis_id))
            expected_brand = (fixtures.get("_M3_DIAGNOSIS_ID_meta") or {}).get("brand_id")
            passed = row is not None and (expected_brand is None or row.get("brand_id") == expected_brand)
            add_check(
                checks,
                "diagnosis_id",
                passed,
                diagnosis_id=diagnosis_id,
                expected_brand_id=expected_brand,
                actual_brand_id=row.get("brand_id") if row else None,
                actual_score=row.get("data_completeness_score") if row else None,
            )

        quote_id = fixtures.get("M3_QUOTE_ID")
        if quote_id:
            row = quote_by_id(cursor, int(quote_id))
            expected_brand = (fixtures.get("_M3_QUOTE_ID_meta") or {}).get("brand_id")
            passed = row is not None and (expected_brand is None or row.get("brand_id") == expected_brand)
            add_check(
                checks,
                "quote_id",
                passed,
                quote_id=quote_id,
                expected_brand_id=expected_brand,
                actual_brand_id=row.get("brand_id") if row else None,
                status=row.get("status") if row else None,
            )
    finally:
        try:
            conn.close()
        except Exception:
            pass

    failures = [check for check in checks if not check["passed"]]
    return {
        "passed": len(failures) == 0,
        "summary": {"checks": len(checks), "failures": len(failures)},
        "checks": checks,
        "failures": failures,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate M3 acceptance fixture IDs against the target DB.")
    parser.add_argument("--fixtures", default=DEFAULT_FIXTURES)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-fail", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        report = analyze(load_fixtures(args.fixtures))
    except Exception as exc:  # noqa: BLE001 - CLI should serialize failures.
        report = {
            "passed": False,
            "error": str(exc),
            "summary": {"checks": 0, "failures": 1},
            "checks": [],
            "failures": [],
        }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=_json_default))
    else:
        print(f"M3 acceptance fixture validation: {'PASS' if report['passed'] else 'FAIL'}")
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2, default=_json_default))
        for failure in report.get("failures", []):
            print(f"FAIL {failure['name']}: {failure}")
    return 0 if report["passed"] or args.no_fail else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
