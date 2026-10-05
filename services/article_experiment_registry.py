"""Pre-registered article-style experiments and conservative evaluation.

Assignments are reserved on an ungenerated topic, then bound to the exact
article generation request.  This prevents a candidate prompt from being
activated globally merely to create its experimental arm.  No result
auto-activates a prompt or edits customer monitoring questions.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Any, Final
from uuid import uuid4

from psycopg2.extras import Json

from services.strict_article_outcomes import normalize_publication_url, parse_citation_urls
from writing.article_style_contract import STYLE_FAMILIES
from writing.title_element_contract import TITLE_YEAR_EXPERIMENT_DIMENSION


EXPERIMENT_CONTRACT_VERSION: Final = "geo-article-experiment-v1.0"
ALLOWED_DIMENSIONS: Final = frozenset({
    "answer_order", "comparison_fields", "evidence_density", "table_shape",
    "section_structure", "citation_placement", "action_checklist",
    # [P2 标题年份合同 2026-08-08] 标题要素默认(年份/地区/行业对象/榜单词/数字)。
    # 原七维全是**正文**结构维,没有一个能装标题侧的单变量改动 —— 缺这一维就只能把
    # 标题实验挂到语义不符的维上,那等于把实验记录写脏。
    # 🔴 这里**导入**合同里的常量而不是手敲同名字符串:两边靠命名巧合对齐的话,
    # 改了合同侧的值这边不会跟着变(子代理复审当场抓到的漂移口子)。
    TITLE_YEAR_EXPERIMENT_DIMENSION,
})
ALLOWED_STATES: Final = frozenset({
    "preregistered", "approved", "canary", "observing", "passed", "failed",
    "insufficient_samples", "cancelled",
})
ALLOWED_SCOPE_KEYS: Final = frozenset({
    "brand_ids", "industries", "question_families", "providers", "models", "surfaces",
})


def _normalize_scope(scope: dict[str, Any] | None) -> dict[str, list[Any]]:
    raw = dict(scope or {})
    unknown = sorted(set(raw) - ALLOWED_SCOPE_KEYS)
    if unknown:
        raise ValueError(f"unknown_experiment_scope_keys:{','.join(unknown)}")
    normalized: dict[str, list[Any]] = {}
    for key, value in raw.items():
        if not isinstance(value, list):
            raise ValueError(f"experiment_scope_{key}_must_be_array")
        if key == "brand_ids":
            normalized[key] = sorted({int(item) for item in value})
        else:
            normalized[key] = sorted({str(item).strip() for item in value if str(item).strip()})
    return normalized


def _deterministic_arm(experiment_key: str, topic_id: int) -> str:
    digest = hashlib.sha256(f"{experiment_key}|topic:{int(topic_id)}".encode("utf-8")).digest()
    return "candidate" if digest[0] & 1 else "control"


def _scope_allows(scope: dict[str, Any], key: str, value: Any) -> bool:
    allowed = scope.get(key) or []
    return not allowed or value in allowed


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _wilson(successes: int, total: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


def create_experiment(
    *,
    style_family: str,
    hypothesis: str,
    single_change_dimension: str,
    baseline_version_id: str,
    candidate_version_id: str,
    scope: dict[str, Any],
    created_by: int,
    min_arm_articles: int = 30,
    minimum_weeks: int = 4,
) -> dict[str, Any]:
    if style_family not in STYLE_FAMILIES:
        raise ValueError("unknown_style_family")
    if single_change_dimension not in ALLOWED_DIMENSIONS:
        raise ValueError("invalid_single_change_dimension")
    if len(str(hypothesis or "").strip()) < 10:
        raise ValueError("hypothesis_required")
    if not baseline_version_id or not candidate_version_id or baseline_version_id == candidate_version_id:
        raise ValueError("two_distinct_style_versions_required")
    from writing.article_style_contract import family_for_style
    from writing.style_control import get_style_version

    baseline_version = get_style_version(baseline_version_id)
    candidate_version = get_style_version(candidate_version_id)
    if not baseline_version or not candidate_version:
        raise ValueError("style_version_not_found")
    if baseline_version.get("style_code") != candidate_version.get("style_code"):
        raise ValueError("experiment_versions_must_share_style_code")
    if family_for_style(str(baseline_version.get("style_code") or "")) != style_family:
        raise ValueError("style_family_does_not_match_versions")
    if candidate_version.get("status") in {"blocked", "retired"}:
        raise ValueError("candidate_version_not_experiment_eligible")
    if not (10 <= int(min_arm_articles) <= 10000):
        raise ValueError("min_arm_articles_out_of_range")
    if not (1 <= int(minimum_weeks) <= 52):
        raise ValueError("minimum_weeks_out_of_range")
    scope = _normalize_scope(scope)
    key = f"AEXP-{uuid4().hex[:16]}"
    frozen = {
        "contract_version": EXPERIMENT_CONTRACT_VERSION,
        "style_family": style_family,
        "hypothesis": hypothesis.strip(),
        "single_change_dimension": single_change_dimension,
        "baseline_version_id": baseline_version_id,
        "candidate_version_id": candidate_version_id,
        "scope": scope,
        "primary_metric": "same_brand_post_publish_exact_url_article_citation_rate",
        "url_match": "exact_normalized",
        "assignment_unit": "article_before_first_publication",
        "assignment_moment": "topic_before_generation",
        "style_code": baseline_version.get("style_code"),
    }
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO geo_article_experiments (
                experiment_key, contract_version, style_family, hypothesis,
                single_change_dimension, primary_metric, baseline_version_id,
                candidate_version_id, scope, min_arm_articles, minimum_weeks,
                frozen_config, state, created_by
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'preregistered',%s)
            RETURNING *
            """,
            (
                key, EXPERIMENT_CONTRACT_VERSION, style_family, hypothesis.strip(),
                single_change_dimension, frozen["primary_metric"], baseline_version_id,
                candidate_version_id, Json(scope), min_arm_articles, minimum_weeks,
                Json(frozen), created_by,
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


def approve_experiment(experiment_id: int, *, actor_user_id: int, reason: str) -> dict[str, Any]:
    if len(str(reason or "").strip()) < 5:
        raise ValueError("approval_reason_required")
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE geo_article_experiments
               SET state='approved', approved_by=%s, approval_reason=%s,
                   approved_at=NOW(), updated_at=NOW()
             WHERE id=%s AND state='preregistered'
            RETURNING *
            """,
            (actor_user_id, reason.strip(), experiment_id),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError("experiment_not_preregistered")
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def assign_article(
    experiment_id: int,
    *,
    article_id: int,
    arm: str,
    actor_user_id: int,
) -> dict[str, Any]:
    if arm not in {"control", "candidate"}:
        raise ValueError("invalid_experiment_arm")
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM geo_article_experiments WHERE id=%s FOR UPDATE", (experiment_id,))
        experiment = cur.fetchone()
        if not experiment:
            raise ValueError("experiment_not_found")
        if experiment.get("state") not in {"approved", "canary", "observing"}:
            raise ValueError("experiment_not_assignable")
        cur.execute(
            """
            SELECT id, topic_id, generation_request_id, style_family, style_version,
                   publication_snapshot_at
              FROM articles WHERE id=%s FOR UPDATE
            """,
            (article_id,),
        )
        article = cur.fetchone()
        if not article:
            raise ValueError("article_not_found")
        if article.get("topic_id") is None:
            raise ValueError("experiment_article_requires_topic")
        if article.get("publication_snapshot_at"):
            raise ValueError("assignment_must_precede_first_publication")
        if article.get("style_family") != experiment.get("style_family"):
            raise ValueError("style_family_mismatch")
        expected_arm = _deterministic_arm(
            str(experiment["experiment_key"]), int(article["topic_id"])
        )
        if arm != expected_arm:
            raise ValueError("deterministic_arm_mismatch")
        expected_version = (
            experiment.get("baseline_version_id") if arm == "control"
            else experiment.get("candidate_version_id")
        )
        if str(article.get("style_version") or "") != str(expected_version or ""):
            raise ValueError("article_style_version_does_not_match_arm")
        request_id = str(article.get("generation_request_id") or "")
        if not request_id:
            raise ValueError("generation_request_id_required")
        digest = hashlib.sha256(
            f"{experiment['experiment_key']}|{article_id}|{request_id}|{arm}".encode("utf-8")
        ).hexdigest()
        cur.execute(
            """
            SELECT * FROM geo_article_experiment_assignments
             WHERE experiment_id=%s AND topic_id=%s FOR UPDATE
            """,
            (experiment_id, article.get("topic_id")),
        )
        reservation = cur.fetchone()
        if reservation:
            if reservation.get("arm") != arm:
                raise ValueError("reserved_arm_mismatch")
            cur.execute(
                """
                UPDATE geo_article_experiment_assignments
                   SET article_id=%s, generation_request_id=%s,
                       article_style_version=%s, assignment_hash=%s
                 WHERE id=%s AND article_id IS NULL
                RETURNING *
                """,
                (article_id, request_id, expected_version, digest, reservation["id"]),
            )
        else:
            raise ValueError("experiment_topic_must_be_reserved_before_generation")
        row = cur.fetchone()
        cur.execute(
            """
            UPDATE geo_article_experiments
               SET state=CASE WHEN state='approved' THEN 'canary' ELSE state END,
                   started_at=COALESCE(started_at,NOW()), updated_at=NOW()
             WHERE id=%s
            """,
            (experiment_id,),
        )
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reserve_topic(
    experiment_id: int,
    *,
    topic_id: int,
    arm: str,
    actor_user_id: int,
) -> dict[str, Any]:
    """Freeze an experiment arm before generation; never changes the live prompt."""
    if arm not in {"control", "candidate"}:
        raise ValueError("invalid_experiment_arm")
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM geo_article_experiments WHERE id=%s FOR UPDATE", (experiment_id,))
        experiment = cur.fetchone()
        if not experiment:
            raise ValueError("experiment_not_found")
        if experiment.get("state") not in {"approved", "canary", "observing"}:
            raise ValueError("experiment_not_assignable")
        cur.execute(
            """SELECT t.id, t.article_id, q.brand_id, q.industry
                 FROM topics t
                 LEFT JOIN quotes q ON q.id=t.quote_id
                WHERE t.id=%s FOR UPDATE OF t""",
            (topic_id,),
        )
        topic = cur.fetchone()
        if not topic:
            raise ValueError("topic_not_found")
        if topic.get("article_id") is not None:
            raise ValueError("topic_already_has_article")
        scope = _normalize_scope(experiment.get("scope") or {})
        if not _scope_allows(scope, "brand_ids", topic.get("brand_id")):
            raise ValueError("topic_outside_experiment_brand_scope")
        if not _scope_allows(scope, "industries", str(topic.get("industry") or "").strip()):
            raise ValueError("topic_outside_experiment_industry_scope")
        expected_arm = _deterministic_arm(str(experiment["experiment_key"]), topic_id)
        if arm != expected_arm:
            raise ValueError("deterministic_arm_mismatch")
        expected_version = (
            experiment.get("baseline_version_id") if arm == "control"
            else experiment.get("candidate_version_id")
        )
        digest = hashlib.sha256(
            f"{experiment['experiment_key']}|topic:{topic_id}|{arm}|{expected_version}".encode("utf-8")
        ).hexdigest()
        cur.execute(
            """
            INSERT INTO geo_article_experiment_assignments (
                experiment_id, article_id, topic_id, generation_request_id, arm,
                article_style_version, assignment_hash, assigned_by
            ) VALUES (%s,NULL,%s,NULL,%s,%s,%s,%s)
            RETURNING *
            """,
            (experiment_id, topic_id, arm, expected_version, digest, actor_user_id),
        )
        row = cur.fetchone()
        cur.execute(
            """
            UPDATE geo_article_experiments
               SET state=CASE WHEN state='approved' THEN 'canary' ELSE state END,
                   started_at=COALESCE(started_at,NOW()), updated_at=NOW()
             WHERE id=%s
            """,
            (experiment_id,),
        )
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def load_generation_assignment(topic_id: int, style_code: str) -> dict[str, Any] | None:
    """Load the frozen prompt for one reserved topic without mutating live style state."""
    from db.connection import get_connection
    from writing.article_style_contract import family_for_style
    from writing.style_control import get_style_version

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT x.*, e.experiment_key, e.style_family, e.state
              FROM geo_article_experiment_assignments x
              JOIN geo_article_experiments e ON e.id=x.experiment_id
             WHERE x.topic_id=%s AND x.article_id IS NULL
               AND e.state IN ('canary','observing')
             ORDER BY x.assigned_at DESC LIMIT 1
            """,
            (topic_id,),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    if family_for_style(style_code) != row.get("style_family"):
        raise ValueError("reserved_experiment_style_family_mismatch")
    version = get_style_version(str(row.get("article_style_version") or ""))
    if not version or version.get("style_code") != style_code:
        raise ValueError("reserved_experiment_style_version_mismatch")
    return {
        "assignment_id": int(row["id"]),
        "experiment_id": int(row["experiment_id"]),
        "experiment_key": row.get("experiment_key"),
        "arm": row.get("arm"),
        "style_version_id": version.get("version_id"),
        "prompt_text": version.get("prompt_text"),
    }


def bind_generation_assignment(
    assignment_id: int,
    *,
    article_id: int,
    generation_request_id: str,
) -> dict[str, Any]:
    """Bind one pre-generation reservation to the exact stored article."""
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE geo_article_experiment_assignments
               SET article_id=%s, generation_request_id=%s
             WHERE id=%s AND article_id IS NULL
            RETURNING *
            """,
            (article_id, generation_request_id, assignment_id),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError("experiment_assignment_not_bindable")
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _load_experiment_facts(cur, experiment_id: int) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    cur.execute("SELECT * FROM geo_article_experiments WHERE id=%s", (experiment_id,))
    experiment = cur.fetchone()
    if not experiment:
        raise ValueError("experiment_not_found")
    cur.execute(
        """
        WITH publication_facts AS (
            SELECT o.article_id, i.publish_url, i.published_at, i.status,
                   i.submitted_content_snapshot_hash AS body_snapshot_hash,
                   'channel_submission_snapshot'::text AS body_observation_level
              FROM mhz_publish_order_items i JOIN mhz_publish_orders o ON o.id=i.order_id
             WHERE i.status='published'
               AND i.submitted_content_snapshot_at IS NOT NULL
            UNION ALL
            SELECT o.article_id, i.publish_url, i.published_at, i.status,
                    CASE WHEN a.publication_snapshot_source='publish_order_items'
                              AND a.publication_snapshot_source_id=i.id
                        THEN a.publication_snapshot->>'content_hash' END,
                   'submission_snapshot'::text
              FROM publish_order_items i JOIN publish_orders o ON o.id=i.order_id
              JOIN articles a ON a.id=o.article_id
             WHERE i.status='published'
            UNION ALL
            SELECT p.article_id, p.public_url, p.created_at, 'published'::text,
                   p.submitted_content_snapshot_hash,
                   'extension_channel_submission_snapshot'::text
              FROM publish_records p
             -- [WO 自报收口 2026-08-19] 实验结论不许建立在被测方自报的发布事实上。
             WHERE p.article_id IS NOT NULL
               AND p.status='success'
               AND p.public_url_verification_state = 'verified'
               AND NULLIF(BTRIM(p.public_url), '') IS NOT NULL
        ), earliest AS (
            SELECT DISTINCT ON (article_id) article_id, publish_url, published_at, status,
                   body_snapshot_hash, body_observation_level
              FROM publication_facts
             WHERE published_at IS NOT NULL AND published_at <= CURRENT_TIMESTAMP
               AND body_snapshot_hash IS NOT NULL
             ORDER BY article_id, published_at ASC
        )
        SELECT x.id AS assignment_id, x.article_id, x.arm, x.article_style_version,
               x.assigned_at, e.body_snapshot_hash AS publication_snapshot_hash, a.style_family,
               a.style_code,
               a.article_review_status, a.article_human_review_status,
               a.article_review, a.evidence_manifest_hash,
               e.body_observation_level,
               q.brand_id, q.industry, e.publish_url, e.published_at, e.status
          FROM geo_article_experiment_assignments x
          JOIN articles a ON a.id=x.article_id
          LEFT JOIN quotes q ON q.id=a.quote_id
          LEFT JOIN earliest e ON e.article_id=a.id
         WHERE x.experiment_id=%s
        """,
        (experiment_id,),
    )
    assignments = [dict(row) for row in cur.fetchall()]
    brand_ids = sorted({int(row["brand_id"]) for row in assignments if row.get("brand_id") is not None})
    if not brand_ids:
        return dict(experiment), assignments, []
    from services.monitoring_identity_review import aggregate_eligible_sql
    # 🔴 [工单 V3-C · C-3] 「血缘如实记录过」取 SSOT,不写死 'complete' ——
    #    C-3 之后计划值行是 model_unconfirmed,写死会让实验样本静默归零。
    from services.monitoring_lineage import recorded_lineage_sql

    _recorded = recorded_lineage_sql("mr")
    cur.execute(
        f"""
        SELECT mr.id, mr.tested_at, mr.search_citations, mr.sent_question_snapshot,
               mr.question_family, mr.provider, mr.model, mr.surface,
               mr.lineage_status, mt.brand_id
          FROM monitoring_results mr
          JOIN monitoring_tasks mt ON mt.id=mr.task_id
         WHERE mt.brand_id = ANY(%s)
           AND {aggregate_eligible_sql('mr')}
           AND mr.tested_at <= CURRENT_TIMESTAMP
           AND {_recorded}
        """,
        (brand_ids,),
    )
    return dict(experiment), assignments, [dict(row) for row in cur.fetchall()]


def evaluate_experiment(experiment_id: int) -> dict[str, Any]:
    """Read-only evaluation. Missing snapshots/URLs/JSON are excluded and counted."""
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        experiment, assignments, monitoring = _load_experiment_facts(cur, experiment_id)
    finally:
        conn.close()

    quality: defaultdict[str, int] = defaultdict(int)
    scope = _normalize_scope(experiment.get("scope") or {})
    arm_stats = {
        "control": {
            "article_ids": set(), "observed_article_ids": set(), "cited_article_ids": set(),
            "opportunities": 0, "citations": 0, "brands": set(), "industries": set(),
            "providers": set(), "models": set(), "surfaces": set(),
            "provider_stats": defaultdict(lambda: {"opportunities": 0, "citations": 0}),
            "ai_surface_stats": defaultdict(lambda: {"opportunities": 0, "citations": 0}),
        },
        "candidate": {
            "article_ids": set(), "observed_article_ids": set(), "cited_article_ids": set(),
            "opportunities": 0, "citations": 0, "brands": set(), "industries": set(),
            "providers": set(), "models": set(), "surfaces": set(),
            "provider_stats": defaultdict(lambda: {"opportunities": 0, "citations": 0}),
            "ai_surface_stats": defaultdict(lambda: {"opportunities": 0, "citations": 0}),
        },
    }
    normalized_rows = []
    url_owners: defaultdict[str, set[int]] = defaultdict(set)
    for row in assignments:
        if not _scope_allows(scope, "brand_ids", row.get("brand_id")):
            quality["assignment_outside_brand_scope"] += 1
            continue
        if not _scope_allows(
            scope, "industries", str(row.get("industry") or "").strip()
        ):
            quality["assignment_outside_industry_scope"] += 1
            continue
        url = normalize_publication_url(row.get("publish_url") or "")
        if not row.get("publication_snapshot_hash"):
            quality["publication_snapshot_missing"] += 1
            continue
        if row.get("body_observation_level") not in {
            "submission_snapshot", "channel_submission_snapshot",
            "extension_channel_submission_snapshot",
        }:
            quality["publication_body_not_submission_snapshot"] += 1
            continue
        machine_review = str(row.get("article_review_status") or "")
        human_review = str(row.get("article_human_review_status") or "")
        review_payload = row.get("article_review") or {}
        reviewed_body_hash = (
            review_payload.get("reviewed_content_hash")
            if isinstance(review_payload, dict) else None
        )
        reviewed_evidence_hash = (
            review_payload.get("reviewed_evidence_manifest_hash")
            if isinstance(review_payload, dict) else None
        )
        if reviewed_body_hash != row.get("publication_snapshot_hash"):
            quality["published_body_not_exact_reviewed_body"] += 1
            continue
        if not reviewed_evidence_hash or reviewed_evidence_hash != row.get("evidence_manifest_hash"):
            quality["published_evidence_not_exact_reviewed_manifest"] += 1
            continue
        if human_review == "rejected":
            quality["publication_review_not_eligible"] += 1
            continue
        if (
            row.get("style_family") == "company_facts"
            or row.get("style_code") in {"brand_softarticle", "company_profile"}
        ) and human_review != "approved":
            quality["publication_review_not_eligible"] += 1
            continue
        if not (
            machine_review == "approved"
            or (machine_review == "pending_human_review" and human_review == "approved")
        ):
            quality["publication_review_not_eligible"] += 1
            continue
        if not url or not row.get("published_at"):
            quality["publication_fact_incomplete"] += 1
            continue
        if row.get("assigned_at") and row["assigned_at"] >= row["published_at"]:
            quality["assignment_not_before_publication"] += 1
            continue
        normalized_rows.append({**row, "normalized_url": url})
        url_owners[url].add(int(row["article_id"]))
    eligible = [r for r in normalized_rows if len(url_owners[r["normalized_url"]]) == 1]
    quality["ambiguous_publication_url"] = len(normalized_rows) - len(eligible)

    for row in eligible:
        arm = row["arm"]
        stats = arm_stats[arm]
        stats["article_ids"].add(int(row["article_id"]))
        article_opportunities = 0
        article_citations = 0
        for result in monitoring:
            if result.get("brand_id") != row.get("brand_id"):
                continue
            if not _scope_allows(scope, "question_families", result.get("question_family")):
                continue
            if not _scope_allows(scope, "providers", result.get("provider")):
                continue
            if not _scope_allows(scope, "models", result.get("model")):
                continue
            if not _scope_allows(scope, "surfaces", result.get("surface")):
                continue
            tested_at = result.get("tested_at")
            if not tested_at or tested_at <= row["published_at"]:
                continue
            parsed = parse_citation_urls(result.get("search_citations"))
            if parsed["status"] in {"invalid_json", "not_array"}:
                quality[f"citation_{parsed['status']}"] += 1
                continue
            stats["opportunities"] += 1
            article_opportunities += 1
            provider = str(result.get("provider") or "unknown")
            model = str(result.get("model") or "unknown")
            surface = str(result.get("surface") or "unknown")
            ai_surface_key = (provider, model, surface)
            stats["providers"].add(provider)
            stats["models"].add(model)
            stats["surfaces"].add(surface)
            stats["provider_stats"][provider]["opportunities"] += 1
            stats["ai_surface_stats"][ai_surface_key]["opportunities"] += 1
            if row["normalized_url"] in parsed["urls"]:
                stats["citations"] += 1
                article_citations += 1
                stats["provider_stats"][provider]["citations"] += 1
                stats["ai_surface_stats"][ai_surface_key]["citations"] += 1
        if article_opportunities:
            stats["observed_article_ids"].add(int(row["article_id"]))
            if row.get("brand_id") is not None:
                stats["brands"].add(int(row["brand_id"]))
            if str(row.get("industry") or "").strip():
                stats["industries"].add(str(row["industry"]).strip())
        if article_citations:
            stats["cited_article_ids"].add(int(row["article_id"]))

    serial: dict[str, Any] = {}
    for arm, stats in arm_stats.items():
        n = int(stats["opportunities"])
        hits = int(stats["citations"])
        low, high = _wilson(hits, n)
        observed_articles = len(stats["observed_article_ids"])
        cited_articles = len(stats["cited_article_ids"])
        article_low, article_high = _wilson(cited_articles, observed_articles)
        serial[arm] = {
            "article_count": len(stats["article_ids"]),
            "observed_article_count": observed_articles,
            "cited_article_count": cited_articles,
            "article_citation_rate": cited_articles / observed_articles if observed_articles else None,
            "article_wilson_95": {"low": article_low, "high": article_high},
            "opportunity_count": n,
            "citation_count": hits,
            "citation_rate": hits / n if n else None,
            "wilson_95": {"low": low, "high": high},
            "brand_count": len(stats["brands"]),
            "industry_count": len(stats["industries"]),
            "provider_count": len(stats["providers"]),
            "model_count": len(stats["models"]),
            "surface_count": len(stats["surfaces"]),
            "by_provider": {
                provider: {
                    **counts,
                    "citation_rate": (
                        counts["citations"] / counts["opportunities"]
                        if counts["opportunities"] else None
                    ),
                }
                for provider, counts in sorted(stats["provider_stats"].items())
            },
            "by_ai_surface": [
                {
                    "provider": provider,
                    "model": model,
                    "surface": surface,
                    **counts,
                    "citation_rate": (
                        counts["citations"] / counts["opportunities"]
                        if counts["opportunities"] else None
                    ),
                }
                for (provider, model, surface), counts
                in sorted(stats["ai_surface_stats"].items())
            ],
        }
    minimum = int(experiment.get("min_arm_articles") or 30)
    sample_ready = all(serial[arm]["observed_article_count"] >= minimum for arm in ("control", "candidate"))
    coverage_ready = all(
        serial[arm]["brand_count"] >= 5 and serial[arm]["industry_count"] >= 3
        for arm in ("control", "candidate")
    )
    started_at = experiment.get("started_at")
    if started_at and getattr(started_at, "tzinfo", None) is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    elapsed_days = max(0, (_now() - started_at).days) if started_at else 0
    minimum_days = int(experiment.get("minimum_weeks") or 4) * 7
    observation_window_ready = bool(started_at and elapsed_days >= minimum_days)
    c0, c1 = serial["control"], serial["candidate"]
    interval_separated = (
        c0["article_wilson_95"]["high"] is not None
        and c1["article_wilson_95"]["low"] is not None
        and c1["article_wilson_95"]["low"] > c0["article_wilson_95"]["high"]
    )
    harm_separated = (
        c1["article_wilson_95"]["high"] is not None
        and c0["article_wilson_95"]["low"] is not None
        and c0["article_wilson_95"]["low"] > c1["article_wilson_95"]["high"]
    )
    if not observation_window_ready:
        decision = "INSUFFICIENT_OBSERVATION_WINDOW"
    elif not coverage_ready:
        decision = "INSUFFICIENT_COVERAGE"
    elif not sample_ready:
        decision = "INSUFFICIENT_SAMPLES"
    elif not c0["opportunity_count"] or not c1["opportunity_count"]:
        decision = "INSUFFICIENT_OBSERVATIONS"
    elif interval_separated:
        decision = "PASS"
    elif harm_separated:
        decision = "FAIL"
    else:
        decision = "INCONCLUSIVE"
    return {
        "contract_version": EXPERIMENT_CONTRACT_VERSION,
        "experiment_id": experiment_id,
        "experiment_key": experiment.get("experiment_key"),
        "state": experiment.get("state"),
        "decision": decision,
        "activation_allowed": decision == "PASS" and experiment.get("state") == "passed",
        "readiness": {
            "minimum_weeks": int(experiment.get("minimum_weeks") or 4),
            "elapsed_days": elapsed_days,
            "observation_window_ready": observation_window_ready,
            "minimum_observed_articles_per_arm": minimum,
            "sample_ready": sample_ready,
            "minimum_brands_per_arm": 5,
            "minimum_industries_per_arm": 3,
            "coverage_ready": coverage_ready,
        },
        "arms": serial,
        "difference": (
            c1["article_citation_rate"] - c0["article_citation_rate"]
            if c1["article_citation_rate"] is not None and c0["article_citation_rate"] is not None else None
        ),
        "quality_counts": dict(sorted(quality.items())),
        "evaluated_at": _now().isoformat(),
        "notes": [
            "机会分母仅含同品牌、发布后、血缘完整的实际监测；非法引用 JSON 不进入分母。",
            "主 PASS 门按文章级是否至少被直接引用一次计算 Wilson 区间；机会率与分 AI 明细仅作诊断。",
            "两臂均需至少 5 个品牌、3 个行业和冻结观察窗口；PASS 仍不会自动启用文体。",
        ],
    }


def summarize_strict_experiment_outcomes(*, limit: int = 50) -> dict[str, Any]:
    """Frontend-compatible, read-only history from strict GEO experiments.

    No prompt fingerprint proxy or editable current body participates.  Rates
    come only from the same-brand, post-publication, exact-URL experiment
    evaluator and retain its PASS/FAIL/INCONCLUSIVE/INSUFFICIENT decision.
    """
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, style_family, baseline_version_id, candidate_version_id,
                   state, started_at, created_at
              FROM geo_article_experiments
             WHERE state IN (
                    'approved','canary','observing','passed','failed',
                    'insufficient_samples'
                  )
             ORDER BY COALESCE(started_at, created_at) DESC, id DESC
             LIMIT %s
            """,
            (max(1, min(int(limit), 200)),),
        )
        experiments = [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()

    measures: list[dict[str, Any]] = []
    from writing.article_style_contract import STYLE_FAMILIES

    for experiment in experiments:
        result = evaluate_experiment(int(experiment["id"]))
        control = result["arms"]["control"]
        candidate = result["arms"]["candidate"]
        decision = str(result.get("decision") or "INSUFFICIENT_SAMPLES")
        insufficient = decision.startswith("INSUFFICIENT_")
        started_at = experiment.get("started_at")
        if started_at and getattr(started_at, "tzinfo", None) is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        age_days = max(0, (_now() - started_at).days) if started_at else None
        family_code = experiment.get("style_family") or "mapping_unknown"
        family = STYLE_FAMILIES.get(str(family_code))
        measures.append({
            "experiment_id": int(experiment["id"]),
            "style_code": family_code,
            "style_name": family.name if family else "文体映射未知",
            "new_version_id": experiment.get("candidate_version_id"),
            "old_version_id": experiment.get("baseline_version_id"),
            "age_days": age_days,
            "new_articles": candidate["observed_article_count"],
            "new_citations": candidate["cited_article_count"],
            "old_articles": control["observed_article_count"],
            "old_citations": control["cited_article_count"],
            "new_rate": candidate["article_citation_rate"],
            "old_rate": control["article_citation_rate"],
            "citation_delta_per_article": result.get("difference"),
            "comparable": not insufficient,
            "insufficient_data": insufficient,
            "effect_decision": decision,
            "truth_level": "T6_direct_preregistered_experiment",
            "quality_counts": result.get("quality_counts") or {},
            "by_ai_surface": {
                "control": control.get("by_ai_surface") or [],
                "candidate": candidate.get("by_ai_surface") or [],
            },
            "reason": (
                "候选显著优于对照" if decision == "PASS" else
                "候选显著劣于对照" if decision == "FAIL" else
                "样本充分但区间未分离" if decision == "INCONCLUSIVE" else
                "样本、覆盖或观察窗口不足"
            ),
        })
    return {
        "mode": "strict_preregistered_experiments",
        "eligible_versions": len(measures),
        "measures": measures,
        "truth_level": "T6_direct_preregistered_experiment",
        "note": "只读；不回写、不自动启用。provider/model/surface 明细保留在每项 by_ai_surface。",
    }


def can_activate_candidate(*, candidate_version_id: str, style_family: str) -> dict[str, Any]:
    """Require an explicitly passed experiment whose live evaluation still passes."""
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id FROM geo_article_experiments
             WHERE candidate_version_id=%s AND style_family=%s AND state='passed'
             ORDER BY updated_at DESC LIMIT 1
            """,
            (candidate_version_id, style_family),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return {"allowed": False, "reason": "no_explicitly_passed_experiment"}
    result = evaluate_experiment(int(row["id"]))
    return {
        "allowed": bool(result.get("activation_allowed")),
        "reason": result.get("decision"),
        "experiment": result,
    }


def record_experiment_decision(
    experiment_id: int,
    *,
    decision: str,
    actor_user_id: int,
    reason: str,
) -> dict[str, Any]:
    """Human signs the frozen evaluation; PASS can be signed only as passed."""
    if decision not in {"passed", "failed", "insufficient_samples", "cancelled"}:
        raise ValueError("invalid_experiment_decision")
    if len(str(reason or "").strip()) < 5:
        raise ValueError("decision_reason_required")
    evaluation = evaluate_experiment(experiment_id)
    if decision == "passed" and evaluation.get("decision") != "PASS":
        raise ValueError("live_evaluation_does_not_pass")
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE geo_article_experiments
               SET state=%s, observation_end=NOW(), updated_at=NOW(),
                   decision_by=%s, decision_reason=%s, decision_snapshot=%s
             WHERE id=%s AND state IN ('approved','canary','observing','insufficient_samples')
            RETURNING *
            """,
            (decision, actor_user_id, reason.strip(), Json(evaluation), experiment_id),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError("experiment_not_decidable")
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
