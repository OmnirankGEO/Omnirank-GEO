"""[v5 req4 · Deploy-CTO 2026-07-13] 系统内部资金操作【耐久补偿工单】。

区别于 db/refund_work_order_db.py(客户发起 → admin 人工微信打款的重型退款工单):
本表是【系统自身】某笔冻结/扣费的 commit/release/refund/状态恢复失败时,落一条【可审计、可补偿重试】
的耐久记录 —— 绝不 except: pass 把资金/状态失败静默吞掉。

来源(source)示例:
  - 'article_gen_thread_window' :文章生成扣费后线程未启动 · 退款失败 → 待补偿
  - 'geo_plan_settle' / 'geo_plan_refund' :GEO 任务 commit/release 失败(req1 队列内也可交叉登记)

kind:'refund' | 'release' | 'commit' | 'state_fix'
status:'pending'(待补偿) | 'resolved'(已收口) | 'manual'(需人工)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from db.schema_guard import add_column_if_missing, replace_in_list_check_if_changed

logger = logging.getLogger("GEO-FundRecovery")


def _enqueue_manual_notification(cursor, order_id: int) -> None:
    from services.notification_events import NotificationEventType
    from services.notification_outbox import enqueue_admin_notification_events

    enqueue_admin_notification_events(
        cursor,
        event_type=NotificationEventType.FUND_RECOVERY_MANUAL_REQUIRED,
        business_id=str(int(order_id)),
        terminal_state="manual",
        facts={
            "business_no": f"RECOVERY-{int(order_id)}",
            "status": "资金恢复任务需要人工处理",
            "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "summary": "自动补偿已停止，请在财务页面核验后处理。",
        },
    )


def _get_conn():
    from db.connection import get_connection
    return get_connection()


def _confirmed_refund_points_cursor(
    cursor,
    *,
    user_id: Optional[int],
    feature_code: Optional[str],
    charge_tx_id: Optional[int],
    ledger_type: Optional[str],
) -> int:
    """Return validated refund evidence for one full charge identity.

    Every consume leg is scoped by user, feature, charge group and ledger, then
    checked independently.  A dirty over-refunded leg or cross-feature row must
    never promote a recovery order to a successful financial terminal state.
    """
    if (
        user_id is None
        or not feature_code
        or not charge_tx_id
        or ledger_type not in ("legacy", "v35")
    ):
        return 0

    def _validated_total(rows, ledger_label: str) -> int:
        total = 0
        for row in rows:
            if isinstance(row, dict):
                original = int(row.get("original_points") or 0)
                refunded = int(row.get("refunded_points") or 0)
            else:
                original = int(row[1] or 0)
                refunded = int(row[2] or 0)
            if original < 0 or refunded < 0 or refunded > original:
                raise RuntimeError(
                    f"{ledger_label} refund evidence leg exceeds original charge"
                )
            total += refunded
        return total

    if ledger_type == "legacy":
        cursor.execute("SELECT to_regclass('point_transactions') AS relation")
        relation = cursor.fetchone()
        if not relation or not (
            relation.get("relation") if isinstance(relation, dict) else relation[0]
        ):
            return 0
        cursor.execute(
            "SELECT order_id FROM point_transactions "
            "WHERE id=%s AND user_id=%s AND feature_code=%s AND type='consume'",
            (int(charge_tx_id), int(user_id), str(feature_code)),
        )
        charge = cursor.fetchone()
        if not charge:
            return 0
        order_id = charge.get("order_id") if isinstance(charge, dict) else charge[0]
        if order_id is None:
            cursor.execute(
                """
                SELECT c.id, ABS(c.amount)::bigint AS original_points,
                       COALESCE((
                           SELECT SUM(r.amount) FROM point_transactions r
                            WHERE r.user_id=c.user_id
                              AND r.type='refund' AND r.order_id=c.id::text
                       ),0)::bigint AS refunded_points
                  FROM point_transactions c
                 WHERE c.id=%s AND c.user_id=%s AND c.feature_code=%s
                   AND c.type='consume'
                """,
                (int(charge_tx_id), int(user_id), str(feature_code)),
            )
        else:
            cursor.execute(
                """
                SELECT c.id, ABS(c.amount)::bigint AS original_points,
                       COALESCE((
                           SELECT SUM(r.amount) FROM point_transactions r
                            WHERE r.user_id=c.user_id
                              AND r.type='refund' AND r.order_id=c.id::text
                       ),0)::bigint AS refunded_points
                  FROM point_transactions c
                 WHERE c.user_id=%s AND c.feature_code=%s
                   AND c.type='consume' AND c.order_id=%s
                 ORDER BY c.created_at,c.id
                """,
                (int(user_id), str(feature_code), order_id),
            )
        return _validated_total(cursor.fetchall(), "legacy")

    # [单账本收敛 2026-07-27] 本分支仅在 ledger_type='v35' 时进入。单账本后
    # billing.refund_points 的 _resolved_ledger_type 恒为 'legacy',新工单不会再走这里;
    # 但【历史】fund_recovery_orders 里仍有 ledger_type='v35' 的行,它们的已退金额
    # 必须继续能从 customer_credit_transactions 查出来 —— 所以保留,不删。
    # (表停写只读、历史数据永久保留,查询恒可用。)
    cursor.execute("SELECT to_regclass('customer_credit_transactions') AS relation")
    relation = cursor.fetchone()
    if not relation or not (
        relation.get("relation") if isinstance(relation, dict) else relation[0]
    ):
        return 0
    cursor.execute(
        "SELECT related_order_id FROM customer_credit_transactions "
        "WHERE id=%s AND customer_user_id=%s AND feature_code=%s "
        "AND type='consume'",
        (int(charge_tx_id), int(user_id), str(feature_code)),
    )
    charge = cursor.fetchone()
    if not charge:
        return 0
    related_order_id = (
        charge.get("related_order_id") if isinstance(charge, dict) else charge[0]
    )
    if related_order_id is None:
        cursor.execute(
            """
            SELECT c.id, ABS(c.points)::bigint AS original_points,
                   COALESCE((
                       SELECT SUM(r.points) FROM customer_credit_transactions r
                        WHERE r.customer_user_id=c.customer_user_id
                          AND r.type='refund' AND r.source='tool_fail_refund'
                          AND r.related_order_id=c.id::text
                   ),0)::bigint AS refunded_points
              FROM customer_credit_transactions c
             WHERE c.id=%s AND c.customer_user_id=%s AND c.feature_code=%s
               AND c.type='consume'
            """,
            (int(charge_tx_id), int(user_id), str(feature_code)),
        )
    else:
        cursor.execute(
            """
            SELECT c.id, ABS(c.points)::bigint AS original_points,
                   COALESCE((
                       SELECT SUM(r.points) FROM customer_credit_transactions r
                        WHERE r.customer_user_id=c.customer_user_id
                          AND r.type='refund' AND r.source='tool_fail_refund'
                          AND r.related_order_id=c.id::text
                   ),0)::bigint AS refunded_points
              FROM customer_credit_transactions c
             WHERE c.customer_user_id=%s AND c.feature_code=%s
               AND c.type='consume' AND c.related_order_id=%s
             ORDER BY c.created_at,c.id
            """,
            (int(user_id), str(feature_code), related_order_id),
        )
    return _validated_total(cursor.fetchall(), "v35")


def _refund_evidence_exists_cursor(
    cursor,
    *,
    user_id: Optional[int],
    feature_code: Optional[str],
    charge_tx_id: Optional[int],
    ledger_type: Optional[str],
) -> bool:
    """Fail-closed evidence check scoped to the complete charge identity."""
    if (
        user_id is None
        or not feature_code
        or not charge_tx_id
        or ledger_type not in ("legacy", "v35")
    ):
        return False
    return _confirmed_refund_points_cursor(
        cursor,
        user_id=user_id,
        feature_code=feature_code,
        charge_tx_id=charge_tx_id,
        ledger_type=ledger_type,
    ) > 0


def init_fund_recovery_tables(cursor=None) -> None:
    """幂等建表。可传入已有 cursor(随 init_wallet_tables 主事务)或自开连接。"""
    owns = cursor is None
    conn = None
    if owns:
        conn = _get_conn()
        cursor = conn.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS fund_recovery_orders (
                id            BIGSERIAL PRIMARY KEY,
                source        TEXT NOT NULL,
                ref_key       TEXT,
                user_id       INTEGER,
                feature_code  TEXT,
                charge_tx_id  BIGINT,
                amount_points INTEGER,
                kind          TEXT NOT NULL DEFAULT 'refund'
                              CHECK (kind IN ('refund','release','commit','state_fix','record','reverse')),
                status        TEXT NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending','processing','resolved','manual','failed')),
                retry_count   SMALLINT NOT NULL DEFAULT 0,
                reason        TEXT,
                last_error    TEXT,
                payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
                next_retry_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                claimed_at    TIMESTAMPTZ,
                worker_id     TEXT,
                claim_token   TEXT,
                ledger_type   TEXT,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                resolved_at   TIMESTAMPTZ
            )
        """)
        # [v6 req3 + v7 finding3] 旧表(v5/v6 已建)幂等升级:补新列 + 放宽 status CHECK 到 5 态
        #   claim_token(v7):worker fencing —— 每次认领生成新 token,resolve/requeue 必须持同 token 才生效,
        #   防【迟到 worker】(被 stale reaper 重新分派后)把 resolved 改回 pending。
        for _col, _ddl in (
            ("next_retry_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
            ("claimed_at", "TIMESTAMPTZ"),
            ("worker_id", "TEXT"),
            ("claim_token", "TEXT"),
            # [v8 P1-1] ledger_type('legacy'|'v35'):退款证据【只查对应账本】· 防跨表同 ID 误判(两表 id 空间独立)
            ("ledger_type", "TEXT"),
        ):
            # [WO_285b] 列缺失才 ALTER:本函数在请求路径上(下单开写 → create_recovery_order)每次都被调,
            #   无条件 DDL 每次都要拿 fund_recovery_orders 的 ACCESS EXCLUSIVE,发车 pg_dump 期间会排队卡死。
            add_column_if_missing(cursor, "fund_recovery_orders", _col, _ddl)
        # [WO_285b] CHECK 允许值集合与期望相同就不动;不同(或没有)才 DROP + ADD —— 语义与原来的「每次 DROP+ADD」一致
        replace_in_list_check_if_changed(cursor, "fund_recovery_orders", "fund_recovery_orders_status_check",
                                         "status", ["pending", "processing", "resolved", "manual", "failed"])
        # [v9 P1-2] 旧表放宽 kind CHECK 到含 'record'/'reverse'(渠道收益记账/冲销 exactly-once 耐久补偿工单)。
        #   否则 insert_recovery_order_cursor(kind='record'/'reverse') 违反 v6 老 CHECK → 抛错 → 主充值/退款事务
        #   回滚 → 无限 fail-closed 重试(brick)。DROP+ADD 幂等(与上 status 同法)。
        replace_in_list_check_if_changed(cursor, "fund_recovery_orders", "fund_recovery_orders_kind_check",
                                         "kind", ["refund", "release", "commit", "state_fix", "record", "reverse"])
        # 认领扫描索引:pending 且到重试点
        cursor.execute("DROP INDEX IF EXISTS idx_fund_recovery_status")
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_fund_recovery_claim
            ON fund_recovery_orders(next_retry_at) WHERE status = 'pending'
        """)
        # [v6 req3 + v7 finding3] 幂等键必须【含 kind/operation identity】—— 同 charge 的 refund 与 state_fix 是不同工单,
        #   绝不互相吞掉(否则一笔的重复登记会把另一笔的补偿静默丢)。仅 charge_tx_id 非空时去重(见 v5 对抗审 P3)。
        #   [v7 finding3 修] WHERE 覆盖【所有未终结状态】(pending/processing/manual)—— 否则一单 processing 时可再建重复工单,
        #     两 worker 各退一次 = 双退。DROP 后重建:CREATE INDEX IF NOT EXISTS 在同名旧定义存在时 no-op → 先 DROP 保证为当前版。
        cursor.execute("DROP INDEX IF EXISTS uniq_fund_recovery_open")
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uniq_fund_recovery_open
            ON fund_recovery_orders(source, COALESCE(ref_key,''), kind, charge_tx_id)
            WHERE status IN ('pending','processing','manual') AND charge_tx_id IS NOT NULL
        """)
        # [v7 对抗审 · 二轮修复净增量] NULL charge_tx_id 的工单【不去重】(保 v6 行为)——
        #   ⚠️ 一度加 (source,ref_key,kind) 的 null-charge 唯一键,但 ref_key(=quote_id)不是 per-charge 判别键:
        #      同 quote 的两笔【不同】article_gen 扣费(v35 空 tx → charge_tx_id 均 NULL)会被误并成一条,
        #      处理器 newest-by-feature 只退最新一笔 → 另一笔漏退 = 客户少退款(比原 over-refund 风险更糟)。已撤销。
        #   根治需在【登记工单时持久化 charge 的 order-group key】(order_id / charge_tx_ids 列表)并按其精确退款,
        #      涉及 server.py 工单登记 + billing 退款路径,超出本 v7 NO-GO 范围 → 记为已知遗留(见出口报告)。
        cursor.execute("DROP INDEX IF EXISTS uniq_fund_recovery_open_nullcharge")
        # [v8 对抗审 P2] geo_plan_settle 工单 charge_tx_id 恒 NULL → 主唯一键不覆盖它;inline vs 巡检的 has_open_workorder
        #   是 TOCTOU · 并发/蓝绿双 scheduler 会为【同一任务】建重复未终结工单(一条被收口后另一条永久 un-closeable)。
        #   用【DB 唯一键 (source, ref_key) 兜底 exactly-once】(ref_key=geoplan_{tid} 任务唯一身份 · 忽略 kind)。
        # 🔴 [v8 二轮对抗审 P1 修] 建唯一键【前必须 pre-dedup 既有重复行】——prod 已存在 TOCTOU 重复(本注释所述场景)时,
        #   非并发 CREATE UNIQUE INDEX 会报 duplicate key · 而本函数常被 init_wallet_tables 用【共享 cursor(owns=False)】调,
        #   异常会污染整个 init 事务(无 SAVEPOINT)→ ledger_type/escrow schema 全回滚 + 索引永不建 + 每次启动重复失败。
        #   故:① pre-dedup 折叠每 (source,ref_key) 只留最新一条(其余 resolved)· ② 索引建立包在 SAVEPOINT 内隔离,
        #   任何失败只回滚到 savepoint · 绝不污染外层 init 事务。
        cursor.execute("SAVEPOINT sp_geoplan_idx")
        try:
            cursor.execute("""
                WITH ranked AS (
                    SELECT id, ROW_NUMBER() OVER (PARTITION BY source, ref_key ORDER BY id DESC) AS rn
                      FROM fund_recovery_orders
                     WHERE source='geo_plan_settle' AND ref_key IS NOT NULL
                       AND status IN ('pending','processing','manual')
                )
                UPDATE fund_recovery_orders f
                   SET status='resolved', resolved_at=NOW(), updated_at=NOW(),
                       last_error = COALESCE(last_error,'') || ' · [v8 pre-dedup] 折叠重复 geo_plan_settle 工单(保最新一条)'
                  FROM ranked r WHERE f.id = r.id AND r.rn > 1
            """)
            cursor.execute("DROP INDEX IF EXISTS uniq_fund_recovery_geoplan_open")
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uniq_fund_recovery_geoplan_open
                ON fund_recovery_orders(source, ref_key)
                WHERE source = 'geo_plan_settle' AND ref_key IS NOT NULL
                  AND status IN ('pending','processing','manual')
            """)
            cursor.execute("RELEASE SAVEPOINT sp_geoplan_idx")
        except Exception as _ie:
            cursor.execute("ROLLBACK TO SAVEPOINT sp_geoplan_idx")
            logger.error(f"[FundRecovery][CRITICAL] geoplan 唯一键建立失败(已回滚到 savepoint · 不污染 init 事务): {_ie}")
        # [v10 item5 · v11 F6] channel_revenue 工单 exactly-once 唯一键 (source, ref_key, kind):
        #   ref_key=order_id · record 与 reverse 是不同 kind(同订单可各有一条)· 覆盖未终结态。
        #   🔴 [v11 F6] 启动路径【禁止静默把未终结工单改成 resolved】(不能像旧版那样 pre-dedup 折叠):
        #     进程启动不能 abort(会 brick),故【探测重复】→ 有重复则【不建唯一键】(退化到 app 层
        #     insert_recovery_order_cursor 的 ON CONFLICT DO NOTHING 幂等)+ CRITICAL 告警,交人工跑
        #     migration_v10b(备份+去重·可逐行恢复)后重启;无重复才建唯一键。绝不在启动期静默 resolve 工单。
        #     (channel_revenue 补偿工单是 v9/v10 新增 · 生产从未部署 → 正常应为零重复 · 直接建键。)
        cursor.execute("SAVEPOINT sp_chanrev_idx")
        try:
            cursor.execute("""
                SELECT COUNT(*) AS c FROM (
                    SELECT 1 FROM fund_recovery_orders
                     WHERE source='channel_revenue' AND ref_key IS NOT NULL
                       AND status IN ('pending','processing','manual')
                     GROUP BY source, ref_key, kind
                    HAVING COUNT(*) > 1
                ) d
            """)
            _cd = cursor.fetchone()
            _dupc = int((_cd["c"] if isinstance(_cd, dict) else _cd[0]) or 0) if _cd else 0
            if _dupc > 0:
                cursor.execute("ROLLBACK TO SAVEPOINT sp_chanrev_idx")
                logger.critical(
                    "[FundRecovery][CRITICAL] channel_revenue 存在 %s 组未终结重复工单 → 暂不建唯一键"
                    "(退化到 app 层 ON CONFLICT 幂等)· 请人工跑 migration_v10b 备份去重后重启 · 绝不启动期静默 resolve",
                    _dupc)
            else:
                cursor.execute("DROP INDEX IF EXISTS uniq_fund_recovery_channel_open")
                cursor.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS uniq_fund_recovery_channel_open
                    ON fund_recovery_orders(source, ref_key, kind)
                    WHERE source = 'channel_revenue' AND ref_key IS NOT NULL
                      AND status IN ('pending','processing','manual')
                """)
                cursor.execute("RELEASE SAVEPOINT sp_chanrev_idx")
        except Exception as _ce:
            cursor.execute("ROLLBACK TO SAVEPOINT sp_chanrev_idx")
            logger.error(f"[FundRecovery][CRITICAL] channel_revenue 唯一键建立失败(已回滚 savepoint · 不污染 init): {_ce}")
        if owns:
            conn.commit()
        logger.info("[FundRecovery] fund_recovery_orders 表就绪")
    finally:
        if owns and conn:
            try:
                conn.close()
            except Exception:
                pass


