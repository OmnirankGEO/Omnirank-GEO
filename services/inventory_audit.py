"""
V3.5 W4 · 库存对账服务

对账等式(每日 cron + admin manual trigger 跑)= **账实相符式**,不是枚举式:

    diff = SUM(agent_inventory_wallets.paid_inventory_points
               + bonus_inventory_points
               + frozen_inventory_points)          -- 账面三池(实)
         - SUM(agent_inventory_transactions.points)  -- 全量流水净额(账)

🔴 为什么不再枚举 type(2026-07-29 返修 · 工单 INVENTORY_AUDIT_EQUATION):
    老等式写成 `purchased + admin_adjust - allocated_out`,只统计
    ('purchase_prepay','purchase_auto') 等已知 type。任何**新增 type**(如
    manufacturer_origin_in 的 1.3 亿)天生落在等式外 → diff 恒为 129,999,870、
    has_drift 天天 true,资金链唯一安全网信噪比归零。补一次 origin_in 只是
    把下一次遗漏推后 —— 枚举式本身就是根因,故整体换成"钱包 vs 全量流水"。
    新增 type 不用改代码就自动进等式(判别锁 #2 就是钉这条)。

🔴 frozen_inventory_points 必须计入:冻结只是 paid → frozen 的**池内搬运**,
    不写流水。漏掉 frozen,一有冻结 diff 立刻转红(判别锁 #4 钉这条)。

⚪ platform_consumed / refunded_or_revoked / historical_purchased /
    historical_admin_adjust 仍然计算并落库作**审计留痕**,但一律**不进 diff**:
    前两者读的是已停写的 customer_credit_transactions(进 diff 会单向漂移),
    后两者是枚举式残留的分项快照(只作人看的明细,不作判定)。

publish pool 单独校验(不变):publish_credit 只能来自 paid_inventory ·
bonus 永不流入 publish。违规独立于金额 drift,一样让本次审计失败。

🔴 失败态(2026-07-29 返修):run_audit 抛异常时调用方必须
   record_audit_failure() 落 inventory_audit_runs 一行 status='failed'
   (且 has_drift=TRUE,让只看 has_drift 的老消费方也变红)+ 触发 ai_ops 告警。
   "对账程序自己死了" 必须比 "对上了" 更响 —— 静默死亡不能再发生。
"""
import json
import logging
from typing import Dict, Any, Optional, List

logger = logging.getLogger("GEO-V35-W4-Audit")

DRIFT_THRESHOLD = 1

# ai_ops 告警规则键(与 services/marketing/reconcile.py 同一告警管道)
ALERT_RULE_KEY = "inventory_audit"
ALERT_FP_DRIFT = "drift"
ALERT_FP_FAILURE = "run_failure"
# lot 维度独立 fingerprint:与金额 drift 分开看 —— 两者的处置方式完全不同
# (金额不平 = 流水漏写;lot 对不上 = 有链路只动聚合钱包不动批次)。
ALERT_FP_LOT_DRIFT = "lot_drift"
ALERT_FP_LOT_CHECK_FAILURE = "lot_drift_check_failure"


