"""
V3.3.1 RBAC L0/L1/L2 路由 gate + 真实路由枚举

模式:
- decorator @rbac_required(min_level='L1') / 'L2'  · 应用于 router
- 中间件层兜底:对 /api/diagnosis /api/quote /api/monitoring 等 GEO 路由强制 L1+
- 审计:每次 block 写 rbac_route_audit

实际生效:V3_3_1_RBAC_GATE_ENABLED 才启用

工具:
- list_real_routes(app)           枚举 FastAPI 真实路由 · 输出 csv
- generate_rbac_matrix(app)       生成 RBAC_ROUTE_MATRIX.csv

关联:
- 决策书 §7
- IDENTITY_DECISIONS_LOCK Q36
- RED_LINES R6
"""

import csv
import logging
from functools import wraps
from typing import Optional, Callable, List, Dict, Any

from fastapi import HTTPException, Request

from db.connection import get_db
from config.v3_3_1_flags import is_rbac_gate_enabled
from services.identity_service import classify_user
from auth.user_ctx import current_user_id

logger = logging.getLogger("GEO-V3.3.1-RBAC")


LEVEL_ORDER = {"L0": 0, "L1": 1, "L2": 2}


# 默认 RBAC 矩阵(决策书 §7.2 产品口径 · 真实路由通过 list_real_routes 校对)
DEFAULT_RBAC_MATRIX: Dict[str, str] = {
    # GEO 诊断
    "GET:/api/diagnosis/list": "L1",
    "POST:/api/diagnosis/run": "L1",
    "GET:/api/diagnosis/{id}": "L1",
    # GEO 报价
    "GET:/api/quote/list": "L1",
    "POST:/api/quote/create": "L1",
    "GET:/api/quote/{id}": "L1",
    # GEO 监测
    "GET:/api/monitoring/list": "L1",
    "POST:/api/monitoring/create": "L1",
    # GEO 写作
    "POST:/api/writing/generate": "L1",
    # 代理专属
    "GET:/api/referral/stats": "L2",
    "GET:/api/referral/network": "L2",
    "GET:/api/referral/quote": "L2",
    "POST:/api/referral/quote/generate": "L2",
    "GET:/api/service-fee/balance": "L2",
    "POST:/api/service-fee/convert": "L2",
    "POST:/api/service-fee/convert-large": "L2",
    "POST:/api/service-fee/withdraw-request": "L2",
    "GET:/api/service-fee/history": "L2",
    # 社媒 - L0 即可访问
    "POST:/api/content/generate": "L0",
    "GET:/api/social/list": "L0",
}


def _audit_block(user_id: Optional[int], user_role: str, method: str, path: str, reason: str):
    """记录 RBAC 拦截日志"""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO rbac_route_audit (
                        user_id, user_role, method, path,
                        response_code, blocked_reason
                    ) VALUES (%s, %s, %s, %s, 403, %s)
                    """,
                    (user_id, user_role, method, path, reason),
                )
                conn.commit()
    except Exception as exc:
        logger.warning("audit_block failed: %s", exc)


def rbac_check(user_id: Optional[int], required_level: str, method: str = "GET", path: str = "") -> bool:
    """单纯校验 · 返回 True/False"""
    if not is_rbac_gate_enabled():
        return True  # gate 未启用 → 放行(默认 OFF)
    if user_id is None:
        return required_level == "L0"
    user_level = classify_user(user_id)
    user_rank = LEVEL_ORDER.get(user_level, 0)
    required_rank = LEVEL_ORDER.get(required_level, 0)
    ok = user_rank >= required_rank
    if not ok:
        _audit_block(user_id, user_level, method, path,
                     f"required_{required_level}_got_{user_level}")
    return ok


def rbac_required(min_level: str = "L1") -> Callable:
    """装饰器:要求至少 min_level"""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            request: Optional[Request] = kwargs.get("request") or next(
                (a for a in args if isinstance(a, Request)), None
            )
            if request is None:
                logger.warning("rbac_required: no Request in args · skip check")
                return await func(*args, **kwargs)
            user = getattr(request.state, "user", None)
            user_id = (current_user_id(user) if isinstance(user, dict) else getattr(user, "id", None)) if user else None
            path = str(request.url.path)
            method = request.method
            if not rbac_check(user_id, min_level, method, path):
                raise HTTPException(
                    status_code=403,
                    detail={
                        "error": "rbac_forbidden",
                        "required_level": min_level,
                        "your_level": classify_user(user_id) if user_id else "anonymous",
                    },
                )
            return await func(*args, **kwargs)
        return wrapper
    return decorator


# ============================================
# 真实路由枚举(Q36)
# ============================================

def list_real_routes(app) -> List[Dict[str, str]]:
    """枚举 FastAPI app 全部 routes · 返 [{"method", "path", "name", "tags"}]"""
    routes = []
    for r in app.routes:
        if not hasattr(r, "methods") or not hasattr(r, "path"):
            continue
        methods = sorted(m for m in r.methods if m not in ("HEAD", "OPTIONS"))
        for m in methods:
            routes.append({
                "method": m,
                "path": r.path,
                "name": getattr(r, "name", "") or "",
                "tags": ",".join(getattr(r, "tags", []) or []),
            })
    return sorted(routes, key=lambda x: (x["path"], x["method"]))


def generate_rbac_matrix_csv(app, out_path: str = "/tmp/RBAC_ROUTE_MATRIX.csv") -> str:
    """生成 RBAC 矩阵 csv · 跑 list_real_routes + 标记每路由对应 level"""
    routes = list_real_routes(app)
    rows: List[Dict[str, Any]] = []
    for r in routes:
        key = f"{r['method']}:{r['path']}"
        # 优先精确匹配
        level = DEFAULT_RBAC_MATRIX.get(key)
        if not level:
            # 兜底:用前缀启发式
            if r["path"].startswith("/api/service-fee/") or r["path"].startswith("/api/referral/"):
                level = "L2"
            elif r["path"].startswith("/api/diagnosis/") or r["path"].startswith("/api/quote/") \
              or r["path"].startswith("/api/monitoring/") or r["path"].startswith("/api/writing/"):
                level = "L1"
            elif r["path"].startswith("/api/admin/"):
                level = "admin"
            elif r["path"].startswith("/api/social/") or r["path"].startswith("/api/content/"):
                level = "L0"
            else:
                level = "L0"  # 默认放开
        rows.append({**r, "required_level": level})

    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["method", "path", "name", "tags", "required_level"]
        )
        writer.writeheader()
        writer.writerows(rows)
    logger.info("RBAC matrix written: %s · %d routes", out_path, len(rows))
    return out_path
