"""
A.5.4 预算熔断模块

两道熔断:
1. 单轮预算熔断: 本轮 (round) 累计成本超 budget_per_round_yuan (默认 350) → 立即停止本轮
2. 月度预算熔断: 本月累计成本超 budget_per_month_yuan (默认 1000) → 拒绝再起新轮

阈值从 geo_research_config 读, 配置缺失走默认值；DB/成本读取异常 fail-closed,
避免预算保护失效时继续触发付费外部调用。

数据源:
- geo_research_cost_log.amount_yuan (DECIMAL(10,4)): cast 成 float 返回
- geo_research_config.value_json (JSONB 数字): psycopg2 取出已是 int/float
"""
import logging
from typing import Dict, Optional

from db.connection import get_connection

logger = logging.getLogger("GEO-ResearchMonitor.Budget")


# ==================== 默认值 (fail-safe) ====================

DEFAULT_BUDGET_PER_ROUND = 350.0
DEFAULT_BUDGET_PER_MONTH = 1000.0


# ==================== 异常类 ====================

class BudgetExhaustedError(Exception):
    """预算熔断异常: round_runner 捕获后归档为 failed_resumable"""

    def __init__(self, level: str, spent: float, limit: float, reason: str):
        # level: 'round' | 'month'
        self.level = level
        self.spent = spent
        self.limit = limit
        self.reason = reason
        super().__init__(reason)


class BudgetDataUnavailableError(Exception):
    """预算或成本数据不可读取。调用方必须 fail-closed, 不能按 0 成本放行。"""


# ==================== 配置读取 ====================

def get_budget_config() -> Dict[str, float]:
    """
    从 geo_research_config 读 budget_per_round_yuan + budget_per_month_yuan。

    配置行缺失 → 走默认值 350 / 1000。
    DB 读取异常 → 抛 BudgetDataUnavailableError, 由 check_* fail-closed。
    """
    config: Dict[str, float] = {
        'budget_per_round_yuan': DEFAULT_BUDGET_PER_ROUND,
        'budget_per_month_yuan': DEFAULT_BUDGET_PER_MONTH,
    }
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT key, value_json
                  FROM geo_research_config
                 WHERE key IN ('budget_per_round_yuan', 'budget_per_month_yuan')
                """
            )
            rows = cur.fetchall() or []
            for row in rows:
                key = row['key']
                value = row['value_json']
                # value_json 是 JSONB, psycopg2 取出已经是 Python 原生类型
                # 数字直接存 (例如 '350' → int 350); 容错任何能转 float 的值
                try:
                    config[key] = float(value)
                except (TypeError, ValueError):
                    logger.warning(
                        f"geo_research_config[{key}] 非数字 (值={value!r}), 走默认值"
                    )
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"读取 budget 配置失败, 预算保护 fail-closed: {e}")
        raise BudgetDataUnavailableError(f"budget config unavailable: {e}") from e

    return config


# ==================== 成本累计读取 ====================

def get_round_cost(round_id: str) -> float:
    """SUM(amount_yuan) FROM geo_research_cost_log WHERE round_id = %s, NULL → 0.0"""
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT COALESCE(SUM(amount_yuan), 0) AS total
                  FROM geo_research_cost_log
                 WHERE round_id = %s
                """,
                (round_id,),
            )
            row = cur.fetchone() or {}
            # amount_yuan 是 DECIMAL(10,4), psycopg2 默认返 Decimal, cast 成 float
            return float(row.get('total') or 0)
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"[{round_id}] 读取 round 成本失败, 预算保护 fail-closed: {e}")
        raise BudgetDataUnavailableError(f"round cost unavailable: {e}") from e


def get_month_cost() -> float:
    """
    SUM(amount_yuan) FROM geo_research_cost_log
    WHERE recorded_at >= 当前月起点 AND recorded_at < 下月起点。
    NULL → 0.0
    """
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT COALESCE(SUM(amount_yuan), 0) AS total
                  FROM geo_research_cost_log
                 WHERE recorded_at >= date_trunc('month', NOW())
                   AND recorded_at <  date_trunc('month', NOW()) + interval '1 month'
                """
            )
            row = cur.fetchone() or {}
            return float(row.get('total') or 0)
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"读取月度成本失败, 预算保护 fail-closed: {e}")
        raise BudgetDataUnavailableError(f"month cost unavailable: {e}") from e


# ==================== 预算检查 ====================

def check_round_budget(round_id: str) -> Dict:
    """
    检查本轮预算是否还够。

    返:
      {
        'ok': bool,            # True=未超预算, False=已超
        'spent': float,        # 本轮已花
        'limit': float,        # 本轮上限
        'remaining': float,    # limit - spent (可能为负)
        'reason': str | None,  # ok=True 时 None; ok=False 时中文说明
      }
    """
    try:
        cfg = get_budget_config()
        limit = float(cfg.get('budget_per_round_yuan') or DEFAULT_BUDGET_PER_ROUND)
        spent = get_round_cost(round_id)
    except BudgetDataUnavailableError as e:
        reason = f"预算数据不可用, 已停止本轮以避免失控扣费: {e}"
        return {
            'ok': False,
            'code': 'budget_data_unavailable',
            'spent': None,
            'limit': None,
            'remaining': None,
            'reason': reason,
        }
    remaining = limit - spent

    if spent >= limit:
        reason = f"单轮预算超支 ¥{spent:.2f}/¥{limit:.2f}"
        return {
            'ok': False,
            'spent': spent,
            'limit': limit,
            'remaining': remaining,
            'reason': reason,
        }

    return {
        'ok': True,
        'spent': spent,
        'limit': limit,
        'remaining': remaining,
        'reason': None,
    }


def check_month_budget() -> Dict:
    """
    检查本月预算是否还够。

    返同 check_round_budget, 但 spent/limit 走月维度。
    """
    try:
        cfg = get_budget_config()
        limit = float(cfg.get('budget_per_month_yuan') or DEFAULT_BUDGET_PER_MONTH)
        spent = get_month_cost()
    except BudgetDataUnavailableError as e:
        reason = f"预算数据不可用, 已拒绝启动跑批以避免失控扣费: {e}"
        return {
            'ok': False,
            'code': 'budget_data_unavailable',
            'spent': None,
            'limit': None,
            'remaining': None,
            'reason': reason,
        }
    remaining = limit - spent

    if spent >= limit:
        reason = f"月度预算超支 ¥{spent:.2f}/¥{limit:.2f}"
        return {
            'ok': False,
            'spent': spent,
            'limit': limit,
            'remaining': remaining,
            'reason': reason,
        }

    return {
        'ok': True,
        'spent': spent,
        'limit': limit,
        'remaining': remaining,
        'reason': None,
    }
