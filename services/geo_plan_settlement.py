"""[v5 req1 · Deploy-CTO 2026-07-13] GEO 任务【耐久结算】收口 + 补偿队列。

铁律:禁止 running 直接宣布 done —— 必须 running→settling→done,且【commit_freeze 明确 success=true】后
     才 done / 展示结果 / 发通知。commit/release 返回 false 或异常 → 进 settlement_pending / refund_pending,
     由 scheduler reconcile 补偿重试(idempotent commit/release)后落终态。

统一补偿队列:worker 成功/失败、API cancel、zombie、server_restart 全部经这里(或 mark_zombie/sweep_server_restart
     的 pending 路由)进入 geo_plan_tasks 的 settlement_pending / refund_pending 状态 → reconcile 收口。

不改 middleware/billing.py:只调 commit_freeze / release_freeze 公共接口并【检查其 success 返回】。
"""
from __future__ import annotations

import logging
from typing import Optional

from db.geo_plan_tasks_db import mark_status, bump_settle_retry, list_pending_settlements, get_task_by_id
from middleware.billing import commit_freeze, release_freeze

logger = logging.getLogger("GEO-PlanSettle")

# 补偿重试次数超过此阈值 → CRITICAL 告警(留队列 · 人工介入)
SETTLE_RETRY_ALERT = 5


# [v6 req1] commit/release 必须校验【精确终态】· 三态判定:
#   ok       :fresh success(success=True 无 idempotent)· 或 idempotent 且 status 恰为期望终态(committed/released)
#   conflict :idempotent 但 status 是【相反终态】(commit 时已 released / release 时已 committed)· 或 ambiguous
#              → 禁落 done/failed,进 settle_conflict 人工桶
#   retry    :其它 success=False(未找到等瞬时)· 留补偿队列重试
#   billing 返回契约(middleware/billing.py 只读):
#     fresh commit/release → {"success": True, "freeze_id", "amount", "balance"}(无 idempotent/status)
#     幂等 → {"success": True, "idempotent": True, "status": <'committed'|'released'>}
#     跨表撞号 → {"success": False, "ambiguous": True}
#     未找到 → {"success": False, "reason": ...}
def _commit_outcome(res) -> str:
    if not res:
        return "retry"
    if res.get("idempotent") is True:
        st = res.get("status")
        return "ok" if st == "committed" else "conflict"   # 已 released / 未知终态 → 冲突
    if res.get("success") is True:
        return "ok"                                          # fresh commit
    if res.get("ambiguous"):
        return "conflict"                                    # 跨表撞号 → 人工
    return "retry"                                           # 瞬时失败可重试


def _release_outcome(res) -> str:
    if not res:
        return "retry"
    if res.get("idempotent") is True:
        st = res.get("status")
        return "ok" if st == "released" else "conflict"     # 已 committed / 未知终态 → 冲突
    if res.get("success") is True:
        return "ok"                                          # fresh release
    if res.get("ambiguous"):
        return "conflict"
    return "retry"


