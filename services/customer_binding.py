"""
V3.5 W2 · 客户-代理绑定服务

核心责任:
- 处理显式商业服务操作产生的当前绑定；已核验的服务商注册邀请可以调用本服务
- UNIQUE 约束保护:`customer_agent_bindings.customer_user_id` NOT NULL UNIQUE
  → 已绑代理冲突时**不覆盖** + UPDATE 原 row 的 dispute_status='pending' + dispute_note
- 代理预付订单(order_type='agent_inventory_purchase')**严禁**写绑定

业务规则铁律:
- referral SSOT 始终保留来源事实；合格服务商邀请可同时建立普通客户首次商业归属
- 支付回调只消费订单快照，严禁创建或重建绑定
- agent_inventory_purchase 走 complete_recharge 早分支 · 不调本服务
- dispute 历史多条留 W4 单独建表(`customer_agent_binding_disputes`)
- W4 才做 admin 解决 dispute · W2 仅展示状态(只读)

关联:
- docs/AI-CONTEXT/V35_FACTORY_INVENTORY_MODEL_v6_2026-05-26.md 章九 W2 边界
- memory feedback_v35_factory_inventory_model_v6
"""

import logging
from typing import Optional, Dict, Any

logger = logging.getLogger("GEO-V35-CustomerBinding")

# A verified registration invitation from an active service provider is an
# explicit customer-service choice. Payment callbacks remain forbidden writers.
ALLOWED_BINDING_SOURCES = {
    "admin_manual", "invite_code", "historical_referral_backfill",
}


class CommercialBindingSubjectError(ValueError):
    """The requested subject is not an ordinary-customer identity."""


def upsert_customer_agent_binding(
    cursor,
    customer_user_id: int,
    agent_user_id: int,
    binding_source: str,
    source_token: Optional[str] = None,
) -> Dict[str, Any]:
    """
    客户绑定 upsert · 主事务内 atomic

    规则:
    - 客户**未绑定** → INSERT 新 binding
    - 客户**已绑定**且 agent_user_id 一致 → 幂等 no-op(可能更新 source_token)
    - 客户**已绑定**且 agent_user_id 冲突 → UPDATE 原 row dispute_status='pending' + dispute_note
                                            **不覆盖** 原 agent_user_id(由 admin / W4 治理层裁决)

    返回:
        {
            "action": "inserted" | "noop" | "dispute_marked",
            "agent_user_id": <最终生效的代理>,
            "dispute_status": "pending" | None,
        }
    """
    if binding_source not in ALLOWED_BINDING_SOURCES:
        raise ValueError(f"binding_source 必须在 {ALLOWED_BINDING_SOURCES} · 实际 {binding_source!r}")

    # Serialize both existing rows and the "no row yet" case with admin governance.
    # A row lock alone cannot protect an absent customer binding.
    from services.commercial_service_routing import lock_commercial_binding_subject

    lock_commercial_binding_subject(cursor, int(customer_user_id))
    cursor.execute(
        """SELECT agent_level FROM user_wallets
           WHERE user_id = %s FOR UPDATE""",
        (int(customer_user_id),),
    )
    identity = cursor.fetchone()
    if identity is None:
        raise CommercialBindingSubjectError("客户业务身份不可用，已拒绝写入商业服务归属")
    agent_level = _row_get(identity, "agent_level", 0)
    if agent_level is None or int(agent_level) >= 1:
        raise CommercialBindingSubjectError("服务商不能写入客户商业服务归属")

    # FOR UPDATE 锁行 · 防 race
    cursor.execute("""
        SELECT id, agent_user_id, binding_source, source_token, dispute_status
        FROM customer_agent_bindings
        WHERE customer_user_id = %s
        FOR UPDATE
    """, (customer_user_id,))
    existing = cursor.fetchone()

    if existing is None:
        # 未绑定 · INSERT
        cursor.execute("""
            INSERT INTO customer_agent_bindings
                (customer_user_id, agent_user_id, binding_source, source_token, bound_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (customer_user_id) DO NOTHING
            RETURNING id
        """, (customer_user_id, agent_user_id, binding_source, source_token))
        row = cursor.fetchone()
        if row:
            from services.commercial_binding_history import record_automatic_binding

            binding_id = _row_get(row, "id", 0)
            record_automatic_binding(
                cursor,
                customer_user_id=int(customer_user_id),
                provider_user_id=int(agent_user_id),
                source_binding_id=int(binding_id),
                binding_source=binding_source,
                source_token=source_token,
            )
            logger.info(
                f"[binding] customer={customer_user_id} → agent={agent_user_id} "
                f"source={binding_source} token={source_token}"
            )
            return {"action": "inserted", "agent_user_id": agent_user_id, "dispute_status": None}
        # race 兜底:刚刚被另一笔写入 · 重读
        cursor.execute("""
            SELECT id, agent_user_id, dispute_status FROM customer_agent_bindings
            WHERE customer_user_id = %s
        """, (customer_user_id,))
        existing = cursor.fetchone()

    existing_agent_id = _row_get(existing, "agent_user_id", 1)
    existing_dispute = _row_get(existing, "dispute_status", 4)

    if existing_agent_id == agent_user_id:
        # 已绑且一致 · 幂等
        logger.debug(
            f"[binding] customer={customer_user_id} 已绑同代理 {agent_user_id} · noop"
        )
        return {"action": "noop", "agent_user_id": existing_agent_id, "dispute_status": existing_dispute}

    # 冲突:已绑代理 X · 来了代理 Y
    # 严禁 INSERT bindings(违 UNIQUE) · 严禁覆盖 · UPDATE 原 row dispute_status='pending'
    dispute_note = (
        f"收到来自 agent_user_id={agent_user_id}(source={binding_source}"
        f"{f', token={source_token}' if source_token else ''})的新绑定请求 · "
        f"与原 agent_user_id={existing_agent_id} 冲突 · 由 admin 裁决"
    )
    cursor.execute("""
        UPDATE customer_agent_bindings
        SET dispute_status = 'pending',
            dispute_note = %s
        WHERE customer_user_id = %s
    """, (dispute_note, customer_user_id))

    # [V3.5 W4] 同时 INSERT 到历史 disputes 表(audit · 多次冲突保留)
    # [boss r5 P1] 用 to_regclass 先查表存在 · 防 W4 migration 未跑时 UndefinedTable
    #              抛错污染主事务(PG aborted state · catch 后续 SQL 全失败)
    cursor.execute("SELECT to_regclass('customer_agent_binding_disputes') AS reg")
    _reg_row = cursor.fetchone()
    _reg = _reg_row.get("reg") if isinstance(_reg_row, dict) else (_reg_row[0] if _reg_row else None)
    if _reg is not None:
        cursor.execute("""
            INSERT INTO customer_agent_binding_disputes
                (customer_user_id, old_agent_user_id, new_agent_user_id,
                 binding_source, source_token, status, note)
            VALUES (%s, %s, %s, %s, %s, 'pending', %s)
        """, (
            customer_user_id, existing_agent_id, agent_user_id,
            binding_source, source_token, dispute_note,
        ))
    else:
        # W4 migration 未跑(staging 早期 · prod 部署时 migration 先于代码 · 不会到此)
        logger.warning(
            "[binding] W4 customer_agent_binding_disputes 表不存在 · 跳过 INSERT · "
            "仅 customer_agent_bindings.dispute_status column 已 UPDATE"
        )

    logger.warning(
        f"[binding] customer={customer_user_id} dispute pending · "
        f"原 agent={existing_agent_id} · 新 agent={agent_user_id}"
    )
    return {
        "action": "dispute_marked",
        "agent_user_id": existing_agent_id,  # 原代理不变
        "dispute_status": "pending",
    }


