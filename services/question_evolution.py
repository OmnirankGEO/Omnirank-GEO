"""Governed research-question evolution; purchased questions are immutable."""
from __future__ import annotations

import re
from typing import Any, Final


QUESTION_POLICY_VERSION: Final = "question-evolution-v1.0"
QUESTION_FAMILY_VERSION: Final = "question-family-v1.0"
ALLOWED_QUERY_KINDS: Final = frozenset({"research", "derived_research"})
ALLOWED_SINGLE_DIMENSIONS: Final = frozenset({
    "intent", "scope", "scenario", "evidence_need", "risk", "funnel_stage", "wording",
})

_FAMILY_PATTERNS: Final = (
    ("case_evidence", re.compile(r"案例|客户|项目|效果|结果|实测|经验")),
    ("price_roi", re.compile(r"价格|费用|报价|预算|ROI|回报|成本")),
    ("risk_compliance", re.compile(r"风险|合规|法规|政策|标准|避坑|注意")),
    ("comparison", re.compile(r"对比|区别|哪个好|优缺点|A.?B|还是")),
    ("recommendation_selection", re.compile(r"推荐|哪家|怎么选|选择|供应商|服务商")),
    ("implementation", re.compile(r"怎么做|如何|步骤|流程|实施|落地")),
    ("trend_policy", re.compile(r"趋势|变化|未来|新规|政策")),
    ("definition", re.compile(r"是什么|什么是|定义|原理|为什么")),
)


def classify_question_family(question: str) -> str:
    text = re.sub(r"\s+", " ", str(question or "")).strip()
    if not text:
        return "mapping_unknown"
    for family, pattern in _FAMILY_PATTERNS:
        if pattern.search(text):
            return family
    return "other"


def _event(cur, *, prompt_id: int, actor_user_id: int, action: str,
           from_status: str | None, to_status: str, reason: str) -> None:
    cur.execute(
        """
        INSERT INTO geo_question_evolution_events (
            prompt_id, actor_user_id, action, from_status, to_status, reason, policy_version
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (prompt_id, actor_user_id, action, from_status, to_status, reason, QUESTION_POLICY_VERSION),
    )


def create_question_candidate(
    *,
    industry_id: int,
    prompt_text: str,
    actor_user_id: int,
    hypothesis: str,
    single_change_dimension: str,
    parent_prompt_id: int | None = None,
    query_kind: str = "derived_research",
) -> dict[str, Any]:
    text = str(prompt_text or "").strip()
    if len(text) < 5:
        raise ValueError("question_too_short")
    if query_kind not in ALLOWED_QUERY_KINDS:
        raise ValueError("purchased_or_monitoring_question_not_allowed")
    if single_change_dimension not in ALLOWED_SINGLE_DIMENSIONS:
        raise ValueError("invalid_single_change_dimension")
    if len(str(hypothesis or "").strip()) < 10:
        raise ValueError("hypothesis_required")
    family = classify_question_family(text)
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM geo_research_prompts WHERE industry_id=%s AND prompt_text=%s LIMIT 1",
            (industry_id, text),
        )
        if cur.fetchone():
            raise ValueError("duplicate_question")
        parent_version = 0
        if parent_prompt_id:
            cur.execute(
                "SELECT question_version FROM geo_research_prompts WHERE id=%s AND industry_id=%s",
                (parent_prompt_id, industry_id),
            )
            parent = cur.fetchone()
            if not parent:
                raise ValueError("parent_question_not_found")
            parent_version = int(parent.get("question_version") or 1)
        cur.execute(
            """
            INSERT INTO geo_research_prompts (
                industry_id, prompt_text, active, source, family_key, parent_prompt_id,
                question_version, query_kind, source_type, hypothesis,
                single_change_dimension, experiment_group, evolution_status, policy_version
            ) VALUES (%s, %s, FALSE, 'ai_generated', %s, %s, %s, %s,
                      'flywheel_candidate', %s, %s, 'shadow', 'draft', %s)
            RETURNING *
            """,
            (
                industry_id, text, family, parent_prompt_id, parent_version + 1,
                query_kind, hypothesis.strip(), single_change_dimension, QUESTION_POLICY_VERSION,
            ),
        )
        row = cur.fetchone()
        _event(
            cur, prompt_id=int(row["id"]), actor_user_id=actor_user_id,
            action="create_candidate", from_status=None, to_status="draft",
            reason=hypothesis.strip(),
        )
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def transition_question_candidate(
    prompt_id: int,
    *,
    actor_user_id: int,
    action: str,
    reason: str,
) -> dict[str, Any]:
    transitions = {
        "approve": ("draft", "approved"),
        "activate_shadow": ("approved", "shadow_active"),
        "retire": (None, "retired"),
        "reject": (None, "rejected"),
    }
    if action not in transitions or len(str(reason or "").strip()) < 5:
        raise ValueError("invalid_transition_or_reason")
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM geo_research_prompts WHERE id=%s FOR UPDATE", (prompt_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError("question_not_found")
        if row.get("query_kind") not in ALLOWED_QUERY_KINDS:
            raise ValueError("purchased_question_immutable")
        expected, target = transitions[action]
        current = str(row.get("evolution_status") or "draft")
        if expected and current != expected:
            raise ValueError(f"invalid_transition:{current}->{target}")
        active = action == "activate_shadow"
        cur.execute(
            """
            UPDATE geo_research_prompts
               SET evolution_status=%s, active=%s,
                   approved_by=CASE WHEN %s='approve' THEN %s::text ELSE approved_by END,
                   approved_at=CASE WHEN %s='approve' THEN NOW() ELSE approved_at END,
                   activated_at=CASE WHEN %s='activate_shadow' THEN NOW() ELSE activated_at END,
                   retired_at=CASE WHEN %s IN ('retire','reject') THEN NOW() ELSE retired_at END,
                   updated_at=NOW()
             WHERE id=%s RETURNING *
            """,
            (target, active, action, actor_user_id, action, action, action, prompt_id),
        )
        updated = cur.fetchone()
        _event(
            cur, prompt_id=prompt_id, actor_user_id=actor_user_id,
            action=action, from_status=current, to_status=target, reason=reason.strip(),
        )
        conn.commit()
        return dict(updated)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
