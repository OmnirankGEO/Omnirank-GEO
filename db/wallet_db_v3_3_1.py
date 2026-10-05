"""
V3.3.1 钱包扩展查询 · 不动 db/wallet_db.py 主体(CLAUDE.md 红线)

提供 get_wallet_balance_with_service_fee(user_id) 给 wallet_api 调用:
- 兼容老返回结构(extends old wallet)
- 加 service_fee 五段统计(pending / settled / converted / withdrawn / debt)
- L1/L2 隔离:agent_level < 2 时 service_fee_* 全 0

关联:
- 决策书 §11
- RED_LINES R5(CLAUDE.md 红线免签)
"""

import logging
from datetime import datetime
from typing import Dict, Any, Optional

from db.connection import get_db
from db.wallet_db import get_wallet_balance as _get_old_wallet_balance
from config.v3_3_1_flags import is_v3_3_1_enabled, ServiceFeeStatus

logger = logging.getLogger("GEO-V3.3.1-Wallet")


def get_wallet_balance_with_service_fee(user_id: int) -> Dict[str, Any]:
    """老 get_wallet_balance + V3.3.1 service_fee 扩展

    Returns:
        {
            ...(原 wallet_db.get_wallet_balance 字段),
            "is_l2": bool,
            "v3_3_1_enabled": bool,
            "service_fee": {  # 仅 L2 显示具体值 · L1/L0 全 0
                "pending_yuan": float,
                "settled_yuan": float,
                "converted_yuan": float,
                "withdrawn_yuan": float,
                "debt_yuan": float,
                "bonus_debt_points": int,
            }
        }
    """
    base = _get_old_wallet_balance(user_id) or {}

    is_l2 = (base.get("agent_level") or 0) >= 2
    enabled = is_v3_3_1_enabled()

    service_fee = {
        "pending_yuan": 0.0,
        "settled_yuan": 0.0,
        "converted_yuan": 0.0,
        "withdrawn_yuan": 0.0,
        "debt_yuan": 0.0,
        "bonus_debt_points": 0,
    }

    # L1/L0 严禁见"服务费"字眼 · 全 0 返
    if enabled and is_l2:
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    # service_fee_records 四段汇总
                    cur.execute(
                        """
                        SELECT
                            COALESCE(SUM(CASE WHEN status=%s THEN amount_yuan ELSE 0 END), 0) AS pending,
                            COALESCE(SUM(CASE WHEN status=%s THEN amount_yuan ELSE 0 END), 0) AS settled,
                            COALESCE(SUM(CASE WHEN status=%s THEN amount_yuan ELSE 0 END), 0) AS converted,
                            COALESCE(SUM(CASE WHEN status=%s THEN amount_yuan ELSE 0 END), 0) AS withdrawn
                          FROM service_fee_records WHERE user_id = %s
                        """,
                        (
                            ServiceFeeStatus.PENDING,
                            ServiceFeeStatus.SETTLED,
                            ServiceFeeStatus.CONVERTED,
                            ServiceFeeStatus.WITHDRAWN,
                            user_id,
                        ),
                    )
                    row = cur.fetchone() or {}
                    if not isinstance(row, dict):
                        row = {
                            "pending": row[0], "settled": row[1],
                            "converted": row[2], "withdrawn": row[3],
                        }
                    service_fee["pending_yuan"] = float(row["pending"] or 0)
                    service_fee["settled_yuan"] = float(row["settled"] or 0)
                    service_fee["converted_yuan"] = float(row["converted"] or 0)
                    service_fee["withdrawn_yuan"] = float(row["withdrawn"] or 0)

                    # service_fee 债务
                    cur.execute(
                        """
                        SELECT COALESCE(SUM(amount_due - amount_settled), 0) AS d
                          FROM service_fee_clawback_pending
                         WHERE user_id = %s AND status IN ('pending','partial_settled')
                        """,
                        (user_id,),
                    )
                    d = cur.fetchone()
                    service_fee["debt_yuan"] = float((d["d"] if isinstance(d, dict) else d[0]) or 0)

                    # bonus 债务(积分单位)
                    cur.execute(
                        """
                        SELECT COALESCE(SUM(amount_due - amount_settled), 0) AS d
                          FROM bonus_clawback_pending
                         WHERE user_id = %s AND status IN ('pending','partial_settled')
                        """,
                        (user_id,),
                    )
                    d = cur.fetchone()
                    service_fee["bonus_debt_points"] = int((d["d"] if isinstance(d, dict) else d[0]) or 0)
        except Exception as exc:
            logger.warning("service_fee SELECTs failed: %s · fallback 0", exc)

    base["is_l2"] = is_l2
    base["v3_3_1_enabled"] = enabled
    base["service_fee"] = service_fee
    return base
