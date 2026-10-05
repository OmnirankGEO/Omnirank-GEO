"""[R 批 · U4] 代理自助单行业调研 —— worker + 验收闸门 + freeze 结算 + 队列消费。

职责(端点只负责 create task + freeze + 触发消费,真正跑批与结算全在这里):
  - run_selfserve_task(task_id):
      queued → running → create_round_with_snapshot('selfserve', ...) → run_round(force_*)
      → 【验收闸门】get_publish_media_board(规范行业名) 判「本行业榜是否点亮」
      → 点亮 commit_freeze(扣钱) / 未点亮 release_freeze(退钱) —— fail-closed。
  - consume_selfserve_queue():无活跃 round 时取最旧一条 queued 派发(单条)。
  - *_sync():BackgroundTasks / APScheduler worker 线程用的 new_event_loop 包装。

🔴 计费/验收铁律(middleware/billing.py:816 V3.5 + 本批 SPEC):
  - freeze / commit / release 必须【同一 user_id + 回传 freeze_table】才路由一致。
  - fail-closed:round 未 completed 或榜未点亮 → 一律 release(没点亮不扣钱)。
  - 验收【直调 get_publish_media_board 函数】(非端点,避 60s 缓存返旧 fallback);
    断言 scope=='industry' 且 rows 非空(空态早返回也带 scope=='industry',故 rows 也要非空)。
  - 验收用【规范行业名】(U3 resolver 沉淀在 industries 表的 name),不用品牌原文(归一同源)。
  - if freeze_id 守卫:admin 豁免 freeze_id=None 时 commit/release 全跳过。

红线:不改 billing/connection/auth/jwt 本体,只调用其公共函数;不碰四张公共池表。
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional, Tuple

from db.connection import get_connection
from db.research_selfserve_db import (
    get_selfserve_task,
    set_selfserve_task_status,
    update_selfserve_task_round,
    list_queued_selfserve_tasks,
    list_stale_pending_selfserve_tasks,
    cancel_stale_pending_selfserve_task,
    claim_next_queued_selfserve_task,
    claim_selfserve_task_by_id,
)
from services.research_monitor.round_state import (
    create_round_with_snapshot,
    RoundAlreadyRunningError,
)
# 结算走 billing 公共函数(只调用,不改本体);模块级引用便于测试 monkeypatch。
from middleware.billing import commit_freeze, release_freeze
# 验收数据源(直调函数,避端点 60s 缓存);模块级引用便于测试 monkeypatch。
from services.media_effectiveness_board import get_publish_media_board
# 完成点亮后失效 E1 行业媒体有效性榜缓存,让代理即时看到点亮结果。
from writing.flywheel_cache import invalidate, SCOPE_ENTITY_RANK

logger = logging.getLogger("GEO-ResearchMonitor.SelfserveWorker")


# ============================================================
# run_round 间接层(懒加载 round_runner · 测试可 monkeypatch 本模块 run_round)
# ============================================================


async def run_round(*args, **kwargs):
    """薄间接层:懒导入 round_runner.run_round 再转发。

    为什么不 top-level import:round_runner 引入 platforms/crawler/oss/placement 等重依赖,
    测试(--noconftest · mock 结算链)只需替换本模块的 run_round 属性即可,不必真导 round_runner。
    """
    from services.research_monitor.round_runner import run_round as _impl
    return await _impl(*args, **kwargs)


# ============================================================
# 只读小工具
# ============================================================


def _has_active_round() -> bool:
    """当前是否有 pending/running 的 round(队列消费的串行闸:一次只跑一轮)。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM geo_research_round WHERE status IN ('pending', 'running') LIMIT 1"
        )
        return cur.fetchone() is not None
    except Exception as exc:
        # fail-closed:查不到状态时保守当「有活跃 round」→ 不派发(宁可等,不重复起轮)
        logger.warning("[selfserve] 查活跃 round 失败(保守当有): %s", str(exc)[:200])
        return True
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _load_task_industry(task: dict) -> Tuple[str, str, Optional[int]]:
    """按 industry_id 取规范行业名 + slug(round 快照 + 验收查榜都用规范名 · 归一同源)。

    行业被软删/取不到 → 降级用 industry_key 当名、md5 slug(不断链;验收大概率不点亮 → 自动退钱)。
    """
    industry_id = task.get("industry_id")
    industry_key = task.get("industry_key") or ""
    name = ""
    slug = ""
    if industry_id is not None:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT name, slug FROM geo_research_industries WHERE id = %s",
                (int(industry_id),),
            )
            row = cur.fetchone()
            if row:
                name = (row.get("name") if isinstance(row, dict) else row[0]) or ""
                slug = (row.get("slug") if isinstance(row, dict) else row[1]) or ""
        except Exception as exc:
            logger.warning("[selfserve] 取行业名失败 id=%s: %s", industry_id, str(exc)[:200])
        finally:
            try:
                conn.close()
            except Exception:
                pass
    if not name:
        name = industry_key or (task.get("industry_raw") or "")
    if not slug:
        try:
            from services.research_monitor.industry_registry import slug_for_name
            slug = slug_for_name(name) if name else ""
        except Exception:
            slug = ""
    return name, slug, industry_id


