"""Dry-run GEO media entity flywheel without writing production recommendation.

Usage:
  python scripts/dry_run_geo_media_flywheel.py --industry "旅游酒店"
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.media_entity_flywheel import (  # noqa: E402
    build_media_entity_seed,
    compute_media_entity_shadow_score,
    match_inventory_to_entity,
    normalize_industry_key,
)


SAMPLE_SEEDS = [
    {"name": "携程", "domain": "ctrip.com", "industry": "旅游酒店", "aliases": ["携程旅行", "Trip.com"]},
    {"name": "马蜂窝", "domain": "mafengwo.cn", "industry": "旅游酒店", "aliases": ["马蜂窝旅游"]},
    {"name": "去哪儿", "domain": "qunar.com", "industry": "旅游酒店", "aliases": ["去哪儿旅行"]},
]

SAMPLE_INVENTORY = [
    {"id": 101, "media_source": "media", "media_name": "携程旅行攻略频道", "domain": "ctrip.com", "price_yuan": 180, "is_active": True},
    {"id": 102, "media_source": "media", "media_name": "马蜂窝旅游频道", "domain": "mafengwo.cn", "price_yuan": 120, "is_active": True},
    {"id": 103, "media_source": "media", "media_name": "低价套餐随机发布", "price_yuan": 9, "is_active": True},
]


def run(industry: str) -> dict:
    industry_key = normalize_industry_key(industry)
    items = []
    for idx, seed in enumerate(SAMPLE_SEEDS, start=1):
        entity = build_media_entity_seed({**seed, "industry": industry})
        matches = match_inventory_to_entity(entity, SAMPLE_INVENTORY)
        score = compute_media_entity_shadow_score(
            entity=entity,
            citation_rollup={
                "answer_adopted_count": max(0, 5 - idx),
                "cited_count": 8 - idx,
                "prompt_count": 10,
                "engine_count": 3,
            },
            inventory_matches=matches,
            outcome_rollup={"published_count": idx, "citation_lift_30d": 2},
        )
        items.append({"entity": entity, "matches": matches, "score": score})
    return {
        "status": "success",
        "mode": "dry_run",
        "industry_key": industry_key,
        "shadow_only": True,
        "items": items,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--industry", default="旅游酒店")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    result = run(args.industry)
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