def run_audit(cursor, triggered_by: str = "cron", triggered_by_user_id: Optional[int] = None) -> Dict[str, Any]:
    """
    跑一次对账 · 写 inventory_audit_runs + 必要时 inventory_audit_diffs
    返回 run record dict
    """
    # 账面【实】· 代理库存三池 · frozen 必须计入(冻结是池内搬运 · 不写流水)
    cursor.execute("""
        SELECT COALESCE(SUM(paid_inventory_points), 0) AS paid,
               COALESCE(SUM(bonus_inventory_points), 0) AS bonus,
               COALESCE(SUM(frozen_inventory_points), 0) AS frozen
        FROM agent_inventory_wallets
    """)
    row = cursor.fetchone()
    agent_paid = int(_g(row, "paid", 0) or 0)
    agent_bonus = int(_g(row, "bonus", 0) or 0)
    agent_frozen = int(_g(row, "frozen", 0) or 0)
    wallet_total = agent_paid + agent_bonus + agent_frozen

    # 账面【账】· 全量流水净额 · 🔴 不带 WHERE type IN (...):
    #   任何新增 type 自动进等式,不会再出现"新 type 天生在等式外"的漏算。
    cursor.execute("""
        SELECT COALESCE(SUM(points), 0) AS s
        FROM agent_inventory_transactions
    """)
    ledger_total = int(_g(cursor.fetchone(), "s", 0) or 0)

    # 客户三池
    # [历史账本只读 · 2026-08-17] 该表自 2026-07-29 停写且三池已清零 → 本项恒 0。
    # 保留为守恒等式的历史项:删掉等式两边就不再对称,老区间对账会失真。
    cursor.execute("""
        SELECT COALESCE(SUM(tool_credit_points), 0) AS tool,
               COALESCE(SUM(publish_credit_points), 0) AS pub,
               COALESCE(SUM(bonus_credit_points), 0) AS bonus
        FROM customer_agent_credit_wallets
    """)
    row = cursor.fetchone()
    cust_tool = int(_g(row, "tool", 0) or 0)
    cust_publish = int(_g(row, "pub", 0) or 0)
    cust_bonus = int(_g(row, "bonus", 0) or 0)

    # 平台已消费【净额】= consume − refund · [BUG-P2] 原只数 type='consume',漏 type='refund'
    # (工具失败退费 tool_fail_refund / 长任务 release 退回客户额度)→ 每笔退费 consume 计入但退回未抵
    # → diff_total 永久 += 退费额,资金链唯一安全网(diff>1 告警)被退费持续打假漂移、掩盖真实漂移。
    # 改净额:refund(退回客户池)抵消对应 consume。(充值退款走 type='revoke',已单独计 refunded_or_revoked)
    cursor.execute("""
        SELECT COALESCE(SUM(CASE WHEN type='consume' THEN ABS(points)
                                 WHEN type='refund'  THEN -ABS(points) ELSE 0 END), 0) AS s
        FROM customer_credit_transactions
        WHERE type IN ('consume', 'refund')
    """)
    platform_consumed = int(_g(cursor.fetchone(), "s", 0) or 0)

    # ⚪ 分项快照(审计留痕 · 供人看明细)· 🔴 绝不进 diff:
    #    这两项就是老枚举式的残骸,它们天生统计不全(新 type 不在 IN 列表里)。
    #    留着是为了看得见"进货/调账各占多少",判定一律走 ledger_total。
    cursor.execute("""
        SELECT COALESCE(SUM(points), 0) AS s
        FROM agent_inventory_transactions
        WHERE type IN ('purchase_prepay', 'purchase_auto') AND points > 0
    """)
    historical_purchased = int(_g(cursor.fetchone(), "s", 0) or 0)

    cursor.execute("""
        SELECT COALESCE(SUM(points), 0) AS s
        FROM agent_inventory_transactions
        WHERE type IN ('purchase_admin_adjust', 'admin_adjust')
    """)
    admin_adjust = int(_g(cursor.fetchone(), "s", 0) or 0)

    # 退款 / revoke 流出
    cursor.execute("""
        SELECT COALESCE(SUM(ABS(points)), 0) AS s
        FROM customer_credit_transactions
        WHERE type = 'revoke'
    """)
    refunded_or_revoked = int(_g(cursor.fetchone(), "s", 0) or 0)

    # ============================================================
    # 对账等式 · 账实相符式(2026-07-29 返修 · 替代枚举式)
    #   diff = 钱包三池合计 − 全量流水净额
    # 生产实测(2026-07-29 只读核验):两边均 = 129,949,312 · diff = 0 · 口径自洽。
    # 🔴 只允许出现 wallet_total / ledger_total 两个量。任何
    #    historical_purchased / admin_adjust / allocated_out / platform_consumed /
    #    refunded_or_revoked 回到这一行,都是把枚举式的漏算根因请回来。
    # ============================================================
    diff_total = wallet_total - ledger_total

    # 三轨列沿用既有约定(schema 未分轨):总差写 diff_paid · 另两轨占位 0
    diff_paid = diff_total
    diff_bonus = 0
    diff_publish = 0

    # publish 单独校验:publish 不可来自 bonus(模块 docstring 不变式)
    # [2026-06-07 实装 · 原 placeholder=0] 写入侧 allocate 按源池拆 paid/bonus 两条 agent 流水
    #   (services/agent_inventory.py:181/202/310/316);agent bonus 仅可进客户 tool/bonus 池,禁入 publish。
    # 无假阳性判定:按 order 聚合,代理 bonus 划出 > 客户(tool+bonus)收入 ⟹ 超出部分必然流入
    #   publish(客户三池中唯一剩余去向)= 违规。溢出点数 = bonus_out - (tool+bonus)_in。
    #   temp 表单元验证:检出真违规 + OK/offline 零误报。
    publish_violations = 0
    publish_overflow_points = 0
    publish_violation_orders: List[str] = []
    cursor.execute("""
        WITH agent_bonus_out AS (
            SELECT related_order_id AS oid, SUM(ABS(points)) AS bonus_out
            FROM agent_inventory_transactions
            WHERE type IN ('allocate_to_customer', 'allocate_to_customer_offline')
              AND pool = 'bonus'
              AND related_order_id IS NOT NULL
            GROUP BY related_order_id
        ),
        cust_nonpublish_in AS (
            SELECT related_order_id AS oid, COALESCE(SUM(points), 0) AS nonpub_in
            FROM customer_credit_transactions
            WHERE type = 'allocate'
              AND pool IN ('tool', 'bonus')
              AND related_order_id IS NOT NULL
            GROUP BY related_order_id
        )
        SELECT ab.oid AS order_id,
               (ab.bonus_out - COALESCE(cn.nonpub_in, 0)) AS overflow_points
        FROM agent_bonus_out ab
        LEFT JOIN cust_nonpublish_in cn ON cn.oid = ab.oid
        WHERE ab.bonus_out > COALESCE(cn.nonpub_in, 0)
        ORDER BY overflow_points DESC
    """)
    for _vr in cursor.fetchall():
        _oid = _vr["order_id"] if isinstance(_vr, dict) else _vr[0]
        _ov = _vr["overflow_points"] if isinstance(_vr, dict) else _vr[1]
        publish_violations += 1
        publish_overflow_points += int(_ov or 0)
        publish_violation_orders.append(str(_oid))
    if publish_violations > 0:
        logger.error(
            f"[V35-AUDIT] PUBLISH 池违规 · {publish_violations} 个 order 代理 bonus 库存流入客户 "
            f"publish 池 · 溢出 {publish_overflow_points} 点(违反「bonus 永不入 publish」不变式 · "
            f"order={publish_violation_orders[:20]})"
        )

    # ============================================================
    # lot 维度(2026-08-12 补 · Owner 追加要求)
    # ============================================================
    # 🔴 为什么必须补这一维:`08_billing.md` §5.3 写明「聚合钱包只是缓存式总量,
    #    有限 lot 才是成本与所有权 SSOT,两者不一致时停止交易」,而上面的等式
    #    只比「钱包 vs 流水」—— **lot 这一维原本没有任何判据**。
    #    实测后果:线下划拨链路只扣聚合钱包不消 lot,生产已漂移两条
    #    (u125 -10000 · u133 -5416),而对账天天报绿。
    #
    # 🔴 只**旁挂告警**、不进 diff_total、不改 has_drift —— 与铸造分布检查同规矩:
    #    金额等式的口径按 2026-07-29 工单保持不动,lot 漂移有自己的 fingerprint 独立拉响。
    #    存量 2 条会让它一上线就红:那是**如实报告**不是误报;
    #    存量处置是单独的资金动作(Owner 2026-08-12 定本轮不动)。
    # 🔴 必须 SAVEPOINT 包住:任何 SQL 失败都会把调用方事务打成 aborted,
    #    上面 inventory_audit_runs 那行 INSERT 会被连带回滚 —— 表面无异常、实际一行没落。
    lot_drift: Optional[Dict[str, Any]] = None
    cursor.execute("SAVEPOINT lot_drift_probe")
    try:
        from services.inventory_lot_ledger import lot_drift_summary

        lot_drift = lot_drift_summary(cursor)
        cursor.execute("RELEASE SAVEPOINT lot_drift_probe")
        if int(lot_drift["drift_agent_count"]) > 0:
            _emit_alert(
                ALERT_FP_LOT_DRIFT, severity="critical",
                title=(
                    f"聚合钱包与库存批次对不上 · {lot_drift['drift_agent_count']} 个服务商 · "
                    f"净差 {lot_drift['drift_total_points']}"
                ),
                detail=(
                    f"drift_agent_count={lot_drift['drift_agent_count']} "
                    f"drift_total_points={lot_drift['drift_total_points']} "
                    f"drift_abs_points={lot_drift['drift_abs_points']}"
                ),
                payload=lot_drift,
            )
        else:
            _resolve_alert(ALERT_FP_LOT_DRIFT)
    except Exception as exc:  # noqa: BLE001
        cursor.execute("ROLLBACK TO SAVEPOINT lot_drift_probe")
        logger.exception("[V35-AUDIT] lot 维度检查失败(不影响金额对账等式)")
        _emit_alert(
            ALERT_FP_LOT_CHECK_FAILURE, severity="warn",
            title="lot 维度检查自身失败",
            detail=f"error={exc!r}", payload={},
        )

    # publish 违规属财务完整性异常 · 即使金额总差 diff_total=0 也必须算审计失败
    has_drift = abs(diff_total) > DRIFT_THRESHOLD or publish_violations > 0

    # 写 run(status='ok' = 程序跑完了 · 与"账对不对"(has_drift)是两件事)
    cursor.execute("""
        INSERT INTO inventory_audit_runs
            (triggered_by, triggered_by_user_id,
             agent_total_paid, agent_total_bonus, agent_total_frozen,
             customer_total_tool, customer_total_publish, customer_total_bonus,
             platform_consumed, historical_purchased, historical_admin_adjust,
             refunded_or_revoked,
             wallet_total, ledger_total,
             diff_paid, diff_bonus, diff_publish, has_drift, status, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id, run_at
    """, (
        triggered_by, triggered_by_user_id,
        agent_paid, agent_bonus, agent_frozen,
        cust_tool, cust_publish, cust_bonus,
        platform_consumed, historical_purchased, admin_adjust,
        refunded_or_revoked,
        wallet_total, ledger_total,
        diff_paid, diff_bonus, diff_publish, has_drift, "ok",
        f"diff_total={diff_total} wallet_total={wallet_total} ledger_total={ledger_total} "
        f"publish_violations={publish_violations} "
        f"publish_overflow_points={publish_overflow_points}",
    ))
    row = cursor.fetchone()
    run_id = row["id"] if isinstance(row, dict) else row[0]
    run_at = row["run_at"] if isinstance(row, dict) else row[1]

    # 写 diff 明细(若 金额 drift 或 publish 违规)
    if has_drift:
        for track, diff in (("paid", diff_paid), ("bonus", diff_bonus), ("publish", diff_publish)):
            if abs(diff) > DRIFT_THRESHOLD:
                detail = {
                    "equation": "wallet_total(paid+bonus+frozen) - ledger_total(SUM all agent_inventory_transactions.points)",
                    "wallet_total": wallet_total, "ledger_total": ledger_total,
                    "agent_paid": agent_paid, "agent_bonus": agent_bonus, "agent_frozen": agent_frozen,
                    "cust_tool": cust_tool, "cust_publish": cust_publish, "cust_bonus": cust_bonus,
                    # ↓ 审计留痕分项 · 不参与判定
                    "platform_consumed": platform_consumed,
                    "historical_purchased": historical_purchased,
                    "admin_adjust": admin_adjust,
                    "refunded_or_revoked": refunded_or_revoked,
                }
                cursor.execute("""
                    INSERT INTO inventory_audit_diffs (run_id, track, diff_points, detail)
                    VALUES (%s, %s, %s, %s::jsonb)
                """, (run_id, track, diff, json.dumps(detail)))
        # publish 池违规明细(bonus→publish)· 独立于金额 drift 的完整性违规 · 必写 diff(不只 notes/日志)
        if publish_violations > 0:
            v_detail = {
                "violation_type": "bonus_to_publish",
                "criteria": "per related_order_id: agent bonus out(allocate_to_customer/_offline·pool=bonus) "
                            "> customer tool+bonus in(allocate) ⟹ 溢出必入 publish",
                "violation_count": publish_violations,
                "order_count": publish_violations,
                "overflow_points": publish_overflow_points,
                "affected_order_ids": publish_violation_orders[:50],
            }
            cursor.execute("""
                INSERT INTO inventory_audit_diffs (run_id, track, diff_points, detail)
                VALUES (%s, %s, %s, %s::jsonb)
            """, (run_id, "publish", publish_overflow_points, json.dumps(v_detail, ensure_ascii=False)))
        logger.error(
            f"[V35-AUDIT] DRIFT/VIOLATION DETECTED run_id={run_id} "
            f"diff_paid={diff_paid} diff_bonus={diff_bonus} diff_publish={diff_publish} "
            f"publish_violations={publish_violations} publish_overflow_points={publish_overflow_points}"
        )
        _emit_alert(
            ALERT_FP_DRIFT, severity="critical",
            title=f"库存对账不平 · diff={diff_total}",
            detail=(f"run_id={run_id} wallet_total={wallet_total} ledger_total={ledger_total} "
                    f"diff={diff_total} publish_violations={publish_violations}"),
            payload={"run_id": run_id, "diff_total": diff_total,
                     "wallet_total": wallet_total, "ledger_total": ledger_total,
                     "publish_violations": publish_violations,
                     "publish_overflow_points": publish_overflow_points,
                     "publish_violation_orders": publish_violation_orders[:20]},
        )
    else:
        logger.info(f"[V35-AUDIT] OK run_id={run_id} diff_total={diff_total}")
        # 账重新对上 → 收掉 drift 告警(失败态告警由下一次成功跑收,见 _resolve_alert 调用)
        _resolve_alert(ALERT_FP_DRIFT)
    # 只要 run_audit 跑完(不论平不平),"程序死了"这条告警就该收
    _resolve_alert(ALERT_FP_FAILURE)

    # [按需铸造 2026-07-29] 分布级健康:铸造笔数必须与平台跳数 1:1。
    # 🔴 只**旁挂告警**,不进 diff / 不改 has_drift —— 对账等式与口径按工单 §7 保持不动。
    #    异常由 inventory_minting_guard 自己那条 ai_ops fingerprint 拉响,和库存 drift 分开看。
    #    这段自身失败(如按需铸造迁移未跑、列不存在)不能把对账带崩,但必须响:
    #    静默吞掉正是 allocated_out NameError 那次事故的根因。
    # 🔴 必须用 SAVEPOINT 包住:任何 SQL 失败都会把**调用方事务**打成 aborted,
    #    上面那行 inventory_audit_runs INSERT 会被连带回滚 —— 表面无异常、实际一行没落,
    #    正是 allocated_out 那次"对账静默死亡"的同一形态。
    mint_distribution: Optional[Dict[str, Any]] = None
    cursor.execute("SAVEPOINT mint_distribution_probe")
    try:
        from services.inventory_minting_guard import mint_distribution_status

        mint_distribution = mint_distribution_status(cursor)
        cursor.execute("RELEASE SAVEPOINT mint_distribution_probe")
    except Exception as exc:  # noqa: BLE001
        cursor.execute("ROLLBACK TO SAVEPOINT mint_distribution_probe")
        logger.exception("[V35-AUDIT] 铸造分布检查失败(不影响对账等式)")
        _emit_alert(
            "mint_distribution_check_failure", severity="warn",
            title="铸造分布检查自身失败",
            detail=f"run_id={run_id} error={exc!r}",
            payload={"run_id": run_id},
        )

    return {
        "mint_distribution": mint_distribution,
        "lot_drift": lot_drift,
        "run_id": run_id,
        "run_at": run_at.isoformat() if run_at else None,
        "status": "ok",
        "has_drift": has_drift,
        "wallet_total": wallet_total,
        "ledger_total": ledger_total,
        "diff_total": diff_total,
        "diff_paid": diff_paid,
        "diff_bonus": diff_bonus,
        "diff_publish": diff_publish,
        "publish_violations": publish_violations,
        "publish_overflow_points": publish_overflow_points,
        "publish_violation_orders": publish_violation_orders[:50],
        "summary": {
            "agent_paid": agent_paid, "agent_bonus": agent_bonus, "agent_frozen": agent_frozen,
            "wallet_total": wallet_total, "ledger_total": ledger_total,
            "customer_tool": cust_tool, "customer_publish": cust_publish, "customer_bonus": cust_bonus,
            # ↓ 审计留痕分项 · 不参与判定
            "platform_consumed": platform_consumed,
            "historical_purchased": historical_purchased,
            "admin_adjust": admin_adjust,
            "refunded_or_revoked": refunded_or_revoked,
        },
    }