def _normalize_snapshot_prompts(raw) -> list:
    """把 prompt_snapshot(存的勾选题目列表)归一成 round 需要的 [{'id','text'}]。"""
    out = []
    for p in raw or []:
        if isinstance(p, dict):
            text = (p.get("text") or p.get("prompt_text") or "").strip()
            if text:
                out.append({"id": p.get("id"), "text": text})
        elif isinstance(p, str) and p.strip():
            out.append({"id": None, "text": p.strip()})
    return out


# ============================================================
# 结算(fail-closed · 四结局)
# ============================================================


async def _release_and_fail(
    task_id: int, freeze_id, freeze_table, user_id, *, status: str, reason: str
) -> None:
    """未完成统一出口；仅在释放结果已确认后通知“费用已退回”。"""
    notification_terminal = status
    if freeze_id:
        try:
            release_result = await release_freeze(
                freeze_id=freeze_id,
                task_ref=f"selfres_{task_id}",
                user_id=user_id,
                freeze_table=freeze_table,
                reason=f"自助调研未完成·自动退还({reason})",
            )
            if release_result and release_result.get("success") is True:
                # 新释放或幂等命中已 released 都是可验证的退款终态；命中
                # committed 等其它状态绝不能对用户宣称费用已退回。
                released_status = release_result.get("status")
                if not release_result.get("idempotent") or released_status == "released":
                    notification_terminal = "refunded"
                else:
                    notification_terminal = "manual_required"
            else:
                notification_terminal = "manual_required"
        except Exception as exc:
            # 退款失败不回滚终态:freeze_sweeper 每小时兜底扫 12h+ frozen 自动 release。
            logger.exception("[selfserve] task=%s release_freeze 失败: %s", task_id, exc)
            notification_terminal = "manual_required"
    try:
        set_selfserve_task_status(
            task_id,
            status,
            failed_reason=reason,
            mark_finished=True,
            notification_terminal=notification_terminal,
        )
    except Exception as exc:
        logger.exception("[selfserve] task=%s 落终态(%s)失败: %s", task_id, status, exc)


