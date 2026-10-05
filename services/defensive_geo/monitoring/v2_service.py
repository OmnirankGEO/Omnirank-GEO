"""§15.9.3 v2 监测服务层 —— **只读投影 + admission**,不含执行器。

🔴 §15.9.3 冻结的是「复用哪条现役路」,禁的是**平行执行器**
------------------------------------------------------------
逐字:「防止实现者为 v2 新建平行激活、写作或**监测执行器**」。

所以本模块的边界很清楚:

* ✅ 允许:enrollment 解析、admission 判定、plan cell 枚举、进度投影、
  attempt 摘要、D0/D30 入参 —— 这些都是**读**与**判**;
* ❌ 禁止:任何 freeze / outbox / provider 调用 / 任务创建。
  真正的执行永远走现役 ``POST /api/monitoring/run`` 那一条。

判据 `test_v2_service_has_no_executor_sink` 用 AST 把这条钉死。

🔴 enrollment 只认服务端 snapshot schema
----------------------------------------
复用窗B 已落地的 ``commercial_milestones.is_v2_enrolled``:
只有 ``pricing_snapshot.delivery_plan.schema_version == 'geo-delivery-plan-v1'``
才算 enrolled。**请求体里的 mode 一个字都不读**(§15.9.1 逐字)。

这一条同时也是「新门不会误伤 legacy」的**结构性**保证:
legacy 报价的 snapshot 里根本没有 ``delivery_plan`` 这一格,
所以它永远走不进 v2 分支 —— 不是靠我们记得写 else。
"""

from __future__ import annotations

from typing import Any, Mapping, NamedTuple, Sequence

from services.defensive_geo.commercial_milestones import is_v2_enrolled
from services.defensive_geo.monitoring import attempt_ledger as _ledger
from services.defensive_geo.monitoring import enrollment as _adm
from services.defensive_geo.monitoring import lineage as _lin

V2_SERVICE_VERSION = "defgeo-monitoring-v2-service-v1"


class V2ServiceError(ValueError):
    """入参不足以判断。**不放行**,而不是默认放行。"""


class MonitoringEnrollment(NamedTuple):
    is_v2: bool
    quote_id: int | None
    service_projection_id: str | None
    service_projection_version: int | None
    campaign_mode: str | None


LEGACY_ENROLLMENT = MonitoringEnrollment(False, None, None, None, None)


def resolve_monitoring_enrollment(cur, *, brand_id: int) -> MonitoringEnrollment:
    """服务端 enrollment 解析。查不到就 **LEGACY**,不猜。

    🔴 这个函数是「新门会不会误伤 legacy」的唯一分流点。
       它只读 ``quote_pricing_snapshots.pricing_snapshot`` 的 schema version;
       legacy 报价没有那一格 ⇒ 恒 LEGACY。
    """
    try:
        cur.execute(
            """
            SELECT s.quote_id, s.pricing_snapshot
              FROM public.quote_pricing_snapshots s
              JOIN public.quotes q ON q.id = s.quote_id
             WHERE q.brand_id = %s
             ORDER BY s.id DESC
             LIMIT 1
            """,
            (int(brand_id),),
        )
        row = cur.fetchone()
    except Exception:
        # 表缺失/查询失败一律按 legacy —— MIG-04「schema 缺失只关闭 v2,
        # legacy 主链继续」。**绝不**因为 v2 侧查不到就拦住现役监测。
        return LEGACY_ENROLLMENT

    if not row:
        return LEGACY_ENROLLMENT
    snapshot = row["pricing_snapshot"] if isinstance(row, Mapping) else row[1]
    if not is_v2_enrolled(snapshot):
        return LEGACY_ENROLLMENT

    plan = (snapshot or {}).get("delivery_plan") or {}
    return MonitoringEnrollment(
        is_v2=True,
        quote_id=int(row["quote_id"] if isinstance(row, Mapping) else row[0]),
        service_projection_id=plan.get("service_projection_id"),
        service_projection_version=plan.get("service_projection_version"),
        campaign_mode=plan.get("campaign_mode"),
    )


def admit_monitoring_run(
    cur,
    *,
    brand_id: int,
    service_activated: bool = False,
    monitoring_budget_available: bool = False,
    budget_within_cap: bool = True,
    approval_satisfied: bool = True,
) -> tuple[MonitoringEnrollment, Any]:
    """§15.9.3 的两方向 admission。

    返回 ``(enrollment, decision)``。legacy 一律 ``admitted=True`` 且
    ``path`` 落在三条 legacy 路径之一 —— 现役计费/幂等**一个字都不动**。
    """
    enr = resolve_monitoring_enrollment(cur, brand_id=brand_id)
    decision = _adm.admit(
        is_v2_enrolled=enr.is_v2,
        legacy_path="legacy_monitoring_run",
        service_activated=service_activated,
        monitoring_budget_available=monitoring_budget_available,
        budget_within_cap=budget_within_cap,
        approval_satisfied=approval_satisfied,
    )
    return enr, decision


# ══════════════════════════════════════════════════════════════════════
# plan cell 枚举 —— 把「标量 planned_cells」变成**可点数的集合**
# ══════════════════════════════════════════════════════════════════════

