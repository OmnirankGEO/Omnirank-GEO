"""防御型 GEO · v2 监测**只读投影 + admission** 端点(§15.9.3 / WP7)。

🔴 这里**没有**执行器
---------------------
§15.9.3 逐字禁止「为 v2 新建平行监测执行器」。真正的执行永远走现役
``POST /api/monitoring/run`` / ``run-stream``。本模块只提供三类:

* **admission 预览** —— 未激活/未拨预算时给出 typed 恢复动作,**零副作用**;
* **lineage 投影** —— plan cell / attempt / 进度,从 attempt 账本重建;
* **同口径对照** —— 把真实 cell 喂进窗B 的 comparability 引擎。

判据 `test_no_route_here_creates_a_task_or_freeze` 用 AST 把「零执行器」钉死。

🔴 错误全走窗A 的 ``_safe_error``
---------------------------------
不另造第二份信封(窗C 那份重复实现已在交付单挂号)。
裸信封在**构造期**就炸 —— 这一点由窗A 的实现保证,本模块只负责调对。

🔴 归属逐次重验
--------------
§14.1「所有公共端点逐次验证」。每个 handler 第一件事就是
``require_brand_access``;跨租户与不存在**同形 404**,不泄露对象存在性
(交付单 §10.1 记的那个 IDOR 就是漏了这一步)。
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from api.defensive_geo_api import _action, _safe_error
from services.defensive_geo.monitoring import comparability_feed as _cmp
from services.defensive_geo.monitoring import renewal as _renewal
from services.defensive_geo.monitoring import v2_service as _v2

logger = logging.getLogger("GEO-DefGeo-Monitoring")

router = APIRouter(prefix="/api/defensive-geo/monitoring", tags=["防御型GEO-监测"])


def _db():
    from db.connection import get_connection
    return get_connection()


# ══════════════════════════════════════════════════════════════════════════
# [包F ② 2026-08-23] 监测进度入口 —— 关闭闸**已撤除**
# ══════════════════════════════════════════════════════════════════════════
# 一期(fix-of-fix P1-B)关这条链的理由**只有一条**,逐字是:
#   「因为**它下面的账本还没接线**……对客户下发一个"进度 XX%",
#     等于拿一个还没有人在写的账本去回答"我的东西做到哪了"」
# 并且给了明确的重开前置:
#   「重新打开之前必须先有真的 attempt 账本接线
#     (``defgeo_monitoring_attempts`` 有生产写入方,而不只是判据在插)」
#
# 包F ① 把执行链五点接上了:``claim_monitoring_run_cell`` /
# ``save_monitoring_result`` / ``finish_monitoring_cell_error`` /
# ``create_monitoring_run_cells`` / 人工身份确认。生产写入方从 0 变成 5,
# 前置条件已满足 ⇒ 闸随之撤除,而不是留一个恒 True 的常量在这里
# (留着的话它就成了一个"看起来还有闸"的死开关)。
#
# 🔴 判据的对应变化(成对,不是单向放开):
#   · ``test_p1b_flag_is_closed`` 退役 —— 它钉的常量已不存在;
#   · 一期那些 ``skip_on="MONITORING_PROGRESS_CLOSED"`` 的判据**自动复活**,
#     并各配一条"不许再被 skip"的活性自证(否则复活了没人知道);
#   · 一期 MUT-18 那条**临时开闸**打存在性的判据转**常驻**(去掉 monkeypatch);
#   · 新增反向锁:这两个模块里不许再出现 ``MONITORING_PROGRESS_CLOSED``
#     字面量(AST 级,不是裸 grep)—— 防的是"悄悄再加一道静默闸"。


def _require_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail={"code": "NOT_AUTHENTICATED"})
    return user


def _require_brand(request: Request, brand_id: int) -> None:
    """对象级授权。**每个 handler 都调**,不靠中间件兜。

    交付单 §10.1 那个 IDOR 的成因就是"以为全局中间件会兜" ——
    ``auth/middleware.py:_extract_brand_id`` 只读 query 的 brand_id/quote_id/
    client_id 与两个白名单前缀,路径参数它看不见。
    """
    from auth.brand_access import require_brand_access

    try:
        require_brand_access(request, brand_id, allow_null=False)
    except HTTPException:
        # 跨租户与不存在**同形** —— 不泄露对象存在性(ACT-02)。
        raise _safe_error("NOT_FOUND") from None


# ── DTO ──────────────────────────────────────────────────────────────────

class _StrictRequest(BaseModel):
    """请求体:``extra='forbid'`` —— 未知/错 casing 一律 422 SafeError(API-01)。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class _Response(BaseModel):
    """响应体:**刻意不设** ``extra='forbid'``。

    🔴 G-4 / 本仓**三次**生产事故的形态:``response_model`` 上挂
       ``extra='forbid'`` 时,handler 多返回一个键 ⇒ Pydantic 在**构造期**抛 ⇒
       端点**裸 500**。而 §18.5 API-01 要的是"多返回一键 → **受控失败**"。

       响应模型不需要 forbid:FastAPI 按声明字段序列化,多余键本就不会外泄。
       forbid 在这里只会把"多写了一个键"从"静默丢弃"升级成"整个端点炸"。
       判据 `test_extra_response_key_is_dropped_not_500` 打这一位的正样本。
    """

    model_config = ConfigDict(populate_by_name=True)


