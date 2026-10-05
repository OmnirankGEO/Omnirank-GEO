"""Generate a shadow writing strategy candidate from existing style snapshots."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.connection import get_connection  # noqa: E402
from db.writing_style_flywheel_db import init_writing_style_flywheel_tables, upsert_strategy_version  # noqa: E402
from services.media_entity_flywheel import normalize_industry_key  # noqa: E402
from services.writing_strategy_service import build_strategy_candidate  # noqa: E402


def load_inputs(industry_key: str, limit: int = 200) -> tuple[list[dict], list[dict], list[dict]]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT features
              FROM writing_style_feature_snapshots
             WHERE industry_key = %s
             ORDER BY created_at DESC
             LIMIT %s
        """, (industry_key, limit))
        style_features = [
            r["features"] if isinstance(r["features"], dict) else {}
            for r in cur.fetchall()
        ]
        cur.execute("""
            SELECT balanced_weight, signal_tier
              FROM geo_research_source_signals
             WHERE industry_key = %s
             ORDER BY balanced_weight DESC
             LIMIT %s
        """, (industry_key, limit))
        source_signals = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            SELECT publish_status, ai_citations_delta_30d, monitoring_brand_score_delta_30d
              FROM writing_strategy_outcome_events
             WHERE industry_key = %s
             ORDER BY observed_at DESC
             LIMIT %s
        """, (industry_key, limit))
        outcome_signals = [dict(r) for r in cur.fetchall()]
        return style_features, source_signals, outcome_signals
    finally:
        conn.close()


def generate(industry: str, persist: bool = False) -> dict:
    init_writing_style_flywheel_tables()
    industry_key = normalize_industry_key(industry)
    style_features, source_signals, outcome_signals = load_inputs(industry_key)
    candidate = build_strategy_candidate(
        industry_key=industry_key,
        style_features=style_features,
        source_signals=source_signals,
        outcome_signals=outcome_signals,
        operator_note="由 shadow 脚本生成，需管理员审核后才能用于生产",
    )
    row = upsert_strategy_version(candidate) if persist else None
    return {
        "status": "success",
        "industry_key": industry_key,
        "candidate": candidate,
        "persisted": bool(row),
        "row": row,
        "shadow_only": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--industry", default="旅游酒店")
    parser.add_argument("--persist", action="store_true")
    args = parser.parse_args()
    print(json.dumps(generate(args.industry, persist=args.persist), ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
