"""Reconciler —— 补登记漏挂的终态源 + 撤回后来失效的源(spec §10.3 / R8)。

- 有界回看(lookback_hours)+ 批上限,禁全表扫;register/withdraw 均幂等,重跑安全。
- 补登记:近 N 小时已完成诊断/监测/调研但无 event(源终态成立,同事务无法接线 → 由此补)。
- 撤回:诊断退款(diagnosis_refund_records)、run 转阻断态(released/withheld/manual/repair)→ 对应 event withdrawn。
- 撤回不改 signals(不可变);聚合下轮 join 只取 promoted 自动剔除 → 公共贡献归零。
"""
from __future__ import annotations

import logging
from typing import Optional

from db.connection import get_db
from . import repository
from .audit import write_audit
from .source_adapters import registered_source_adapters
from .source_hooks import DIAG_BLOCKED

logger = logging.getLogger("GEO-ObservationReconciler")


def _table_exists(cur, name: str) -> bool:
    cur.execute("SELECT to_regclass(%s)", (name,))
    return cur.fetchone()["to_regclass"] is not None


def reconcile_registration(cur, *, lookback_hours: int = 24, batch_limit: int = 500) -> dict:
    """补登记近 lookback_hours 已终态但无 event 的三来源。返回各来源新登记数。"""
    counts = {"paid_diagnosis": 0, "recurring_monitoring": 0, "research_round": 0}
    adapters = registered_source_adapters()

    # 付费诊断:committed/completed_exempt 且无 event
    if _table_exists(cur, "diagnosis_runs"):
        cur.execute(
            """
            SELECT dr.run_token, dr.finished_at
              FROM diagnosis_runs dr
             WHERE dr.run_status IN ('committed','completed_exempt')
               AND dr.finished_at > NOW() - make_interval(hours => %s)
               AND NOT EXISTS (SELECT 1 FROM public.geo_observation_events e
                               WHERE e.source_type='paid_diagnosis' AND e.source_record_id = dr.run_token)
             ORDER BY dr.finished_at DESC
             LIMIT %s
            """,
            (lookback_hours, batch_limit),
        )
        for r in cur.fetchall():
            counts["paid_diagnosis"] += adapters["paid_diagnosis"].register_terminal(cur, r)

    # 持续监测:task completed 且 result 无 event
    if _table_exists(cur, "monitoring_results") and _table_exists(cur, "monitoring_tasks"):
        from services.monitoring_identity_review import aggregate_eligible_sql
        cur.execute(
            f"""
            SELECT mr.id
              FROM monitoring_results mr JOIN monitoring_tasks mt ON mt.id = mr.task_id
             WHERE mt.status='completed' AND mr.tested_at > NOW() - make_interval(hours => %s)
               AND {aggregate_eligible_sql('mr')}
               AND NOT EXISTS (SELECT 1 FROM public.geo_observation_events e
                               WHERE e.source_type='recurring_monitoring' AND e.source_record_id = mr.id::text)
             ORDER BY mr.tested_at DESC
             LIMIT %s
            """,
            (lookback_hours, batch_limit),
        )
        for r in cur.fetchall():
            counts["recurring_monitoring"] += adapters["recurring_monitoring"].register_terminal(cur, r)

    # 公共调研:geo_research_raw 无 event(round completed 检查在 register 内)
    if _table_exists(cur, "geo_research_raw"):
        cur.execute(
            """
            SELECT r.id
              FROM geo_research_raw r
             WHERE r.created_at > NOW() - make_interval(hours => %s)
               AND NOT EXISTS (SELECT 1 FROM public.geo_observation_events e
                               WHERE e.source_type='research_round' AND e.source_record_id = r.id::text)
             ORDER BY r.created_at DESC
             LIMIT %s
            """,
            (lookback_hours, batch_limit),
        )
        for r in cur.fetchall():
            counts["research_round"] += adapters["research_round"].register_terminal(cur, r)

    return counts


def reconcile_withdrawals(cur, *, batch_limit: int = 500, **_legacy) -> dict:
    """撤回源已失效的诊断 event。事件驱动(扫非 withdrawn 的 event,无时间窗盲区:停机/积压超期也不残留)。

    退款 / 阻断态 / 晋升后可见性降级三类,逐条 batch 撤回;withdraw 幂等,多轮排空 backlog。
    """
    if not _table_exists(cur, "diagnosis_runs"):
        return {"withdrawn": 0}
    has_refund = _table_exists(cur, "diagnosis_refund_records")
    has_records = _table_exists(cur, "diagnosis_records")

    # 退款:任何非 withdrawn 的诊断 event,其 run 已有退款流水 → 撤回
    todo: dict[int, str] = {}
    if has_refund:
        cur.execute(
            """SELECT e.id FROM public.geo_observation_events e
                WHERE e.source_type='paid_diagnosis' AND e.processing_state <> 'withdrawn'
                  AND EXISTS (SELECT 1 FROM diagnosis_refund_records rf WHERE rf.run_token = e.source_record_id)
                ORDER BY e.id LIMIT %s""",
            (batch_limit,),
        )
        for r in cur.fetchall():
            todo[r["id"]] = "source_refunded"
    # 阻断态:run 转 released/failed_exempt/manual/repair/cancelled
    cur.execute(
        """SELECT e.id FROM public.geo_observation_events e
             JOIN diagnosis_runs dr ON dr.run_token = e.source_record_id
            WHERE e.source_type='paid_diagnosis' AND e.processing_state <> 'withdrawn'
              AND dr.run_status = ANY(%s)
            ORDER BY e.id LIMIT %s""",
        (list(DIAG_BLOCKED), batch_limit),
    )
    for r in cur.fetchall():
        todo.setdefault(r["id"], "source_blocked")
    # 可见性降级:已晋升 event 的最新 record 非 published(晋升期严格 published 门的事后守护)
    if has_records:
        cur.execute(
            """SELECT e.id FROM public.geo_observation_events e
                 JOIN diagnosis_records rec ON rec.id = (
                     SELECT id FROM diagnosis_records WHERE run_token = e.source_record_id ORDER BY id DESC LIMIT 1)
                WHERE e.source_type='paid_diagnosis' AND e.processing_state='promoted'
                  AND rec.result_visibility IS DISTINCT FROM 'published'
                ORDER BY e.id LIMIT %s""",
            (batch_limit,),
        )
        for r in cur.fetchall():
            todo.setdefault(r["id"], "visibility_downgraded")

    withdrawn = 0
    for eid, reason in todo.items():
        if repository.withdraw_event(cur, eid, reason_codes=[reason]):
            write_audit(cur, "withdraw", "system", event_id=eid, reason_codes=[reason], idempotency_token="withdraw")
            withdrawn += 1
    return {"withdrawn": withdrawn}