def _raise_settle_conflict(task_id, *, freeze_id, user_id, brand_id, op: str, res, error_code=None):
    """[v6 req1] commit/release 命中相反终态 → 标 settle_conflict(禁 done/failed)+ 落 fund_recovery 人工工单 + CRITICAL。

    [v6 对抗审 P3 修] 工单登记以【赢得 settle_conflict CAS 转移】为门控 —— inline 与 reconcile 并发同一 conflict
      任务时,只有赢得转移的那一个落工单(exactly-once),避免同 task 堆重复 manual 工单。
    """
    won = mark_status(task_id, "settle_conflict", error_code=(error_code or f"{op}_conflict"),
                      error_detail=f"{op} 命中相反终态/歧义: {str(res)[:300]}")
    if not won:
        logger.critical(f"[settle][CONFLICT] task={task_id} {op} 命中相反终态但 settle_conflict CAS 未赢(已被并发方处置)· "
                        f"跳过重复工单 · res={res}")
        return
    try:
        from db.fund_recovery_db import create_recovery_order
        # 冲突不可自动重试(idempotent+相反终态,retry 不会改变)→ 直接 status='manual'(人工处置 · scheduler 不领)
        create_recovery_order(
            "geo_plan_settle", op, ref_key=f"geoplan_{task_id}", user_id=user_id,
            feature_code="geo_plan_l1l2_fallback", charge_tx_id=None, status="manual",
            reason=f"GEO 任务 {op} 命中相反终态/歧义 · 需人工核对 freeze 真状态",
            last_error=str(res)[:400],
            payload={"task_id": task_id, "freeze_id": freeze_id, "brand_id": brand_id, "op": op, "billing_res": str(res)[:400]},
        )
    except Exception as e:
        logger.critical(f"[settle] task={task_id} {op} conflict 落工单失败(仍已标 settle_conflict): {e}")
    logger.critical(f"[settle][CONFLICT] task={task_id} {op} 命中相反终态/歧义 → settle_conflict 人工桶 · res={res}")


def build_ready_content(result: Optional[dict]) -> str:
    """GEO 方案就绪通知文案(从 result 提取主题包/词数/入门价)。"""
    result = result or {}
    clusters = result.get("clusters") or []
    keyword_package = result.get("keyword_package") or []
    tier = result.get("tier_summaries") or {}
    cluster_count = len(clusters) if isinstance(clusters, list) else 0
    kw_count = len(keyword_package) if isinstance(keyword_package, list) else 0
    entry_price = 0
    prices = []
    if isinstance(tier, dict) and tier:
        prices = [(v.get("price_yuan") or v.get("total_yuan") or 0) for v in tier.values() if isinstance(v, dict)]
    elif isinstance(tier, list) and tier:
        prices = [(t.get("price_yuan") or t.get("total_yuan") or 0) for t in tier if isinstance(t, dict)]
    prices = [int(p) for p in prices if isinstance(p, (int, float)) and p > 0]
    if prices:
        entry_price = min(prices)
    return (f"{cluster_count} 个主题包 · {kw_count} 词"
            + (f" · 推荐套餐 ¥{entry_price}起" if entry_price > 0 else ""))


def _notify_ready(brand_id, task_id, result):
    """Compatibility hook; durable user notification is emitted by mark_status()."""
    logger.info("[settle] task=%s ready notification queued by status transaction", task_id)


def _notify_failed(brand_id, task_id, content):
    """Compatibility hook; durable user notification is emitted by mark_status()."""
    if content:
        logger.info("[settle] task=%s failed notification queued by status transaction", task_id)


# [v5 对抗审 P2 修] 失败通知文案由【持久化的 error_code】派生(单一来源)· 使 inline 与 reconcile 补偿
#   两条收口路径都能一致地发通知,消除"经补偿队列收口的失败任务被退款却收不到通知"的静默丢失。
#   仅 low_quality/external_api/internal 三类给用户失败告知;cancelled/timeout/zombie/server_restart → None(不通知,同原语义)。
_FAILURE_NOTIFY_BY_CODE = {
    "low_quality": "关键词数据不足，请补充更多关键词后重试。相关积分已退回。",
    "external_api": "外部服务暂时不可用，请稍后重试。相关积分已退回。",
    "internal": "系统内部错误，请稍后重试或联系客服。相关积分已退回。",
}


def failure_notify_content(error_code: Optional[str]) -> Optional[str]:
    """据 error_code 派生用户失败告知文案;不在白名单(cancelled/timeout/zombie/restart 等)→ None(不通知)。"""
    return _FAILURE_NOTIFY_BY_CODE.get(error_code or "")


# ============================================================
# [v7 finding2 · P1] settle_conflict 人工收口 —— 据【真实 freeze 状态】派生唯一合法终态
# ============================================================

_CONFLICT_FAILURE_TERMINALS = ("failed", "cancelled", "timeout")