async def _settle_completed(
    task_id: int, industry_name: str, freeze_id, freeze_table, user_id,
    billing_exempt: bool = False,
) -> None:
    """round completed 后的验收闸门 + 结算。

    🔴 验收:直调 get_publish_media_board(industry=规范行业名)(非端点·allow_all_industry_fallback=False),
    断言 scope=='industry' 且 rows 非空 ⟺ 本行业自有 shadow 快照被点亮。
    """
    # [R#4] 防御:规范行业名过泛(通用/全部行业)→ get_publish_media_board 会退成 all_industry scope,
    #   验收永假。此时当无效验收(退款 + 告警),不静默走「点亮/未点亮」误判。端点已在冻结前拦(422),
    #   这里是纵深防御(行业事后被改名成泛名等边缘场景)。
    try:
        from services.media_entity_flywheel import is_all_industry_scope
        if is_all_industry_scope(industry_name or ""):
            logger.error("[selfserve] task=%s 验收行业名过泛(%r)·当无效验收退款", task_id, industry_name)
            await _release_and_fail(
                task_id, freeze_id, freeze_table, user_id,
                status="failed", reason="industry_too_generic_release",
            )
            return
    except Exception:
        pass  # 判定不可用不阻断正常验收

    board = None
    try:
        board = get_publish_media_board(
            industry=industry_name or "",
            allow_all_industry_fallback=False,  # 只认本行业自有快照,退全行业不算点亮
        )
    except Exception as exc:
        logger.exception("[selfserve] task=%s 验收查榜失败(当未点亮): %s", task_id, exc)
        board = None

    lit = bool(
        board
        and board.get("scope") == "industry"
        and board.get("rows")
    )

    # [P0-2] 点亮后落 completed(带 marker)+ 失效榜缓存的公共尾:needs_manual / needs_recharge 分支复用。
    #   关键:这些分支【不 release】冻结 —— 保持 frozen 等人工补扣 / 重试 commit(幂等);且 freeze_sweeper
    #   已按 failed_reason(needs_manual/needs_recharge)排除,不会被当 12h 僵尸自动 release(否则钱退了榜还亮)。
    def _complete_with_marker(reason: str) -> None:
        try:
            set_selfserve_task_status(
                task_id, "completed", failed_reason=reason, mark_finished=True,
                notification_terminal="manual_required",
            )
        except Exception as exc:
            logger.exception("[selfserve] task=%s 落 completed(%s)失败: %s", task_id, reason, exc)
        try:
            invalidate([SCOPE_ENTITY_RANK])
        except Exception:
            pass

    if lit:
        if freeze_id:
            # [P0-2] 点亮 → commit(扣钱)。回传 user_id + freeze_table(V3.5 路由铁律)。
            #   commit_freeze 必须【严格 success=True】(或幂等已 committed)才算扣费成功:异常 / 返回
            #   success=False / ambiguous / not-found / None,一律【不落普通 completed】,标 needs_manual
            #   保持冻结待人工补扣,绝不 fall-through 免费交付。
            commit_result = None
            try:
                commit_result = await commit_freeze(
                    freeze_id=freeze_id,
                    task_ref=f"selfres_{task_id}",
                    user_id=user_id,
                    freeze_table=freeze_table,
                    reason="自助调研完成·本行业榜已点亮",
                )
            except Exception as exc:
                # 扣费异常 → 榜确实交付了(标 completed),但绝不静默免费:打 needs_manual 待人工补扣,
                #   冻结保持 frozen(不 release · 已被 sweeper 排除),不 fall-through 到普通 completed。
                logger.exception(
                    "[selfserve] task=%s commit_freeze 异常·榜已点亮但未确认扣费·需人工补扣 freeze_id=%s: %s",
                    task_id, freeze_id, exc,
                )
                _complete_with_marker("billing_commit_failed_needs_manual")
                return

            if commit_result is None:
                # 返回 None(理论不该发生)→ 无法确认扣费成功 → needs_manual(fail-closed 保住钱)。
                logger.error(
                    "[selfserve] task=%s commit_freeze 返回 None·无法确认扣费·需人工补扣 freeze_id=%s",
                    task_id, freeze_id,
                )
                _complete_with_marker("billing_commit_failed_needs_manual")
                return

            # [R#15] commit 命中【已 released】幂等 no-op(冻结被 freeze_sweeper 12h+ 提前扫走退款)→
            #   榜已点亮但 0 扣费 = 免费送。绝不静默:告警 + 落 completed 带 needs_recharge 供人工补扣。
            if (commit_result.get("idempotent")
                    and commit_result.get("status") == "released"):
                logger.error(
                    "[selfserve] task=%s commit 命中已 released 冻结(疑被 freeze_sweeper 扫走)·"
                    "榜已点亮但未扣费·需人工补扣 freeze_id=%s", task_id, freeze_id,
                )
                _complete_with_marker("billing_swept_needs_recharge")
                return

            # [P0-2] 真扣费成功(success=True·非 released 幂等)或 idempotent 已 committed(=已扣过)→ 正常 completed。
            #   其余(success=False / ambiguous / not-found)→ needs_manual(同异常分支:保住钱不静默免费)。
            if commit_result.get("success") is not True:
                logger.error(
                    "[selfserve] task=%s commit_freeze 未成功(result=%s)·榜已点亮但未确认扣费·需人工补扣 freeze_id=%s",
                    task_id, {k: commit_result.get(k) for k in ("success", "reason", "ambiguous", "status")}, freeze_id,
                )
                _complete_with_marker("billing_commit_failed_needs_manual")
                return
        elif billing_exempt:
            # admin / 零成本合法免费:无 freeze 可扣,正常完成。
            logger.info("[selfserve] task=%s 完成·榜已点亮·计费豁免(无扣费)", task_id)
        else:
            # [R#1] freeze_id 缺失且非豁免 → 孤儿态,绝不静默当免费完成(应被 run 前守卫拦住,纵深防御)。
            logger.error(
                "[selfserve] task=%s 点亮但无 freeze_id 且非豁免·异常不静默免费·标记待人工核", task_id,
            )
            try:
                set_selfserve_task_status(
                    task_id, "completed",
                    failed_reason="no_freeze_not_exempt_anomaly", mark_finished=True,
                    notification_terminal="manual_required",
                )
            except Exception as exc:
                logger.exception("[selfserve] task=%s 落 completed(anomaly)失败: %s", task_id, exc)
            try:
                invalidate([SCOPE_ENTITY_RANK])
            except Exception:
                pass
            return
        try:
            set_selfserve_task_status(
                task_id, "completed", mark_finished=True,
                notification_terminal="completed",
            )
        except Exception as exc:
            logger.exception("[selfserve] task=%s 落 completed 失败: %s", task_id, exc)
        # 失效 E1 行业媒体有效性榜缓存(fail-soft)。
        try:
            invalidate([SCOPE_ENTITY_RANK])
        except Exception:
            pass
        logger.info("[selfserve] task=%s 完成·榜已点亮·已结算", task_id)
    else:
        # 未点亮 → 退钱(fail-closed)。
        await _release_and_fail(
            task_id, freeze_id, freeze_table, user_id,
            status="failed", reason="board_not_lit_refunded",
        )
        logger.info("[selfserve] task=%s 完成但本行业榜未点亮·已退款", task_id)