# ============================================================
# 失败态 · 「对账程序自己死了」必须比「对上了」更响
# ============================================================

def record_audit_failure(
    error: BaseException,
    triggered_by: str = "cron",
    triggered_by_user_id: Optional[int] = None,
) -> Optional[int]:
    """
    run_audit 抛异常时调用 · 落 inventory_audit_runs 一行失败态 + 触发告警。

    🔴 三条设计约束(2026-07-29 返修 · 直接对着「静默死亡」这个事故写的):
      1. **自己开连接**。run_audit 炸掉后调用方那条事务多半已 aborted,
         同连接再 INSERT 只会拿到 InFailedSqlTransaction —— 等于又静默一次。
      2. **has_drift 也置 TRUE**。老消费方(api/finance_api.py:/reconciliation 的
         has_recent_drift、前端 v35Terminology)只认 has_drift;只写 status='failed'
         的话财务页仍显示绿 —— 那就是换个姿势继续静默。
      3. **告警与落库互不阻断**。落库失败照样告警,告警失败照样 logger.exception。
         任何一环都不 raise 回调用方(调用方自己会记 exception / 返 500)。

    返回失败行 run_id(落库失败则 None)。
    """
    msg = f"{type(error).__name__}: {error}"
    logger.exception(f"[V35-AUDIT] ❌ 对账运行失败(status=failed)· triggered_by={triggered_by} · {msg}")

    run_id: Optional[int] = None
    try:
        from db.connection import get_db as _get_db
        with _get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO inventory_audit_runs
                    (triggered_by, triggered_by_user_id,
                     agent_total_paid, agent_total_bonus, agent_total_frozen,
                     customer_total_tool, customer_total_publish, customer_total_bonus,
                     platform_consumed, historical_purchased, historical_admin_adjust,
                     refunded_or_revoked,
                     wallet_total, ledger_total,
                     diff_paid, diff_bonus, diff_publish,
                     has_drift, status, error_message, notes)
                VALUES (%s, %s, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, NULL, NULL, 0, 0, 0,
                        TRUE, 'failed', %s, %s)
                RETURNING id
            """, (triggered_by, triggered_by_user_id, msg[:2000],
                  f"AUDIT_FAILED · {msg[:400]}"))
            row = cur.fetchone()
            run_id = row["id"] if isinstance(row, dict) else (row[0] if row else None)
            conn.commit()
    except Exception:  # noqa: BLE001
        logger.exception("[V35-AUDIT] ❌❌ 失败态落库也失败 —— 仅剩告警与日志")

    _emit_alert(
        ALERT_FP_FAILURE, severity="critical",
        title="库存对账程序运行失败(未产出对账结果)",
        detail=f"triggered_by={triggered_by} run_id={run_id} error={msg[:400]}",
        payload={"run_id": run_id, "triggered_by": triggered_by, "error": msg[:1000]},
    )
    return run_id


def _emit_alert(fingerprint: str, *, severity: str, title: str, detail: str,
                payload: Optional[Dict[str, Any]] = None) -> None:
    """复用 AI Ops 告警管道 · fail-soft(告警挂了绝不能把对账本身带崩)。"""
    try:
        from db import ai_ops_db
        ai_ops_db.upsert_alert(ALERT_RULE_KEY, severity=severity, title=title,
                               detail=detail, fingerprint=fingerprint, payload=payload or {})
    except Exception:  # noqa: BLE001
        logger.exception(f"[V35-AUDIT] 告警发送失败(不阻断对账)· fp={fingerprint} · {title}")


def _resolve_alert(fingerprint: str) -> None:
    """恢复告警 · fail-soft。"""
    try:
        from db import ai_ops_db
        ai_ops_db.resolve_alerts(ALERT_RULE_KEY, fingerprint=fingerprint)
    except Exception:  # noqa: BLE001
        logger.debug(f"[V35-AUDIT] 告警恢复失败(忽略)· fp={fingerprint}", exc_info=True)


def list_audit_runs(cursor, limit: int = 30, drift_only: bool = False) -> List[Dict[str, Any]]:
    # drift_only 同时收失败态:程序没跑成也是"需要人看"的行,不能被过滤掉
    where = "WHERE (has_drift = TRUE OR status = 'failed')" if drift_only else ""
    cursor.execute(f"""
        SELECT id, run_at, triggered_by, triggered_by_user_id,
               agent_total_paid, agent_total_bonus, agent_total_frozen,
               customer_total_tool, customer_total_publish, customer_total_bonus,
               platform_consumed, historical_purchased, historical_admin_adjust,
               refunded_or_revoked,
               wallet_total, ledger_total,
               diff_paid, diff_bonus, diff_publish, has_drift, status, error_message, notes
        FROM inventory_audit_runs
        {where}
        ORDER BY run_at DESC
        LIMIT %s
    """, (limit,))
    out = []
    for row in cursor.fetchall():
        d = dict(row) if isinstance(row, dict) else {}
        d["run_at"] = d["run_at"].isoformat() if d.get("run_at") else None
        out.append(d)
    return out


def _g(row, key, default=None):
    if row is None: return default
    if isinstance(row, dict): return row.get(key, default)
    return default
