"""协议门禁触发的最小可观测(工单 2026-07-29 §4.1)。

一条铁律贯穿本模块:**观测绝不能成为新的阻断源。**
记录失败只写日志,永远不向登录路径抛异常 —— 否则"看不见"就升级成"登不进"。

它回答管理员两个问题:
  1. 这几天有没有人被协议门禁挡在门外?(按天计数)
  2. 挡住的是不是同一批人在反复撞?(按账号去重)

配套的判读口径写在 `gate_trigger_summary` 里:**连续多天非零 = 有人正卡在门外**,
因为正常形态是"触发 → 几秒内补签成功 → 再也不触发"。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger("GEO-AgreementGate")

_TABLE = "registration_agreement_gate_events"


def record_gate_trigger(*, user_id: int, auth_method: str) -> bool:
    """记一次门禁触发。返回是否真的落库(供测试断言),失败不抛。"""
    try:
        from db.connection import get_db

        with get_db() as conn:
            cur = conn.cursor()
            # 用**非限定名**探测:下面的 INSERT 也是非限定的,两者必须走同一套
            # search_path 解析。写死 'public.' 会变成"探一张表、写另一张表"。
            cur.execute("SELECT to_regclass(%s) AS relation", (_TABLE,))
            row = cur.fetchone()
            relation = row["relation"] if isinstance(row, dict) else (row[0] if row else None)
            if relation is None:
                # 迁移还没跑(或老环境)。静默跳过 —— 观测缺失可以接受,登录被打断不行。
                return False
            cur.execute(
                f"INSERT INTO {_TABLE} (user_id, auth_method) VALUES (%s, %s)",
                (int(user_id), str(auth_method or "")[:20]),
            )
            conn.commit()
            return True
    except Exception as exc:  # noqa: BLE001 — 见模块 docstring
        logger.warning("[agreement-gate] 触发计数写入失败: %s", type(exc).__name__)
        return False


def gate_trigger_summary(cur, *, days: int = 14) -> Dict[str, Any]:
    """近 N 天的门禁触发情况。查不动就返回空结构,不抛。"""
    empty: Dict[str, Any] = {
        "available": False, "days": int(days), "total": 0,
        "distinct_users": 0, "daily": [], "consecutive_days_with_triggers": 0,
    }
    try:
        # 用**非限定名**探测:下面的 INSERT/SELECT 也是非限定的,两者必须走同一套
        # search_path 解析。写死 'public.' 会变成"探一张表、写另一张表"。
        cur.execute("SELECT to_regclass(%s) AS relation", (_TABLE,))
        row = cur.fetchone()
        relation = row["relation"] if isinstance(row, dict) else (row[0] if row else None)
        if relation is None:
            return empty
        cur.execute(
            f"""
            SELECT occurred_at::date AS day,
                   count(*)                AS triggers,
                   count(DISTINCT user_id) AS users
              FROM {_TABLE}
             WHERE occurred_at >= NOW() - make_interval(days => %s)
             GROUP BY 1
             ORDER BY 1 DESC
            """,
            (int(days),),
        )
        rows = [dict(item) for item in (cur.fetchall() or [])]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[agreement-gate] 触发汇总查询失败: %s", type(exc).__name__)
        return empty

    daily: List[Dict[str, Any]] = [
        {
            "day": item["day"].isoformat() if hasattr(item["day"], "isoformat") else str(item["day"]),
            "triggers": int(item["triggers"]),
            "users": int(item["users"]),
        }
        for item in rows
    ]
    # rows 已按天倒序 —— 从最近一天往回数连续有触发的天数。
    streak = 0
    for item in daily:
        if item["triggers"] <= 0:
            break
        streak += 1
    return {
        "available": True,
        "days": int(days),
        "total": sum(item["triggers"] for item in daily),
        # 按天去重之和 != 期间去重(同一账号可能连撞多天),所以单独查一次。
        "distinct_users": _distinct_users(cur, days),
        "daily": daily,
        "consecutive_days_with_triggers": streak,
    }


def _distinct_users(cur, days: int) -> int:
    try:
        cur.execute(
            f"SELECT count(DISTINCT user_id) AS n FROM {_TABLE} "
            f"WHERE occurred_at >= NOW() - make_interval(days => %s)",
            (int(days),),
        )
        row = cur.fetchone()
        return int(row["n"] if isinstance(row, dict) else row[0])
    except Exception:  # noqa: BLE001
        return 0