def get_customer_binding(cursor, customer_user_id: int) -> Optional[Dict[str, Any]]:
    """读客户当前绑定 · 不锁行"""
    cursor.execute("""
        SELECT id, customer_user_id, agent_user_id, binding_source, source_token,
               bound_at, dispute_status, dispute_note
        FROM customer_agent_bindings
        WHERE customer_user_id = %s
    """, (customer_user_id,))
    row = cursor.fetchone()
    if not row:
        return None
    if isinstance(row, dict):
        return dict(row)
    return {
        "id": row[0], "customer_user_id": row[1], "agent_user_id": row[2],
        "binding_source": row[3], "source_token": row[4], "bound_at": row[5],
        "dispute_status": row[6], "dispute_note": row[7],
    }


def list_agent_customers(
    cursor, agent_user_id: int, limit: int = 50, offset: int = 0
) -> Dict[str, Any]:
    """
    推广中心 · 代理已绑客户列表(脱敏)

    Returns: {items: [...], total: N}
    """
    cursor.execute("""
        SELECT COUNT(*) AS c FROM customer_agent_bindings WHERE agent_user_id = %s
    """, (agent_user_id,))
    row = cursor.fetchone()
    total = _row_get(row, "c", 0)

    cursor.execute("""
        SELECT cab.customer_user_id, cab.binding_source, cab.source_token,
               cab.bound_at, cab.dispute_status,
               u.display_name, u.phone, u.username,
               -- [单账本收敛 2026-07-27] 改读客户 user_wallets(信用钱包迁移后恒 0)
               -- 字段名沿用,publish_credit 恒 0(单账本无独立发布池)
               COALESCE(uw.paid_points, 0) AS tool_credit,
               0 AS publish_credit,
               COALESCE(uw.bonus_points, 0) AS bonus_credit
        FROM customer_agent_bindings cab
        LEFT JOIN users u ON u.id = cab.customer_user_id
        LEFT JOIN user_wallets uw ON uw.user_id = cab.customer_user_id
        WHERE cab.agent_user_id = %s
        ORDER BY cab.bound_at DESC
        LIMIT %s OFFSET %s
    """, (agent_user_id, limit, offset))
    items = []
    for row in cursor.fetchall():
        d = dict(row) if isinstance(row, dict) else _row_to_dict(row, [
            "customer_user_id", "binding_source", "source_token", "bound_at",
            "dispute_status", "display_name", "phone", "username",
            "tool_credit", "publish_credit", "bonus_credit",
        ])
        # 脱敏手机号
        if d.get("phone"):
            phone = d["phone"]
            if len(phone) >= 11:
                d["phone_masked"] = phone[:3] + "****" + phone[-4:]
            else:
                d["phone_masked"] = "***"
        else:
            d["phone_masked"] = None
        d.pop("phone", None)  # 严禁返回原始号
        # 不返回 username(可能含邮箱)
        d.pop("username", None)
        items.append(d)
    return {"items": items, "total": total}


# ============================================================
# 内部 helper
# ============================================================

def _row_get(row, key: str, idx: int = None, default=None):
    """row 可能是 dict 或 tuple · 兼容两种"""
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    if idx is not None and idx < len(row):
        return row[idx]
    return default


def _row_to_dict(row, keys: list) -> Dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return {k: row[i] for i, k in enumerate(keys) if i < len(row)}
