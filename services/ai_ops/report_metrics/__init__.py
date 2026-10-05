"""
AI Ops 日报 v2 · 运营指标采集包 · 2026-07-02

结构(按 FULL_OPERATION_EXECUTION_BRIEF §5.1 建议):
  finance / geo_delivery / monitoring / research_monitor / llm_cost / publishing

纪律(不可违反):
  - 每段独立采集独立失败:某段炸了只影响该段,日报照常生成。
  - 子指标也独立失败:同段内某张表不存在,其余子指标照常出数。
  - 失败一律返回 {"status": "unavailable", "error": "..."},绝不编数字。
  - SQL 只读;不写任何业务表;口径复用现有 SSOT(见各模块 docstring)。
  - 失败后必须 rollback 清 aborted 事务,防止污染连接池/同连接后续查询。
"""

import logging

logger = logging.getLogger("AiOps-ReportMetrics")


def open_conn():
    from db.connection import get_connection
    return get_connection()


def unavailable(err) -> dict:
    return {"status": "unavailable", "error": str(err)[:160]}


def sub_metric(conn, fn) -> dict:
    """子指标独立失败:失败 rollback(清 aborted 事务,同连接后续子查询可继续)。"""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return unavailable(e)


def sub_ok(x) -> bool:
    """子指标是否成功出数。"""
    return isinstance(x, dict) and x.get("status") != "unavailable"


def collect_all(report_date) -> dict:
    """采集全部运营段。每段独立 try:任何一段失败都不影响其他段和整份日报。"""
    from services.ai_ops.report_metrics import (
        finance, geo_delivery, llm_cost, monitoring, publishing, research_monitor,
    )
    segments = {
        "finance": finance.collect,
        "geo_delivery": geo_delivery.collect,
        "monitoring": monitoring.collect,
        "research_monitor": research_monitor.collect,
        "llm_cost": llm_cost.collect,
        "publishing": publishing.collect,
    }
    out: dict = {}
    for name, fn in segments.items():
        try:
            out[name] = fn(report_date)
        except Exception as e:  # noqa: BLE001
            logger.warning("[report_metrics] 段 %s 采集失败(日报照常生成): %s", name, e)
            out[name] = unavailable(e)
    return out
