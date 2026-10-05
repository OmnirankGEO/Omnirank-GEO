"""内部结算与管理员审计账号编号(§8.1 / §16 Q32)

SV/CH 编号只进入管理员接口、内部报价快照和审计记录。普通客户、服务商、
公开链接、导出和错误响应均不得序列化这些编号。
铁律:
  - 绝不用递增 user_id 当对外编号(可枚举反查)。
  - 编号随机、不可枚举、不可反查实名。实名/KYC 仅 admin/财务经权限接口可查。
  - code -> 实名 的映射只存在 public_account_codes；非管理员 API 不返回 code。

红线:不碰 users 表(独立映射表);不写税/实名字段。
"""

import logging
import secrets
from typing import Any, Dict, Iterable, List, Optional, Sequence

import psycopg2

from db.connection import get_db

logger = logging.getLogger("GEO-AccountCodes")

# 无易混字符(去掉 0/O/1/I),8 位 → 32^8 ≈ 1.1e12 空间 · 不可枚举
_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def _rand_suffix(n: int = 8) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


_CODE_COLUMNS = {
    "service": ("service_account_code", "SV"),
    "channel": ("channel_account_code", "CH"),
}
_ADVISORY_NAMESPACE = 920715


def _read_code(user_id: int, column: str, cur=None) -> Optional[str]:
    """只读内部编号。仅管理员与内部定价范围解析调用。"""
    if user_id is None:
        raise ValueError("user_id required")
    sql = f"SELECT {column} FROM public_account_codes WHERE user_id = %s"
    if cur is not None:
        cur.execute(sql, (int(user_id),))
        row = cur.fetchone()
        return row.get(column) if isinstance(row, dict) and row.get(column) else None
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, (int(user_id),))
        row = cursor.fetchone()
        return row.get(column) if isinstance(row, dict) and row.get(column) else None


def get_service_code(user_id: int, cur=None) -> Optional[str]:
    """只读服务商公开编号；缺失返回 None，绝不创建。"""
    return _read_code(user_id, "service_account_code", cur=cur)


def get_channel_code(user_id: int, cur=None) -> Optional[str]:
    """只读渠道公开编号；缺失返回 None，绝不创建。"""
    return _read_code(user_id, "channel_account_code", cur=cur)


def _prepare_one(
    cur, user_id: int, column: str, prefix: str, *, validate_user: bool = True,
) -> tuple[str, bool]:
    """admin 治理事务内生成单个编号；per-user lock 保证跨 worker 幂等。"""
    user_id = int(user_id)
    cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (_ADVISORY_NAMESPACE, user_id))
    if validate_user:
        cur.execute("SELECT 1 FROM users WHERE id=%s AND COALESCE(is_active, 1)=1", (user_id,))
        if not cur.fetchone():
            raise ValueError(f"目标账号 {user_id} 不存在或已停用")
    existing = _read_code(user_id, column, cur=cur)
    if existing:
        return existing, False

    # 先保证 user_id 行存在；随后用 savepoint 隔离极低概率的唯一编号碰撞。
    cur.execute(
        "INSERT INTO public_account_codes(user_id) VALUES (%s) ON CONFLICT(user_id) DO NOTHING",
        (user_id,),
    )
    for attempt in range(12):
        candidate = f"{prefix}-{_rand_suffix()}"
        savepoint = f"account_code_{attempt}"
        cur.execute(f"SAVEPOINT {savepoint}")
        try:
            cur.execute(
                f"UPDATE public_account_codes SET {column}=%s "
                f"WHERE user_id=%s AND {column} IS NULL RETURNING {column}",
                (candidate, user_id),
            )
            row = cur.fetchone()
            cur.execute(f"RELEASE SAVEPOINT {savepoint}")
            if row:
                return row[column], True
            existing = _read_code(user_id, column, cur=cur)
            if existing:
                return existing, False
        except psycopg2.errors.UniqueViolation:  # unique code collision; rollback only this attempt
            cur.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            cur.execute(f"RELEASE SAVEPOINT {savepoint}")
    raise RuntimeError("account_code 生成连续碰撞(异常)")


