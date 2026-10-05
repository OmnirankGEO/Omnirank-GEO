"""
freeze_sweeper · zombie point_freezes 兜底扫描

来源:Deploy-CTO 2026-05-22 D0 实证发现
  - point_freezes 表 8 笔历史 zombie · 最早 2026-04-22 · 最新 2026-05-22 · 4 用户 5460 积分永久冻结
  - 各业务异常分支有 release_freeze · 但**通用 sweep job 在 prod 0 跑**
  - 项目地图 v2 §3.9 F7 老板预期"sweep job 1h 自动 release"未落地

设计:
  - 独立模块 · 不动 middleware/billing.py(🔴 A 级官方红线)
  - 调用现有公开接口 middleware.billing.release_freeze · 复用全套幂等 + 流水
  - 阈值 STALE_HOURS=12 默认 · 兼顾长任务(诊断 ~5min · 监测月包 24h+ · GEO 方案 ~108s)
    · 12h 安全余量:即便最长 monitor_run 也只跑 1-2h · 12h 仍 frozen = 真 zombie
  - 每小时整点跑 · 由 api/scheduler.py 注册

不动:
  - middleware/billing.py(A 级红线)
  - point_freezes schema(直接 SELECT · 不改字段)
  - release_freeze 函数本体(只调用)

红线判断(memory feedback_full_authorization_llm_first):
  - 非"极其危险" · 不报告先动
  - sweep 调 release_freeze 公开接口 · release 本身已通过 V3.3.1 Codex P1-9 审 · 安全
  - 频率每小时 1 次 · 单批 LIMIT 100 · 不打 DB

历史 zombie 8 笔由 Deploy-CTO 手动 SQL release(本 PR 含 scripts/sql/release_zombie_freezes_2026-05-22.sql)
新 zombie 由本 cron 自动兜底 · 不再累积

2026-05-22 · CTO-15.23 GEO 主业 · 上线前最后一轮检测
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List

from db.connection import get_db

logger = logging.getLogger("GEO-Freeze-Sweeper")

STALE_HOURS_DEFAULT = 12
BATCH_LIMIT = 100


async def reconcile_stale_monitoring_keyword_settlements(
    stale_hours: int = STALE_HOURS_DEFAULT,
) -> Dict[str, int]:
    """Settle hard-killed daily keywords from durable dispatch and freeze evidence."""
    from db.monitoring_db import (
        list_stale_monitoring_keyword_settlements,
        find_monitoring_freeze_for_settlement,
        record_monitoring_subscription_charge_for_settlement,
        release_subscription_claim,
        settle_monitoring_keyword_reference,
    )
    from middleware.billing import commit_freeze, release_freeze

    rows = await asyncio.to_thread(
        list_stale_monitoring_keyword_settlements, int(stale_hours)
    )
    summary = {"scanned": len(rows), "committed": 0, "released": 0, "failed": 0}
    for row in rows[:BATCH_LIMIT]:
        reference = str(row["settlement_reference"])
        try:
            dispatched = bool(row.get("provider_dispatched"))
            user_id = row.get("billing_user_id")
            freeze_id = row.get("freeze_id")
            freeze_table = row.get("freeze_table")
            if user_id and not freeze_id:
                recovered_freeze = await asyncio.to_thread(
                    find_monitoring_freeze_for_settlement, reference, int(user_id)
                )
                if recovered_freeze:
                    freeze_id = recovered_freeze.get("id")
                    freeze_table = recovered_freeze.get("freeze_table")
            if dispatched or row.get("state") == "committed":
                if row.get("state") not in {"committed", "admin_covered"} and user_id:
                    result = await commit_freeze(
                        freeze_id=int(freeze_id) if freeze_id else None,
                        task_ref=reference,
                        user_id=int(user_id),
                        freeze_table=freeze_table,
                        reason="每日监测硬崩恢复：供应商已发送",
                    )
                    if result.get("success") is False:
                        raise RuntimeError(result.get("reason") or "freeze commit failed")
                    await asyncio.to_thread(
                        settle_monitoring_keyword_reference, reference, "committed"
                    )
                elif row.get("state") != "committed":
                    await asyncio.to_thread(
                        settle_monitoring_keyword_reference, reference, "admin_covered"
                    )
                await asyncio.to_thread(
                    record_monitoring_subscription_charge_for_settlement, reference
                )
                summary["committed"] += 1
                continue

            if user_id and freeze_id:
                result = await release_freeze(
                    freeze_id=int(freeze_id) if freeze_id else None,
                    task_ref=reference,
                    user_id=int(user_id),
                    freeze_table=freeze_table,
                    reason="每日监测硬崩恢复：供应商未发送",
                )
                if result.get("success") is False:
                    raise RuntimeError(result.get("reason") or "freeze release failed")
            if row.get("subscription_id") and row.get("claim_token") is not None:
                released = await asyncio.to_thread(
                    release_subscription_claim,
                    int(row["subscription_id"]),
                    row.get("claim_token"),
                    row.get("previous_claim_at"),
                )
                if not released:
                    raise RuntimeError("subscription claim release fence lost")
            await asyncio.to_thread(
                settle_monitoring_keyword_reference, reference, "released"
            )
            summary["released"] += 1
        except Exception as exc:
            summary["failed"] += 1
            logger.error(
                "[FreezeSweeper] monitoring settlement reconcile failed ref=%s: %s",
                reference, exc,
            )
    return summary


async def reconcile_stale_monitoring_task_settlements(
    stale_hours: int = STALE_HOURS_DEFAULT,
) -> Dict[str, int]:
    """Recover hard-killed batch/scheduled/SSE freezes from their task cell fence."""
    from db.monitoring_db import (
        find_monitoring_task_freeze,
        list_stale_monitoring_task_settlements,
        recover_abandoned_monitoring_task_execution,
        refresh_monitoring_task_from_cells,
        revoke_monitoring_task_coverage_for_organization_refund,
        set_monitoring_task_fulfillment_state,
    )
    from middleware.billing import commit_freeze, release_freeze

    rows = await asyncio.to_thread(
        list_stale_monitoring_task_settlements, int(stale_hours)
    )
    summary = {
        "scanned": len(rows), "committed": 0, "released": 0,
        "execution_recovered": 0, "admin_recovered": 0,
        "organization_projected": 0, "organization_skipped": 0, "failed": 0,
    }
    for row in rows[:BATCH_LIMIT]:
        reference = str(row["settlement_reference"])
        try:
            if int(row.get("task_count") or 0) != 1:
                raise RuntimeError("monitoring settlement reference spans multiple tasks")
            task_id = int(row["task_id"])
            execution_abandoned = bool(row.get("execution_abandoned"))
            if bool(row.get("all_admin_covered")):
                if execution_abandoned:
                    await asyncio.to_thread(
                        recover_abandoned_monitoring_task_execution, task_id
                    )
                    await asyncio.to_thread(refresh_monitoring_task_from_cells, task_id)
                    summary["execution_recovered"] += 1
                summary["admin_recovered"] += 1
                continue
            freeze = await asyncio.to_thread(find_monitoring_task_freeze, reference)
            if not freeze:
                if bool(row.get("provider_dispatched")):
                    raise RuntimeError(
                        "monitoring task dispatched without readable freeze evidence"
                    )
                # Tasks and cells are durable before freeze_points. A kill in
                # that window has no money leg to release, so terminalize the
                # abandoned execution instead of retrying it forever.
                if execution_abandoned:
                    await asyncio.to_thread(
                        recover_abandoned_monitoring_task_execution, task_id
                    )
                    summary["execution_recovered"] += 1
                await asyncio.to_thread(
                    set_monitoring_task_fulfillment_state, task_id, "released"
                )
                await asyncio.to_thread(refresh_monitoring_task_from_cells, task_id)
                summary["released"] += 1
                continue
            if bool(freeze.get("organization_linked")):
                # The organization ledger exclusively owns money movement.  Its
                # terminal states are nevertheless authoritative projection facts
                # for this monitoring task; non-terminal states remain quarantined.
                if execution_abandoned:
                    await asyncio.to_thread(
                        recover_abandoned_monitoring_task_execution, task_id
                    )
                    await asyncio.to_thread(refresh_monitoring_task_from_cells, task_id)
                    summary["execution_recovered"] += 1
                organization_status = str(freeze.get("organization_status") or "")
                if organization_status in {"committed", "released", "refunded"}:
                    projected_state = (
                        "covered" if organization_status == "committed" else "released"
                    )
                    if organization_status == "refunded":
                        await asyncio.to_thread(
                            revoke_monitoring_task_coverage_for_organization_refund,
                            task_id,
                            int(freeze["organization_charge_id"]),
                        )
                    else:
                        await asyncio.to_thread(
                            set_monitoring_task_fulfillment_state, task_id, projected_state
                        )
                    await asyncio.to_thread(refresh_monitoring_task_from_cells, task_id)
                    summary["organization_projected"] += 1
                    continue
                summary["organization_skipped"] += 1
                logger.warning(
                    "[FreezeSweeper] monitoring task ref=%s delegated to organization ledger status=%s",
                    reference, organization_status or "unreadable",
                )
                continue
            billing_args = {
                "freeze_id": int(freeze["id"]),
                "task_ref": reference,
                "user_id": int(freeze["billing_user_id"]),
                "freeze_table": freeze["freeze_table"],
            }
            if bool(row.get("provider_dispatched")):
                result = await commit_freeze(
                    **billing_args,
                    reason="监测任务硬崩恢复：供应商已发送",
                )
                if result.get("success") is False:
                    raise RuntimeError(result.get("reason") or "freeze commit failed")
                if execution_abandoned:
                    await asyncio.to_thread(
                        recover_abandoned_monitoring_task_execution, task_id
                    )
                await asyncio.to_thread(
                    set_monitoring_task_fulfillment_state, task_id, "covered"
                )
                await asyncio.to_thread(
                    refresh_monitoring_task_from_cells, task_id
                )
                if execution_abandoned:
                    summary["execution_recovered"] += 1
                summary["committed"] += 1
            else:
                result = await release_freeze(
                    **billing_args,
                    reason="监测任务硬崩恢复：供应商未发送",
                )
                if result.get("success") is False:
                    raise RuntimeError(result.get("reason") or "freeze release failed")
                if execution_abandoned:
                    await asyncio.to_thread(
                        recover_abandoned_monitoring_task_execution, task_id
                    )
                await asyncio.to_thread(
                    set_monitoring_task_fulfillment_state, task_id, "released"
                )
                await asyncio.to_thread(
                    refresh_monitoring_task_from_cells, task_id
                )
                if execution_abandoned:
                    summary["execution_recovered"] += 1
                summary["released"] += 1
        except Exception as exc:
            summary["failed"] += 1
            logger.error(
                "[FreezeSweeper] monitoring task reconcile failed ref=%s: %s",
                reference, exc,
            )
    return summary


async def sweep_zombie_freezes(stale_hours: int = STALE_HOURS_DEFAULT, dry_run: bool = False) -> Dict[str, Any]:
    """扫 point_freezes status='frozen' 且 created_at < NOW() - stale_hours 自动 release。

    Args:
        stale_hours: 多久未 commit/release 算 zombie(默认 12)
        dry_run: 只扫不动(测试用)

    Returns:
        {scanned, released, failed, sample_ids, total_amount_released}
    """
    monitoring_reconciliation = {"scanned": 0, "committed": 0, "released": 0, "failed": 0}
    if not dry_run:
        try:
            monitoring_reconciliation = await reconcile_stale_monitoring_keyword_settlements(
                stale_hours=stale_hours
            )
        except Exception as exc:
            logger.error("[FreezeSweeper] monitoring keyword settlement ledger unreadable: %s", exc)
        try:
            task_reconciliation = await reconcile_stale_monitoring_task_settlements(
                stale_hours=stale_hours
            )
            monitoring_reconciliation.update({
                f"task_{key}": value for key, value in task_reconciliation.items()
            })
        except Exception as exc:
            logger.error("[FreezeSweeper] monitoring task settlement ledger unreadable: %s", exc)

    # 1. SELECT zombie 候选(只读 · 不锁表)· 两表:legacy point_freezes + V3.5 customer_credit_freezes(批2B)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, user_id, feature_code, amount_total, created_at, task_ref, brand_id
            FROM point_freezes pf
            WHERE pf.status = 'frozen'
              AND pf.created_at < CURRENT_TIMESTAMP - (%s || ' hours')::INTERVAL
              AND NOT EXISTS (
                    SELECT 1
                      FROM public.organization_charge_links charge
                     WHERE charge.physical_backend='legacy_user_wallet'
                       AND charge.physical_freeze_id=pf.id::text
                       AND charge.status IN ('reserved','unknown','refund_pending')
              )
              AND NOT EXISTS (
                    SELECT 1
                      FROM public.monitoring_run_cells cell
                     WHERE cell.settlement_reference=pf.task_ref
                       AND cell.provider_dispatched_at IS NOT NULL
                       AND cell.fulfillment_state IN ('reserved','coverage_unknown')
              )
              AND NOT EXISTS (
                    SELECT 1
                      FROM public.monitoring_keyword_settlements settlement
                     WHERE settlement.settlement_reference=pf.task_ref
                       AND settlement.state NOT IN ('committed','released','admin_covered')
              )
            ORDER BY pf.id ASC
            LIMIT %s
            """,
            (str(int(stale_hours)), BATCH_LIMIT),
        )
        zombies: List[Dict[str, Any]] = cur.fetchall() or []
        for z in zombies:
            z["_v35"] = False
        # [A0/A1 现行口径] V3.5 客户冻结(customer_credit_freezes)· 扫描即知表 → 下方 release 显式回传
        # freeze_table(_v35→'v35'/'legacy')免猜;两段都传真实 user_id(customer_user_id/user_id)做 user-scope
        # 约束。(旧"legacy 段不传 user_id 防误路由"口径已废:不传 None 反而退化非 user-scope·撞号才误路由。)
        try:
            cur.execute(
                """
                SELECT id, customer_user_id AS user_id, feature_code, amount_total,
                       created_at, task_ref, brand_id
                FROM customer_credit_freezes ccf
                WHERE ccf.status = 'frozen'
                  AND ccf.created_at < CURRENT_TIMESTAMP - (%s || ' hours')::INTERVAL
                  AND NOT EXISTS (
                        SELECT 1
                          FROM public.monitoring_run_cells cell
                         WHERE cell.settlement_reference=ccf.task_ref
                           AND cell.provider_dispatched_at IS NOT NULL
                           AND cell.fulfillment_state IN ('reserved','coverage_unknown')
                  )
                  AND NOT EXISTS (
                        SELECT 1
                          FROM public.monitoring_keyword_settlements settlement
                         WHERE settlement.settlement_reference=ccf.task_ref
                           AND settlement.state NOT IN ('committed','released','admin_covered')
                  )
                ORDER BY ccf.id ASC
                LIMIT %s
                """,
                (str(int(stale_hours)), BATCH_LIMIT),
            )
            v35_zombies: List[Dict[str, Any]] = cur.fetchall() or []
            for z in v35_zombies:
                z["_v35"] = True
            zombies = list(zombies) + list(v35_zombies)
        except Exception as e:
            # [#3] 关键:query2 抛错(customer_credit_freezes 表未建/异常)会让本【非 autocommit】事务
            #   进入 InFailedSqlTransaction(db/connection.py 强制 autocommit=False)。若不 rollback,
            #   下方 query3(自助调研活跃/待人工冻结排除)的 execute 必抛 'current transaction is aborted'
            #   被静默吞 → active_selfserve_refs 空 → needs_manual/needs_recharge/活跃 selfres 冻结全部漏出
            #   被当 12h 僵尸 release = 免费送。两查询皆只读 SELECT,rollback 不丢数据(zombies 已在内存)。
            try:
                conn.rollback()
            except Exception:
                pass
            # 新表未建(migration 未跑)时不崩 · legacy 仍兜底
            logger.warning(f"[FreezeSweeper] customer_credit_freezes 扫描跳过(表可能未建): {e}")

        # Organization reservations reuse the legacy physical wallet freeze,
        # but their charge/limit/plan evidence has a stricter state machine.
        # In particular, status='unknown' deliberately holds the wallet after
        # an external response was lost. The generic age-based sweeper must
        # never release those physical legs behind the organization's back.
        try:
            cur.execute(
                """
                SELECT physical_freeze_id
                FROM organization_charge_links
                WHERE physical_backend='legacy_user_wallet'
                  AND status IN ('reserved','unknown','refund_pending')
                  AND physical_freeze_id IS NOT NULL
                """
            )
            protected_organization_freezes = {
                int(row["physical_freeze_id"])
                for row in (cur.fetchall() or [])
                if str(row.get("physical_freeze_id") or "").isdigit()
            }
            if protected_organization_freezes:
                zombies = [
                    row for row in zombies
                    if row.get("_v35") or int(row["id"]) not in protected_organization_freezes
                ]
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            if getattr(e, "pgcode", None) != "42P01":
                # A present-but-unreadable organization ledger means the
                # sweeper cannot prove any legacy freeze is safe to release.
                # Keep all legacy candidates frozen and retry next cycle.
                zombies = [row for row in zombies if row.get("_v35")]
                logger.error(
                    "[FreezeSweeper] 组织预留排除查询失败·fail-closed 跳过全部 legacy 冻结: %s",
                    e,
                )

        # Monitoring plans bind the billing task_ref before the first provider
        # boundary. A hard-killed worker can therefore leave a valid durable
        # result/dispatch fence while its freeze is still old and frozen. Such
        # rows require reconciliation/commit and must never be age-released.
        monitoring_lookup_ok = True
        protected_monitoring_refs: set = set()
        try:
            cur.execute(
                "SELECT pg_catalog.to_regclass('public.monitoring_run_cells') AS relation"
            )
            relation_row = cur.fetchone()
            if relation_row and relation_row.get("relation") is not None:
                cur.execute(
                    """
                    SELECT DISTINCT settlement_reference
                      FROM public.monitoring_run_cells
                     WHERE settlement_reference IS NOT NULL
                       AND provider_dispatched_at IS NOT NULL
                       AND fulfillment_state IN ('reserved','coverage_unknown')
                    """
                )
                protected_monitoring_refs = {
                    str(row["settlement_reference"])
                    for row in (cur.fetchall() or [])
                    if row.get("settlement_reference")
                }
                cur.execute(
                    """
                    SELECT settlement_reference
                      FROM public.monitoring_keyword_settlements
                     WHERE state NOT IN ('committed','released','admin_covered')
                    """
                )
                protected_monitoring_refs.update(
                    str(row["settlement_reference"])
                    for row in (cur.fetchall() or [])
                    if row.get("settlement_reference")
                )
        except Exception as e:
            monitoring_lookup_ok = False
            try:
                conn.rollback()
            except Exception:
                pass
            logger.error(
                "[FreezeSweeper] monitoring dispatch exclusion unreadable; "
                "fail-closed for monitoring freezes: %s",
                e,
            )
        if monitoring_lookup_ok and protected_monitoring_refs:
            zombies = [
                row for row in zombies
                if str(row.get("task_ref") or "") not in protected_monitoring_refs
            ]
        elif not monitoring_lookup_ok:
            monitoring_prefixes = (
                "monitor_stream_", "monitor_run_", "batch_mon_", "sched_mon_",
                "monitoring_daily:",
            )
            zombies = [
                row for row in zombies
                if not str(row.get("task_ref") or "").startswith(monitoring_prefixes)
            ]

        # [R#15 + P0-2] 排除「自助调研非僵尸」的冻结,两类:
        #   (1) 活跃任务:selfres_{task_id} 且 geo_research_selfserve_queue 仍 pending/queued/running —— 这类
        #       可能因反复 RoundAlreadyRunning 退回 queued、freeze 合法冻着数小时;若被 12h sweeper 提前 release,
        #       worker 后续 commit_freeze 会命中 released 幂等 no-op → 榜点亮但 0 扣费 = 免费送。
        #   (2) [P0-2] 待人工/待补扣任务:failed_reason 含 'needs_manual'(billing_commit_failed_needs_manual)
        #       或 'needs_recharge'(billing_swept_needs_recharge)—— 榜已点亮、commit 未确认成功,冻结【被有意保留
        #       frozen】等人工补扣 / 重试幂等 commit。若被 sweeper 自动 release,钱退了榜还亮 = 免费送。
        #   这两类冻结均交由 worker 结算 / 人工处理,sweeper 不越俎。
        active_selfserve_refs: set = set()
        # [GEO-R2-CAN-011] 排除查询是否成功。失败(表未建/事务中止)时不能把空排除集当"无活跃任务",
        #   否则 130 行 `if active_selfserve_refs:` 为 False → 不过滤 → 活跃/待人工 selfres_ 冻结全被
        #   当 12h 僵尸 release = 免费送(fail-open)。下方改为 fail-closed:查失败即保守跳过所有 selfres_ 冻结。
        exclusion_lookup_ok = True
        try:
            # 无参数 execute → psycopg2 不做 %-替换,LIKE 里单 % 即字面通配(不需 %% 转义)。
            cur.execute(
                "SELECT id FROM geo_research_selfserve_queue "
                "WHERE status IN ('pending', 'queued', 'running') "
                "   OR failed_reason LIKE '%needs_manual%' "
                "   OR failed_reason LIKE '%needs_recharge%'"
            )
            active_selfserve_refs = {f"selfres_{r['id']}" for r in (cur.fetchall() or [])}
        except Exception as e:
            # [#3] 防御性 rollback:让连接脱离潜在失败态(query2 已 rollback,此处再兜一次),
            #   避免污染 get_db 收尾 commit。
            try:
                conn.rollback()
            except Exception:
                pass
            # [GEO-R2-CAN-011] 排除查询失败 → 无法判定哪些 selfres_ 冻结活跃/待人工 → 置 fail-closed 标记,
            #   下方保守跳过所有 selfres_ 冻结(绝不 release),交下轮 sweep 或 worker/人工结算。
            #   非 selfres_ 的普通 zombie 不受影响,仍正常兜底 release。
            exclusion_lookup_ok = False
            # 表未建(migration 未跑)时不崩 · 其余冻结仍正常兜底
            logger.warning(f"[FreezeSweeper] 自助调研活跃任务扫描跳过(表可能未建 → selfres_ 冻结本轮全跳过): {e}")
        if not exclusion_lookup_ok:
            # [GEO-R2-CAN-011] fail-closed:排除查询失败,保守剔除所有 selfres_ 冻结,防止活跃/待人工任务冻结被误 release。
            _before_n = len(zombies)
            zombies = [z for z in zombies if not (z.get("task_ref") or "").startswith("selfres_")]
            _skipped = _before_n - len(zombies)
            if _skipped:
                logger.warning(
                    f"[FreezeSweeper] 排除查询失败 · fail-closed 跳过 {_skipped} 笔 selfres_ 冻结(不 release,交下轮/人工)"
                )
        elif active_selfserve_refs:
            _before_n = len(zombies)
            zombies = [z for z in zombies if (z.get("task_ref") or "") not in active_selfserve_refs]
            _skipped = _before_n - len(zombies)
            if _skipped:
                logger.info(
                    f"[FreezeSweeper] 跳过 {_skipped} 笔自助调研非僵尸冻结(活跃/待人工补扣,交 worker 结算)"
                )

    scanned = len(zombies)
    if not zombies:
        logger.debug(f"[FreezeSweeper] 0 zombie · stale_hours={stale_hours}")
        return {
            "scanned": 0,
            "released": 0,
            "failed": 0,
            "sample_ids": [],
            "total_amount_released": 0,
            "dry_run": dry_run,
            "monitoring_reconciliation": monitoring_reconciliation,
        }

    sample_ids = [int(z["id"]) for z in zombies[:10]]
    total_amount = sum(int(z["amount_total"] or 0) for z in zombies)

    if dry_run:
        logger.warning(
            f"[FreezeSweeper] dry_run · scanned={scanned} sample_ids={sample_ids} total_amount={total_amount}"
        )
        return {
            "scanned": scanned,
            "released": 0,
            "failed": 0,
            "sample_ids": sample_ids,
            "total_amount_released": 0,
            "dry_run": True,
            "monitoring_reconciliation": monitoring_reconciliation,
            "would_release_ids": [int(z["id"]) for z in zombies],
        }

    # 2. 逐个 release(用 release_freeze 公开接口 · 幂等)
    from middleware.billing import release_freeze

    released = 0
    failed = 0
    released_amount = 0
    failed_ids: List[int] = []

    for fz in zombies:
        fz_id = int(fz["id"])
        try:
            # [A1] 两条路径都传真实 user_id:freeze_id 跨 point_freezes/customer_credit_freezes 两表独立自增
            # 必撞号,legacy 丢成 None 反而会让 _route_freeze_table 不带 user 约束 → 撞号误路由到别客户的
            # v35 行(跨客户错退最毒一条)。带 user_id 后路由/加锁始终 user-scoped,跨客户错退消除。
            uid = fz.get("user_id")
            # [A0] sweeper 扫描时即知冻结所在表(_v35 标记)→ 显式回传 freeze_table 免猜,
            # 根除 freeze_id 跨表撞号歧义(sweeper 按 id 遍历两表 · 撞号暴露面最大,最该用显式标记)。
            ftable = "v35" if fz.get("_v35") else "legacy"
            reason = (
                f"[FreezeSweeper] 12h+ zombie 自动 release · "
                f"feature={fz['feature_code']} created={fz['created_at']}"
            )
            r = await release_freeze(freeze_id=fz_id, user_id=uid, reason=reason, freeze_table=ftable)
            if r.get("success"):
                released += 1
                released_amount += int(fz["amount_total"] or 0)
                logger.warning(
                    f"[FreezeSweeper] released id={fz_id} user={fz['user_id']} "
                    f"feature={fz['feature_code']} amount={fz['amount_total']} "
                    f"frozen_since={fz['created_at']}"
                )
            else:
                failed += 1
                failed_ids.append(fz_id)
                logger.error(
                    f"[FreezeSweeper] release failed id={fz_id} reason={r.get('reason')}"
                )
        except Exception as e:
            failed += 1
            failed_ids.append(fz_id)
            logger.exception(f"[FreezeSweeper] release exception id={fz_id}: {e}")

    if released > 0 or failed > 0:
        logger.warning(
            f"[FreezeSweeper] cycle done · scanned={scanned} released={released} "
            f"failed={failed} released_amount={released_amount} failed_ids={failed_ids[:10]}"
        )

    return {
        "scanned": scanned,
        "released": released,
        "failed": failed,
        "sample_ids": sample_ids,
        "total_amount_released": released_amount,
        "failed_ids": failed_ids[:10],
        "dry_run": False,
        "monitoring_reconciliation": monitoring_reconciliation,
    }


async def run_freeze_sweep_hourly() -> Dict[str, Any]:
    """每小时整点 cron 入口 · 由 api/scheduler.py 调用。

    包装 sweep_zombie_freezes · 加 try/except 防 cron 整体崩。
    """
    try:
        result = await sweep_zombie_freezes(stale_hours=STALE_HOURS_DEFAULT, dry_run=False)
        return result
    except Exception as e:
        logger.exception(f"[FreezeSweeper] hourly cron exception: {e}")
        return {"scanned": 0, "released": 0, "failed": 0, "error": str(e)}
