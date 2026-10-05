"""Step 13 GEO five-case smoke harness.

The default mode is dry-run because the listed GEO POST endpoints may create
diagnosis/quote/report records. Live mode therefore requires an explicit
--allow-geo-write-smoke acknowledgement.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = ROOT / "outputs"


def build_geo_smoke_plan() -> list[dict[str, Any]]:
    return [
        {
            "case_id": "diagnosis_run",
            "method": "POST",
            "path": "/api/diagnosis/run",
            "payload": {"brand_id": "BRD-0094", "smoke": True},
            "expect": {"score_present": True, "engines_min": 4},
            "requires_explicit_live_ack": True,
        },
        {
            "case_id": "quote_generate",
            "method": "POST",
            "path": "/api/quote/generate",
            "payload": {"brand_id": "BRD-0094", "smoke": True},
            "expect": {"price_tiers_min": 3},
            "requires_explicit_live_ack": True,
        },
        {
            "case_id": "keyword_expand",
            "method": "POST",
            "path": "/api/keyword/expand",
            "payload": {"seed": "OmniRank GEO smoke", "limit": 20},
            "expect": {"keywords_min": 15},
            "requires_explicit_live_ack": True,
        },
        {
            "case_id": "industry_research",
            "method": "POST",
            "path": "/api/research/industry",
            "payload": {"industry": "教育咨询行业", "city": "全国", "smoke": True},
            "expect": {"sources_min": 5},
            "requires_explicit_live_ack": True,
        },
        {
            "case_id": "content_report",
            "method": "POST",
            "path": "/api/content/generate-report",
            "payload": {"brand_id": "BRD-0094", "article_count": 1, "smoke": True},
            "expect": {"article_count_min": 1, "total_score_present": True},
            "requires_explicit_live_ack": True,
        },
    ]


def validate_geo_smoke_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "total_cases": len(results),
        "passed_cases": sum(1 for item in results if item.get("ok")),
        "all_ok": len(results) == 5 and all(item.get("ok") for item in results),
        "results": results,
    }


def run_dry_geo_smoke() -> dict[str, Any]:
    plan = build_geo_smoke_plan()
    results = [
        {
            "case_id": item["case_id"],
            "method": item["method"],
            "path": item["path"],
            "dry_run": True,
            "ok": True,
            "note": "not executed; live GEO smoke requires --allow-geo-write-smoke",
        }
        for item in plan
    ]
    return {"mode": "dry_run_contract", "plan": plan, **validate_geo_smoke_results(results)}


async def run_live_geo_smoke(*, base_url: str, token: str, allow_geo_write_smoke: bool) -> dict[str, Any]:
    if not allow_geo_write_smoke:
        raise ValueError("live GEO smoke requires --allow-geo-write-smoke")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=180.0) as client:
        for case in build_geo_smoke_plan():
            started = time.perf_counter()
            status_code = 0
            body: dict[str, Any] | list[Any] | None = None
            error = None
            try:
                resp = await client.request(case["method"], f"{base_url.rstrip('/')}{case['path']}", headers=headers, json=case["payload"])
                status_code = resp.status_code
                try:
                    body = resp.json()
                except json.JSONDecodeError:
                    body = {"raw": resp.text[:500]}
            except Exception as exc:  # pragma: no cover - staging-only path
                error = f"{type(exc).__name__}:{exc}"
            ok = status_code < 500 and error is None and _matches_expectation(case["expect"], body)
            results.append(
                {
                    "case_id": case["case_id"],
                    "status_code": status_code,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "ok": ok,
                    "error": error,
                }
            )
    return {"mode": "live_staging", **validate_geo_smoke_results(results)}


def _matches_expectation(expect: dict[str, Any], body: dict[str, Any] | list[Any] | None) -> bool:
    if body is None:
        return False
    text = json.dumps(body, ensure_ascii=False, default=str)
    if expect.get("score_present") and "score" not in text:
        return False
    if expect.get("total_score_present") and "total_score" not in text and "score" not in text:
        return False
    if "engines_min" in expect and _count_collection_like(body, ("engines", "engine_results", "results")) < int(expect["engines_min"]):
        return False
    if "price_tiers_min" in expect and _count_collection_like(body, ("tiers", "price_tiers", "plans")) < int(expect["price_tiers_min"]):
        return False
    if "keywords_min" in expect and _count_collection_like(body, ("keywords", "items", "results")) < int(expect["keywords_min"]):
        return False
    if "sources_min" in expect and _count_collection_like(body, ("sources", "citations", "results")) < int(expect["sources_min"]):
        return False
    if "article_count_min" in expect and _count_collection_like(body, ("articles", "items", "reports")) < int(expect["article_count_min"]):
        return False
    return True


def _count_collection_like(body: dict[str, Any] | list[Any], keys: tuple[str, ...]) -> int:
    if isinstance(body, list):
        return len(body)
    for key in keys:
        value = body.get(key)
        if isinstance(value, list):
            return len(value)
        if isinstance(value, dict):
            return len(value)
    for value in body.values():
        if isinstance(value, dict):
            count = _count_collection_like(value, keys)
            if count:
                return count
    return 0


def write_report(report: dict[str, Any], out_dir: Path | str = DEFAULT_OUT_DIR) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"geo_5_smoke_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Step 13 GEO five-case smoke checks.")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--token", default="")
    parser.add_argument("--allow-geo-write-smoke", action="store_true")
    parser.add_argument("--write-report", action="store_true")
    args = parser.parse_args()
    if args.live:
        if not args.base_url or not args.token:
            raise SystemExit("--live requires --base-url and --token")
        import asyncio

        report = asyncio.run(
            run_live_geo_smoke(base_url=args.base_url, token=args.token, allow_geo_write_smoke=args.allow_geo_write_smoke)
        )
    else:
        report = run_dry_geo_smoke()
    if args.write_report:
        report = {**report, "report_path": str(write_report(report))}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("all_ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