def read_freeze_status(freeze_id, freeze_table=None, user_id=None) -> Optional[str]:
    """[v7 finding2] 只读 freeze 真实状态('frozen'|'committed'|'released'|'ambiguous'|None)· 绝不动钱。

    据 task 存的 freeze_table 精确读对应表(legacy=point_freezes / v35=customer_credit_freezes)。
    未知/缺失 freeze_table → 两表都查(freeze_id 跨表独立自增可能撞号 → 两表都命中返 'ambiguous',交人工核)。
    先 to_regclass 探表:表不存在时直接 SELECT 会 abort 事务;只读连接故用后即弃。
    """
    if not freeze_id:
        return None
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()

        def _reg(tbl):
            cur.execute("SELECT to_regclass(%s) AS r", (tbl,))
            r = cur.fetchone()
            return (r["r"] if isinstance(r, dict) else r[0]) if r else None

        def _read(tbl, id_col_extra):
            if _reg(tbl) is None:
                return None
            if user_id is not None:
                cur.execute(f"SELECT status FROM {tbl} WHERE id=%s AND {id_col_extra}=%s", (freeze_id, user_id))
            else:
                cur.execute(f"SELECT status FROM {tbl} WHERE id=%s", (freeze_id,))
            r = cur.fetchone()
            return (r["status"] if isinstance(r, dict) else r[0]) if r else None

        ft = (freeze_table or "").strip().lower()
        if ft == "legacy":
            return _read("point_freezes", "user_id")
        if ft == "v35":
            return _read("customer_credit_freezes", "customer_user_id")
        # 未知/缺失 → 两表都查
        s_legacy = _read("point_freezes", "user_id")
        s_v35 = _read("customer_credit_freezes", "customer_user_id")
        if s_legacy is not None and s_v35 is not None:
            return "ambiguous"
        return s_legacy if s_legacy is not None else s_v35
    finally:
        try:
            conn.close()
        except Exception:
            pass


