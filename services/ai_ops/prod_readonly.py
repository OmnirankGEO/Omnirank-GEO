"""
AI Ops 生产只读接入 · 2026-07-01

L2 生产只读能力框架:只读 SQL / 容器状态 / tail 日志 / 健康检查。
默认关闭,gated on Runner 就绪 + 专用只读凭证。绝不写生产。

安全约束:
  - 只读 SQL 走独立只读账号(AI_OPS_READONLY_DATABASE_URL),不复用应用写连接池。
  - 语句级白名单:只允许 SELECT / WITH ... SELECT / EXPLAIN / SHOW,拒绝任何写/DDL。
  - statement_timeout 限制,防慢查询拖库。
  - 输出过 redaction 脱敏。
  - 读敏感表全量 / 导出数据仍需审批(设计 §8 L2 边界)。
"""

import logging
import os
import re
from typing import Optional

from services.ai_ops import redaction

logger = logging.getLogger("AiOps-ProdRO")

# 只读语句白名单(去注释后必须以这些开头)
_ALLOWED_PREFIXES = ("select", "with", "explain", "show", "table")
# 明确禁止的写/DDL 关键字(即便被包在子句里也拒绝)
_FORBIDDEN = re.compile(
    r"(?i)\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|"
    r"merge|call|do|copy|vacuum|reindex|comment|set|begin|commit|rollback)\b"
)


class NotReadOnly(Exception):
    pass


class ReadOnlyNotConfigured(Exception):
    pass


def _strip_sql_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql.strip()


def assert_select_only(sql: str) -> None:
    """拒绝任何非只读语句 / 多语句。"""
    cleaned = _strip_sql_comments(sql)
    if not cleaned:
        raise NotReadOnly("空 SQL")
    # 禁止分号多语句(留末尾一个)
    if cleaned.rstrip().rstrip(";").count(";") > 0:
        raise NotReadOnly("不允许多语句")
    low = cleaned.lower().lstrip("(")
    if not low.startswith(_ALLOWED_PREFIXES):
        raise NotReadOnly(f"只允许只读查询,收到: {cleaned[:40]}")
    if _FORBIDDEN.search(cleaned):
        raise NotReadOnly("SQL 含写/DDL 关键字,拒绝")


def is_configured() -> bool:
    return bool(os.getenv("AI_OPS_READONLY_DATABASE_URL"))


def readonly_sql(sql: str, params: Optional[tuple] = None, *, timeout_ms: int = 5000,
                 max_rows: int = 500) -> dict:
    """
    对生产只读账号跑一条 SELECT。返回 {columns, rows, truncated}。
    未配置只读账号 → ReadOnlyNotConfigured。非只读语句 → NotReadOnly。
    """
    assert_select_only(sql)
    url = os.getenv("AI_OPS_READONLY_DATABASE_URL")
    if not url:
        raise ReadOnlyNotConfigured("AI_OPS_READONLY_DATABASE_URL 未配置(Runner 未就绪)")

    import psycopg2
    import psycopg2.extras
    conn = psycopg2.connect(url)
    try:
        conn.set_session(readonly=True, autocommit=True)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(f"SET statement_timeout = {int(timeout_ms)}")
        cur.execute(sql, params or ())
        rows = cur.fetchmany(max_rows + 1)
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        columns = [d[0] for d in (cur.description or [])]
        # 脱敏每个字符串单元格
        clean_rows = [
            {k: (redaction.redact(v) if isinstance(v, str) else v) for k, v in dict(r).items()}
            for r in rows
        ]
        return {"columns": columns, "rows": clean_rows, "truncated": truncated}
    finally:
        conn.close()


def container_status(runner=None) -> dict:
    """docker ps 只读容器状态(gated · Runner 侧)。未就绪返回 not_configured。"""
    if os.getenv("AI_OPS_SSH_RUNNER_ENABLED", "false").lower() not in ("1", "true", "yes"):
        return {"configured": False, "reason": "ssh_runner_disabled"}
    import subprocess
    run = runner or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=30))
    result = run(["docker", "ps", "--format", "{{.Names}} {{.Status}}"])
    return {
        "configured": True,
        "output": redaction.redact(getattr(result, "stdout", "") or ""),
        "returncode": getattr(result, "returncode", None),
    }