def _normalize_ids(values: Optional[Iterable[int]]) -> List[int]:
    return sorted({int(value) for value in (values or []) if int(value) > 0})


def _default_targets(cur) -> Dict[str, List[int]]:
    """目标集合只取既有业务事实；不会据此创建任何渠道上下级关系。"""
    cur.execute(
        """
        SELECT DISTINCT u.id
        FROM users u
        LEFT JOIN user_wallets w ON w.user_id=u.id
        WHERE COALESCE(u.is_active, 1)=1
          AND (
            COALESCE(w.agent_level, 0) >= 1
            OR EXISTS (SELECT 1 FROM customer_agent_bindings b WHERE b.agent_user_id=u.id)
            OR EXISTS (
                SELECT 1 FROM agent_sku_overrides o
                WHERE o.agent_user_id=u.id AND o.is_active=TRUE AND o.deleted_at IS NULL
            )
          )
        ORDER BY u.id
        """
    )
    service_ids = [int(row["id"]) for row in cur.fetchall()]
    cur.execute(
        """
        SELECT DISTINCT upstream_channel_account_id AS id
        FROM channel_pricing_relationships
        WHERE status='active' AND effective_to IS NULL
        ORDER BY upstream_channel_account_id
        """
    )
    channel_ids = [int(row["id"]) for row in cur.fetchall()]
    return {"service_user_ids": service_ids, "channel_user_ids": channel_ids}


def _targets(cur, service_user_ids: Optional[Sequence[int]],
             channel_user_ids: Optional[Sequence[int]]) -> Dict[str, List[int]]:
    defaults = _default_targets(cur)
    return {
        "service_user_ids": (
            _normalize_ids(service_user_ids) if service_user_ids is not None
            else defaults["service_user_ids"]
        ),
        "channel_user_ids": (
            _normalize_ids(channel_user_ids) if channel_user_ids is not None
            else defaults["channel_user_ids"]
        ),
    }