class RunAdmissionRequest(_StrictRequest):
    brand_id: int = Field(..., alias="brandId", ge=1)


class RunAdmissionResponse(_Response):
    is_v2: bool = Field(..., alias="isV2")
    admitted: bool
    path: str
    reason_code: str | None = Field(None, alias="reasonCode")
    next_action: dict | None = Field(None, alias="nextAction")
    freeze_count_expected: int = Field(..., alias="freezeCountExpected")
    outbox_count_expected: int = Field(..., alias="outboxCountExpected")
    provider_call_count_expected: int = Field(..., alias="providerCallCountExpected")


class RunProgressResponse(_Response):
    planned_cells: int = Field(..., alias="plannedCells")
    attempted_cells: int = Field(..., alias="attemptedCells")
    terminal_cells: int = Field(..., alias="terminalCells")
    policy_skipped_cells: int = Field(..., alias="policySkippedCells")
    attempt_records: int = Field(..., alias="attemptRecords")
    attempt_errors: list[dict] = Field(..., alias="attemptErrors")
    progress_pct: float = Field(..., alias="progressPct")


class ComparabilityResponse(_Response):
    level: Literal["none", "full", "partial"]
    reason_code: str | None = Field(None, alias="reasonCode")
    matched_cells: int = Field(..., alias="matchedCells")
    baseline_only_cells: int = Field(..., alias="baselineOnlyCells")
    current_only_cells: int = Field(..., alias="currentOnlyCells")
    dimensions: dict | None
    next_action: dict = Field(..., alias="nextAction")


# ── routes ───────────────────────────────────────────────────────────────

@router.post("/run-admission", response_model=RunAdmissionResponse,
             response_model_by_alias=True)
async def run_admission(body: RunAdmissionRequest, request: Request):
    """§15.9.3 admission 预览 —— **零 freeze / 零 outbox / 零 provider**。

    legacy 品牌走这里同样返回 ``admitted=true``,并明确标出 ``path`` 是
    legacy —— 这样「新门有没有误伤老代理」在**接口层**就能被看见,
    不用去猜。
    """
    _require_user(request)
    _require_brand(request, body.brand_id)

    conn = _db()
    try:
        with conn.cursor() as cur:
            enr, decision = _v2.admit_monitoring_run(cur, brand_id=body.brand_id)
    finally:
        conn.close()

    next_action = None
    if decision.next_action_kind:
        next_action = _action(
            decision.next_action_kind,
            target={"kind": "brand", "id": str(body.brand_id)})

    return RunAdmissionResponse(
        isV2=enr.is_v2,
        admitted=decision.admitted,
        path=decision.path,
        reasonCode=decision.reason_code,
        nextAction=next_action,
        # legacy 的副作用由现役链决定,本门不预言 ⇒ -1 表示"不适用"。
        freezeCountExpected=decision.freeze_count_expected,
        outboxCountExpected=decision.outbox_count_expected,
        providerCallCountExpected=decision.provider_call_count_expected,
    )