def enumerate_plan_cells(
    *,
    run_authority_id: str,
    tenant_owner_id: int,
    enrollment: MonitoringEnrollment,
    questions: Sequence[Mapping[str, Any]],
    platform_keys: Sequence[str],
    question_set_revision: str,
    route_plan_revision: str,
    run_index: int = 1,
    planned_surface: str = "ai_search",
    search_mode: str = "ai_search",
) -> list[_lin.PlanCell]:
    """题单 × 平台 × 轮次 ⇒ 逐格。

    🔴 平台**去重后**再乘。窗A 现役 preview 用的是
       ``total_count * len(body.platform_keys)``,``platform_keys`` 无去重,
       传 ``["doubao","doubao"]`` 会让格数与**冻结的算力**双倍
       (见交付单 §10.2 挂号)。本函数按去重集合枚举,
       所以 ``len(enumerate_plan_cells(...))`` 是**真格数**;
       判据 `test_duplicate_platform_keys_do_not_double_the_cells`
       把这条差异钉住 —— 它同时是那个存量缺陷的回归判据。
    """
    platforms = sorted(set(str(p) for p in platform_keys if str(p)))
    if not platforms:
        raise V2ServiceError("platform_keys 去重后为空 —— 没有可测的平台")
    cells: list[_lin.PlanCell] = []
    for q in questions:
        for platform in platforms:
            cells.append(_lin.build_plan_cell(
                run_authority_id=run_authority_id,
                tenant_owner_id=int(tenant_owner_id),
                service_projection_id=enrollment.service_projection_id,
                service_projection_version=enrollment.service_projection_version,
                plan_item_key=q.get("plan_item_key"),
                question_set_revision=question_set_revision,
                question_identity_key=str(q["question_identity_key"]),
                question_revision=int(q["question_revision"]),
                family=str(q["family_key"]),
                public_platform=platform,
                planned_surface=planned_surface,
                search_mode=search_mode,
                scheduled_window=None,
                run_index=int(run_index),
                route_plan_revision=route_plan_revision,
            ))
    return cells


# ══════════════════════════════════════════════════════════════════════
# MON-11 进度投影 —— attempted / terminal / progressPct **可复算**
# ══════════════════════════════════════════════════════════════════════

class RunProgress(NamedTuple):
    planned_cells: int
    attempted_cells: int
    terminal_cells: int
    policy_skipped_cells: int
    attempt_records: int
    attempt_errors: tuple[Mapping[str, Any], ...]
    progress_pct: float

    def assert_recomputable(self) -> None:
        """MON-11 逐字:「progressPct **可复算**」+ completed 必须 100。

        判据不接受"服务端算了个数下发" —— 它自己按同一公式重算一遍,
        对不上就红。
        """
        if self.planned_cells <= 0:
            if self.progress_pct != 0.0:
                raise V2ServiceError("零计划格却有进度")
            return
        expect = round(self.terminal_cells * 100.0 / self.planned_cells, 4)
        if abs(self.progress_pct - expect) > 1e-9:
            raise V2ServiceError(
                f"progressPct={self.progress_pct} 与 terminal/planned 复算值 "
                f"{expect} 不符(MON-11「progressPct 可复算」)")
        if self.terminal_cells > self.planned_cells:
            raise V2ServiceError(
                f"terminal({self.terminal_cells}) > planned({self.planned_cells})")
        if self.attempted_cells > self.planned_cells:
            raise V2ServiceError("attempted 不得超过 planned")


def project_run_progress(
    cur, *, plan_cell_ids: Sequence[str]
) -> RunProgress:
    """从 attempt 账本重建进度。**不读任何缓存计数列**。

    🔴 MON-11 的三个坑各有一句对应实现:
       · skipped 无 provider attempt ⇒ 计 terminal **不计** attempted;
       · attemptRecords 与真实调用数一致 ⇒ 直接 len(attempts);
       · error/skipped 格有 reason ⇒ attemptErrors 逐条带 code。
    """
    planned = len(set(plan_cell_ids))
    attempted = terminal = skipped = records = 0
    errors: list[Mapping[str, Any]] = []
    for pcid in sorted(set(plan_cell_ids)):
        attempts = _ledger.attempts_for_cell(cur, plan_cell_id=pcid)
        facts = _ledger.cell_denominator_facts(attempts)
        records += int(facts["attemptRecords"])
        if facts["isAttempted"]:
            attempted += 1
        if facts["isTerminal"]:
            terminal += 1
        if facts["canonicalState"] == "policy_skipped":
            skipped += 1
        for err in facts["attemptErrors"]:
            errors.append({"planCellId": pcid, **err})

    pct = round(terminal * 100.0 / planned, 4) if planned else 0.0
    progress = RunProgress(
        planned_cells=planned, attempted_cells=attempted, terminal_cells=terminal,
        policy_skipped_cells=skipped, attempt_records=records,
        attempt_errors=tuple(errors), progress_pct=pct,
    )
    progress.assert_recomputable()
    return progress


def census() -> dict[str, Any]:
    return {
        "serviceVersion": V2_SERVICE_VERSION,
        "enrollmentPredicate":
            "services.defensive_geo.commercial_milestones.is_v2_enrolled",
        "admissionReasons": list(_adm.ADMISSION_REASONS),
        "legacyPaths": list(_adm.LEGACY_PATHS),
    }