def resolve_settle_conflict(task_id: int, admin_id, requested_terminal: Optional[str] = None) -> dict:
    """[v7 finding2 · P1] settle_conflict 任务人工收口:据【真实 freeze 状态】派生【唯一合法终态】,不信任请求终态。

    铁律(防"已退款却交付" / "已扣款却标失败"):
      - freeze committed(已扣款)→ 只能落 done(不接受 failed/cancelled/timeout);
      - freeze released(已退款)→ 只能落失败态(admin 可选 failed/cancelled/timeout · 绝不接受 done);
      - freeze frozen(未终结)→ 拒绝人工强制终态(应由补偿队列 reconcile commit/release 收口);
      - None/ambiguous → 拒绝(需人工核对 point_freezes/customer_credit_freezes 两表)。
    freeze 已终结 → 【不再动钱】,仅使任务终态与资金方向一致(CAS 落终态 + 对应通知)。
    返回 {"ok": bool, "terminal": Optional[str], "freeze_status": Optional[str], "reason": str}。
    """
    task = get_task_by_id(task_id)
    if not task:
        return {"ok": False, "terminal": None, "freeze_status": None, "reason": "任务不存在"}
    freeze_id = task.get("freeze_id")
    freeze_table = task.get("freeze_table")
    user_id = task.get("user_id")
    brand_id = task.get("brand_id")
    error_code = task.get("error_code") or "settle_conflict_manual"

    if not freeze_id:
        # 无 freeze 的冲突(无资金)· 按请求失败态收口(默认 failed)
        legal = requested_terminal if requested_terminal in _CONFLICT_FAILURE_TERMINALS else "failed"
        won = mark_status(task_id, legal, error_code=error_code,
                          error_detail=f"admin={admin_id} 无 freeze 冲突收口 → {legal}")
        if won:
            _notify_failed(brand_id, task_id, failure_notify_content(task.get("error_code")))
        return {"ok": bool(won), "terminal": legal, "freeze_status": None,
                "reason": "ok" if won else f"任务状态无法转 {legal}(当前 {task.get('status')})"}

    fs = read_freeze_status(freeze_id, freeze_table, user_id)
    if fs == "committed":
        if requested_terminal and requested_terminal != "done":
            return {"ok": False, "terminal": None, "freeze_status": fs,
                    "reason": f"freeze 已 committed(已扣款)· 只能落 done · 拒绝 {requested_terminal}(防已扣款却标失败)"}
        legal = "done"
    elif fs == "released":
        if requested_terminal == "done":
            return {"ok": False, "terminal": None, "freeze_status": fs,
                    "reason": "freeze 已 released(已退款)· 只能落失败态 · 拒绝 done(防已退款却交付)"}
        # [v7 对抗审 P1 修] released 分支【绝不回落 pending_terminal】—— 由 settling→settlement_pending 派生的冲突任务
        #   其 pending_terminal='done'(zombie_settling/restart_settling/commit 补偿口径),若 admin 省略 terminal(None)
        #   回落到它会把【已退款】任务标 done + 发就绪通知 = 已退款却交付(正是本函数要杜绝的资金终态不一致)。
        #   released → 只允许失败态;admin 未指定则默认 failed(绝不 done)。
        legal = requested_terminal if requested_terminal in _CONFLICT_FAILURE_TERMINALS else "failed"
    elif fs == "frozen":
        return {"ok": False, "terminal": None, "freeze_status": fs,
                "reason": "freeze 仍 frozen(未终结)· 不可人工强制终态 · 应由补偿队列 reconcile commit/release 收口"}
    else:  # None / ambiguous
        return {"ok": False, "terminal": None, "freeze_status": fs,
                "reason": f"freeze 状态无法确定({fs})· 需人工核对 point_freezes/customer_credit_freezes 两表后处理"}

    # freeze 已终结 → 不再动钱 · CAS 落派生终态(使任务终态与资金方向一致)
    won = mark_status(task_id, legal, error_code=error_code,
                      error_detail=f"admin={admin_id} settle_conflict 收口 · freeze={fs} → {legal}")
    if not won:
        return {"ok": False, "terminal": legal, "freeze_status": fs,
                "reason": f"任务状态无法转 {legal}(可能已被收口)· 当前 {task.get('status')}"}
    if legal == "done":
        _notify_ready(brand_id, task_id, task.get("result_json"))
    else:
        _notify_failed(brand_id, task_id, failure_notify_content(task.get("error_code")))
    return {"ok": True, "terminal": legal, "freeze_status": fs, "reason": "ok"}


async def finalize_success(task_id: int, *, freeze_id, freeze_table, user_id, brand_id,
                           result: dict, data_mode: str) -> str:
    """LLM 成功 → running→settling(落 result)→ commit_freeze → done / settlement_pending。

    返回最终状态字符串。禁止跳过 settling 直接 done;禁止 commit 未确认就 done/通知。
    """
    # running→settling(结果落库 · 崩溃可由补偿队列 commit)
    settling_won = mark_status(task_id, "settling", result_json=result)
    if not settling_won:
        logger.warning(f"[settle] task={task_id} settling CAS 未赢(已被抢先置终态)· 跳过结算,不动帐")
        return "skipped"

    need_commit = (data_mode == "l1l2_fallback" and freeze_id)
    if not need_commit:
        if mark_status(task_id, "done"):
            _notify_ready(brand_id, task_id, result)
        return "done"

    cm = None
    try:
        cm = await commit_freeze(freeze_id=freeze_id, reason="C 端 GEO 方案完成 · L1L2 兜底扣费",
                                 user_id=user_id, freeze_table=freeze_table)
    except Exception as e:
        logger.exception(f"[settle] task={task_id} commit_freeze 异常: {e}")
    # [v6 req1] 精确终态:ok=fresh/idempotent committed → done;conflict=已released/歧义 → settle_conflict(禁 done);retry → settlement_pending
    outcome = _commit_outcome(cm)
    if outcome == "ok":
        if mark_status(task_id, "done"):
            _notify_ready(brand_id, task_id, result)
            logger.info(f"[settle] task={task_id} settling→done(commit ok): {cm}")
        return "done"
    if outcome == "conflict":
        _raise_settle_conflict(task_id, freeze_id=freeze_id, user_id=user_id, brand_id=brand_id, op="commit", res=cm)
        return "settle_conflict"
    mark_status(task_id, "settlement_pending", error_code="commit_failed",
                error_detail=str(cm)[:400], pending_terminal="done")
    logger.error(f"[settle] task={task_id} commit retry 态 → settlement_pending 待补偿: {cm}")
    return "settlement_pending"