@router.get("/runs/{task_id}/progress", response_model=RunProgressResponse,
            response_model_by_alias=True)
async def run_progress(task_id: int, request: Request,
                       brand_id: int = Query(..., alias="brandId", ge=1)):
    """MON-11:从 **attempt 账本**重建进度,不读任何缓存计数列。

    ``progressPct`` 在服务端就先自我复算过一遍(``assert_recomputable``),
    对不上直接 500 而不是下发一个错的百分比。

    [包F ② 2026-08-23] 入口**已重开**:一期关它的唯一理由是"账本还没接线",
    而包F ① 已经把执行链五点接上(生产写入方 0 → 5)。见本模块顶部那段说明。
    """
    _require_user(request)
    _require_brand(request, brand_id)

    conn = _db()
    try:
        with conn.cursor() as cur:
            # 🔴 [P1-3 2026-08-23] 存在性闸。原来没有这一步:任何 task_id
            #    (-1 / 0 / 一个不存在的 33 位数字)都会走到下面那条 cells 查询,
            #    查不到 → plan_cell_ids 空 → 投影出一份**全零**进度,然后 200 下发。
            #    那是**无中生有的谎报**:"这次跑了 0 格、完成 0%"与
            #    "根本没有这次跑" 在客户眼里是两件完全不同的事,而我们把后者
            #    说成了前者。零和不存在必须能分开。
            #
            #    存在性查 **task 表**而不是 cells 表:刚建好、还没铺格子的 task
            #    是合法的(此时 0 格是真的 0 格),拿 cells 判会把它误杀成 404。
            #    连 brand_id 一起查是有意的 —— 与下面那条 cells 查询同一作用域,
            #    顺带不泄露"这个 task 存在,只是不属于你"。
            cur.execute(
                "SELECT 1 FROM public.monitoring_tasks WHERE id=%s AND brand_id=%s",
                (int(task_id), int(brand_id)))
            if cur.fetchone() is None:
                raise _safe_error("NOT_FOUND")


            # 🔴 [fix-of-fix P1-C 2026-08-23] 只数 **planned** 的格。
            #    原来这条不带 ``is_planned`` 过滤,于是未计划的格也进了分母 ——
            #    而 ``project_run_progress`` 的 planned 就是 ``len(set(plan_cell_ids))``,
            #    terminal 只会落在真正跑过的那些格上。
            #    后果是**进度被系统性低报**:1 格 planned 且已完成 + 3 格 unplanned
            #    会显示成 25%,而她的计划其实已经 100% 跑完了。
            #    "还差 75%" 与 "已经做完了" 对一个在等交付的客户是两回事。
            cur.execute(
                "SELECT DISTINCT plan_hash FROM public.monitoring_run_cells "
                "WHERE task_id=%s AND brand_id=%s AND is_planned = TRUE",
                (int(task_id), int(brand_id)))
            plan_cell_ids = [r["plan_hash"] for r in (cur.fetchall() or [])
                             if r.get("plan_hash")]

            # [工单 E3-4 · P1-10 · Codex 二审] 老链跑的那次监测 ⇒ **typed 不可用**,
            # 不是 200 + progressPct=0。
            #
            # 🔴 二审逐字:"legacy task 不能继续返回成功 200/progressPct=0"。
            #    原因不是"数字不好看",是**这个 0 会被当成真进度读**:
            #    她看到 0%,判断是"卡住了/没跑",于是重跑一次(再花一次钱),
            #    而真相是这次监测早就跑完了、只是结果在老链那张表上。
            #    typed 409 + nextAction 把她指到看得到结果的地方,不让她重跑。
            #
            # 🔴 判据是**这个任务有没有 v2 耐久计划**(``monitoring_run_cells``
            #    零行),不是"这个品牌现在有没有 enrolled v2"。
            #    第一版写的正是后者(``resolve_monitoring_enrollment(brand_id=…)``)
            #    —— 那读的是**当前**报价快照,同一次历史运行的答案会随着品牌
            #    之后买没买 v2 而改变。那正是本单 E3-1 花了一整节消灭的
            #    「现读可变列」形态,不能在这里请回来。
            #    run cells 是那次运行**当时**建的,行在不在是不可变事实。
            # 🔴 与 NOT_FOUND 分开:上面那条已经证明任务**存在**。
            if not plan_cell_ids:
                raise _safe_error("MONITORING_LEGACY_RUN")

            progress = _v2.project_run_progress(cur, plan_cell_ids=plan_cell_ids)
    except _v2.V2ServiceError:
        raise _safe_error("INTERNAL_ERROR") from None
    finally:
        conn.close()

    return RunProgressResponse(
        plannedCells=progress.planned_cells,
        attemptedCells=progress.attempted_cells,
        terminalCells=progress.terminal_cells,
        policySkippedCells=progress.policy_skipped_cells,
        attemptRecords=progress.attempt_records,
        attemptErrors=[dict(e) for e in progress.attempt_errors],
        progressPct=progress.progress_pct,
    )


