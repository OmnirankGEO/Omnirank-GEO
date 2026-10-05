"""Business identity SSOT backed only by ``user_wallets.agent_level``."""

from __future__ import annotations

from typing import Optional

from db.connection import get_db


class BusinessIdentityUnavailable(RuntimeError):
    """Raised when identity cannot be proven from PostgreSQL."""


def get_agent_level(user_id: int, *, cur=None) -> int:
    """Return the authoritative agent level; database/query failures fail closed."""
    if not user_id:
        raise BusinessIdentityUnavailable("用户身份缺失")
    sql = "SELECT agent_level FROM user_wallets WHERE user_id=%s"
    try:
        if cur is not None:
            cur.execute(sql, (int(user_id),))
            row = cur.fetchone()
        else:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(sql, (int(user_id),))
                row = cursor.fetchone()
        if not row:
            return 0
        if isinstance(row, dict):
            return int(row.get("agent_level") or 0)
        return int(row[0] or 0)
    except BusinessIdentityUnavailable:
        raise
    except Exception as exc:
        raise BusinessIdentityUnavailable("业务身份暂不可验证") from exc


def is_service_provider(user_id: int, *, cur=None) -> bool:
    return get_agent_level(user_id, cur=cur) >= 1