async def finalize_failure(task_id: int, *, freeze_id, freeze_table, user_id, brand_id,
                           terminal: str, error_code: str, error_detail: Optional[str] = None,
                           result_json: Optional[dict] = None) -> bool:
    """失败/取消/超时收口:有 freeze → running→refund_pending(记 pending_terminal)→ release → 终态 / 留队列。

    返回 True=已落终态;False=CAS 未赢 或 release 失败留 refund_pending(补偿队列接管)。
    [v5 对抗审 P2] 失败通知由 error_code 派生(见 failure_notify_content),inline/reconcile 两路一致。
    """
    if not freeze_id:
        # 无 freeze:直接终态(无资金需退)
        won = mark_status(task_id, terminal, error_code=error_code, error_detail=error_detail, result_json=result_json)
        if won:
            _notify_failed(brand_id, task_id, failure_notify_content(error_code))
        return won

    # 有 freeze:先 claim refund_pending(running→refund_pending · 记退款后应落的终态)
    claim = mark_status(task_id, "refund_pending", error_code=error_code, error_detail=error_detail,
                        result_json=result_json, pending_terminal=terminal)
    if not claim:
        logger.warning(f"[settle] task={task_id} refund_pending CAS 未赢(已终态)· 跳过 release(freeze 由赢家/补偿处理)")
        return False

    rr = None
    try:
        rr = await release_freeze(freeze_id=freeze_id, reason=f"GEO 方案 {error_code} 退费",
                                  user_id=user_id, freeze_table=freeze_table)
    except Exception as e:
        logger.exception(f"[settle] task={task_id} release_freeze 异常: {e}")
    # [v6 req1] 精确终态:ok=fresh/idempotent released → 终态;conflict=已committed/歧义 → settle_conflict(禁 failed);retry → 留 refund_pending
    outcome = _release_outcome(rr)
    if outcome == "ok":
        if mark_status(task_id, terminal, error_code=error_code, error_detail=error_detail):
            _notify_failed(brand_id, task_id, failure_notify_content(error_code))
            logger.info(f"[settle] task={task_id} refund_pending→{terminal}(release ok)")
        return True
    if outcome == "conflict":
        _raise_settle_conflict(task_id, freeze_id=freeze_id, user_id=user_id, brand_id=brand_id,
                               op="release", res=rr, error_code=error_code)
        return False
    logger.error(f"[settle] task={task_id} release retry 态 → 留 refund_pending 待补偿: {rr}")
    return False


def _wo_task_id(wo: dict):
    """从 geo_plan_settle 工单解析关联 task_id(payload.task_id 优先 · 回落 ref_key=geoplan_{tid})。"""
    payload = wo.get("payload") or {}
    if isinstance(payload, str):
        try:
            import json as _json
            payload = _json.loads(payload)
        except Exception:
            payload = {}
    tid = payload.get("task_id") if isinstance(payload, dict) else None
    if tid is None:
        ref = wo.get("ref_key") or ""
        if ref.startswith("geoplan_"):
            try:
                tid = int(ref.split("geoplan_", 1)[1])
            except Exception:
                tid = None
    return tid


_TERMINAL_STATUSES = ("done", "failed", "cancelled", "timeout")


