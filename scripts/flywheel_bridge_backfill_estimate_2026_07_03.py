"""READ-ONLY dry-run estimate for the flywheel downstream backfill (2026-07-03).

Deliverable #2 of the 「GEO 数据飞轮 · 下游桥接链常态化」 fix package.  This
NEVER writes to the database.  It only counts what the shadow/candidate backfill
WOULD produce, so the boss can approve the cost (esp. the LLM cost of answer-entity
extraction) before Deploy-CTO runs any real backfill.

Outputs five estimates (修复指令 §六 / 补充指令 §1-§4):
  1. source_signals backfill   — cited raw rows behind the last shadow signal.
  2. answer_adoption backfill   — Doubao/Kimi answer-adoption marks (determinable
                                  via in-answer [n] markers; cite_url alone ≠ adoption).
  3. answer_facts/_entities     — pending answer count × LLM call/cost  (THE big one).
  4. binding_candidates         — current candidates + revivable deleted rows.
  5. strategy_candidate         — industries with signals → shadow strategy candidates.

Cost uses the live PRICING_TABLE SSOT (tools/llm_call_tracker) so it never drifts
from the real per-token price.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.connection import get_connection  # noqa: E402
from tools.llm_call_tracker import PRICING_TABLE  # noqa: E402

# Answer-entity extraction runs on DashScope qwen3.7-max (extractor DEFAULT_MODEL).
_LLM_PRICING_KEY = ("dashscope", "qwen3.7-max")
# Conservative per-call token assumptions (overridable via CLI). Answer is truncated
# to 8000 chars (~4000 tokens) in the extractor; output capped at max_tokens=4000.
_DEFAULT_INPUT_TOKENS = 3000
_DEFAULT_OUTPUT_TOKENS = 800


def _scalar(sql: str, params: tuple = ()) -> Any:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        return list(row.values())[0] if row else None
    except Exception as exc:
        return f"__error__: {exc}"
    finally:
        conn.close()


def _rows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def estimate_source_signals() -> dict[str, Any]:
    last_signal_at = _scalar("SELECT MAX(observed_at) AS v FROM geo_research_source_signals")
    # cited raw rows newer than the last shadow signal = the gap that a bridge would fill.
    if isinstance(last_signal_at, str) or last_signal_at is None:
        gap_rows = _rows(
            """
            SELECT engine, COUNT(*) AS c
              FROM geo_research_raw
             WHERE COALESCE(cite_url, '') <> ''
             GROUP BY engine ORDER BY c DESC
            """
        )
        cutoff = None
    else:
        gap_rows = _rows(
            """
            SELECT engine, COUNT(*) AS c
              FROM geo_research_raw
             WHERE COALESCE(cite_url, '') <> ''
               AND created_at > %s
             GROUP BY engine ORDER BY c DESC
            """,
            (last_signal_at,),
        )
        cutoff = str(last_signal_at)
    total = sum(int(r["c"]) for r in gap_rows)
    return {
        "last_source_signal_at": str(last_signal_at) if last_signal_at else None,
        "cited_raw_rows_in_gap": total,
        "by_engine": {r["engine"]: int(r["c"]) for r in gap_rows},
        "estimated_new_source_signals_upper_bound": total,
        "note": "upper bound; unique-key dedup (source_url,industry,engine,prompt,tier,round) reduces actual writes. "
                f"gap cutoff = {cutoff or 'ALL history (no prior signals)'}",
    }


def estimate_answer_adoption(days: int) -> dict[str, Any]:
    # Reuse the P0-2 backfill dry-run so the determinable-vs-undeterminable split is
    # computed with the exact in-answer [n]-marker rule (never cite_url alone).
    try:
        from scripts.backfill_doubao_kimi_answer_adoption_2026_07_03 import backfill
        return backfill(days=days, limit=0, dry_run=True)
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def estimate_answer_entities(input_tokens: int, output_tokens: int) -> dict[str, Any]:
    try:
        from db.research_answer_entity_db import count_extractable_answers
        counts = count_extractable_answers(industry_values=None, limit=200)
    except Exception as exc:
        return {"status": "error", "error": str(exc)}

    pending = int(counts.get("pending_extract") or 0)
    total_answers = int(counts.get("total_answers") or 0)
    pricing = PRICING_TABLE.get(_LLM_PRICING_KEY, {"input": 0.006, "output": 0.018})
    per_call_cny = round(
        (input_tokens / 1000.0) * pricing["input"] + (output_tokens / 1000.0) * pricing["output"], 6
    )
    return {
        "total_answers": total_answers,
        "pending_extract": pending,
        "llm_model": "qwen3.7-max (dashscope)",
        "llm_calls_nominal": pending,
        "llm_calls_worst_case_retry_x3": pending * 3,
        "assumed_input_tokens_per_call": input_tokens,
        "assumed_output_tokens_per_call": output_tokens,
        "pricing_cny_per_1k": pricing,
        "cost_per_call_cny": per_call_cny,
        "estimated_cost_cny_nominal": round(per_call_cny * pending, 2),
        "estimated_cost_cny_worst_case": round(per_call_cny * pending * 3, 2),
        "note": "answer-entity extraction is the ONLY LLM cost. Gated OFF by flag "
                "flywheel_answer_entity_auto + per-round/daily caps. No spend until authorized.",
    }


def estimate_binding_candidates() -> dict[str, Any]:
    total = _scalar("SELECT COUNT(*) AS v FROM geo_media_binding_candidates")
    by_status = _rows(
        "SELECT status, COUNT(*) AS c FROM geo_media_binding_candidates GROUP BY status ORDER BY c DESC"
    )
    revivable_deleted = _scalar(
        "SELECT COUNT(*) AS v FROM geo_media_binding_candidates WHERE status = 'deleted'"
    )
    revivable_high_conf = _scalar(
        """
        SELECT COUNT(*) AS v FROM geo_media_binding_candidates
         WHERE status = 'deleted' AND (can_approve = TRUE OR match_confidence >= 0.9)
        """
    )
    shadow_entities = _scalar("SELECT COUNT(*) AS v FROM geo_media_entities")
    return {
        "current_total_candidates": total,
        "by_status": {r["status"]: int(r["c"]) for r in by_status},
        "revivable_deleted_candidates": revivable_deleted,
        "revivable_high_confidence": revivable_high_conf,
        "shadow_media_entities": shadow_entities,
        "note": "actual new candidates depend on the media-entity rebuild; deleted rows are revived "
                "only when re-match still passes (rejected/approved are never revived). "
                "Revival resets review fields; still requires human approval to enter mapping.",
    }


def estimate_strategy_candidates() -> dict[str, Any]:
    industries = _scalar(
        "SELECT COUNT(DISTINCT industry_key) AS v FROM geo_research_source_signals"
    )
    style_snapshots = _scalar("SELECT COUNT(*) AS v FROM writing_style_feature_snapshots")
    return {
        "distinct_industries_with_signals": industries,
        "style_feature_snapshots": style_snapshots,
        "estimated_shadow_strategy_candidates": industries,
        "note": "one shadow/pending_review strategy version per industry per run; never auto-activated.",
    }


def build_estimate(days: int, input_tokens: int, output_tokens: int) -> dict[str, Any]:
    return {
        "status": "success",
        "dry_run": True,
        "writes": "NONE (read-only estimate)",
        "1_source_signals": estimate_source_signals(),
        "2_answer_adoption": estimate_answer_adoption(days),
        "3_answer_facts_entities_LLM": estimate_answer_entities(input_tokens, output_tokens),
        "4_binding_candidates": estimate_binding_candidates(),
        "5_strategy_candidates": estimate_strategy_candidates(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=20, help="answer-adoption lookback window (days)")
    parser.add_argument("--input-tokens", type=int, default=_DEFAULT_INPUT_TOKENS,
                        help="assumed LLM input tokens per answer-entity extraction call")
    parser.add_argument("--output-tokens", type=int, default=_DEFAULT_OUTPUT_TOKENS,
                        help="assumed LLM output tokens per answer-entity extraction call")
    args = parser.parse_args()
    result = build_estimate(args.days, args.input_tokens, args.output_tokens)
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