def create_recovery_order(
    source: str,
    kind: str,
    *,
    ref_key: Optional[str] = None,
    user_id: Optional[int] = None,
    feature_code: Optional[str] = None,
    charge_tx_id: Optional[int] = None,
    amount_points: Optional[int] = None,
    reason: Optional[str] = None,
    last_error: Optional[str] = None,
    payload: Optional[dict] = None,
    status: str = "pending",
    ledger_type: Optional[str] = None,   # [v8 P1-1] 'legacy'|'v35' · 退款证据只查对应账本(防跨表同 ID 误判)
) -> Optional[int]:
    """登记一条耐久补偿工单(自开连接 · 幂等 ON CONFLICT DO NOTHING)。

    返回 work-order id;登记本身失败时返回 None 并 CRITICAL(绝不静默吞 —— 已是最后兜底)。
    """
    conn = None
    try:
        conn = _get_conn()
        # 表未建时自愈建(部署序:init 可能未先行)
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO fund_recovery_orders
                    (source, ref_key, user_id, feature_code, charge_tx_id, amount_points,
                     kind, status, reason, last_error, payload, ledger_type)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (source, ref_key, user_id, feature_code, charge_tx_id, amount_points,
                 kind, status, reason, last_error,
                 json.dumps(payload or {}, ensure_ascii=False), ledger_type),
            )
        except Exception:
            conn.rollback()
            init_fund_recovery_tables(cur)
            cur.execute(
                """
                INSERT INTO fund_recovery_orders
                    (source, ref_key, user_id, feature_code, charge_tx_id, amount_points,
                     kind, status, reason, last_error, payload, ledger_type)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (source, ref_key, user_id, feature_code, charge_tx_id, amount_points,
                 kind, status, reason, last_error,
                 json.dumps(payload or {}, ensure_ascii=False), ledger_type),
            )
        row = cur.fetchone()
        wid = None
        if row is not None:
            wid = row["id"] if isinstance(row, dict) else row[0]
        if wid is not None:
            conn.commit()
            logger.error(
                "[FundRecovery] 已登记补偿工单 id=%s source=%s kind=%s ref=%s charge_tx=%s reason=%s",
                wid, source, kind, ref_key, charge_tx_id, reason,
            )
            return int(wid)
        # [v5 对抗审 P3 + v7 finding3] RETURNING None = 唯一键冲突被 DO NOTHING(仅 charge_tx_id 非空时可能)→ 已有同笔【未终结】工单。
        #   不再谎报"已登记 id=None":查出既有工单 id,如实记"重复登记被去重",返回既有 id(可审计 · 非 None 表示已有耐久记录)。
        #   [v7 finding3] 唯一键已覆盖 pending/processing/manual → 既有工单查询也必须覆盖同一集合(否则 processing 期间
        #     重复登记被索引挡下却查不到既有 id → 误返 None)。
        # [v8 二轮对抗审 P2 修] geo_plan_settle 的 exactly-once 唯一键是 (source, ref_key)(忽略 kind · charge_tx_id 恒 NULL)。
        #   通用 fallback 用 kind+charge_tx_id=%s 对它永不命中(NULL=%s 恒假)→ 误返 None(违反 v7 "非 None=已有耐久记录" 契约)。
        #   故 geo_plan_settle 按 (source, ref_key) 查既有;其余 source 保原 (source,ref_key,kind,charge_tx_id) 口径。
        if source == "geo_plan_settle" and ref_key:
            cur.execute(
                "SELECT id FROM fund_recovery_orders WHERE status IN ('pending','processing','manual') "
                "AND source=%s AND ref_key=%s ORDER BY id LIMIT 1",
                (source, ref_key),
            )
        else:
            cur.execute(
                "SELECT id FROM fund_recovery_orders WHERE status IN ('pending','processing','manual') AND source=%s "
                "AND COALESCE(ref_key,'')=COALESCE(%s,'') AND kind=%s AND charge_tx_id=%s ORDER BY id LIMIT 1",
                (source, ref_key, kind, charge_tx_id),
            )
        ex = cur.fetchone()
        conn.commit()
        ex_id = (ex["id"] if isinstance(ex, dict) else ex[0]) if ex else None
        logger.error(
            "[FundRecovery] 已存在待补偿工单 id=%s · 本次重复登记被去重(source=%s kind=%s ref=%s charge_tx=%s reason=%s)",
            ex_id, source, kind, ref_key, charge_tx_id, reason,
        )
        return int(ex_id) if ex_id is not None else None
    except Exception as e:
        # 🔴 连登记工单都失败 = 真正最后兜底:CRITICAL(带全部上下文)· 不 raise(避免掩盖原始异常)· 不静默
        logger.critical(
            "[FundRecovery][CRITICAL] 补偿工单登记失败!source=%s kind=%s ref=%s charge_tx=%s user=%s "
            "reason=%s last_error=%s · 登记异常=%r · 需人工立即介入",
            source, kind, ref_key, charge_tx_id, user_id, reason, last_error, e,
        )
        return None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def create_report_export_refund_recovery(
    source: str,
    kind: str,
    *,
    ref_key: Optional[str] = None,
    user_id: Optional[int] = None,
    feature_code: Optional[str] = None,
    charge_tx_id: Optional[int] = None,
    amount_points: Optional[int] = None,
    reason: Optional[str] = None,
    last_error: Optional[str] = None,
    payload: Optional[dict] = None,
    status: str = "pending",
    ledger_type: Optional[str] = None,
) -> Optional[int]:
    """Atomically persist report refund recovery and close charge replay.

    The report export endpoint always carries a billing idempotency key in its
    payload.  An open recovery order and ``refund_pending`` must therefore be
    one transaction; a previously confirmed refund creates/resolves only an
    audit row and never regresses the charge to pending.
    """
    if source != "report_export" or kind != "refund":
        raise ValueError("specialized recovery writer only accepts report_export/refund")
    if status != "pending":
        raise ValueError("report export recovery must start pending")
    if not isinstance(payload, dict) or not payload.get("idempotency_key"):
        raise ValueError("report export recovery requires idempotency_key")
    if (
        user_id is None
        or feature_code is None
        or charge_tx_id is None
        or ledger_type not in ("legacy", "v35")
    ):
        raise ValueError("report export recovery requires immutable charge identity")

    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT status, deducted
              FROM billing_deduction_idempotency
             WHERE idempotency_key=%s AND user_id=%s AND feature_code=%s
               AND charge_tx_id=%s AND ledger_type=%s
             FOR UPDATE
            """,
            (
                str(payload["idempotency_key"]),
                int(user_id),
                str(feature_code),
                int(charge_tx_id),
                ledger_type,
            ),
        )
        charge = cur.fetchone()
        if charge is None:
            raise RuntimeError("report export charge lifecycle row is unavailable")
        charge_status = str(
            charge.get("status") if isinstance(charge, dict) else charge[0]
        )
        charged_points = int(
            charge.get("deducted") if isinstance(charge, dict) else charge[1]
        )
        if amount_points is None or int(amount_points) != charged_points:
            raise ValueError("report export recovery amount must match exact charge")
        if charge_status not in ("charged", "refund_pending", "refunded"):
            raise RuntimeError("report export charge lifecycle is not recoverable")

        target_status = "resolved" if charge_status == "refunded" else status
        terminal_filter = (
            " AND status IN ('pending','processing','manual','resolved')"
            if charge_status == "refunded"
            else " AND status IN ('pending','processing','manual')"
        )
        cur.execute(
            "SELECT id FROM fund_recovery_orders "
            "WHERE source=%s AND COALESCE(ref_key,'')=COALESCE(%s,'') "
            "AND kind=%s AND charge_tx_id=%s" + terminal_filter +
            " ORDER BY id DESC LIMIT 1",
            (source, ref_key, kind, int(charge_tx_id)),
        )
        existing = cur.fetchone()
        if existing is None:
            cur.execute(
                """
                INSERT INTO fund_recovery_orders
                    (source, ref_key, user_id, feature_code, charge_tx_id,
                     amount_points, kind, status, reason, last_error, payload,
                     ledger_type, resolved_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,
                        CASE WHEN %s='resolved' THEN NOW() ELSE NULL END)
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (
                    source,
                    ref_key,
                    int(user_id),
                    str(feature_code),
                    int(charge_tx_id),
                    int(amount_points) if amount_points is not None else None,
                    kind,
                    target_status,
                    reason,
                    last_error,
                    json.dumps(payload, ensure_ascii=False),
                    ledger_type,
                    target_status,
                ),
            )
            existing = cur.fetchone()
            if existing is None:
                cur.execute(
                    """
                    SELECT id FROM fund_recovery_orders
                     WHERE source=%s AND COALESCE(ref_key,'')=COALESCE(%s,'')
                       AND kind=%s AND charge_tx_id=%s
                       AND status IN ('pending','processing','manual')
                     ORDER BY id DESC LIMIT 1
                    """,
                    (source, ref_key, kind, int(charge_tx_id)),
                )
                existing = cur.fetchone()
        if existing is None:
            raise RuntimeError("report export recovery row unavailable after insert")
        order_id = int(existing["id"] if isinstance(existing, dict) else existing[0])

        if charge_status == "refunded":
            cur.execute(
                """
                UPDATE fund_recovery_orders
                   SET status='resolved', resolved_at=COALESCE(resolved_at,NOW()),
                       updated_at=NOW()
                 WHERE id=%s AND status IN ('pending','processing','manual','resolved')
                """,
                (order_id,),
            )
        else:
            cur.execute(
                """
                UPDATE billing_deduction_idempotency
                   SET status='refund_pending',
                       refund_pending_at=COALESCE(refund_pending_at,NOW()),
                       updated_at=NOW()
                 WHERE idempotency_key=%s AND status IN ('charged','refund_pending')
                RETURNING idempotency_key
                """,
                (str(payload["idempotency_key"]),),
            )
            if cur.fetchone() is None:
                raise RuntimeError("report export refund_pending CAS failed")
        conn.commit()
        return order_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def channel_revenue_index_healthy(cursor) -> tuple:
    """[v12 item4] 自检 channel_revenue exactly-once 唯一约束是否健康 → (ok: bool, why: str)。

    健康 = ①部分唯一索引 uniq_fund_recovery_channel_open 存在 ②定义含 UNIQUE + (source,ref_key,kind) +
      channel_revenue 谓词 ③无【未清重复】未终结工单。任一不满足 → 不健康(补偿创建路径据此 fail-closed)。
    只读自检 · 不修表(建索引/去重是 prestart migration 单飞职责)。
    """
    cursor.execute("SELECT indexdef FROM pg_indexes WHERE indexname='uniq_fund_recovery_channel_open'")
    r = cursor.fetchone()
    if not r:
        return False, "唯一索引 uniq_fund_recovery_channel_open 缺失"
    _def = ((r["indexdef"] if isinstance(r, dict) else r[0]) or "")
    _du = _def.upper()
    if "UNIQUE" not in _du or "channel_revenue" not in _def or "kind" not in _def \
            or "source" not in _def or "ref_key" not in _def:
        return False, f"唯一索引定义不符(期望 UNIQUE(source,ref_key,kind) WHERE channel_revenue):{_def}"
    cursor.execute(
        """SELECT COUNT(*) AS c FROM (
             SELECT 1 FROM fund_recovery_orders
              WHERE source='channel_revenue' AND ref_key IS NOT NULL
                AND status IN ('pending','processing','manual')
              GROUP BY source, ref_key, kind HAVING COUNT(*) > 1) d"""
    )
    rc = cursor.fetchone()
    dupc = int((rc["c"] if isinstance(rc, dict) else rc[0]) or 0) if rc else 0
    if dupc > 0:
        return False, f"存在 {dupc} 组未清重复 channel_revenue 未终结工单(需 prestart v10b 备份去重)"
    return True, "ok"


def insert_recovery_order_cursor(cursor, source: str, kind: str, *, ref_key: Optional[str] = None,
                                 user_id: Optional[int] = None, feature_code: Optional[str] = None,
                                 reason: Optional[str] = None, last_error: Optional[str] = None,
                                 payload: Optional[dict] = None, status: str = "pending") -> Optional[int]:
    """[v9 P1-2] 在【调用方事务(cursor)】内登记一条耐久补偿工单 —— 与主业务(充值/退款)原子共存亡。

    区别于 create_recovery_order(自开连接·独立事务提交):本函数【复用传入 cursor·不 commit】,
    使工单与订单/退款在【同一事务】提交,消除"主事务已提交但工单登记(独立事务)丢失"的原子性缺口
    —— 这是渠道收益记账/冲销 exactly-once 耐久补偿的前提(见 wallet_db._record_channel_revenue_if_applicable /
    referral_api 退款冲销)。

    - ON CONFLICT DO NOTHING 幂等(重复登记被去重返 None);
    - 🔴 INSERT 若因表缺失/约束等抛错 → 【不吞】,由调用方事务回滚(fail-closed 兜底:主流程一并回滚重试,
      绝不"记账丢失还静默完成订单")。返回 work-order id;被去重返 None。
    - 🔴 [v12 item4] channel_revenue 补偿【必须】在 exactly-once 唯一约束健康时才登记:约束缺失/定义错/重复未清 →
      ON CONFLICT DO NOTHING 会退化成裸插入制造更多重复 = fail-open。故先自检,不健康则【fail-closed 抛错】,
      调用方事务回滚(主流程 fail-closed 重试/等人工),绝不谎报"补偿工单已耐久"。
    """
    if source == "channel_revenue":
        _ok, _why = channel_revenue_index_healthy(cursor)
        if not _ok:
            raise RuntimeError(
                f"[fund_recovery][fail-closed] channel_revenue exactly-once 约束不健康({_why})· "
                f"拒绝登记补偿工单(避免 fail-open 制造重复)· 需 prestart migration 修复后重启")
    cursor.execute(
        """
        INSERT INTO fund_recovery_orders
            (source, ref_key, user_id, feature_code, kind, status, reason, last_error, payload)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        (source, ref_key, user_id, feature_code, kind, status, reason, last_error,
         json.dumps(payload or {}, ensure_ascii=False)),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return int(row["id"] if isinstance(row, dict) else row[0])


# ============================================================
# [v6 req3] 处理器:claim/retry CAS + 退避 + 终态 + 人工处置
# ============================================================

MAX_AUTO_ATTEMPTS = 6            # 超过 → 转 manual(人工)
_BACKOFF_SEC = (30, 120, 300, 900, 1800, 3600)  # 指数退避(封顶 1h)


def _backoff_seconds(retry_count: int) -> int:
    idx = min(max(retry_count, 0), len(_BACKOFF_SEC) - 1)
    return _BACKOFF_SEC[idx]


STALE_PROCESSING_SEC = 600   # [v6 对抗审 P2] processing 超此秒数视为 orphan(worker 崩/蓝绿重启)→ 可被重新认领

def claim_next_recovery_order(worker_id: str = "reconcile") -> Optional[dict]:
    """[v6 req3] 原子认领一条到期 pending 工单(FOR UPDATE SKIP LOCKED · CAS 置 processing)。

    [v6 对抗审 P2 修] 同时回收【卡死的 processing】(claimed_at 超 STALE_PROCESSING_SEC · worker 崩/蓝绿重启遗留):
      否则认领后 commit=processing 再崩 → 该工单永不再被扫、退款静默停止补偿。回收即重新处理(退避/超限转 manual 仍生效)。
    并发 scheduler/worker 各自只领到不同工单(不重复处理)。无可领工单返 None。
    """
    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        # [v7 finding3] 每次认领生成【新 claim_token】(含 stale-processing 回收 → 新 token 使旧 worker 的 lease 失效)。
        #   gen_random_uuid() PG13+ 内置(无需 pgcrypto)。resolve/requeue 必须持同 token 才生效(worker fencing)。
        # [v7 对抗审 P3] stale-processing 回收时【bump retry_count】—— 纯硬崩(SIGKILL/OOM/蓝绿重启)不走 requeue,
        #   retry_count 永不增,确定性崩溃会无限 claim→崩→回收 死循环、永不转 manual、永不告警。回收即计次;
        #   计到 MAX_AUTO_ATTEMPTS 直接转 manual(不再 processing 交回)+ CRITICAL,由人工介入。
        cur.execute("""
            WITH nxt AS (
                SELECT id, retry_count, (status = 'processing') AS was_stale
                  FROM fund_recovery_orders
                 WHERE (status = 'pending' AND next_retry_at <= NOW())
                    OR (status = 'processing' AND claimed_at < NOW() - (%s || ' seconds')::interval)
                 ORDER BY next_retry_at ASC
                 FOR UPDATE SKIP LOCKED
                 LIMIT 1
            )
            UPDATE fund_recovery_orders f
               SET retry_count = CASE WHEN nxt.was_stale THEN f.retry_count + 1 ELSE f.retry_count END,
                   status = CASE WHEN nxt.was_stale AND nxt.retry_count + 1 >= %s THEN 'manual' ELSE 'processing' END,
                   claimed_at = NOW(), worker_id = %s, claim_token = gen_random_uuid()::text, updated_at = NOW()
              FROM nxt WHERE f.id = nxt.id
            RETURNING f.*, nxt.was_stale AS _was_stale
        """, (str(STALE_PROCESSING_SEC), MAX_AUTO_ATTEMPTS, worker_id))
        row = cur.fetchone()
        if row and row.get("status") == "manual":
            _enqueue_manual_notification(cur, int(row["id"]))
        conn.commit()
        if not row:
            return None
        d = dict(row)
        # 回收计次触顶 → 已在同一 UPDATE 转 manual(人工桶)· CRITICAL 告警。仍返回该行(status='manual'),
        #   由处理器识别 manual 跳过并【继续扫下一单】(不作为 processing 处理 · 不 break 整个扫描循环)。
        if d.get("status") == "manual":
            logger.critical(f"[FundRecovery][CRITICAL] 工单 id={d['id']} stale-processing 回收累计 {d.get('retry_count')} 次仍未收口 "
                            f"→ 转 manual 人工处置(疑似确定性崩溃循环)· source={d.get('source')} ref={d.get('ref_key')}")
        return d
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def resolve_recovery_order(order_id: int, note: Optional[str] = None) -> bool:
    """[admin 处置] 终态 resolved(admin 确认已处理 / 人工收口)。仅 pending 或 manual → resolved。

    [v7 finding3 修] 不再允许从 processing 结案 —— processing 是【live worker 持锁中】,admin 结案会与 worker
      竞态(worker 随后 requeue 又翻回 pending / 或双动钱)。processing 单交给 worker 自己收口或 stale reaper 回收。
    """
    return _terminal(order_id, "resolved", note)


def fail_recovery_order(order_id: int, note: Optional[str] = None) -> bool:
    """[admin 处置] 终态 failed(admin 判定无需/无法补偿)。仅 pending 或 manual(见 resolve 说明 · 不含 processing)。"""
    return _terminal(order_id, "failed", note)


def _terminal(order_id: int, status: str, note: Optional[str]) -> bool:
    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        # [v7 finding3] admin 终态只作用于 pending/manual(排除 processing · worker fencing)
        cur.execute(
            "UPDATE fund_recovery_orders SET status=%s, resolved_at=NOW(), updated_at=NOW(), "
            "last_error=COALESCE(%s, last_error) WHERE id=%s AND status IN ('pending','manual') "
            "RETURNING id",
            (status, note, order_id),
        )
        ok = cur.fetchone() is not None
        conn.commit()
        return ok
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def resolve_recovery_order_by_worker(order_id: int, claim_token: str, note: Optional[str] = None) -> bool:
    """[v7 finding3 · worker 处置] 处理成功 → resolved · 必须持【当前 claim_token】(worker fencing)。

    若 lease 已失效(被 stale reaper 重新分派 · claim_token 已换)→ 命中 0 行 no-op(返回 False)· 由新 worker 收口。
    """
    if not claim_token:
        return False
    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        work_select = """
            SELECT source, kind, user_id, feature_code, charge_tx_id,
                   ledger_type, payload, amount_points
              FROM fund_recovery_orders
             WHERE id=%s AND status='processing' AND claim_token=%s
        """
        cur.execute(
            work_select,
            (order_id, claim_token),
        )
        work_hint = cur.fetchone()
        if work_hint is None:
            conn.commit()
            return False

        def _work_value(row, name: str, index: int):
            return row.get(name) if isinstance(row, dict) else row[index]

        source = (
            work_hint.get("source")
            if isinstance(work_hint, dict)
            else work_hint[0]
        )
        kind = (
            work_hint.get("kind")
            if isinstance(work_hint, dict)
            else work_hint[1]
        )
        refund_context = None
        if source == "report_export" and kind == "refund":
            payload = (
                work_hint.get("payload")
                if isinstance(work_hint, dict)
                else work_hint[6]
            )
            if isinstance(payload, str):
                payload = json.loads(payload)
            idempotency_key = payload.get("idempotency_key") if isinstance(payload, dict) else None
            if idempotency_key:
                work_user_id = int(_work_value(work_hint, "user_id", 2))
                work_feature_code = str(_work_value(work_hint, "feature_code", 3))
                work_charge_tx_id = int(_work_value(work_hint, "charge_tx_id", 4))
                work_ledger_type = str(_work_value(work_hint, "ledger_type", 5))
                from services.billing_debt_offset_outbox import (
                    lock_charge_refund_context_cursor,
                )

                # Global order shared with direct refunds and debt workers:
                # lifecycle -> debt outbox -> recovery row -> debt rows.
                refund_context = lock_charge_refund_context_cursor(
                    cur,
                    user_id=work_user_id,
                    feature_code=work_feature_code,
                    charge_tx_id=work_charge_tx_id,
                    ledger_type=work_ledger_type,
                )

        cur.execute(work_select + " FOR UPDATE", (order_id, claim_token))
        work_order = cur.fetchone()
        if work_order is None:
            conn.commit()
            return False
        locked_identity = tuple(
            _work_value(work_order, name, index)
            for name, index in (
                ("source", 0),
                ("kind", 1),
                ("user_id", 2),
                ("feature_code", 3),
                ("charge_tx_id", 4),
                ("ledger_type", 5),
                ("amount_points", 7),
            )
        )
        hinted_identity = tuple(
            _work_value(work_hint, name, index)
            for name, index in (
                ("source", 0),
                ("kind", 1),
                ("user_id", 2),
                ("feature_code", 3),
                ("charge_tx_id", 4),
                ("ledger_type", 5),
                ("amount_points", 7),
            )
        )
        if locked_identity != hinted_identity:
            raise RuntimeError("fund recovery immutable identity changed while locking")

        if source == "report_export" and kind == "refund":
            payload = _work_value(work_order, "payload", 6)
            if isinstance(payload, str):
                payload = json.loads(payload)
            idempotency_key = payload.get("idempotency_key") if isinstance(payload, dict) else None
            if idempotency_key:
                work_user_id = int(_work_value(work_order, "user_id", 2))
                work_feature_code = str(_work_value(work_order, "feature_code", 3))
                work_charge_tx_id = int(_work_value(work_order, "charge_tx_id", 4))
                work_ledger_type = str(_work_value(work_order, "ledger_type", 5))
                work_amount_points = int(_work_value(work_order, "amount_points", 7))
                confirmed_refund_points = _confirmed_refund_points_cursor(
                    cur,
                    user_id=work_user_id,
                    feature_code=work_feature_code,
                    charge_tx_id=work_charge_tx_id,
                    ledger_type=work_ledger_type,
                )
                if confirmed_refund_points < work_amount_points:
                    raise RuntimeError("report export refund evidence is incomplete")
                from services.billing_debt_offset_outbox import (
                    reconcile_charge_refund_cursor,
                )
                reconciliation = reconcile_charge_refund_cursor(
                    cur,
                    context=refund_context,
                    confirmed_refund_points=confirmed_refund_points,
                )
                if reconciliation.get("lifecycle_status") != "refunded":
                    raise RuntimeError("report export refunded lifecycle CAS failed")
                if reconciliation.get("status") not in ("cancelled", "reversed"):
                    raise RuntimeError("report export debt offset refund is incomplete")
        cur.execute(
            "UPDATE fund_recovery_orders SET status='resolved', resolved_at=NOW(), updated_at=NOW(), "
            "last_error=COALESCE(%s, last_error) WHERE id=%s AND status='processing' AND claim_token=%s "
            "RETURNING id",
            (note, order_id, claim_token),
        )
        ok = cur.fetchone() is not None
        conn.commit()
        return ok
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def requeue_recovery_order(order_id: int, error: Optional[str] = None, claim_token: Optional[str] = None) -> int:
    """[worker 处置] 处理失败 → 退避重排(processing→pending · retry_count++ · next_retry_at=NOW()+backoff)。
    超过 MAX_AUTO_ATTEMPTS → 转 manual(人工)+ 返回新 retry_count(调用方据此告警)。

    [v7 finding3 修] worker fencing:必须持【当前 claim_token】才生效(WHERE status='processing' AND claim_token=%s)。
      迟到 worker(lease 已被 stale reaper 换新 token)命中 0 行 → 【不改状态 · 返回 -1】· 绝不把 resolved 翻回 pending。
    """
    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        # 只锁【自己仍持锁】的 processing 行(claim_token 匹配)· lease 失效则 r=None → 不动
        if claim_token:
            cur.execute("SELECT retry_count FROM fund_recovery_orders WHERE id=%s AND status='processing' "
                        "AND claim_token=%s FOR UPDATE", (order_id, claim_token))
        else:
            # 无 token(兼容旧调用)· 仍限定 processing 防翻 resolved
            cur.execute("SELECT retry_count FROM fund_recovery_orders WHERE id=%s AND status='processing' FOR UPDATE",
                        (order_id,))
        r = cur.fetchone()
        if not r:
            conn.commit()
            return -1   # lease 失效 / 非 processing → 不动(与"工单不存在"区分)
        n = (r["retry_count"] if isinstance(r, dict) else r[0]) + 1
        if n >= MAX_AUTO_ATTEMPTS:
            cur.execute(
                "UPDATE fund_recovery_orders SET status='manual', retry_count=%s, last_error=%s, "
                "claim_token=NULL, updated_at=NOW() WHERE id=%s RETURNING id",
                (n, str(error)[:400] if error else None, order_id))
            moved_manual = cur.fetchone()
            if moved_manual:
                _enqueue_manual_notification(cur, int(order_id))
            logger.critical(f"[FundRecovery][CRITICAL] 工单 id={order_id} 自动补偿 {n} 次仍失败 → 转 manual 人工处置 · err={error}")
        else:
            cur.execute(
                "UPDATE fund_recovery_orders SET status='pending', retry_count=%s, last_error=%s, "
                "next_retry_at=NOW() + (%s || ' seconds')::interval, claimed_at=NULL, worker_id=NULL, "
                "claim_token=NULL, updated_at=NOW() WHERE id=%s",
                (n, str(error)[:400] if error else None, str(_backoff_seconds(n)), order_id))
        conn.commit()
        return n
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


_DEFER_SEC = 300   # [v10 item3] 退款在途工单延后间隔(5min · 与 scheduler 1min tick 解耦)


def defer_recovery_order(order_id: int, claim_token: Optional[str] = None,
                         note: Optional[str] = None, delay_sec: int = _DEFER_SEC) -> bool:
    """[v10 item3 · worker 处置] 延后一条【已认领】工单 —— 用于"退款在途(pending/approved)正常等待"。

    区别于 requeue_recovery_order:**只推 next_retry_at · 绝不累计 retry_count**(故永不因等待触 MAX→manual)。
    worker fencing:必须持【当前 claim_token】(WHERE status='processing' AND claim_token=%s)· 迟到 worker 命中 0 行 no-op。
    """
    if not claim_token:
        return False
    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            "UPDATE fund_recovery_orders SET status='pending', "
            "next_retry_at=NOW() + (%s || ' seconds')::interval, claimed_at=NULL, worker_id=NULL, "
            "claim_token=NULL, updated_at=NOW(), last_error=COALESCE(%s, last_error) "
            "WHERE id=%s AND status='processing' AND claim_token=%s RETURNING id",
            (str(int(max(1, delay_sec))), note, order_id, claim_token),
        )
        ok = cur.fetchone() is not None
        conn.commit()
        return ok
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def refund_evidence_exists(
    user_id: Optional[int],
    charge_tx_id: Optional[int],
    ledger_type: Optional[str] = None,
    feature_code: Optional[str] = None,
) -> bool:
    """[v7 finding6 · v8 P1-1 修] 只读:某笔 charge 是否【已有退款流水证据】· **只查工单声明的对应账本**。

    退款成功后工单未 resolved 就崩溃 → 重试 refund_points 因该 charge 已退而返"未找到扣费记录"(与"从未扣费"不可区分)。
    据退款流水证据(refund 行 order_id/related_order_id = 被退 consume 的 id = charge_tx_id)判【幂等已完成】→ 应 resolved。

    🔴 [v8 P1-1] point_transactions 与 customer_credit_transactions 的 id 空间【彼此独立】,同一数字可能在两表都存在。
       旧实现两表都查 → V3.5 扣费 id=N 未退,但恰有同 user 无关 legacy 退款 order_id=N → false-positive →
       客户【实际未退款】工单却被误 resolved(资金损失)。故必须按
       user_id + feature_code + charge_tx_id + ledger_type 完整身份核验。
       任一身份字段缺失 → **保守返 False**(不谎报已退 → 交重试/人工;
       false-negative 安全:退款幂等最多重试;false-positive 才致漏退)。
    """
    if not feature_code or not charge_tx_id or ledger_type not in ("legacy", "v35"):
        return False   # 保守:身份不完整 → 不认定已退(宁可重试也不误 resolved 致漏退)
    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()

        return _refund_evidence_exists_cursor(
            cur,
            user_id=user_id,
            feature_code=feature_code,
            charge_tx_id=charge_tx_id,
            ledger_type=ledger_type,
        )
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def list_recovery_orders(status: Optional[str] = None, limit: int = 100, offset: int = 0) -> list[dict]:
    """人工处置后台列表(admin)。status=None → 未终结(pending/processing/manual)。"""
    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        if status:
            cur.execute("SELECT * FROM fund_recovery_orders WHERE status=%s ORDER BY id DESC LIMIT %s OFFSET %s",
                        (status, min(limit, 500), offset))
        else:
            cur.execute("SELECT * FROM fund_recovery_orders WHERE status IN ('pending','processing','manual') "
                        "ORDER BY id DESC LIMIT %s OFFSET %s", (min(limit, 500), offset))
        return [dict(r) for r in cur.fetchall()]
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def has_open_workorder(source: str, ref_key: str) -> bool:
    """[v8 P2] 是否存在【未终结】(pending/processing/manual)的 (source, ref_key) 工单 · 供巡检去重。"""
    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM fund_recovery_orders WHERE source=%s AND ref_key=%s "
                    "AND status IN ('pending','processing','manual') LIMIT 1", (source, ref_key))
        return cur.fetchone() is not None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def list_open_workorders_by_source(source: str, limit: int = 500, before_id: Optional[int] = None) -> list[dict]:
    """[v8 对抗审 P2] 列某 source 的【未终结】工单(pending/processing/manual)· 供巡检反向收口(任务已终态则关工单)。

    [v8 二轮对抗审 P3] 支持 id 游标(before_id)分页排空 · 避免固定 LIMIT 500 时旧孤儿被新工单挤出窗口永不扫到。
    """
    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        if before_id is not None:
            cur.execute("SELECT id, ref_key, payload, status FROM fund_recovery_orders "
                        "WHERE source=%s AND status IN ('pending','processing','manual') AND id < %s "
                        "ORDER BY id DESC LIMIT %s", (source, before_id, min(int(limit or 500), 1000)))
        else:
            cur.execute("SELECT id, ref_key, payload, status FROM fund_recovery_orders "
                        "WHERE source=%s AND status IN ('pending','processing','manual') ORDER BY id DESC LIMIT %s",
                        (source, min(int(limit or 500), 1000)))
        return [dict(r) for r in cur.fetchall()]
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_recovery_order(order_id: int) -> Optional[dict]:
    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM fund_recovery_orders WHERE id=%s", (order_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