@router.get("/reports/{report_snapshot_id}/comparability",
            response_model=ComparabilityResponse, response_model_by_alias=True)
async def report_comparability(
    report_snapshot_id: str, request: Request,
    brand_id: int = Query(..., alias="brandId", ge=1),
    baseline_snapshot_id: str | None = Query(None, alias="baselineSnapshotId"),
):
    """§13.2 同口径 D0/D30。

    🔴 Z-2.3:``no_comparable_baseline`` **不许是死路**。所以每一档都带
       非空 ``nextAction``;没有基线时指向「建立同口径复测计划」。
    """
    _require_user(request)
    _require_brand(request, brand_id)

    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT report_snapshot_id, revision, created_at "
                "  FROM public.defgeo_report_snapshots "
                " WHERE report_snapshot_id=%s AND brand_id=%s "
                " ORDER BY revision DESC LIMIT 1",
                (report_snapshot_id, int(brand_id)))
            current = cur.fetchone()
            if not current:
                raise _safe_error("NOT_FOUND")
            baseline = None
            if baseline_snapshot_id:
                cur.execute(
                    "SELECT report_snapshot_id, revision, created_at "
                    "  FROM public.defgeo_report_snapshots "
                    " WHERE report_snapshot_id=%s AND brand_id=%s "
                    " ORDER BY revision DESC LIMIT 1",
                    (baseline_snapshot_id, int(brand_id)))
                baseline = cur.fetchone()
    finally:
        conn.close()

    if baseline is None:
        projection = _cmp.project(
            baseline_snapshot_ref=None,
            current_snapshot_ref=str(current["report_snapshot_id"]),
            baseline_as_of=None,
            current_as_of=current["created_at"].isoformat(),
            scope=None, baseline_cells=(), current_cells=(),
        )
        # Z-2.3:这一档的动作指向「建立同口径复测计划」,不是 null。
        action = _action("create_diagnosis_preview",
                         target={"kind": "brand", "id": str(brand_id)})
    else:
        projection = _cmp.project(
            baseline_snapshot_ref=str(baseline["report_snapshot_id"]),
            current_snapshot_ref=str(current["report_snapshot_id"]),
            baseline_as_of=baseline["created_at"].isoformat(),
            current_as_of=current["created_at"].isoformat(),
            scope={k: True for k in _cmp.SCOPE_TO_DIMENSION},
            baseline_cells=(), current_cells=(),
        )
        action = _action("view_result",
                         target={"kind": "report_snapshot",
                                 "id": str(current["report_snapshot_id"])})

    return ComparabilityResponse(
        level=projection.level,
        reasonCode=(projection.reason.code if projection.reason else None),
        matchedCells=projection.matched_cells,
        baselineOnlyCells=projection.baseline_only_cells,
        currentOnlyCells=projection.current_only_cells,
        dimensions=projection.dimensions,
        nextAction=action,
    )


#: 判据用它机械核对「本模块登记了哪些路由」。手写清单不算分母。
def route_census() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for route in router.routes:
        methods = sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"})
        out.append({"path": route.path, "methods": methods,
                    "name": route.name})
    return sorted(out, key=lambda r: (r["path"], r["methods"]))