# ============================================================
# 主 worker
# ============================================================


async def run_selfserve_task(task_id: int, *, pre_claimed: bool = False) -> None:
    """执行一条自助调研任务。fire-and-forget,不向上抛。

    [R#5] 并发防重复付费轮:
      - pre_claimed=True:调用方(consume 侧 claim_next_queued_selfserve_task)已原子把该 task 置 running。
      - pre_claimed=False(直调):本函数原子自占(claim_selfserve_task_by_id · queued→running),
        败者(已被并发占用)直接跳过,不进建轮/付费链。
    """
    try:
        task = get_selfserve_task(task_id)
    except Exception as exc:
        logger.exception("[selfserve] task=%s 读取失败: %s", task_id, exc)
        return
    if not task:
        logger.error("[selfserve] task=%s 不存在,忽略", task_id)
        return

    status = task.get("status")
    if pre_claimed:
        # consume 已原子领取并置 running;防御:状态漂移则跳过。
        if status != "running":
            logger.info("[selfserve] task=%s pre_claimed 但 status=%s 非 running,跳过", task_id, status)
            return
    else:
        # 直调:只有 queued 可占;原子占用败者跳过(幂等 + 防并发二次派发)。
        if status != "queued":
            logger.info("[selfserve] task=%s status=%s 非 queued,跳过(幂等)", task_id, status)
            return
        if not claim_selfserve_task_by_id(task_id):
            logger.info("[selfserve] task=%s 已被并发占用,跳过(不二次跑)", task_id)
            return

    freeze_id = task.get("freeze_id")
    freeze_table = task.get("freeze_table")
    billing_exempt = bool(task.get("billing_exempt"))
    user_id = task.get("user_id")
    industry_id = task.get("industry_id")

    # [FIX-3 · P1] 孤儿守卫:freeze_id 缺失且非豁免 → freeze 未回填 / exempt 标记失败
    #   (API 源头闸本应拦住,纵深防御)。绝不当免费任务跑(4 引擎白花成本 + 免费交付)。
    #   🔴 旧实现「退回 queued」是致命 BUG:pending 态机下 queued 之后【无任何 freeze 回填路径】
    #   (回填只在 API create→promote 之前发生一次,worker/consume 侧无回填代码)→ 退回 queued 后
    #   该孤儿 created_at 恒定最旧永在队头 → 每次 consume 都 claim 到它、再弹回 → 永久 FIFO 饥饿,
    #   堵死后面所有付费单;且 reaper 只捞 status='pending',够不着这条 queued 孤儿 = 无自愈。
    #   改为一次性终结态 cancelled(freeze_id=None 无钱可退,无需 release),孤儿立即离队。
    if freeze_id is None and not billing_exempt:
        logger.warning(
            "[selfserve] task=%s freeze_id 缺失且非豁免·标 cancelled 终结(不免费跑·不弹回队头)", task_id,
        )
        try:
            set_selfserve_task_status(
                task_id, "cancelled", failed_reason="billing_orphan", mark_finished=True,
                notification_terminal="cancelled",
            )
        except Exception:
            pass
        return

    try:
        prompts_list = _normalize_snapshot_prompts(task.get("prompt_snapshot"))
        if not prompts_list:
            await _release_and_fail(
                task_id, freeze_id, freeze_table, user_id,
                status="failed", reason="empty_prompt_snapshot",
            )
            return

        industry_name, industry_slug, industry_id = _load_task_industry(task)
        industries = [{"id": industry_id, "name": industry_name, "slug": industry_slug}]
        prompts_by_industry = {str(industry_id): prompts_list}

        # 建 round(enforce_single_active:与 cron/manual 串行)
        try:
            round_id = create_round_with_snapshot(
                "selfserve",
                industries,
                prompts_by_industry,
                triggered_user_id=user_id,
                enforce_single_active=True,
            )
        except RoundAlreadyRunningError:
            # [R#5] 关掉「completed-still-bridging 窗口」二次派发:
            #   仅【从未派发过 round】(round_id 为空)的 task 才退回 queued 改天消费(不 release,钱还冻着);
            #   已有 round_id 的 task 说明它自己那轮已在跑/结算,绝不退回 queued(否则会被二次 claim → 二次付费轮)。
            if task.get("round_id"):
                logger.info(
                    "[selfserve] task=%s 已有 round_id=%s·保持 running 不退回(避二次 claim)",
                    task_id, task.get("round_id"),
                )
            else:
                logger.info("[selfserve] task=%s 有活跃 round,退回 queued 等下轮", task_id)
                set_selfserve_task_status(task_id, "queued")
            return

        update_selfserve_task_round(task_id, round_id)

        snapshot = {
            "industries": industries,
            "prompts_by_industry": {str(industry_id): prompts_list},
        }

        # 付费自助轮:强制桥接 + 答案实体抽取 + 旁路月预算熔断
        status = await run_round(
            round_id,
            snapshot,
            force_bridge=True,
            force_answer_entity=True,
            skip_month_budget=True,
        )

        if status == "completed":
            await _settle_completed(
                task_id, industry_name, freeze_id, freeze_table, user_id, billing_exempt,
            )
        elif status == "cancelled":
            await _release_and_fail(
                task_id, freeze_id, freeze_table, user_id,
                status="cancelled", reason="round_cancelled",
            )
        else:
            # failed / failed_resumable / partial_success / timeout / 其它 → fail-closed 退钱
            await _release_and_fail(
                task_id, freeze_id, freeze_table, user_id,
                status="failed", reason=f"round_{status}",
            )

    except (KeyboardInterrupt, asyncio.CancelledError):
        # 尽力退钱(钱不能卡冻结)再上抛
        await _release_and_fail(
            task_id, freeze_id, freeze_table, user_id,
            status="failed", reason="worker_cancelled",
        )
        raise
    except Exception as exc:
        logger.exception("[selfserve] task=%s 未预期异常: %s", task_id, exc)
        await _release_and_fail(
            task_id, freeze_id, freeze_table, user_id,
            status="failed", reason=f"worker_exception:{str(exc)[:120]}",
        )