def ensure_settle_conflict_workorders() -> dict:
    """[v8 P2 · 对抗审加固] 巡检【双向】收口 settle_conflict 任务 ↔ geo_plan_settle 工单,防两者脱钩:

    正向:任务 settle_conflict 但【缺工单】(_raise_settle_conflict 标了任务却建单失败)→ 补建 manual 工单(缺人工入口)。
    反向:工单未终结但【关联任务已终态】(settle_geo_task 先标任务后关工单之间崩溃 / 历史重复工单)→ resolved 工单
         (否则该工单永久 un-closeable:settle-geo-task 因任务已终态 409、通用接口又被 P2 拦)。
    去重靠 DB 唯一键 uniq_fund_recovery_geoplan_open(source,ref_key)· 巡检幂等。返回 {"created", "closed"}。
    """
    from db.geo_plan_tasks_db import list_settle_conflict_tasks, get_task_by_id
    from db.fund_recovery_db import (has_open_workorder, create_recovery_order,
                                     list_open_workorders_by_source, resolve_recovery_order)
    created = 0
    closed = 0
    # 正向:settle_conflict 任务缺工单 → 补建。
    #   [v9 · Deploy-CTO NO-GO P2-4] id 游标【分页排空】(不再固定 LIMIT 200 致第 201+ 条冲突任务永远排不进窗口
    #   → 缺工单永久饥饿)· 与下方反向巡检对称。补建工单不改任务状态,故按 id 严格递减游标每条恰访问一次,无环。
    _fwd_before = None
    for _fpage in range(50):   # 上限 50 页 × 200 = 1 万 · 防异常无界循环
        fwd = list_settle_conflict_tasks(limit=200, before_id=_fwd_before)
        if not fwd:
            break
        for t in fwd:
            tid = t["id"]
            ref = f"geoplan_{tid}"
            try:
                if has_open_workorder("geo_plan_settle", ref):
                    continue
                wid = create_recovery_order(
                    "geo_plan_settle", "state_fix", ref_key=ref, user_id=t.get("user_id"),
                    feature_code="geo_plan_l1l2_fallback", charge_tx_id=None, status="manual",
                    reason="巡检补建:settle_conflict 任务缺人工工单入口(建单曾失败)",
                    payload={"task_id": tid, "freeze_id": t.get("freeze_id"), "brand_id": t.get("brand_id"),
                             "source": "patrol_repair"},
                )
                if wid:
                    created += 1
                    logger.warning(f"[settle patrol] task={tid} settle_conflict 缺工单 → 补建 manual 工单 id={wid}")
            except Exception as e:
                logger.error(f"[settle patrol] task={tid} 补建工单失败(下轮再试): {e}")
        _fwd_before = fwd[-1]["id"]
        if len(fwd) < 200:
            break
    else:
        # [v9 对抗审 · no-silent-caps] 50 页跑满未 break → backlog 可能 > 1万,本轮未排空(下一轮从头继续)。
        #   不静默截断:告警提示仍有缺工单 settle_conflict 任务待下轮补建(settle_conflict 是罕见错误态,>1万 属异常)。
        logger.warning("[settle patrol] 正向巡检达 50 页(~1万)上限仍未排空 settle_conflict backlog · 仍有缺工单任务待下轮补建")
    # 反向:未终结工单但任务已终态 → 关工单(自愈 settle_geo_task 先标任务后关工单的崩溃窗口 / 历史重复工单 / dispatch 审计单)。
    #   [v8 二轮对抗审 P3] ① 同时扫 geo_plan_settle + geo_plan_dispatch(对称自愈,不只 settle);② id 游标【分页排空】
    #   (不再固定 LIMIT 500 致旧孤儿被新工单挤出窗口永不扫到);③ 每页【批量】取任务状态(一次 = ANY 查代替 N 次 get_task_by_id)。
    from db.geo_plan_tasks_db import get_task_statuses
    for src in ("geo_plan_settle", "geo_plan_dispatch"):
        before = None
        for _page in range(50):   # 上限 50 页 × 200 = 1 万 · 防异常无界循环
            page = list_open_workorders_by_source(src, limit=200, before_id=before)
            if not page:
                break
            wo_by_tid = {}
            for wo in page:
                tid = _wo_task_id(wo)
                if tid is not None:
                    wo_by_tid.setdefault(int(tid), []).append(wo["id"])
            try:
                statuses = get_task_statuses(list(wo_by_tid.keys())) if wo_by_tid else {}
                for tid, wids in wo_by_tid.items():
                    if statuses.get(tid) in _TERMINAL_STATUSES:
                        for wid in wids:
                            if resolve_recovery_order(wid, note=f"巡检反向收口:关联任务 {tid} 已终态 {statuses.get(tid)}"):
                                closed += 1
                                logger.warning(f"[settle patrol] 工单 {wid}(task={tid})任务已终态 {statuses.get(tid)} → 收口 resolved")
            except Exception as e:
                logger.error(f"[settle patrol] {src} 反向收口本页失败(下轮再试): {e}")
            before = page[-1]["id"]
            if len(page) < 200:
                break
        else:
            # [v9 对抗审 · no-silent-caps] 反向巡检 50 页跑满未 break → 该 source 未终结工单 backlog 可能 > 1万,本轮未排空。
            logger.warning(f"[settle patrol] 反向巡检 {src} 达 50 页(~1万)上限仍未排空 · 仍有未终结工单待下轮收口")
    return {"created": created, "closed": closed}