def account_code_dry_run(
    *, service_user_ids: Optional[Sequence[int]] = None,
    channel_user_ids: Optional[Sequence[int]] = None,
) -> Dict[str, Any]:
    """admin-only caller 的只读影响分析；本函数自身零写入。"""
    with get_db() as conn:
        cur = conn.cursor()
        target = _targets(cur, service_user_ids, channel_user_ids)
        service_rows: List[Dict[str, Any]] = []
        for user_id in target["service_user_ids"]:
            cur.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM customer_agent_bindings WHERE agent_user_id=%s) AS bound_customers,
                  (SELECT COUNT(*) FROM agent_sku_overrides
                   WHERE agent_user_id=%s AND is_active=TRUE AND deleted_at IS NULL) AS active_skus
                """,
                (user_id, user_id),
            )
            impact = dict(cur.fetchone())
            code = get_service_code(user_id, cur=cur)
            service_rows.append({
                "user_id": user_id, "code": code, "missing": code is None,
                "bound_customers": int(impact.get("bound_customers") or 0),
                "active_skus": int(impact.get("active_skus") or 0),
            })
        channel_rows: List[Dict[str, Any]] = []
        for user_id in target["channel_user_ids"]:
            cur.execute(
                """SELECT COUNT(*) AS downstream_count FROM channel_pricing_relationships
                   WHERE upstream_channel_account_id=%s AND status='active' AND effective_to IS NULL""",
                (user_id,),
            )
            impact = dict(cur.fetchone())
            code = get_channel_code(user_id, cur=cur)
            channel_rows.append({
                "user_id": user_id, "code": code, "missing": code is None,
                "downstream_count": int(impact.get("downstream_count") or 0),
            })
        return {
            "service_accounts": service_rows,
            "channel_accounts": channel_rows,
            "target_service_count": len(service_rows),
            "target_channel_count": len(channel_rows),
            "missing_service_count": sum(1 for row in service_rows if row["missing"]),
            "missing_channel_count": sum(1 for row in channel_rows if row["missing"]),
        }


def prepare_account_codes(
    *, service_user_ids: Optional[Sequence[int]] = None,
    channel_user_ids: Optional[Sequence[int]] = None,
) -> Dict[str, Any]:
    """admin-only 幂等批量准备。关系表只用于确定已有显式目标，绝不创建关系。"""
    with get_db() as conn:
        cur = conn.cursor()
        target = _targets(cur, service_user_ids, channel_user_ids)
        prepared: List[Dict[str, Any]] = []
        for kind, ids in (
            ("service", target["service_user_ids"]),
            ("channel", target["channel_user_ids"]),
        ):
            column, prefix = _CODE_COLUMNS[kind]
            for user_id in ids:
                code, created = _prepare_one(cur, user_id, column, prefix)
                prepared.append({"kind": kind, "user_id": user_id, "code": code, "created": created})
        return {
            "prepared": prepared,
            "created_count": sum(1 for row in prepared if row["created"]),
            "existing_count": sum(1 for row in prepared if not row["created"]),
        }


def account_code_status() -> Dict[str, Any]:
    """开闸使用的只读汇总；重复值即使约束被篡改也会被检测。"""
    detail = account_code_dry_run()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COALESCE(SUM(c - 1), 0) AS duplicates FROM (
              SELECT COUNT(*) AS c FROM public_account_codes
              WHERE service_account_code IS NOT NULL GROUP BY service_account_code HAVING COUNT(*) > 1
              UNION ALL
              SELECT COUNT(*) AS c FROM public_account_codes
              WHERE channel_account_code IS NOT NULL GROUP BY channel_account_code HAVING COUNT(*) > 1
            ) d
            """
        )
        duplicate_count = int(cur.fetchone()["duplicates"] or 0)
        cur.execute("SELECT COUNT(*) AS total FROM public_account_codes")
        row_count = int(cur.fetchone()["total"] or 0)
    blockers: List[str] = []
    if detail["missing_service_count"]:
        blockers.append(f"{detail['missing_service_count']} 个目标服务商缺少 SV 编号")
    if detail["missing_channel_count"]:
        blockers.append(f"{detail['missing_channel_count']} 个目标渠道缺少 CH 编号")
    if duplicate_count:
        blockers.append(f"公开编号存在 {duplicate_count} 个重复值")
    return {
        **detail,
        "row_count": row_count,
        "duplicate_count": duplicate_count,
        "ready": not blockers,
        "blockers": blockers,
    }


def _get_or_create(user_id: int, column: str, prefix: str) -> str:
    """兼容旧的受信管理调用；公开请求不得调用。"""
    with get_db() as conn:
        cur = conn.cursor()
        code, _ = _prepare_one(cur, user_id, column, prefix, validate_user=False)
        return code


def get_or_create_service_code(user_id: int) -> str:
    """兼容管理调用。客户/catalog/quote 请求请用 get_service_code。"""
    return _get_or_create(user_id, "service_account_code", "SV")


def get_or_create_channel_code(user_id: int) -> str:
    """兼容管理调用。客户/catalog/quote 请求请用 get_channel_code。"""
    return _get_or_create(user_id, "channel_account_code", "CH")


def resolve_user_by_service_code(code: str) -> Optional[int]:
    """仅供后端/财务:service_code -> user_id。绝不暴露给普通用户接口。"""
    if not code:
        return None
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id FROM public_account_codes WHERE service_account_code = %s", (code,)
        )
        row = cur.fetchone()
        return row["user_id"] if row else None


def resolve_user_by_channel_code(code: str) -> Optional[int]:
    """仅供后端/财务:channel_code -> user_id。"""
    if not code:
        return None
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id FROM public_account_codes WHERE channel_account_code = %s", (code,)
        )
        row = cur.fetchone()
        return row["user_id"] if row else None
