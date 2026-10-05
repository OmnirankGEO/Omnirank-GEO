"""飞轮健康仪表(A2 · 2026-07-29)· 管理员只读。

回答一个此前谁也答不上来的问题:**飞轮那几个 job 到底跑没跑成、上次是什么时候、处理了多少条。**
在此之前它们的执行痕迹只存在于容器日志里,滚掉就没了 —— 媒体推荐池停更 78 天没人发现就是这么来的。

全部端点只读:读 `flywheel_job_heartbeats` 心跳账本 + 两道闸门状态,不触发任何 job、不写任何业务表。
手动补跑不放在这里(要跑走各自既有的管理端入口),避免多开一个可以"点一下就花钱"的面。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/admin/flywheel", tags=["飞轮健康"])


def _require_admin(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


@router.get("/health")
async def get_flywheel_health(request: Request):
    """飞轮总览:每个 job 的最近成功/最近状态/处理量/是否超期,加两道闸门状态。"""
    _require_admin(request)
    from services.flywheel_heartbeat import evaluate_gate_health, evaluate_job_health

    jobs = evaluate_job_health()
    gates = [g for g in evaluate_gate_health() if g.get("firing")]
    unhealthy = [j for j in jobs if not j["healthy"]]
    return {
        "success": True,
        "jobs": jobs,
        "gate_findings": gates,
        "summary": {
            "total": len(jobs),
            "healthy": len(jobs) - len(unhealthy),
            "unhealthy": len(unhealthy),
            "gates_firing": len(gates),
        },
    }


@router.get("/runs")
async def get_flywheel_runs(request: Request, job_key: str = "", limit: int = 50):
    """某个 job(或全部)的最近执行明细。"""
    _require_admin(request)
    from db.flywheel_job_heartbeat_db import list_recent_runs

    return {"success": True, "runs": list_recent_runs(job_key=job_key, limit=limit)}


@router.get("/judgments")
async def get_flywheel_judgments(request: Request, point_key: str = "", limit: int = 50):
    """B 段判断留痕:每次判断的输入摘要/输出/模型/版本,以及当日额度用了多少。

    "留痕可复核"这条约束的人可读出口 —— 判断到底是模型给的还是规则兜底给的,一眼可见。
    """
    _require_admin(request)
    from db.flywheel_judgment_db import list_judgments, today_usage
    from services.flywheel_judgment import JUDGMENT_POINTS, judgment_enabled

    points = []
    for key, point in JUDGMENT_POINTS.items():
        usage = today_usage(key)
        points.append({
            "point_key": key,
            "name": point.name,
            "why": point.why,
            "enabled": judgment_enabled(key),
            "daily_call_cap": point.daily_call_cap,
            "daily_cost_cap_cny": point.daily_cost_cap_cny,
            "today": usage,
            "model_chain": [f"{p}/{m}" for p, m in point.model_chain],
        })
    return {
        "success": True,
        "points": points,
        "judgments": list_judgments(point_key=point_key, limit=limit),
    }


@router.get("/corpus-value")
async def get_corpus_value_labels(request: Request, industry_key: str = "", limit: int = 50):
    """B2 语料价值标签 + 标注覆盖率。"""
    _require_admin(request)
    from db.flywheel_corpus_label_db import REUSE_SCENARIOS, label_coverage, list_reusable_corpus

    return {
        "success": True,
        "coverage": label_coverage(),
        "scenarios": REUSE_SCENARIOS,
        "reusable": list_reusable_corpus(industry_key, limit=limit),
    }


@router.get("/diagnosis-reuse")
async def get_diagnosis_reuse_material(
    request: Request, brand_name: str, industry: str, city: str = "",
):
    """B4 诊断沉淀复用素材(advisory)。只读预览,不触发也不改任何诊断。"""
    _require_admin(request)
    from services.flywheel_diagnosis_reuse import suggest_reusable_material

    return {
        "success": True,
        "material": suggest_reusable_material(
            brand_name=brand_name, industry=industry, city=city
        ),
    }


@router.get("/assignments")
async def get_writing_assignments(request: Request, industry_key: str = "", limit: int = 100):
    """写作策略指派账本(A4 管道有没有水,看这里)。"""
    _require_admin(request)
    from services.writing_strategy_assignment import list_assignments

    rows = list_assignments(limit=limit, industry_key=industry_key)
    by_resolution: dict[str, int] = {}
    for row in rows:
        key = str(row.get("resolution") or "unknown")
        by_resolution[key] = by_resolution.get(key, 0) + 1
    return {"success": True, "assignments": rows, "by_resolution": by_resolution}