async def reconcile_pending(limit: int = 50) -> dict:
    """[v5 req1] scheduler 补偿:扫 settlement_pending / refund_pending → 重试 commit / release → 落终态。

    commit/release 幂等:重试安全。超过 SETTLE_RETRY_ALERT 次仍失败 → CRITICAL(留队列人工介入)。
    [v8 P2] 每轮先巡检补建缺失的 settle_conflict 人工工单(防标了任务却建单失败致脱钩)。
    """
    try:
        _patched = ensure_settle_conflict_workorders()   # {"created", "closed"} · 双向收口
    except Exception as _pe:
        _patched = {"created": 0, "closed": 0}
        logger.error(f"[reconcile] settle_conflict 工单巡检异常(不阻断补偿): {_pe}")
    tasks = list_pending_settlements(limit)
    stats = {"scanned": len(tasks), "settled_done": 0, "refunded_terminal": 0, "still_pending": 0}
    for t in tasks:
        tid = t["id"]
        status = t["status"]
        freeze_id = t.get("freeze_id")
        freeze_table = t.get("freeze_table")
        user_id = t.get("user_id")
        brand_id = t.get("brand_id")
        retry = t.get("settle_retry_count") or 0

        if status == "settlement_pending":
            # 重试 commit → done
            if not freeze_id:
                if mark_status(tid, "done"):
                    _notify_ready(brand_id, tid, t.get("result_json"))
                    stats["settled_done"] += 1
                continue
            cm = None
            try:
                cm = await commit_freeze(freeze_id=freeze_id, reason="GEO 方案补偿结算 commit",
                                         user_id=user_id, freeze_table=freeze_table)
            except Exception as e:
                logger.exception(f"[reconcile] task={tid} commit 异常: {e}")
            outcome = _commit_outcome(cm)   # [v6 req1] 精确终态
            if outcome == "ok":
                if mark_status(tid, "done"):
                    _notify_ready(brand_id, tid, t.get("result_json"))
                    stats["settled_done"] += 1
                    logger.info(f"[reconcile] task={tid} settlement_pending→done(补偿 commit ok)")
            elif outcome == "conflict":
                _raise_settle_conflict(tid, freeze_id=freeze_id, user_id=user_id, brand_id=brand_id, op="commit", res=cm)
                stats.setdefault("conflict", 0)
                stats["conflict"] += 1
            else:
                n = bump_settle_retry(tid, error=f"commit retry: {cm}")
                stats["still_pending"] += 1
                (logger.critical if n >= SETTLE_RETRY_ALERT else logger.warning)(
                    f"[reconcile] task={tid} commit 补偿第 {n} 次仍失败 → 留队列: {cm}")

        elif status == "refund_pending":
            terminal = t.get("pending_terminal") or "failed"
            if not freeze_id:
                # [v8 P1-3] freeze_id 缺失可能是"冻结成功但 freeze_id 回填前崩溃"的 orphan(server_restart_queued)→
                #   按 task_ref(=geoplan_task_{tid} · 与 freeze_points 一致)尝试释放:释放到 / 幂等已释放 / 确认无冻结 → 落终态;
                #   命中相反终态(已 committed)→ settle_conflict;瞬时失败 → 留队列。绝不无脑落终态漏释放 orphan 冻结。
                orr = None
                try:
                    orr = await release_freeze(task_ref=f"geoplan_task_{tid}", reason="GEO queued 重启补偿 · orphan freeze 释放",
                                               user_id=user_id, freeze_table=freeze_table)
                except Exception as e:
                    logger.exception(f"[reconcile] task={tid} orphan release 异常: {e}")
                # [v8 对抗审 P2 修] "无冻结可释放"判定:优先结构化 flag(billing 若返 not_found/no_freeze),
                #   回落中文子串(billing/customer_credit 现返 reason='未找到冻结记录';billing 为红线暂只读)。
                _no_freeze = bool(orr and (orr.get("not_found") is True or orr.get("no_freeze") is True
                                           or (orr.get("reason") or "").find("未找到") >= 0))
                _o = _release_outcome(orr)
                if _o == "ok" or _no_freeze:
                    if mark_status(tid, terminal, error_code=t.get("error_code"), error_detail=t.get("error_detail")):
                        _notify_failed(brand_id, tid, failure_notify_content(t.get("error_code")))
                        stats["refunded_terminal"] += 1
                elif _o == "conflict":
                    _raise_settle_conflict(tid, freeze_id=None, user_id=user_id, brand_id=brand_id,
                                           op="release", res=orr, error_code=t.get("error_code"))
                    stats.setdefault("conflict", 0)
                    stats["conflict"] += 1
                else:
                    n = bump_settle_retry(tid, error=f"orphan release retry: {orr}")
                    stats["still_pending"] += 1
                    (logger.critical if n >= SETTLE_RETRY_ALERT else logger.warning)(
                        f"[reconcile] task={tid} orphan release 补偿第 {n} 次仍失败 → 留队列: {orr}")
                continue
            rr = None
            try:
                rr = await release_freeze(freeze_id=freeze_id, reason="GEO 方案补偿退款 release",
                                          user_id=user_id, freeze_table=freeze_table)
            except Exception as e:
                logger.exception(f"[reconcile] task={tid} release 异常: {e}")
            outcome = _release_outcome(rr)   # [v6 req1] 精确终态
            if outcome == "ok":
                if mark_status(tid, terminal, error_code=t.get("error_code"), error_detail=t.get("error_detail")):
                    # [v5 对抗审 P2] 补偿队列收口终态时也发失败通知(据持久化 error_code 派生),
                    #   消除"经补偿而非 inline 收口的失败任务被退款却收不到通知"的不对称静默丢失。
                    _notify_failed(brand_id, tid, failure_notify_content(t.get("error_code")))
                    stats["refunded_terminal"] += 1
                    logger.info(f"[reconcile] task={tid} refund_pending→{terminal}(补偿 release ok)")
            elif outcome == "conflict":
                _raise_settle_conflict(tid, freeze_id=freeze_id, user_id=user_id, brand_id=brand_id,
                                       op="release", res=rr, error_code=t.get("error_code"))
                stats.setdefault("conflict", 0)
                stats["conflict"] += 1
            else:
                n = bump_settle_retry(tid, error=f"release retry: {rr}")
                stats["still_pending"] += 1
                (logger.critical if n >= SETTLE_RETRY_ALERT else logger.warning)(
                    f"[reconcile] task={tid} release 补偿第 {n} 次仍失败 → 留队列: {rr}")
    return stats
