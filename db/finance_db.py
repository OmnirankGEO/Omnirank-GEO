"""
财务中心 · operating_expenses(运营成本)表 + CRUD
GEO CTO-15.23 · 2026-05-30

唯一新表 · 纯新增 · 不碰扣费逻辑(billing.py 红线 0 改)。
operating_expenses = admin 手录的月度固定成本(服务器 / 带宽 / 域名 / 人力 / 其它)。
P&L 的 OpEx 行来源 · 不补则毛利虚高(SaaS 成本 30-50% 缺失)。

字段类型遵循 SQL 4 维核验:
  - amount_cents INTEGER(分 · 与全库金额口径一致)
  - period_month DATE(归属月 · 存 YYYY-MM-01)
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("GEO-Finance-DB")

# 录入分类白名单(前端下拉同款)· other 兜底
VALID_CATEGORIES = ("server", "bandwidth", "domain", "labor", "other")
CATEGORY_LABELS = {
    "server": "服务器",
    "bandwidth": "带宽/CDN",
    "domain": "域名",
    "labor": "人力",
    "other": "其它",
}

# 允许 update 的字段(防 IDOR / 防改 created_by 等)
_UPDATABLE = ("period_month", "category", "amount_cents", "note")


def _conn():
    from db.connection import get_connection
    return get_connection()


def ensure_operating_expenses_table() -> None:
    """幂等建表 · CREATE IF NOT EXISTS。

    Deploy 时走 scripts/migration_finance_opex_2026_05_30.sql 先 migrate;
    此函数是代码侧兜底(每次 CRUD lazy 调用 · IF NOT EXISTS 幂等无开销)。
    """
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS operating_expenses (
                id SERIAL PRIMARY KEY,
                period_month DATE NOT NULL,
                category TEXT NOT NULL,
                amount_cents INTEGER NOT NULL DEFAULT 0,
                note TEXT,
                created_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_opex_period ON operating_expenses(period_month)"
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"[finance_db] ensure operating_expenses 表失败: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def list_operating_expenses(
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """列出运营成本(可按归属月区间过滤)· period_* 为 'YYYY-MM-01' 字符串/date。"""
    ensure_operating_expenses_table()
    conn = _conn()
    try:
        cur = conn.cursor()
        where: List[str] = []
        params: List[Any] = []
        if period_start:
            where.append("period_month >= %s")
            params.append(period_start)
        if period_end:
            where.append("period_month <= %s")
            params.append(period_end)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        cur.execute(
            f"""
            SELECT id, period_month, category, amount_cents, note,
                   created_by, created_at, updated_at
            FROM operating_expenses
            {where_sql}
            ORDER BY period_month DESC, category
            """,
            tuple(params),
        )
        return [dict(r) for r in cur.fetchall()]
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def create_operating_expense(
    period_month: str,
    category: str,
    amount_cents: int,
    note: Optional[str],
    created_by: Optional[int],
) -> Dict[str, Any]:
    """新增一条运营成本。category 不在白名单则归 'other'。"""
    ensure_operating_expenses_table()
    if category not in VALID_CATEGORIES:
        category = "other"
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO operating_expenses (period_month, category, amount_cents, note, created_by)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, period_month, category, amount_cents, note, created_by, created_at, updated_at
            """,
            (period_month, category, int(amount_cents or 0), note, created_by),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else {}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def update_operating_expense(opex_id: int, fields: Dict[str, Any]) -> bool:
    """更新一条运营成本(仅 _UPDATABLE 字段)。"""
    ensure_operating_expenses_table()
    sets: List[str] = []
    params: List[Any] = []
    for k in _UPDATABLE:
        if k in fields and fields[k] is not None:
            v = fields[k]
            if k == "category" and v not in VALID_CATEGORIES:
                v = "other"
            if k == "amount_cents":
                v = int(v or 0)
            sets.append(f"{k} = %s")
            params.append(v)
    if not sets:
        return False
    sets.append("updated_at = CURRENT_TIMESTAMP")
    params.append(int(opex_id))
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE operating_expenses SET {', '.join(sets)} WHERE id = %s",
            tuple(params),
        )
        updated = cur.rowcount > 0
        conn.commit()
        return updated
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def delete_operating_expense(opex_id: int) -> bool:
    """删除一条运营成本(硬删 · 该表无业务关联 · admin 录入误删可重录)。"""
    ensure_operating_expenses_table()
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM operating_expenses WHERE id = %s", (int(opex_id),))
        deleted = cur.rowcount > 0
        conn.commit()
        return deleted
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def sum_opex_by_category(period_start: str, period_end: str) -> List[Dict[str, Any]]:
    """期间内按 category 聚合(P&L OpEx 行用)· 返回 [{category, label, amount_cents}]。"""
    ensure_operating_expenses_table()
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT category, COALESCE(SUM(amount_cents), 0) AS amount_cents
            FROM operating_expenses
            WHERE period_month >= %s AND period_month <= %s
            GROUP BY category
            ORDER BY amount_cents DESC
            """,
            (period_start, period_end),
        )
        return [
            {
                "category": r["category"],
                "label": CATEGORY_LABELS.get(r["category"], r["category"]),
                "amount_cents": int(r["amount_cents"] or 0),
            }
            for r in cur.fetchall()
        ]
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def sum_opex_total(period_start: str, period_end: str) -> int:
    """期间内 OpEx 总额(分)。"""
    rows = sum_opex_by_category(period_start, period_end)
    return sum(int(r["amount_cents"] or 0) for r in rows)
