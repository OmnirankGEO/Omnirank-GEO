"""CLI wrapper for the R6 read-only article-structure analysis."""

from __future__ import annotations

import argparse
import json

from services.article_structure_analysis import analyze_article_structure_patterns


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze article structures from answer-adopted GEO research articles.")
    parser.add_argument("--industry", default="旅游酒店")
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--min-chars", type=int, default=500)
    args = parser.parse_args()
    result = analyze_article_structure_patterns(args.industry, limit=args.limit, min_chars=args.min_chars)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
