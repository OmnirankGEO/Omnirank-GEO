"""Gold-standard calibration for GEO article/outcome judges."""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import math
from typing import Any, Final

from psycopg2.extras import Json


CALIBRATION_VERSION: Final = "geo-article-expert-calibration-v1.0"
ARTICLE_LABELS: Final = frozenset({"approved", "pending_human_review", "rewrite_required", "blocked"})
OUTCOME_LABELS: Final = frozenset({
    "recommended", "conditionally_recommended", "candidate_only", "mentioned_only",
    "criteria_only", "refused_no_evidence", "refused_risk", "not_mentioned",
    "entity_ambiguous", "engine_error",
})


def _wilson_upper(errors: int, total: int, z: float = 1.96) -> float | None:
    if total <= 0:
        return None
    p = errors / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return min(1.0, centre + margin)


def add_gold_label(
    *,
    judge_kind: str,
    source_id: int,
    human_label: str,
    reviewer_user_id: int,
    rationale: str,
) -> dict[str, Any]:
    labels = ARTICLE_LABELS if judge_kind == "article_review" else OUTCOME_LABELS if judge_kind == "target_outcome" else None
    if labels is None or human_label not in labels:
        raise ValueError("invalid_gold_label")
    if len(str(rationale or "").strip()) < 5:
        raise ValueError("gold_rationale_required")
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        if judge_kind == "article_review":
            cur.execute(
                "SELECT article_review_status AS machine_label, article_review AS snapshot "
                "FROM articles WHERE id=%s",
                (source_id,),
            )
        else:
            from services.monitoring_identity_review import aggregate_eligible_sql
            cur.execute(
                f"""
                SELECT target_outcome AS machine_label,
                       jsonb_build_object(
                           'provider',provider,'model',model,'surface',surface,
                           'question',sent_question_snapshot,'lineage_status',lineage_status
                       ) AS snapshot
                  FROM monitoring_results
                 WHERE id=%s AND {aggregate_eligible_sql()}
                """,
                (source_id,),
            )
        source = cur.fetchone()
        if not source:
            raise ValueError("gold_source_not_found")
        cur.execute(
            """
            INSERT INTO geo_article_gold_labels (
                judge_kind, source_id, machine_label, human_label,
                input_snapshot, reviewer_user_id, rationale, calibration_version
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (judge_kind, source_id, reviewer_user_id)
            DO UPDATE SET human_label=EXCLUDED.human_label,
                          rationale=EXCLUDED.rationale,
                          input_snapshot=EXCLUDED.input_snapshot,
                          machine_label=EXCLUDED.machine_label,
                          created_at=NOW()
            RETURNING *
            """,
            (
                judge_kind, source_id, source.get("machine_label") or "unknown",
                human_label, Json(source.get("snapshot") or {}), reviewer_user_id,
                rationale.strip(), CALIBRATION_VERSION,
            ),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def evaluate_gold_standard(judge_kind: str) -> dict[str, Any]:
    """Require two-reviewer consensus and enough high-risk negatives for PASS."""
    if judge_kind not in {"article_review", "target_outcome"}:
        raise ValueError("invalid_judge_kind")
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT source_id, machine_label, human_label, reviewer_user_id, input_snapshot
              FROM geo_article_gold_labels
             WHERE judge_kind=%s AND calibration_version=%s
             ORDER BY source_id, reviewer_user_id
            """,
            (judge_kind, CALIBRATION_VERSION),
        )
        raw = [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()

    grouped: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in raw:
        grouped[int(row["source_id"])].append(row)
    consensus: list[tuple[str, str]] = []
    disagreements = 0
    for rows in grouped.values():
        if len({row["reviewer_user_id"] for row in rows}) < 2:
            continue
        snapshot_ids = {
            hashlib.sha256(
                json.dumps(row.get("input_snapshot") or {}, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            for row in rows
        }
        if len(snapshot_ids) != 1:
            disagreements += 1
            continue
        counts = Counter(str(row["human_label"]) for row in rows)
        label, votes = counts.most_common(1)[0]
        if votes < 2 or len([n for n in counts.values() if n == votes]) > 1:
            disagreements += 1
            continue
        consensus.append((str(rows[-1]["machine_label"]), label))

    labels = sorted(ARTICLE_LABELS if judge_kind == "article_review" else OUTCOME_LABELS)
    f1_by_label: dict[str, float | None] = {}
    for label in labels:
        tp = sum(1 for machine, human in consensus if machine == label and human == label)
        fp = sum(1 for machine, human in consensus if machine == label and human != label)
        fn = sum(1 for machine, human in consensus if machine != label and human == label)
        precision = tp / (tp + fp) if tp + fp else None
        recall = tp / (tp + fn) if tp + fn else None
        f1_by_label[label] = (
            2 * precision * recall / (precision + recall)
            if precision is not None and recall is not None and precision + recall else None
        )
    observed_f1 = [value for value in f1_by_label.values() if value is not None]
    macro_f1 = sum(observed_f1) / len(observed_f1) if observed_f1 else None

    if judge_kind == "article_review":
        high_risk_human = {"blocked", "rewrite_required"}
        unsafe_machine = {"approved"}
    else:
        high_risk_human = {"refused_no_evidence", "refused_risk", "engine_error"}
        unsafe_machine = {"recommended", "conditionally_recommended", "candidate_only"}
    high_risk_rows = [(m, h) for m, h in consensus if h in high_risk_human]
    high_risk_false_accepts = sum(1 for m, _h in high_risk_rows if m in unsafe_machine)
    upper = _wilson_upper(high_risk_false_accepts, len(high_risk_rows))
    represented_labels = {human for _machine, human in consensus}
    missing_labels = sorted(set(labels) - represented_labels)

    if len(consensus) < 100 or len(high_risk_rows) < 189 or missing_labels:
        decision = "INSUFFICIENT_SAMPLES"
    elif macro_f1 is None or macro_f1 < 0.90 or high_risk_false_accepts > 0 or (upper or 1) > 0.02:
        decision = "FAIL"
    else:
        decision = "PASS"
    return {
        "calibration_version": CALIBRATION_VERSION,
        "judge_kind": judge_kind,
        "decision": decision,
        "consensus_samples": len(consensus),
        "unresolved_disagreements": disagreements,
        "macro_f1": macro_f1,
        "f1_by_label": f1_by_label,
        "missing_human_labels": missing_labels,
        "high_risk_negative_samples": len(high_risk_rows),
        "high_risk_false_accepts": high_risk_false_accepts,
        "high_risk_false_accept_wilson_95_upper": upper,
        "gates": {
            "minimum_consensus_samples": 100,
            "minimum_high_risk_negatives": 189,
            "macro_f1_minimum": 0.90,
            "high_risk_false_accepts_required": 0,
            "high_risk_false_accept_upper_maximum": 0.02,
        },
    }
