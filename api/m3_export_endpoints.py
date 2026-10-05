"""M3 Excel 导出 endpoints · CTO-15.18 PM 干预 类 C(C.2/C.3/C.4)

老板红线(2026-04-28):
- 旧版有 Excel 导出 / M3 砍了 → 中坚代理回旧版根本动机(根因 #4 工具回归)
- Q4 老板裁决:openpyxl 完整 xlsx · 中坚代理月度 KPI 必备

3 个 endpoint:
- GET /api/m3/export/quotes      报价单 8 列
- GET /api/m3/export/diagnoses   诊断报告 6 列
- GET /api/m3/export/monitoring  监测 7 列(可选 brand_id 过滤)

RBAC 沿用 m3_api.py 标准三段(admin / user_clients / owner)
is_test filter 默认 ON(跟客户池一致)· ?include_test=true 可调试

挂在 m3_api.py 同 router 下 · 单文件分离避免 m3_api.py 过长
"""
from fastapi import APIRouter, Request
from datetime import date
import logging

from utils.excel_export import build_xlsx_response

router = APIRouter(tags=["M3 导出"])
logger = logging.getLogger("GEO-M3-Export")


def _get_user(request: Request) -> dict:
    """复用 m3_api 的 user 获取(简化版 · 不导入避免循环)"""
    user = getattr(request.state, "user", None)
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="未登录")
    return user


def _is_test_filter(request: Request) -> tuple[str, bool]:
    """返 (SQL 子句, include_test bool)"""
    qp = (request.query_params.get("include_test") or "").lower()
    inc = qp in ("1", "true", "yes", "y")
    sql = "" if inc else " AND (b.is_test IS NULL OR b.is_test = FALSE)"
    return sql, inc


@router.get("/api/m3/export/diagnoses")
async def export_diagnoses_xlsx(request: Request):
    """C.3 诊断报告 Excel 导出 · 6 列"""
    from db.diagnosis_db import get_connection

    user = _get_user(request)
    user_id = user.get("user_id")
    is_admin = user.get("is_admin", False)
    test_filter, _ = _is_test_filter(request)

    conn = get_connection()
    try:
        cur = conn.cursor()
        if is_admin:
            cur.execute(
                f"""
                SELECT b.name AS brand_name, b.industry, dr.total_score, dr.level,
                       dr.diagnosis_type, dr.created_at
                FROM diagnosis_records dr
                JOIN brands b ON b.id = dr.brand_id
                WHERE (dr.is_deleted IS NULL OR dr.is_deleted = FALSE)
                  AND (b.is_deleted IS NULL OR b.is_deleted = FALSE)
                  {test_filter}
                ORDER BY dr.created_at DESC
                LIMIT 5000
                """
            )
        elif user_id:
            cur.execute(
                f"""
                SELECT b.name AS brand_name, b.industry, dr.total_score, dr.level,
                       dr.diagnosis_type, dr.created_at
                FROM diagnosis_records dr
                JOIN brands b ON b.id = dr.brand_id
                WHERE b.owner_user_id = %s
                  AND (dr.is_deleted IS NULL OR dr.is_deleted = FALSE)
                  AND (b.is_deleted IS NULL OR b.is_deleted = FALSE)
                  AND (dr.result_visibility IS NULL OR dr.result_visibility = 'published')
                  {test_filter}
                ORDER BY dr.created_at DESC
                LIMIT 5000
                """,
                (user_id,),
            )
        else:
            cur.execute("SELECT 1 WHERE FALSE")
        rows = cur.fetchall() or []
    finally:
        try:
            conn.close()
        except Exception:
            pass

    data_rows = [[
        r.get("brand_name") or "—",
        r.get("industry") or "—",
        r.get("total_score") if r.get("total_score") is not None else "—",
        r.get("level") or "—",
        r.get("diagnosis_type") or "—",
        r.get("created_at"),
    ] for r in rows]

    return build_xlsx_response(
        filename=f"omnirank_诊断报告_{date.today()}.xlsx",
        sheet_name="诊断报告",
        headers=["客户", "行业", "评分", "等级", "类型", "诊断时间"],
        rows=data_rows,
        column_widths=[22, 16, 8, 12, 14, 18],
    )


@router.get("/api/m3/export/monitoring")
async def export_monitoring_xlsx(request: Request, brand_id: int | None = None):
    """C.2 监测 Excel 导出 · 7 列(可选 brand_id 过滤)"""
    from db.diagnosis_db import get_connection

    user = _get_user(request)
    user_id = user.get("user_id")
    is_admin = user.get("is_admin", False)
    test_filter, _ = _is_test_filter(request)

    conn = get_connection()
    try:
        cur = conn.cursor()
        try:
            sql = f"""
                SELECT b.name AS brand_name, mk.keyword,
                       COALESCE(mk.latest_appearance_rate, 0) AS appearance_rate,
                       COALESCE(mk.previous_appearance_rate, 0) AS prev_rate,
                       COALESCE(mk.target_appearance_rate, 0) AS target_rate,
                       mk.status,
                       mk.updated_at
                FROM monitoring_keywords mk
                JOIN brands b ON b.id = mk.brand_id
                WHERE (b.is_deleted IS NULL OR b.is_deleted = FALSE)
                  {test_filter}
            """
            args: list = []
            if not is_admin and user_id:
                sql += " AND b.owner_user_id = %s"
                args.append(user_id)
            if brand_id:
                sql += " AND mk.brand_id = %s"
                args.append(brand_id)
            sql += " ORDER BY b.name, mk.updated_at DESC LIMIT 5000"
            cur.execute(sql, tuple(args))
            rows = cur.fetchall() or []
        except Exception as e:
            logger.warning(f"[m3/export/monitoring] 查询失败 · 可能列名差异: {e}")
            rows = []
    finally:
        try:
            conn.close()
        except Exception:
            pass

    def _delta(cur_v, prev_v) -> str:
        try:
            d = float(cur_v or 0) - float(prev_v or 0)
            sign = "+" if d > 0 else ""
            return f"{sign}{d:.1f}%"
        except Exception:
            return "—"

    data_rows = [[
        r.get("brand_name") or "—",
        r.get("keyword") or "—",
        f"{float(r.get('appearance_rate') or 0):.1f}%",
        _delta(r.get("appearance_rate"), r.get("prev_rate")),
        f"{float(r.get('target_rate') or 0):.1f}%",
        r.get("status") or "—",
        r.get("updated_at"),
    ] for r in rows]

    return build_xlsx_response(
        filename=f"omnirank_监测_{date.today()}.xlsx",
        sheet_name="监测",
        headers=["客户", "关键词", "出现率", "变化", "目标", "状态", "最后更新"],
        rows=data_rows,
        column_widths=[22, 24, 12, 12, 12, 12, 18],
    )