def reconcile_retention(cur, *, batch_limit: int = 500) -> dict:
    """保留期到期(retention_until < now)且非 legal_hold 的 event → 不可逆匿名化 owner/brand 映射 + 私域哈希 + 删 HMAC 桶 + 审计。

    - 清 owner_user_id/brand_id(受限来源映射)+ answer_hash/prompt_fingerprint(私域派生证据);删 contributor bucket。
    - 保留匿名 signal(不可变)与 source_record_id 源引用(源表数据由源自身保留期负责;不改业务唯一键 → 不撞唯一/不触发重登记)。
    - 幂等标记 = owner/brand/answer_hash/prompt_fingerprint 全 NULL(已匿名化 → 跳过)。
    """
    cur.execute(
        """SELECT id FROM public.geo_observation_events
            WHERE retention_until IS NOT NULL AND retention_until < NOW() AND legal_hold = FALSE
              AND (processing_state <> 'withdrawn'
                   OR owner_user_id IS NOT NULL OR brand_id IS NOT NULL
                   OR answer_hash IS NOT NULL OR prompt_fingerprint IS NOT NULL
                   OR EXISTS (SELECT 1 FROM public.geo_observation_contributor_buckets b
                               WHERE b.event_id=geo_observation_events.id))
            ORDER BY retention_until ASC LIMIT %s""",
        (batch_limit,),
    )
    ids = [r["id"] for r in cur.fetchall()]
    processed = 0
    for eid in ids:
        # Freeze eligibility first. The promoted-input immutability trigger then
        # permits HMAC-bucket erasure without ever exposing a half-anonymized
        # still-promoted row. Same transaction => epoch bump + anonymization +
        # audit commit (or roll back) together.
        repository.withdraw_event(cur, eid, reason_codes=["retention_expired"])
        cur.execute(
            """UPDATE public.geo_observation_events
                  SET owner_user_id=NULL, brand_id=NULL, answer_hash=NULL, prompt_fingerprint=NULL, updated_at=NOW()
                WHERE id=%s AND legal_hold=FALSE""",
            (eid,),
        )
        cur.execute("DELETE FROM public.geo_observation_contributor_buckets WHERE event_id=%s", (eid,))
        write_audit(cur, "retention_anonymize", "system", event_id=eid, reason_codes=["retention_expired"],
                    idempotency_token="retention")
        processed += 1
    return {"anonymized": processed}


def _withdraw_run_events(cur, run_token: str, reasons: list) -> int:
    cur.execute(
        """SELECT id FROM public.geo_observation_events
            WHERE source_type='paid_diagnosis' AND source_record_id=%s AND processing_state <> 'withdrawn'""",
        (str(run_token),),
    )
    ids = [row["id"] for row in cur.fetchall()]
    n = 0
    for eid in ids:
        if repository.withdraw_event(cur, eid, reason_codes=reasons):
            write_audit(cur, "withdraw", "system", event_id=eid, reason_codes=reasons,
                        idempotency_token="withdraw")
            n += 1
    return n


def run_reconciler(
    *,
    register_lookback_hours: int = 24,
    batch_limit: int = 500,
    registration_enabled: bool = True,
) -> dict:
    """一轮 reconcile；关闭 ingest 时仍执行撤回与保留期维护。"""
    out = {}
    if registration_enabled:
        with get_db() as conn:
            out["registration"] = reconcile_registration(
                conn.cursor(),
                lookback_hours=register_lookback_hours,
                batch_limit=batch_limit,
            )
    else:
        out["registration"] = {
            "paid_diagnosis": 0,
            "recurring_monitoring": 0,
            "research_round": 0,
            "enabled": False,
        }
    with get_db() as conn:
        out["withdrawals"] = reconcile_withdrawals(conn.cursor(), batch_limit=batch_limit)
    with get_db() as conn:
        out["retention"] = reconcile_retention(conn.cursor(), batch_limit=batch_limit)
    logger.info("reconciler round: %s", out)
    return out