def run_selfserve_task_sync(task_id: int) -> None:
    """new_event_loop 包装(照 _run_round_sync),供 BackgroundTasks / 直调用。"""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run_selfserve_task(task_id))
    except Exception as exc:
        logger.error("[selfserve] run_selfserve_task_sync task=%s 异常: %s", task_id, exc, exc_info=True)
    finally:
        try:
            loop.close()
        except Exception:
            pass


# ============================================================
# 队列消费(单条派发 · 端点 BackgroundTask + scheduler 兜底都调 *_sync)
# ============================================================


async def consume_selfserve_queue() -> Optional[int]:
    """无活跃 round 时【原子领取】最旧一条 queued 派发(单条)。返回派发的 task_id 或 None。

    [R#5] 用 claim_next_queued_selfserve_task(FOR UPDATE SKIP LOCKED)原子置 running 后再跑,
    根除非原子 read-check-write 致并发消费者抢同一 task → 二次完整付费轮。
    """
    if _has_active_round():
        return None
    try:
        claimed = claim_next_queued_selfserve_task()
    except Exception as exc:
        logger.exception("[selfserve] 原子领取队列失败: %s", exc)
        return None
    if not claimed:
        return None
    task_id = int(claimed["id"])
    # 已由 claim_next 原子置 running → pre_claimed=True,run_selfserve_task 不再二次占用。
    await run_selfserve_task(task_id, pre_claimed=True)
    return task_id


