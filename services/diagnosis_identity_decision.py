"""Diagnosis brand-cell human review · decision service (板块 A · 2026-07-22).

复用 Review-CTO D6 裁决的监测人工确认 SSOT:

- 决策审计写共享 ``monitoring_identity_decision_events``(source_kind='diagnosis',
  source_result_id=diagnosis_records.id,tenant_id=brands.owner_user_id 快照,ip/reason);
- 别名真相写共享 ``monitoring_identity_name_decisions``(brand_id+normalized_name 主键),
  安全别名在同 brand 跨诊断/监测确定性复用,rejected alias 同样跨链生效;
- 确认动作全程 ``resolve_local`` 纯本地重判:**provider_calls=0、billing writes=0、
  不覆盖原始回答**(full_response 永远不改写,只追加人工决策字段);
- 单事务原子重算:dimension_stats → funnel(评分 SSOT)→ 总分/等级 →
  diagnosis_records + report_v2_modules_jsonb 回写 → brands.latest_score 同步 →
  公开视角经 resolve_canonical_score 读同一列,无独立快照需要失效。

DB 访问通过 ``get_conn`` 注入(默认 db.connection.get_connection),测试可 fake,
本地无 PG 也能跑通全部决策语义。
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Callable, Optional

from services.brand_latest_ssot import sync_brand_latest
from services.diagnosis_identity_review import (
    REVIEW_STATE_CONFIRMED,
    REVIEW_STATE_REJECTED,
    STATE_NOT_COLLECTED,
    STATE_PENDING_IDENTITY,
    VERDICT_NO,
    VERDICT_YES,
    aggregate_dimension_stats,
    build_diagnosis_cell_evidence_hash,
    cell_candidates,
    cell_near_miss,
    classify_cell_state,
)

logger = logging.getLogger("GEO-DiagnosisIdentity")


class DiagnosisIdentityReviewConflict(RuntimeError):
    """409 · 并发/幂等/证据冲突。"""


class DiagnosisIdentityReviewNotFound(LookupError):
    """404 · 诊断/单元格不存在。"""


VALID_ACTIONS = ("confirm_yes", "confirm_no", "add_alias")
_ACTION_TO_EVENT = {"confirm_yes": "yes", "confirm_no": "no", "add_alias": "custom"}


def _default_get_conn():
    from db.connection import get_connection

    return get_connection()


def _is_request_id_integrity_violation(exc: Exception) -> bool:
    """是否为 events 表 UNIQUE(request_id) 并发冲突（同 request_id 并发写）。

    [2026-07-22 R2 修复] 收窄为 UniqueViolation(pgcode 23505)：FK 冲突
    （brand 中途被删）、NOT NULL 冲突（数据 bug 信号）必须放行冒 500 留痕，
    不得吞成 409 让人无效重试。psycopg2 不可导入时保守返回 False。
    """
    try:
        import psycopg2
    except Exception:  # pragma: no cover - psycopg2 正常必装
        return False
    if not isinstance(exc, psycopg2.errors.UniqueViolation):
        return False
    pgcode = getattr(exc, "pgcode", None)
    if pgcode and pgcode != "23505":
        return False
    return True


def _rows(cur) -> list[dict]:
    return [dict(row) for row in (cur.fetchall() or [])]


def _parse_raw_data(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}


def _ai_visibility(raw: dict) -> dict:
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    ai = data.get("ai_visibility") if isinstance(data.get("ai_visibility"), dict) else {}
    return ai


def _find_cell(ai: dict, question: str, engine: str) -> tuple[Optional[dict], Optional[dict]]:
    """Return (question_item, cell) inside detail_table, or (None, None)."""
    for item in ai.get("detail_table") or []:
        if not isinstance(item, dict) or item.get("question") != question:
            continue
        results = item.get("results") or {}
        cell = results.get(engine)
        return item, (cell if isinstance(cell, dict) else None)
    return None, None


def _load_identity_for_review(brand_id: int, fallback_name: str = ""):
    from services.brand_identity_resolver import load_brand_identity

    return load_brand_identity(brand_id, fallback_name=fallback_name)


def _classifier(identity):
    return lambda cell: classify_cell_state(cell, identity=identity)


def _funnel_from_dimension_stats(dimension_stats: dict) -> dict:
    from tools.scoring.funnel_score import calculate_funnel_score

    brand_st = dimension_stats.get("brand_awareness") or {}
    local_st = dimension_stats.get("regional_industry") or {}
    scenario_st = dimension_stats.get("super_tier1") or {}
    return calculate_funnel_score(
        brand_detected=int(brand_st.get("detected", 0) or 0),
        brand_total=int(brand_st.get("total", 0) or 0),
        local_detected=int(local_st.get("detected", 0) or 0),
        local_total=int(local_st.get("total", 0) or 0),
        scenario_detected=int(scenario_st.get("detected", 0) or 0),
        scenario_total=int(scenario_st.get("total", 0) or 0),
    )


def _aggregates(ai: dict, identity) -> dict:
    question_types = ai.get("question_types") if isinstance(ai.get("question_types"), dict) else {}
    # [WO_BRAND_QUESTION_LEAK 2026-08-05 §2.2] 身份复核重算漏斗时同口径:题面含品牌名
    #   的题归品牌认知层,否则复核完的分仍带着"公司题冒充场景层"那 20 分虚高。
    #   名字取 identity 的 canonical/trusted **原串**,不走 all_trusted_names
    #   (它会把括号里的城市名拆成独立别名 —— 在途城市别名 P0)。
    # [#149 2026-09-08] 豁免改成**逐题**:谁享豁免由 diagnosis_question_origin 一处判定。
    #   改前这里是整体豁免(verbatim 就把 brand_name/aliases 清空),
    #   AI 出的品牌定向题进来后会被一起豁免掉 —— 那正是本单要堵的洞。
    from services.diagnosis_question_origin import brand_filter_exempt_questions
    _detail = ai.get("detail_table") or []
    _canonical = tuple(getattr(identity, "canonical_names", ()) or ())
    _brand_name = _canonical[0] if _canonical else ""
    _aliases = tuple(_canonical[1:]) + tuple(getattr(identity, "trusted_aliases", ()) or ())
    _exempt = brand_filter_exempt_questions(
        ai, [i.get("question", "") for i in _detail if isinstance(i, dict)])
    dimension_stats = aggregate_dimension_stats(
        _detail,
        question_types,
        classifier=_classifier(identity),
        brand_name=_brand_name,
        brand_aliases=_aliases,
        exempt_questions=_exempt,
    )
    totals = {
        "total": 0,
        "detected": 0,
        "pending_identity": 0,
        "provider_unknown": 0,
        "not_collected": 0,
    }
    for bucket in dimension_stats.values():
        for key in totals:
            totals[key] += int(bucket.get(key, 0) or 0)
    return {"dimension_stats": dimension_stats, "totals": totals}


def apply_recomputed_totals(ai: dict, aggregates: dict) -> dict:
    """把重算结果落回 ``ai_visibility`` —— dimension_stats 与汇总计数**单点同源**。

    [WO_MENTION_COUNT_NOT_RECOMPUTED 2026-08-08 §4.1]

    背景:改写单元格 ``brand_verdict`` 的路径一直在刷 ``dimension_stats`` 和分数,
    却把 ``detected_count`` / ``overall_mention_rate`` 原样带过去。客户版报告正文
    那句「品牌被提及 **N** 次」读的正是 ``detected_count``
    (``services/report_writer_v2.py:1329``)—— 服务商人工确认了 9 格,报告数字不涨。

    本函数是这两个字段在改写路径上的**唯一**派生点:人工确认链与存量重建脚本
    都必须走它。§3.2 明令不许再写第二套统计,否则「分数用一套、提及数用另一套」照旧。

    🔴 **分母口径一个字不动**:``overall_mention_rate`` 仍以记录自己的
    ``total_tests`` 为分母,公式与产生点 ``tools/ai_visibility/ai_tester.py:3942``
    逐字一致(``round(detected / total_tests * 100, 1)``)。
    「待确认格算不算分母」是挂在 Owner 的独立问题(§3.1),本函数不碰 ——
    这里只刷新分子。分子与分母口径无关:它就是「有多少格判 YES」。

    ``totals["detected"]`` 的口径 = ``classify_cell_state`` 判 ``YES`` 的格数
    (SSOT 见 ``services/diagnosis_identity_review``),与 funnel 评分同源。
    ⚠️ 若某题的 ``question_types`` 落在三个漏斗桶之外,该题的格子会被
    ``aggregate_dimension_stats`` 整体跳过 —— 那么它在**分数和计数里同时缺席**,
    两者仍自洽;这正是"复用同一个聚合"想要的性质。

    Returns:
        ``{"detected_count": {"before": x, "after": y},
           "overall_mention_rate": {"before": a, "after": b}}``
        —— 供调用方落审计/交付单 before-after,不供再算。
    """
    totals = aggregates.get("totals") or {}
    detected_after = int(totals.get("detected", 0) or 0)

    detected_before = ai.get("detected_count")
    rate_before = ai.get("overall_mention_rate")

    # 分母:用记录自己的 total_tests(不重定义、不重算、不回退到别的字段)
    try:
        total_tests = int(ai.get("total_tests") or 0)
    except (TypeError, ValueError):
        total_tests = 0

    ai["dimension_stats"] = aggregates["dimension_stats"]
    ai["detected_count"] = detected_after

    # 🔴 没有分母就**不动 rate**,绝不写 0。
    # 老记录里确实存在没有 `total_tests` 的 ai_visibility(既有假库夹具就是这样)。
    # 若在这种情况下写 0,等于凭空把客户的推荐率抹成 0% —— 正是本单要消灭的
    # 「把客户成绩说低」那个方向,比不刷新更坏。分子不需要分母,照常刷新。
    if total_tests > 0:
        rate_after = round(detected_after / total_tests * 100, 1)
        ai["overall_mention_rate"] = rate_after
    else:
        rate_after = rate_before

    return {
        "detected_count": {"before": detected_before, "after": detected_after},
        "overall_mention_rate": {"before": rate_before, "after": rate_after},
        # 没有分母时本次没有重算 rate —— 显式标出来,免得调用方把 after 当成"算过了"
        "rate_recomputed": total_tests > 0,
    }


def _rate_or_none(totals_delta: dict):
    """列镜像用:没重算过 rate 就返回 None,让 SQL 的 COALESCE 保留原值。"""
    if not totals_delta.get("rate_recomputed"):
        return None
    after = totals_delta["overall_mention_rate"]["after"]
    return None if after is None else float(after)


def _cell_dto(
    *,
    diagnosis_id: int,
    brand_id: int,
    question: str,
    engine: str,
    cell: Optional[dict],
    identity,
) -> dict:
    if cell is None:
        return {
            "question": question,
            "engine": engine,
            "state": STATE_NOT_COLLECTED,
            "brand_verdict": None,
            "brand_detected": False,
            "matched_text": None,
            "candidates": [],
            "near_miss_candidates": [],
            "evidence_snippet": None,
            "answer_summary": None,
            "answer_full": None,
            "answer_truncated": False,
            "detection_reason": None,
            "detection_method": None,
            "identity_review_state": "not_required",
            "identity_decision_version": 0,
            "evidence_hash": None,
            "human_decision": None,
        }
    state = classify_cell_state(cell, identity=identity)
    candidates: list[str] = []
    snippet = None
    if state == STATE_PENDING_IDENTITY:
        candidates, snippet = cell_candidates(cell, identity=identity)
    # [WO 2026-08-06 §1.2-1] 已判 NO 的格也要露出「疑似同品牌变体」。
    #   诊断 561 的 7 个 NO 格,复核层理由里逐条写着「证据中为'阿强龙虾'」——
    #   verdict 保持 NO(测量诚实),但代理必须看得到这条线索并能一键确认。
    #   ``decide_brand_cell`` 本来就不要求 state==PENDING(只看版本与是否已处理),
    #   所以这里只是把已有能力露出来,没有放宽任何权限闸。
    near_miss = (
        cell_near_miss(cell, identity=identity) if state == VERDICT_NO else []
    )
    matched_text = cell.get("matched_text") or (candidates[0] if candidates else None)
    # [P1-8 · 2026-07-26] 「疑似提到」卡片必须能看全原文才判得动。
    #   旧版只给 150 字 answer_summary + 前端 line-clamp-2 的 evidence_snippet，
    #   代理看不全上下文就没法点"确认提到/确认未提到"，SSOT §10.3 明确要求
    #   旁边提供"查看原文和判定依据"。这里把该格的 AI 原文一并返回（截断到 6000
    #   字防超大 payload）；这是本品牌自己的实测回答，代理本就有权限看。
    raw_answer = cell.get("full_response") or cell.get("response") or cell.get("answer_summary") or ""
    answer_full = str(raw_answer)[:6000] if isinstance(raw_answer, (str, bytes)) else ""
    return {
        "question": question,
        "engine": engine,
        "state": state,
        "brand_verdict": cell.get("brand_verdict"),
        "brand_detected": bool(cell.get("brand_detected")),
        "matched_text": matched_text,
        "candidates": candidates,
        # [WO 2026-08-06 §1.2-1] 疑似同品牌变体(不是命中,是待确认线索)。
        "near_miss_candidates": near_miss,
        "evidence_snippet": snippet,
        "answer_summary": cell.get("answer_summary"),
        # [P1-8] 完整原文 + 判定依据（前端弹层展示，按钮不再被截断挤走）
        "answer_full": answer_full or None,
        "answer_truncated": bool(isinstance(raw_answer, str) and len(raw_answer) > 6000),
        "detection_reason": cell.get("detection_reason"),
        "detection_method": cell.get("detection_method"),
        "identity_review_state": cell.get("identity_review_state") or "not_required",
        "identity_decision_version": int(cell.get("identity_decision_version") or 0),
        "evidence_hash": build_diagnosis_cell_evidence_hash(
            brand_id=brand_id,
            diagnosis_id=diagnosis_id,
            question=question,
            engine=engine,
            full_response=str(cell.get("full_response") or ""),
        ),
        "human_decision": cell.get("human_decision") or None,
    }


def list_brand_cells(
    diagnosis_id: int,
    brand_id: int,
    *,
    get_conn: Optional[Callable] = None,
) -> dict:
    """List every question x engine cell with explicit five-state verdicts.

    Reads only diagnosis_records.raw_data_json plus the local resolver — no LLM.
    """
    get_conn = get_conn or _default_get_conn
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, brand_id, brand_name, raw_data_json FROM diagnosis_records WHERE id = %s",
            (int(diagnosis_id),),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    if not row or int(row.get("brand_id") or 0) != int(brand_id):
        raise DiagnosisIdentityReviewNotFound("诊断记录不存在")

    raw = _parse_raw_data(row.get("raw_data_json"))
    ai = _ai_visibility(raw)
    identity = _load_identity_for_review(int(brand_id), fallback_name=row.get("brand_name") or "")

    detail_table = ai.get("detail_table") or []
    engines = list(ai.get("engines_tested") or [])
    if not engines:
        seen: list[str] = []
        for item in detail_table:
            for engine in (item.get("results") or {}):
                if engine not in seen:
                    seen.append(engine)
        engines = seen

    cells: list[dict] = []
    for item in detail_table:
        if not isinstance(item, dict):
            continue
        question = item.get("question") or ""
        results = item.get("results") or {}
        row_engines = list(engines) or list(results.keys())
        for engine in row_engines:
            cells.append(
                _cell_dto(
                    diagnosis_id=int(diagnosis_id),
                    brand_id=int(brand_id),
                    question=question,
                    engine=engine,
                    cell=results.get(engine),
                    identity=identity,
                )
            )

    aggregates = _aggregates(ai, identity)
    funnel = _funnel_from_dimension_stats(aggregates["dimension_stats"])
    return {
        "diagnosis_id": int(diagnosis_id),
        "brand_id": int(brand_id),
        "cells": cells,
        "aggregates": aggregates,
        "score": funnel.get("total_score"),
        "level": funnel.get("level"),
    }


def _assert_actor_can_decide(cur, *, brand: dict, actor_user_id: int, actor_is_admin: bool) -> None:
    """Live re-check mirroring decide_monitoring_identity_review (RBAC re-validation)."""
    cur.execute(
        """
        SELECT 1
          FROM public.user_roles ur
          JOIN public.roles r ON r.id = ur.role_id
         WHERE ur.user_id = %s AND r.name = 'admin'
         LIMIT 1
        """,
        (int(actor_user_id),),
    )
    live_is_admin = bool(cur.fetchone())
    if not live_is_admin and int(brand.get("owner_user_id") or 0) != int(actor_user_id):
        cur.execute(
            "SELECT 1 FROM public.user_clients WHERE user_id = %s AND brand_id = %s",
            (int(actor_user_id), int(brand.get("id"))),
        )
        live_assignment = cur.fetchone()
        if not live_assignment:
            cur.execute(
                """
                SELECT pg_catalog.to_regclass('public.organization_memberships') IS NOT NULL
                       AND pg_catalog.to_regclass('public.organization_brand_assignments') IS NOT NULL
                       AS ready
                """
            )
            organization_ready = bool((cur.fetchone() or {}).get("ready"))
            if organization_ready:
                cur.execute(
                    """
                    SELECT 1
                      FROM public.organization_memberships membership
                      JOIN public.organization_brand_assignments assignment
                        ON assignment.membership_id = membership.id
                       AND assignment.organization_id = membership.organization_id
                     WHERE membership.user_id = %s
                       AND membership.status = 'active'
                       AND assignment.brand_id = %s
                       AND assignment.status = 'active'
                     LIMIT 1
                    """,
                    (int(actor_user_id), int(brand.get("id"))),
                )
                live_assignment = cur.fetchone()
        if not live_assignment:
            raise PermissionError("品牌访问权已变化，请刷新后重试")


def decide_brand_cell(
    *,
    diagnosis_id: int,
    brand_id: int,
    actor_user_id: int,
    question: str,
    engine: str,
    action: str,
    selected_name: str | None = None,
    reason: str | None = None,
    request_id: str,
    expected_version: int = 0,
    ip: str | None = None,
    actor_is_admin: bool = False,
    get_conn: Optional[Callable] = None,
) -> dict:
    """Apply one human brand-cell decision atomically (single transaction).

    Zero provider calls, zero billing writes, original answers never rewritten.
    """
    from psycopg2.extras import Json
    from services.brand_identity_resolver import (
        BrandIdentity,
        BrandIdentityResolver,
        normalize_brand_name,
        normalize_confirmed_display_names,
        parse_brand_display_names,
    )

    normalized_action = str(action or "").strip().lower()
    if normalized_action not in VALID_ACTIONS:
        raise ValueError("无效的确认动作")
    question = str(question or "").strip()
    engine = str(engine or "").strip()
    if not question or not engine:
        raise ValueError("缺少问题或引擎定位")
    try:
        request_uuid = uuid.UUID(str(request_id))
    except (TypeError, ValueError):
        raise ValueError("request_id 必须是 UUID")
    expected_version = int(expected_version or 0)
    if expected_version < 0:
        raise ValueError("expected_version 非法")

    get_conn = get_conn or _default_get_conn
    conn = get_conn()
    try:
        cur = conn.cursor()
        # ---- 1. 锁诊断行 -------------------------------------------------
        cur.execute(
            """
            SELECT id, brand_id, brand_name, raw_data_json, report_v2_version
              FROM diagnosis_records
             WHERE id = %s
             FOR UPDATE
            """,
            (int(diagnosis_id),),
        )
        record = cur.fetchone()
        if not record or int(record.get("brand_id") or 0) != int(brand_id):
            raise DiagnosisIdentityReviewNotFound("诊断记录不存在")

        # ---- 2. 锁品牌行 + 活体权限复核 ---------------------------------
        cur.execute(
            "SELECT id, owner_user_id, name, company_name, brand_display_names FROM public.brands "
            "WHERE id = %s AND (is_deleted IS NULL OR is_deleted = FALSE) FOR UPDATE",
            (int(brand_id),),
        )
        brand = cur.fetchone()
        if not brand:
            raise DiagnosisIdentityReviewNotFound("品牌不存在")
        _assert_actor_can_decide(
            cur, brand=brand, actor_user_id=actor_user_id, actor_is_admin=actor_is_admin
        )

        # ---- 3. 幂等:同 request_id 同参返既有结果 · 异参 409 -------------
        cur.execute(
            """
            SELECT event_id, source_result_id, brand_id, action, selected_name,
                   normalized_name, result_version_before, result_version_after,
                   metadata, decided_at
              FROM public.monitoring_identity_decision_events
             WHERE request_id = %s::uuid
            """,
            (str(request_uuid),),
        )
        prior = cur.fetchone()
        if prior:
            prior_meta = prior.get("metadata") or {}
            if isinstance(prior_meta, str):
                try:
                    prior_meta = json.loads(prior_meta)
                except Exception:
                    prior_meta = {}
            same_request = (
                int(prior.get("source_result_id") or 0) == int(diagnosis_id)
                and int(prior.get("brand_id") or 0) == int(brand_id)
                and prior.get("action") == _ACTION_TO_EVENT[normalized_action]
                and normalize_brand_name(prior.get("selected_name") or "")
                == normalize_brand_name(selected_name or "")
                and str(prior_meta.get("question") or "") == question
                and str(prior_meta.get("engine") or "") == engine
                and int(prior.get("result_version_before") or 0) == expected_version
            )
            if not same_request:
                raise DiagnosisIdentityReviewConflict("请求编号已用于其他确认")
            conn.rollback()
            stored_response = prior_meta.get("response") if isinstance(prior_meta, dict) else None
            return {
                "success": True,
                "status": "idempotent",
                "event_id": prior.get("event_id"),
                **(stored_response if isinstance(stored_response, dict) else {}),
            }

        # ---- 4. 定位单元格 + 版本/状态闸 --------------------------------
        raw = _parse_raw_data(record.get("raw_data_json"))
        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        ai = _ai_visibility(raw)
        _item, cell = _find_cell(ai, question, engine)
        if cell is None:
            raise DiagnosisIdentityReviewNotFound("该问题×引擎的单元格不存在")
        full_response = str(cell.get("full_response") or "")
        if not full_response.strip():
            raise DiagnosisIdentityReviewConflict("该单元格没有已采集的回答，无法确认")
        review_state = cell.get("identity_review_state") or "not_required"
        if review_state in (REVIEW_STATE_CONFIRMED, REVIEW_STATE_REJECTED):
            raise DiagnosisIdentityReviewConflict("该记录已经处理")
        cell_version = int(cell.get("identity_decision_version") or 0)
        if cell_version != expected_version:
            raise DiagnosisIdentityReviewConflict("确认状态已变化，请刷新后重试")
        evidence_hash = build_diagnosis_cell_evidence_hash(
            brand_id=int(brand_id),
            diagnosis_id=int(diagnosis_id),
            question=question,
            engine=engine,
            full_response=full_response,
        )

        # ---- 5. 纯本地重判(provider_calls=0) ------------------------------
        identity = _load_identity_for_review(
            int(brand_id), fallback_name=record.get("brand_name") or brand.get("name") or ""
        )
        local_decision = BrandIdentityResolver(identity).resolve_local(full_response)
        local_candidates, local_snippet = cell_candidates(cell, identity=identity)

        # ---- 6. 决策语义 -------------------------------------------------
        decision = "positive" if normalized_action in ("confirm_yes", "add_alias") else "negative"
        if normalized_action == "add_alias" and not str(selected_name or "").strip():
            raise ValueError("添加别名必须填写名称")

        decision_names: list[tuple[str, str]] = []  # (normalized, display)
        # [WO_239-甲] 「这条回答没提到我」且没有任何可选名字 —— 与监测面
        # `decide_monitoring_identity_review` 的 `answer_absent` 同名同义。
        answer_absent = False
        chosen_display = str(selected_name or "").strip()
        if decision == "positive":
            if not chosen_display:
                chosen_display = (
                    local_candidates[0]
                    if local_candidates
                    else (getattr(local_decision, "matched_alias", None) or brand.get("name") or "")
                )
            valid_names = normalize_confirmed_display_names([chosen_display])
            if not valid_names:
                raise ValueError("请填写有效的品牌名称")
            chosen_display = valid_names[0]
            decision_names = [(normalize_brand_name(chosen_display), chosen_display)]
        else:
            negative_candidates = [chosen_display] if chosen_display else list(local_candidates)
            if not negative_candidates and getattr(local_decision, "matched_alias", None):
                negative_candidates = [local_decision.matched_alias]
            if not negative_candidates:
                # 🔴 [WO_239-甲 · 2026-09-18] 这里原来是
                #    `raise ValueError("请选择候选名称或填写正确名称")`。
                #
                #    后果:真客户点「确认未提到」**必然报错**。#727 广东星衍朗
                #    (`is_test=false`,有分享链接)千问那格判定依据是
                #    `registry_name_correction_requires_review` —— 它要求人复核,
                #    却落库 `matched_text: null` + `identity_candidates: []`,
                #    三级回落全空 ⇒ 抛。该 reason 覆盖 **61 条**诊断,近期几乎全是
                #    真客户且都有分享链接。**纠错通道在最需要它的那一类上是断的。**
                #
                #    🔴 这**不是**新放宽的口径,是把**监测面早就有的概念**补到诊断面:
                #    `db/monitoring_db.decide_monitoring_identity_review:5509` 逐字写着
                #        answer_absent = normalized_action == "no" and not normalized_name
                #        if not normalized_name and not answer_absent: raise ...
                #    并在 :5649 对这种情况给出 `decision_names = ()` +
                #    `decision_scope = "full_answer_absent"`。
                #    生产实证与它一致:109 次 `action='no'` 的 `normalized_name` 全空、
                #    **109/109 名字表零行**(反向对照:35 次 `yes` 35/35 有行,JOIN 有效)。
                #    ⇒ 「零名字」不是没测过的边角,它是**每一次成功的「确认未提到」
                #      一直以来的样子**;`decision_names = ()` 让写入循环空转,
                #      名字表本来就不该有它的位置。
                #
                #    同一个谓词长在两处,只有一处拿到了修法 ——
                #    本仓 feedback_one_predicate_one_place_or_half_goes_unverified。
                #
                #    语义:客户点的是「这条回答没提到我」,那是对**回答**的判断,
                #    不需要任何名字。名字表记的是「这个名字不是这个品牌」,
                #    没有名字时**没有名字可记**,但**格子这件事仍要记**
                #    (下方 cell 判定与事件落库都不依赖 decision_names)。
                answer_absent = True
                decision_names = []
            else:
                answer_absent = False
                valid_names = normalize_confirmed_display_names(negative_candidates)
                decision_names = [(normalize_brand_name(name), name) for name in valid_names]

        #  trusted-name 保护:不可把本品牌可信名整组标记为"不是"
        if decision == "negative":
            trusted_names = {
                normalize_brand_name(value)
                for value in (
                    brand.get("name") or "",
                    brand.get("company_name") or "",
                    *parse_brand_display_names(brand.get("brand_display_names")),
                )
                if normalize_brand_name(value)
            }
            if any(name_key in trusted_names for name_key, _ in decision_names):
                raise DiagnosisIdentityReviewConflict(
                    "候选中包含当前品牌的可信名称，不能整组标记为不是"
                )

        # ---- 7. 共享别名真相表(D6 · 同 brand 跨 surface 生效) --------------
        cur.execute(
            """
            SELECT normalized_name, decision
              FROM public.monitoring_identity_name_decisions
             WHERE brand_id = %s AND normalized_name = ANY(%s)
             FOR UPDATE
            """,
            (int(brand_id), [name_key for name_key, _ in decision_names]),
        )
        existing_decisions = {
            row["normalized_name"]: row["decision"] for row in _rows(cur)
        }
        if any(
            existing_decisions.get(name_key) not in (None, decision)
            for name_key, _ in decision_names
        ):
            raise DiagnosisIdentityReviewConflict("该名称已有相反确认，无法覆盖")

        # [2026-07-22 R3 · P2] step 7 写入段与 step 12 events INSERT 同口径:
        # name_decisions 表同样有 UNIQUE(request_id),并发同 request_id 打不同诊断
        # 会在此撞唯一约束 —— 收窄捕获 UniqueViolation(pgcode 23505)转 409 人话,
        # FK/NOT NULL 等其他完整性错误仍冒 500 留痕(与 R2 收窄语义一致)。
        try:
            for index, (name_key, display_name) in enumerate(decision_names):
                projection_request_id = (
                    str(request_uuid)
                    if index == 0
                    else str(uuid.uuid5(request_uuid, name_key))
                )
                cur.execute(
                    """
                    INSERT INTO public.monitoring_identity_name_decisions
                        (brand_id, normalized_name, display_name, decision, decision_version,
                         evidence_hash, decided_by, request_id)
                    VALUES (%s, %s, %s, %s, 1, %s, %s, %s::uuid)
                    ON CONFLICT (brand_id, normalized_name) DO UPDATE
                       SET display_name = EXCLUDED.display_name,
                           evidence_hash = EXCLUDED.evidence_hash,
                           decided_by = EXCLUDED.decided_by,
                           request_id = EXCLUDED.request_id,
                           updated_at = CURRENT_TIMESTAMP,
                           decision_version = public.monitoring_identity_name_decisions.decision_version + 1
                     WHERE public.monitoring_identity_name_decisions.decision = EXCLUDED.decision
                    """,
                    (
                        int(brand_id), name_key, display_name, decision,
                        evidence_hash, int(actor_user_id), projection_request_id,
                    ),
                )
                if cur.rowcount != 1:
                    raise DiagnosisIdentityReviewConflict("名称确认发生冲突")
        except Exception as name_write_err:
            if _is_request_id_integrity_violation(name_write_err):
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise DiagnosisIdentityReviewConflict(
                    "请求编号与其他确认并发冲突，请刷新后重试"
                ) from name_write_err
            raise

        # ---- 8. 安全别名持久化(brands SSOT + client_profiles 镜像) ---------
        merged_display_names: tuple[str, ...] = ()
        if decision == "positive":
            existing_names = list(parse_brand_display_names(brand.get("brand_display_names")))
            merged_display_names = normalize_confirmed_display_names(
                [*existing_names, chosen_display]
            )
            payload = json.dumps(list(merged_display_names), ensure_ascii=False)
            cur.execute(
                """
                UPDATE public.brands
                   SET brand_display_names = %s, updated_at = CURRENT_TIMESTAMP
                 WHERE id = %s
                """,
                (payload, int(brand_id)),
            )
            cur.execute(
                """
                UPDATE public.client_profiles
                   SET brand_display_names = %s, updated_at = CURRENT_TIMESTAMP
                 WHERE brand_id = %s
                   AND (is_deleted = 0 OR is_deleted IS NULL)
                """,
                (payload, int(brand_id)),
            )

        # ---- 9. 更新单元格(不覆盖原始回答) ---------------------------------
        next_version = expected_version + 1
        new_verdict = VERDICT_YES if decision == "positive" else VERDICT_NO
        cell["brand_verdict"] = new_verdict
        cell["brand_detected"] = decision == "positive"
        cell["identity_review_state"] = (
            REVIEW_STATE_CONFIRMED if decision == "positive" else REVIEW_STATE_REJECTED
        )
        cell["identity_decision_version"] = next_version
        cell["detection_reason"] = "human_confirmed"
        cell["detection_method"] = "human_review"
        if decision == "positive":
            cell["matched_text"] = chosen_display
        cell["human_decision"] = {
            "action": normalized_action,
            "selected_name": chosen_display or None,
            "decided_names": [display for _, display in decision_names],
            "reason": (reason or "").strip() or None,
            "actor_user_id": int(actor_user_id),
            "request_id": str(request_uuid),
            "source": "diagnosis_human_review",
            "local_rejudge": {
                "verdict": getattr(getattr(local_decision, "verdict", None), "value", None),
                "reason": getattr(local_decision, "reason", None),
                "method": getattr(local_decision, "method", None),
            },
        }

        # ---- 10. 原子重算:dimension_stats → funnel(SSOT) -------------------
        post_identity = BrandIdentity(
            brand_id=identity.brand_id,
            canonical_names=identity.canonical_names,
            trusted_aliases=(
                *identity.trusted_aliases,
                *((chosen_display,) if decision == "positive" else ()),
            ),
            rejected_aliases=(
                *identity.rejected_aliases,
                *(tuple(display for _, display in decision_names) if decision == "negative" else ()),
            ),
            industry=identity.industry,
            load_error=identity.load_error,
        )
        aggregates = _aggregates(ai, post_identity)
        # [WO_MENTION_COUNT 2026-08-08 §4.1] dimension_stats 与汇总计数单点同源:
        # 此前只刷了 dimension_stats,detected_count/overall_mention_rate 原样带过去,
        # 客户报告「品牌被提及 N 次」因此不随人工确认上涨(5 份真客户被少报)。
        totals_delta = apply_recomputed_totals(ai, aggregates)
        funnel = _funnel_from_dimension_stats(aggregates["dimension_stats"])
        data["ai_visibility"] = ai
        raw["data"] = data

        cur.execute(
            "UPDATE diagnosis_records SET raw_data_json = %s WHERE id = %s",
            (json.dumps(raw, ensure_ascii=False, default=str), int(diagnosis_id)),
        )
        # [WO_MENTION_COUNT 2026-08-08 §4.1 · 扫出的第三处陈旧点] 列镜像同事务刷新。
        # diagnosis_records.ai_detected_count / ai_mention_rate 只在 save_diagnosis_record
        # (新诊断落库)时写过一次,改写路径从不碰 → 与 raw JSON 分家。消费方两个:
        #   · /api/strategy/generate(server.py:13506)拿它喂销售话术 LLM;
        #   · scripts/regen_v2_reports.py:107 会拿列值**反向覆盖回 JSON** ——
        #     只修 JSON 不修列,跑一次 regen 就把修好的冲掉。
        # 放在 raw_data_json 那条 UPDATE 之后、与之同事务,成功/降级两条路径都覆盖到。
        cur.execute(
            """
            UPDATE diagnosis_records
               SET ai_detected_count = %s,
                   ai_mention_rate = COALESCE(%s, ai_mention_rate)
             WHERE id = %s
            """,
            (
                int(totals_delta["detected_count"]["after"]),
                # 没有分母时 after 为 None → COALESCE 保留原列值,绝不写 0
                _rate_or_none(totals_delta),
                int(diagnosis_id),
            ),
        )

        # ---- 11. v2 报告重生 + 总分/等级/brands.latest_score 同事务回写 -----
        report_rebuilt = False
        report_error: Optional[str] = None
        # [2026-07-22 R3 · P2] SAVEPOINT 隔离:v2 回写内部 SQL 失败会把事务打进中止态
        # (25P02),此前降级写 execute 必再抛 → 整体 500,降级分支对设计目标场景恰好无效。
        # SAVEPOINT 后失败仅回滚 v2 写段,原始决策/别名/单元格重算全部保留。
        cur.execute("SAVEPOINT sp_v2_write")
        try:
            cur.execute(
                "SELECT * FROM client_profiles WHERE brand_id = %s LIMIT 1",
                (int(brand_id),),
            )
            profile_row = cur.fetchone()
            # 🔴 [#54/#55 2026-09-04] 报告关联的报价按**本次诊断**取,
            #    不再走「该品牌最新一张 LIMIT 1」—— 那会把**另一次服务**的报价
            #    算到这份报告头上(与 WP7 修掉的 brand max merge 同一个病)。
            #    多张命中时的确定性规则:created_at DESC, id DESC —— 取最后签发的那张,
            #    id 作次序兜底,保证同一份数据每次读出同一张(不靠数据库返回顺序)。
            cur.execute(
                """SELECT * FROM quotes WHERE diagnosis_id = %s
                   ORDER BY created_at DESC, id DESC LIMIT 1""",
                (int(diagnosis_id),),
            )
            quote_row = cur.fetchone()

            # 🔴 [#54/#55] 「已发布证据」**复用权威谓词,不写第二份**。
            #    `quote_published_active` 内部覆盖四条发布链、状态集、cutoff、
            #    撤稿回落与按 unit_key 去重;我第一版写的
            #    `COUNT(*) FROM media_publications` 只覆盖其中一条链,
            #    会把只走代发链的客户读成 0 —— 而那正是
            #    publication_stage_sources 逐字点名的事故形态:
            #    「客户付了钱、发了稿、监测不跑,而且不会报错」。
            #
            #    🔴 `cursor=cur` 必须透传:不传会自开连接,
            #    而本函数是**先写后装配**(上面已 UPDATE、现在在 SAVEPOINT 里),
            #    在装配深处自开连接 = 原地重放 08-10 那次把生产打成 503 的自锁死。
            from services.publication_stage_adapters import quote_published_active

            _qid = (dict(quote_row).get("id") if quote_row else None)
            if _qid:
                # 🔴 [#54/#55] 投影查不到 = 该字段退出分母，**不能拖垮整份报告装配**。
                #    不包这一层时：发布链一报错，异常会冒到外层 try，
                #    整个 v2 装配走 SAVEPOINT 降级 —— 用户丢的不是一个字段，是整份报告。
                #    四态里 `unavailable` 存在的意义就是接住这一格。
                try:
                    _n = quote_published_active(int(_qid), cursor=cur)
                except Exception:
                    _n = None
                published_state = (
                    {"count": int(_n), "available": True, "reason": None}
                    if _n is not None else
                    {"count": None, "available": False, "reason": "projection_unavailable"}
                )
            else:
                # 🔴 「这次诊断名下没有报价」与「查不到」是两件事,reason 分开。
                published_state = {"count": None, "available": False, "reason": "no_quote"}

            from services.diagnosis_report_v2 import (
                assemble_diagnosis_report_v2,
                update_diagnosis_v2_in_db,
            )

            brand_for_report = dict(brand)
            if decision == "positive":
                brand_for_report["brand_display_names"] = json.dumps(
                    list(merged_display_names), ensure_ascii=False
                )
            v2_result = assemble_diagnosis_report_v2(
                diagnosis_results=raw,
                brand=brand_for_report,
                profile=dict(profile_row) if profile_row else {},
                quote=dict(quote_row) if quote_row else {},
                brand_id=int(brand_id),
                score_data=raw.get("score_data") or raw.get("scores") or {},
                published=published_state,
            )
            report_error = v2_result.get("error")
            # 决策重算以评分 SSOT 为准:装配产物中的 funnel 必须与本次重算一致,
            # 不一致时以重算值覆盖(防装配层读取到陈旧 dimension_stats)。
            v2_result["funnel_score"] = funnel
            if not update_diagnosis_v2_in_db(int(diagnosis_id), v2_result, conn=conn):
                report_error = report_error or "v2 报告回写失败"
            else:
                report_rebuilt = not report_error
        except Exception as rebuild_err:  # noqa: BLE001 决策已生效,报告失败降级手工回写
            logger.warning(
                "[diagnosis_identity] 报告重生失败 · 降级只写评分列 diagnosis_id=%s: %s",
                diagnosis_id, rebuild_err,
            )
            report_error = f"v2 报告重生异常: {rebuild_err}"

        if not report_rebuilt:
            # fail-closed 口径:报告重生失败时仍然同事务落评分 SSOT 列,
            # 并显式写 report_v2_error,绝不让代理端/公开端读到不一致分数。
            # [R3 · P2] 先回滚到 SAVEPOINT:清掉 v2 写段可能造成的事务中止态/半写,
            # 降级写才能在中止态之外的干净子事务上执行。
            cur.execute("ROLLBACK TO SAVEPOINT sp_v2_write")
            # [R3 · P3] 降级分支不再标报告"生成时间":报告实际未生成却标新鲜会
            # 误导消费者;仅落评分 SSOT 列 + report_v2_error 标记(防回潮有静态闸)。
            cur.execute(
                """
                UPDATE diagnosis_records
                   SET total_score = %s,
                       level = %s,
                       report_v2_error = %s
                 WHERE id = %s
                """,
                (
                    int(funnel.get("total_score") or 0),
                    funnel.get("level"),
                    report_error,
                    int(diagnosis_id),
                ),
            )
            # [WO_BRAND_LATEST_CROSS_TENANT_WRITE + WO_V2_REGEN_POLLUTES 2026-08-08]
            #   原写法 `WHERE b.id = %s` 把 diagnosis_id 与 brand_id 两个自由入参直接配对、
            #   零校验;且本分支正是「服务商对**旧**诊断做人工身份确认」的日常路径 ——
            #   两个缺陷在这一条 SQL 上同时存在。改走 SSOT 单点(归属 + 最新 + published-only)。
            _synced = sync_brand_latest(
                cur,
                diagnosis_id=int(diagnosis_id),
                score=int(funnel.get("total_score") or 0),
                brand_id=int(brand_id),
            )
            if _synced == 0:
                # 0 行 = 守卫挡下。对旧诊断做人工确认时**这就是期望行为**:
                # 报告本身照常重生,但不把客户列表分数拽回旧诊断。
                logger.info(
                    "[identity_decision] brands.latest_* 未同步(守卫挡下):"
                    "diagnosis_id=%s brand_id=%s —— 非该品牌 published-only 最新那份,"
                    "或二者不配对。报告已重生,冗余列保持指向真最新。",
                    diagnosis_id, brand_id,
                )
        # v2 写段收尾:成功/降级两条路径都显式释放 SAVEPOINT(保持事务内整洁)。
        cur.execute("RELEASE SAVEPOINT sp_v2_write")

        # ---- 12. 审计事件(source_kind='diagnosis' · append-only) -----------
        cell_snapshot = _cell_dto(
            diagnosis_id=int(diagnosis_id),
            brand_id=int(brand_id),
            question=question,
            engine=engine,
            cell=cell,
            identity=post_identity,
        )
        response_payload = {
            "cell": cell_snapshot,
            "aggregates": aggregates,
            "score": funnel.get("total_score"),
            "level": funnel.get("level"),
            "report_rebuilt": report_rebuilt,
            # [WO_MENTION_COUNT 2026-08-08] 汇总计数 before/after 进审计元数据:
            # 这次改动唯一会动的客户可见数字,留痕才能事后核账。
            "totals_delta": totals_delta,
        }
        try:
            cur.execute(
                """
                INSERT INTO public.monitoring_identity_decision_events
                    (result_id, brand_id, action, selected_name, normalized_name,
                     evidence_hash, result_version_before, result_version_after,
                     actor_user_id, request_id, metadata,
                     source_kind, source_result_id, tenant_id, ip, reason)
                VALUES (NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s::uuid, %s,
                        'diagnosis', %s, %s, %s, %s)
                RETURNING event_id, decided_at
                """,
                (
                    int(brand_id),
                    _ACTION_TO_EVENT[normalized_action],
                    chosen_display or (decision_names[0][1] if decision_names else ""),
                    decision_names[0][0] if decision_names else "",
                    evidence_hash,
                    expected_version,
                    next_version,
                    int(actor_user_id),
                    str(request_uuid),
                    Json({
                        "source": "diagnosis_human_review",
                        "question": question,
                        "engine": engine,
                        "decision_name_count": len(decision_names),
                        "decision_names": [display for _, display in decision_names],
                        # [WO_239-甲] 与监测面同名同义(`monitoring_db:5650`)。
                        # 🔴 留这个标记是为了让「**从未有过名字**」与「**人工确认没有名字**」
                        #    在库里分得开 —— 两种不同原因长成同一个读数(空 selected_name)
                        #    是本仓反复出现的病。事后归因靠它,不靠猜。
                        "decision_scope": (
                            "full_answer_absent" if answer_absent
                            else ("candidate_set_rejected" if decision == "negative"
                                  else "candidate_confirmed")
                        ),
                        "local_rejudge": cell["human_decision"]["local_rejudge"],
                        "response": response_payload,
                    }),
                    int(diagnosis_id),
                    int(brand.get("owner_user_id") or 0) or None,
                    (ip or "").strip() or None,
                    (reason or "").strip() or None,
                ),
            )
            event = dict(cur.fetchone())
            conn.commit()
        except Exception as write_err:
            # [集中严审 R1 · P2-1] 跨诊断并发同 request_id：events 表 UNIQUE(request_id)
            # 撞唯一约束时不再冒 500，转 409（与同键异参 409 同语义 · 人话）。
            if _is_request_id_integrity_violation(write_err):
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise DiagnosisIdentityReviewConflict(
                    "请求编号与其他确认并发冲突，请刷新后重试"
                ) from write_err
            raise
        return {
            "success": True,
            "status": "resolved",
            "event_id": event.get("event_id"),
            "decided_at": event.get("decided_at"),
            **response_payload,
        }
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


__all__ = [
    "DiagnosisIdentityReviewConflict",
    "DiagnosisIdentityReviewNotFound",
    "VALID_ACTIONS",
    "decide_brand_cell",
    "list_brand_cells",
]