def consume_selfserve_queue_sync() -> None:
    """new_event_loop 包装(BackgroundTasks / APScheduler worker 线程无 loop)。"""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(consume_selfserve_queue())
    except Exception as exc:
        logger.error("[selfserve] consume_selfserve_queue_sync 异常: %s", exc, exc_info=True)
    finally:
        try:
            loop.close()
        except Exception:
            pass


# ============================================================
# [#4] stale 'pending' 孤儿回收(P0-1 副作用兜底)
# ============================================================

_PENDING_REAP_MINUTES_DEFAULT = 15


def _pending_reap_minutes() -> int:
    """stale pending 判定阈值(分钟)· env RESEARCH_SELFSERVE_PENDING_REAP_MINUTES 可调 · 默认 15。

    pending 只由本请求同步线程 promote(线程死了永不 promote),故 15min(远超请求时长)仍 pending = 真孤儿。
    """
    try:
        return max(1, int(os.getenv("RESEARCH_SELFSERVE_PENDING_REAP_MINUTES",
                                    str(_PENDING_REAP_MINUTES_DEFAULT))))
    except Exception:
        return _PENDING_REAP_MINUTES_DEFAULT


async def reap_stale_pending_selfserve() -> int:
    """[#4] 回收滞留 'pending' 的自助调研孤儿任务(freeze 已回填但进程被杀在 promote 前 → 钱冻死无自愈)。

    P0-1 扩大了 freeze→promote 窗口;进程在窗口内被杀(蓝绿 SIGTERM/OOM)→ point_freezes 已 commit
    (钱冻)但 task 永卡 'pending':worker 只领 queued、freeze_sweeper 排除 pending、restart_recovery 只管
    round → 钱无自愈。本 reaper 兜底:对每条超阈值 stale pending → release_freeze(退回冻结的钱,带
    user_id + freeze_table · V3.5 路由铁律)+ 置 status='cancelled'(failed_reason='pending_orphan_reaped',
    mark_finished)。cancelled 后自然离开 freeze_sweeper 的 pending 排除集,不重复退款(release 幂等)。

    fail-soft:单条失败不影响其余;release 失败仍继续标 cancelled(freeze_sweeper 12h+ 再兜底)。返回回收条数。
    """
    minutes = _pending_reap_minutes()
    try:
        stale = list_stale_pending_selfserve_tasks(minutes)
    except Exception as exc:
        logger.exception("[selfserve] reap 扫描 stale pending 失败: %s", exc)
        return 0
    reaped = 0
    for task in stale or []:
        try:
            task_id = int(task.get("id"))
        except Exception:
            continue
        try:
            # [FIX-8] 先原子条件 cancel(WHERE status='pending' RETURNING),赢家才退款。
            #   🔴 消除 TOCTOU 资金竞态:reaper 拿扫描快照后、退款前,若 API promote 已把 task 推成
            #   queued(用户被告知成功、worker 可能已 claim 开跑),旧实现「先无条件退款 + 无条件 cancel」
            #   会退款并覆盖 running = 钱退了任务照跑(4 引擎白花 + 免费交付),或静默 cancel 已付费合法
            #   单 = 丢单。条件 UPDATE 只在 task 仍 pending 时赢(rowcount=1 → RETURNING 行);漂移则
            #   None → 一律不 release 不 cancel,交 worker 正常结算。
            cancelled_row = cancel_stale_pending_selfserve_task(task_id)
        except Exception as exc:
            logger.exception("[selfserve] reap task=%s 原子 cancel 失败(跳过·不退款): %s", task_id, exc)
            continue
        if not cancelled_row:
            logger.info("[selfserve] reap task=%s 已漂移出 pending(promote/并发)·跳过不退款", task_id)
            continue
        # 赢得 pending→cancelled(freeze 字段以 RETURNING 为准,非扫描旧快照)→ 退款。
        freeze_id = cancelled_row.get("freeze_id")
        freeze_table = cancelled_row.get("freeze_table")
        user_id = cancelled_row.get("user_id")
        if freeze_id:
            try:
                await release_freeze(
                    freeze_id=freeze_id,
                    task_ref=f"selfres_{task_id}",
                    user_id=user_id,
                    freeze_table=freeze_table,
                    reason="自助调研 pending 孤儿回收·自动退还(promote 前进程被杀)",
                )
            except Exception as exc:
                # 退款失败不回滚 cancel:task 已 cancelled+pending_orphan_reaped 不在 freeze_sweeper
                # pending 排除集 → freeze_sweeper 12h+ 兜底扫 frozen 自动 release(幂等)。
                logger.exception(
                    "[selfserve] reap task=%s release 失败(仍 cancelled·待 sweeper 12h 兜底): %s",
                    task_id, exc,
                )
        # [出口审核 F1 覆审] reaper 【刻意不】在此 deactivate 空行业:merge/alias 任务的 industry_id 指向
        #   平台【既有】行业(非本任务新建),而「本任务新建」(was_created)是 API 内存态、未持久化到队列行,
        #   reaper 无法区分「新建」vs「仅引用」→ 会误软删他人 / admin 预置的合法空行业(如无题的 CSV 录入
        #   行业)致其从 resolver 选项池消失且无自愈(MED)。为一个 LOW(空行业遗留·不进 cron·非资金非功能)
        #   引入 schema 列 + 误删风险不划算 → 保留 F2 原状,不在 reaper 清空行业。
        reaped += 1
        logger.warning(
            "[selfserve] reap pending 孤儿 task=%s user=%s freeze_id=%s·已 cancel+退款",
            task_id, user_id, freeze_id,
        )
    if reaped:
        logger.warning("[selfserve] reap 完成·回收 %s 条 stale pending 孤儿", reaped)
    return reaped


def reap_stale_pending_selfserve_sync() -> None:
    """new_event_loop 包装(APScheduler worker 线程无 loop),供 scheduler 周期 job 调用。"""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(reap_stale_pending_selfserve())
    except Exception as exc:
        logger.error("[selfserve] reap_stale_pending_selfserve_sync 异常: %s", exc, exc_info=True)
    finally:
        try:
            loop.close()
        except Exception:
            pass


# ============================================================
# [GEO-R8-CAN-009] 结算待处理(needs_manual)自动对账 —— 幂等 commit 重试自愈
# ============================================================

# [GEO-R8-CAN-009] _settle_completed 在 commit_freeze 异常 / 返回 None / success!=True(ambiguous/
#   not-found)时刻意落 completed + 此 marker、且【不 release】冻结(fail-closed 保住钱),等人工补扣或
#   重试幂等 commit。原设计缺「自动重试」→ 钱恒 frozen 直到人工介入(P3:资金已守住不丢,仅需自愈+可见)。
#   本对账器周期性对这些 needs_manual 单幂等重试 commit_freeze:多为瞬时 billing 抖动 / 探测锁竞争,
#   重试即可扣费成功自愈,无需人工。commit_freeze 天然幂等(已 committed→no-op success),重试安全。
_SETTLEMENT_NEEDS_MANUAL_MARKER = "billing_commit_failed_needs_manual"
_SETTLEMENT_NEEDS_RECHARGE_MARKER = "billing_swept_needs_recharge"
_SETTLEMENT_RECONCILED_MARKER = "billing_commit_reconciled"


def _list_needs_manual_settlement_tasks(limit: int = 200) -> list:
    """[GEO-R8-CAN-009] 列出仍带 needs_manual 结算 marker 且冻结未退的 completed 任务(对账器专用)。

    只捞 freeze_id 非空(有钱可扣)· failed_reason=needs_manual(未升级 needs_recharge)· status=completed。
    内联 SQL(照本模块 _has_active_round / _load_task_industry 直查风格 · 不新增 db helper · 单文件纪律)。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, freeze_id, freeze_table, user_id FROM geo_research_selfserve_queue "
            "WHERE status = 'completed' AND failed_reason = %s AND freeze_id IS NOT NULL "
            "ORDER BY finished_at ASC NULLS LAST LIMIT %s",
            (_SETTLEMENT_NEEDS_MANUAL_MARKER, int(limit)),
        )
        return [dict(r) for r in (cur.fetchall() or [])]
    except Exception as exc:
        # fail-soft:扫不到就当无待对账单(不阻断其它周期 job)。
        logger.warning("[selfserve] 扫 needs_manual 待对账单失败: %s", str(exc)[:200])
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


async def reconcile_pending_settlement_selfserve() -> int:
    """[GEO-R8-CAN-009] 对 needs_manual 结算单幂等重试 commit_freeze 自愈,返回本轮成功对账条数。

    对每条:重试 commit_freeze(回传 user_id + freeze_table · V3.5 路由铁律)——
      - success=True 且非「已 released 幂等」→ 扣费成功(或幂等已 committed)→ 清 marker 为 reconciled;
      - 命中【已 released】幂等(冻结疑被 freeze_sweeper 扫走)→ 无冻结可扣 → 升级 needs_recharge
        (退出 needs_manual 集 · 交人工补充值 · 绝不静默当扣费成功);
      - 仍 success!=True(ambiguous/not-found)/ 异常 → 保持 needs_manual 原状,不 release,待下轮重试或人工。
    fail-soft:单条失败不影响其余;绝不 release(release=免费送) · 绝不 fall-through 静默清 marker。
    """
    tasks = _list_needs_manual_settlement_tasks()
    reconciled = 0
    for task in tasks or []:
        try:
            task_id = int(task.get("id"))
        except Exception:
            continue
        freeze_id = task.get("freeze_id")
        freeze_table = task.get("freeze_table")
        user_id = task.get("user_id")
        if not freeze_id:
            continue  # 无钱可扣(理论被 WHERE 拦掉,纵深防御)
        try:
            commit_result = await commit_freeze(
                freeze_id=freeze_id,
                task_ref=f"selfres_{task_id}",
                user_id=user_id,
                freeze_table=freeze_table,
                reason="自助调研结算对账·needs_manual 幂等重试补扣",
            )
        except Exception as exc:
            # 重试仍异常 → 保持 needs_manual(不动钱不清 marker),待下轮 / 人工。
            logger.warning(
                "[selfserve] 对账 task=%s commit 重试异常(保持 needs_manual): %s", task_id, str(exc)[:200],
            )
            continue
        if not commit_result:
            continue  # None → 无法确认,保持 needs_manual
        if (commit_result.get("idempotent")
                and commit_result.get("status") == "released"):
            # 冻结已被 sweeper 扫走退款 → 榜已点亮却 0 扣费 = 免费送,commit 补不回 → 升级人工补充值。
            logger.error(
                "[selfserve] 对账 task=%s 冻结已 released(疑 sweeper 扫走)·无法补扣·升级 needs_recharge freeze_id=%s",
                task_id, freeze_id,
            )
            try:
                set_selfserve_task_status(
                    task_id, "completed", failed_reason=_SETTLEMENT_NEEDS_RECHARGE_MARKER,
                )
            except Exception as exc:
                logger.exception("[selfserve] 对账 task=%s 升级 needs_recharge 落库失败: %s", task_id, exc)
            continue
        if commit_result.get("success") is not True:
            # ambiguous / not-found → 保持 needs_manual,不清 marker(下轮再试或人工)。
            logger.warning(
                "[selfserve] 对账 task=%s commit 仍未成功(result=%s)·保持 needs_manual freeze_id=%s",
                task_id, {k: commit_result.get(k) for k in ("success", "reason", "ambiguous", "status")}, freeze_id,
            )
            continue
        # 扣费成功(或幂等已 committed)→ 清 marker,退出 needs_manual 集,自愈完成。
        try:
            set_selfserve_task_status(task_id, "completed", failed_reason=_SETTLEMENT_RECONCILED_MARKER)
        except Exception as exc:
            logger.exception("[selfserve] 对账 task=%s 清 needs_manual marker 失败: %s", task_id, exc)
            continue
        reconciled += 1
        logger.warning(
            "[selfserve] 对账成功 task=%s user=%s freeze_id=%s·needs_manual 幂等补扣完成",
            task_id, user_id, freeze_id,
        )
    if reconciled:
        logger.warning("[selfserve] 结算对账完成·自愈 %s 条 needs_manual", reconciled)
    return reconciled


def reconcile_pending_settlement_selfserve_sync() -> None:
    """[GEO-R8-CAN-009] new_event_loop 包装,供 scheduler 周期 job 调用(照 reap_*_sync)。"""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(reconcile_pending_settlement_selfserve())
    except Exception as exc:
        logger.error("[selfserve] reconcile_pending_settlement_selfserve_sync 异常: %s", exc, exc_info=True)
    finally:
        try:
            loop.close()
        except Exception:
            pass
