"""
A.7 Group 3 - 调研监测跑批管理 admin API

提供 6 个接口管理 geo_research_round (跑批轮次):
1. GET  /rounds                       - 列表(分页 + 状态过滤)
2. GET  /rounds/{round_id}            - 详情(含 round_call / article / cost 统计)
3. POST /rounds/manual-trigger        - 手动触发一轮(后台异步跑)
4. POST /rounds/{round_id}/cancel     - 取消跑批(只标记,不主动 kill)
5. POST /rounds/{round_id}/resume     - 续跑 failed_resumable 状态(从头跑)
6. GET  /rounds/{round_id}/cost       - 看本轮成本明细

复用 services/research_monitor 的 round_state / round_runner / budget_guard,
不修改 service 层。所有 SQL 走 %s 参数化。
"""
import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, Field

from db.connection import get_connection
from services.research_monitor.budget_guard import (
    check_month_budget,
    check_round_budget,
    get_round_cost,
)
from services.research_monitor.round_runner import run_round_with_auto_resume
from services.research_monitor.round_state import (
    RoundAlreadyRunningError,
    create_round_with_snapshot,
    get_round_snapshot,
    get_round_status,
    mark_round_resume_requested,
    update_round_complete,
)
from auth.user_ctx import current_user_id

logger = logging.getLogger("GEO-ResearchMonitor.RoundAPI")

router = APIRouter(prefix="/api/admin/research-monitor", tags=["调研监测-跑批"])


# ==================== 鉴权 ====================

def _require_admin(request: Request) -> dict:
    """从 request.state.user 拿 admin 用户; 未登录 401, 非 admin 403"""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# ==================== 常量 ====================

ALLOWED_STATUSES = {
    "pending",
    "running",
    "completed",
    "partial_success",
    "failed",
    "cancelled",
    "failed_resumable",
}
ALLOWED_TRIGGERED_BY = {"cron", "manual", "missed_cron_recovery", "selfserve"}
CANCELLABLE_STATUSES = {"running", "pending"}
# P14-v9 (2026-05-27 老板反馈): cancelled round 也允许续跑
#   语义: 管理员 cancel 通常是因为环境问题(网络挂/AI key 失效)· 数据没污染 · 该可续
#   配合 _infer_resume_from_stage 自动从断点继续 · 不浪费已成功的 stage
RESUMABLE_STATUSES = {"failed_resumable", "cancelled"}


# ==================== 工具函数 ====================

def _row_to_dict(row: Any) -> Dict:
    """RealDictRow 转普通 dict, datetime 转 ISO 字符串"""
    if row is None:
        return {}
    out = dict(row)
    for k, v in list(out.items()):
        if isinstance(v, datetime):
            out[k] = v.isoformat()
    return out


def _parse_jsonb(value: Any) -> Optional[Dict]:
    """psycopg2 RealDictCursor 取 JSONB 通常已是 dict, 但若是 str 兜底解析"""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return None


_STAGE_ORDER = [
    'stage_1', 'stage_2', 'stage_3', 'stage_4', 'stage_4_5',
    'stage_5', 'stage_6', 'stage_7', 'stage_8',
]


def _infer_resume_from_stage(current_stage: Optional[str]) -> str:
    """P14-v9 · 根据 round 上次停止的 current_stage 推算续跑起点

    [2026-07-16 超时自动续跑] 实现逐字下沉到 round_state.infer_resume_from_stage
    (services 层的自动续跑外壳也要用, 不能反向 import api)。本别名保留原签名,
    既有调用点(:1105)与测试零改动。规则/示例见 round_state 版 docstring。
    """
    from services.research_monitor.round_state import infer_resume_from_stage
    return infer_resume_from_stage(current_stage)


def _run_round_sync(
    round_id: str,
    snapshot: Dict,
    resume_from_stage: Optional[str] = None,
    *,
    force_bridge: bool = False,
    force_answer_entity: bool = False,
    skip_month_budget: bool = False,
):
    """
    BackgroundTasks 运行 async run_round 的 sync 包装。

    force_bridge / force_answer_entity / skip_month_budget: 付费自助轮(U4 派发)传 True,
    强制桥接 + 答案实体抽取 + 旁路月预算熔断。manual-trigger 现有 add_task 用位置参数
    (round_id, snapshot, None), 三个 keyword-only 参数取默认 False → manual 行为不变。

    用 new_event_loop + 显式 close 而非 asyncio.run() · C-4 backlog 修复:
    - asyncio.run() 在每次调用都创建+关闭新 loop · 跑完 close
      但若 run_round 内部 aiohttp/httpx 全局 session 是 thread-bound, asyncio.run 关 loop 会
      让 thread 内 session 失效 · 后续 BackgroundTask 共用同 thread 时崩
    - new_event_loop + 显式 close 在 finally 块, 异常路径也保证 close 干净

    任何异常只 log 不抛 (run_round 内部已归档 round 状态)。
    """
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        # [2026-07-16 改动二] 换 run_round_with_auto_resume: 4h 硬超时自动续跑的
        # 重入全部发生在本 run_until_complete 存活期内(throwaway loop 拆除之前),
        # 满足 run_round docstring 的 await 约束 → API 路径(手动触发/续跑)同样适用。
        loop.run_until_complete(
            run_round_with_auto_resume(
                round_id,
                snapshot,
                resume_from_stage=resume_from_stage,
                force_bridge=force_bridge,
                force_answer_entity=force_answer_entity,
                skip_month_budget=skip_month_budget,
            )
        )
    except Exception as e:
        logger.error(
            f"[round_runner] round {round_id} 后台跑批异常: {e}",
            exc_info=True,
        )
    finally:
        try:
            loop.close()
        except Exception:
            pass
        # 清掉当前 thread 的 event loop 绑定 · 防内存泄漏
        try:
            asyncio.set_event_loop(None)
        except Exception:
            pass


# ==================== 1. GET /rounds 列表 ====================

@router.get("/rounds")
async def list_rounds(
    request: Request,
    status: Optional[str] = None,
    triggered_by: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> Dict:
    """
    分页 + 状态过滤拉 round 列表。
    不返大字段 snapshot_json (>100KB 拖响应)。
    排序: started_at DESC NULLS LAST, round_id DESC
    """
    _require_admin(request)

    # 参数校验
    if status is not None and status not in ALLOWED_STATUSES:
        raise HTTPException(status_code=400, detail=f"status 必须是 {sorted(ALLOWED_STATUSES)} 之一")
    if triggered_by is not None and triggered_by not in ALLOWED_TRIGGERED_BY:
        raise HTTPException(status_code=400, detail=f"triggered_by 必须是 {sorted(ALLOWED_TRIGGERED_BY)} 之一")
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="limit 必须在 1-100")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset 不能为负")

    where_parts: List[str] = []
    params: List[Any] = []
    if status:
        where_parts.append("status = %s")
        params.append(status)
    if triggered_by:
        where_parts.append("triggered_by = %s")
        params.append(triggered_by)
    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    conn = get_connection()
    try:
        cur = conn.cursor()
        # total
        cur.execute(f"SELECT COUNT(*) AS cnt FROM geo_research_round {where_sql}", params)
        total_row = cur.fetchone() or {}
        total = int(total_row.get("cnt") or 0)

        # 列表(不取整个 snapshot_json · >100KB · 但取 industries 子字段让前端能显示行业名)
        # P14-v8 (2026-05-27 老板反馈): round_id 纯时间戳看不出行业 · 必须带 industries[]
        # P14-v8b (2026-05-27 老板反馈): 排序 bug · pending round started_at=NULL 被 NULLS LAST
        #   排到底 · 新启动的 round 看不见. 用 COALESCE(started_at, created_at) 让 pending 也按创建时间排
        list_sql = f"""
            SELECT round_id, status, triggered_by, triggered_user_id, current_stage,
                   started_at, finished_at, last_heartbeat_at,
                   progress_json, summary_json,
                   snapshot_json->'industries' AS industries
              FROM geo_research_round
              {where_sql}
             ORDER BY COALESCE(started_at, created_at) DESC, round_id DESC
             LIMIT %s OFFSET %s
        """
        cur.execute(list_sql, params + [limit, offset])
        rows = cur.fetchall() or []
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            f"[列表 rounds] 失败 status={status} triggered_by={triggered_by}: "
            f"{type(e).__name__}: {e}"
        )
        raise HTTPException(status_code=500, detail="查询 round 列表失败")
    finally:
        # finally 兜底 rollback: 异常路径若直接抛 HTTPException 没走 except,
        # 连接归还前必须 rollback 防 aborted 事务污染连接池
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()

    rounds = []
    for row in rows:
        d = _row_to_dict(row)
        d["progress_json"] = _parse_jsonb(d.get("progress_json"))
        d["summary_json"] = _parse_jsonb(d.get("summary_json"))
        # P14-v8: 解 industries 子字段 · 只透 id + name (slug 不暴露给前端)
        inds = _parse_jsonb(d.get("industries")) or []
        d["industries"] = [{"id": i.get("id"), "name": i.get("name")} for i in inds if isinstance(i, dict)]
        rounds.append(d)

    return {
        "rounds": rounds,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


# ==================== 1b. GET /rounds/cron-status 自动跑批 cron 状态 ====================
# P14-v10 (2026-05-28): 老板要求 UI 能看 + 改自动跑批时间
# 必须在 `/rounds/{round_id}` 之前注册 · 否则 FastAPI 把 cron-status 当 round_id 吃了

@router.get("/rounds/cron-status", summary="查自动跑批 cron 当前配置 + 下次触发时间")
async def get_cron_status(request: Request) -> Dict:
    """返回:
      {
        enabled: bool,
        days: '1,16',          # 每月哪几天 (逗号分隔)
        hour: 2,               # 几点 (北京时间)
        next_run_at: ISO,      # 下次触发时间 (含时区) · null 表示未注册
        timezone: 'Asia/Shanghai',
      }
    """
    _require_admin(request)
    try:
        from services.research_monitor.scheduler_setup import (
            _load_cron_config_from_db,
            get_next_cron_run_time,
        )
        cfg = _load_cron_config_from_db()
        next_at = get_next_cron_run_time()
        return {
            "enabled": cfg['enabled'],
            "days": cfg['days'],
            "hour": cfg['hour'],
            "next_run_at": next_at,
            "timezone": "Asia/Shanghai",
        }
    except Exception as e:
        logger.exception(f"[cron-status] 失败: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="查询 cron 状态失败")


# ==================== 1c. GET /rounds/{round_id}/live-status 实时监控聚合 ====================
# P15.1 (2026-05-28): 运行监控页用 · 一次拿齐 round + per-platform + stage 进度 + 失败摘要
# 必须在 `/rounds/{round_id}` GET 之前 (路径冲突防御 · FastAPI 顺序匹配)

_PLATFORM_LABELS = {'doubao': '豆包', 'deepseek': 'DeepSeek', 'qwen': 'Qwen', 'kimi': 'Kimi'}
_STAGE_LABELS_RUNTIME = {
    'stage_1': '1. AI 问答',
    'stage_2': '2. URL 过滤',
    'stage_3': '3. 抓取原文',
    'stage_4': '4. 规则清洗',
    'stage_4_5': '4.5 文章意图分类',
    'stage_5': '5. 入文章库',
    'stage_7': '7. 聚合统计',
    'stage_8': '8. 完成通知',
}


def _stage_index_from_current(current_stage: Optional[str]) -> int:
    """current_stage 字段提取阶段编号 · stage_3_crawl_done → 3 · None → 0"""
    if not current_stage:
        return 0
    if current_stage.startswith('stage_4_5'):
        return 4
    import re as _re
    m = _re.match(r'stage_(\d+)', current_stage)
    return int(m.group(1)) if m else 0


def _stage_order_position(current_stage: Optional[str]) -> int:
    """按真实阶段顺序返回 1-based 位置 · stage_4_5 算独立阶段。"""
    if not current_stage:
        return 0
    if current_stage.startswith('stage_4_5'):
        key = 'stage_4_5'
    else:
        import re as _re
        m = _re.match(r'(stage_\d+)', current_stage)
        key = m.group(1) if m else current_stage
    try:
        return _STAGE_ORDER.index(key) + 1
    except ValueError:
        return 0


def _is_done_stage(current_stage: Optional[str]) -> bool:
    return bool(current_stage and current_stage.endswith('_done'))


@router.get("/rounds/{round_id}/live-status", summary="运行监控聚合 · per-platform + stage + 失败摘要")
async def get_round_live_status(round_id: str, request: Request) -> Dict:
    """返回完整运行时状态 · 给运行监控页面用"""
    _require_admin(request)
    base = get_round_status(round_id)
    if not base:
        raise HTTPException(status_code=404, detail=f"round {round_id} 不存在")

    conn = get_connection()
    try:
        cur = conn.cursor()
        # 1. snapshot 取行业名 + 取 summary_json.stage_3 (P14.1 C3+ 持久 stage 3 reasons + error_samples)
        cur.execute(
            """
            SELECT snapshot_json->'industries' AS industries,
                   summary_json AS summary
              FROM geo_research_round WHERE round_id=%s
            """,
            (round_id,),
        )
        snap_row = cur.fetchone() or {}
        inds = snap_row.get('industries') if isinstance(snap_row, dict) else snap_row[0]
        inds = _parse_jsonb(inds) or []
        industry_names = [i.get('name') for i in inds if isinstance(i, dict) and i.get('name')]
        # P14.1 C3+: 取 summary_json.stage_3 备份 (round 完成后 progress_json 被覆盖 · 历史 fallback 用)
        summary_full = _parse_jsonb(
            snap_row.get('summary') if isinstance(snap_row, dict) else snap_row[1]
        ) or {}
        stage_3_from_summary = summary_full.get('stage_3') if isinstance(summary_full, dict) else None
        stage_4_5_from_summary = summary_full.get('stage_4_5') if isinstance(summary_full, dict) else None

        # 2. per-platform 聚合 (status / count / citations / latest error)
        cur.execute(
            """
            SELECT platform,
                   COUNT(*) AS total,
                   SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success,
                   SUM(CASE WHEN status='failed'  THEN 1 ELSE 0 END) AS failed,
                   SUM(CASE WHEN status='pending' OR status='running' THEN 1 ELSE 0 END) AS pending,
                   SUM(COALESCE(citations_count, 0)) AS citations,
                   MAX(CASE WHEN status='failed' THEN error_message END) AS latest_error
              FROM geo_research_round_call
             WHERE round_id = %s
             GROUP BY platform
             ORDER BY platform
            """,
            (round_id,),
        )
        plat_rows = cur.fetchall() or []

        # 3. articles 多维聚合 (P14-v15 review HIGH#4 fix: stage 3-5 不再依赖 progress_json · 改 ground truth)
        #    stage_3 完成数 = articles 总数 (stage_3 写入 articles)
        #    stage_4 完成数 = clean_status='cleaned'
        #    stage_5 入库数 = review_status IN ('in_library', 'imported_to_reference')
        cur.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN clean_status='cleaned' THEN 1 ELSE 0 END) AS cleaned,
                   SUM(CASE WHEN intent_type IS NOT NULL THEN 1 ELSE 0 END) AS intent_classified,
                   SUM(CASE WHEN review_status IN ('in_library','imported_to_reference') THEN 1 ELSE 0 END) AS in_library
              FROM geo_research_articles
             WHERE first_seen_round_id = %s
            """,
            (round_id,),
        )
        art_agg = cur.fetchone() or {}
        articles_crawled = int(art_agg.get('total') or 0)
        articles_cleaned = int(art_agg.get('cleaned') or 0)
        articles_intent_classified = int(art_agg.get('intent_classified') or 0)
        articles_done = int(art_agg.get('in_library') or 0)

        # 4. 失败错误归类聚合 (同 error_message 计数 · 取前 5)
        cur.execute(
            """
            SELECT platform,
                   COALESCE(error_message, '(无 error_message)') AS err,
                   COUNT(*) AS c
              FROM geo_research_round_call
             WHERE round_id=%s AND status='failed'
             GROUP BY platform, err
             ORDER BY c DESC
             LIMIT 20
            """,
            (round_id,),
        )
        err_rows = cur.fetchall() or []
    finally:
        try: conn.rollback()
        except Exception: pass
        conn.close()

    # 装配 platforms
    platforms: List[Dict] = []
    plat_total_calls = 0
    for r in plat_rows:
        d = _row_to_dict(r)
        plat = d.get('platform')
        success = int(d.get('success') or 0)
        failed = int(d.get('failed') or 0)
        pending = int(d.get('pending') or 0)
        total = int(d.get('total') or 0)
        plat_total_calls += total
        if success == total and total > 0:
            pstatus = 'success'
        elif failed == total and total > 0:
            pstatus = 'failed'
        elif success > 0 and failed > 0:
            pstatus = 'partial'
        elif pending > 0:
            pstatus = 'running'
        else:
            pstatus = 'unknown'
        platforms.append({
            'platform': plat,
            'label': _PLATFORM_LABELS.get(plat, plat),
            'status': pstatus,
            'success': success,
            'failed': failed,
            'pending': pending,
            'total': total,
            'citations': int(d.get('citations') or 0),
            'latest_error': (d.get('latest_error') or '')[:200] if d.get('latest_error') else None,
            'can_retry': failed > 0,
        })

    # stage 进度: 从 current_stage 推算 + 各 stage 数据来源
    cur_stage = base.get('current_stage')
    cur_idx = _stage_index_from_current(cur_stage)
    cur_done = _is_done_stage(cur_stage)
    progress = _parse_jsonb(base.get('progress_json')) or {}
    current_is_intent_stage = bool(cur_stage and cur_stage.startswith('stage_4_5'))

    def _stage_status(stage_num: int) -> str:
        if stage_num < cur_idx:
            return 'done'
        if stage_num == cur_idx:
            return 'done' if cur_done else 'running'
        return 'pending'

    # P14-v15 (review HIGH#4 fix): stage 进度从 round_call + articles 表聚合 (持久 ground truth)
    # 之前依赖 progress_json · 每次 stage 切换被覆盖 · 跑到 stage_8 时 stage_2/3 数据丢失
    # 现在: stage_1 = round_call · stage_2 = progress 实时(纯 SQL stage · 数据不持久) · stage_3-5 = articles
    crawl_total = int(progress.get('total_urls', progress.get('total', 0)) or 0)
    crawl_done_runtime = int(progress.get('crawled', 0) or 0) + int(progress.get('skipped', 0) or 0)
    prefilter_kept = int(progress.get('kept_url_count', 0) or 0)
    prefilter_total = int(progress.get('raw_url_count', 0) or 0)

    # stage_1 ground truth: round_call 统计 (success + failed = 已处理 · pending+running = 在跑)
    stage1_done = sum(p['success'] + p['failed'] for p in platforms)

    # stage_3-5 用 articles 表聚合 (持久 · 不依赖 progress_json)
    # 但 articles 总数随 stage_3 而增 · 所以 articles_crawled 是 stage_3 done · cleaned 是 stage_4 done · in_library 是 stage_5 done
    # total 都用 articles_crawled (即 stage_3 写入数 · 后续 stage 处理的就是这批)
    stage3_total = max(articles_crawled, prefilter_kept) if prefilter_kept else articles_crawled

    stage_progress = [
        {'stage': 'stage_1', 'label': '1. AI 问答',
         'status': _stage_status(1), 'done': stage1_done, 'total': plat_total_calls},
        {'stage': 'stage_2', 'label': '2. URL 过滤',
         'status': _stage_status(2),
         'done': prefilter_kept if cur_idx >= 2 else 0,
         'total': prefilter_total if cur_idx >= 2 else 0},
        {'stage': 'stage_3', 'label': '3. 抓取原文',
         'status': _stage_status(3),
         # 优先用 progress (跑 stage_3 时实时) · stage_3 done 后用 articles count
         'done': crawl_done_runtime if cur_idx == 3 and not cur_done else articles_crawled,
         'total': crawl_total or stage3_total},
        {'stage': 'stage_4', 'label': '4. 规则清洗',
         'status': 'done' if current_is_intent_stage else _stage_status(4),
         'done': articles_cleaned,
         'total': articles_crawled},
        {'stage': 'stage_4_5', 'label': '4.5 文章意图分类',
         'status': ('done' if cur_idx >= 5 else ('done' if current_is_intent_stage and cur_done else ('running' if current_is_intent_stage else 'pending'))),
         'done': int(progress.get('classified', articles_intent_classified) or 0) if current_is_intent_stage else articles_intent_classified,
         'total': int(progress.get('total', articles_cleaned) or 0) if current_is_intent_stage else articles_cleaned},
        {'stage': 'stage_5', 'label': '5. 入文章库',
         'status': _stage_status(5),
         'done': articles_done,
         'total': articles_cleaned or articles_crawled},
        {'stage': 'stage_7', 'label': '7. 聚合统计',
         'status': _stage_status(7), 'done': 0, 'total': 0},
        {'stage': 'stage_8', 'label': '8. 完成通知',
         'status': _stage_status(8), 'done': 0, 'total': 0},
    ]

    # P14.1 C3+: stage_3_summary · running 时优先 progress 实时 · 完成态用 summary 持久备份
    # 老板复核 round_20260528_232257 暴露 · stage 8 update_round_progress 覆盖 progress_json ·
    # 完成后历史视图丢 reasons · 用 summary_json.stage_3 兜底
    _round_status_lower = (base.get('status') or '').lower()
    _is_terminal = _round_status_lower in {
        'completed', 'failed', 'cancelled', 'partial_success', 'failed_resumable',
    }
    stage_3_summary: Optional[Dict] = None
    _progress_has_stage3 = isinstance(progress.get('reasons'), dict)
    if _is_terminal and stage_3_from_summary:
        # 完成态: 优先 summary_json.stage_3 (持久 · 不被 stage 4-8 覆盖)
        stage_3_summary = stage_3_from_summary
    elif _progress_has_stage3:
        # 运行时: progress_json 实时(stage 3 期间每 5/10s flush)
        stage_3_summary = {
            'total_urls': progress.get('total'),
            'processed': progress.get('processed'),
            'jina_requests': progress.get('jina_requests'),
            'error_samples': progress.get('error_samples') or [],
            **(progress.get('reasons') or {}),
        }
    elif stage_3_from_summary:
        # 兜底: progress_json 没 stage_3 数据但 summary 有
        stage_3_summary = stage_3_from_summary

    # P15 · stage_4_5 文章意图分类摘要 · running 时用 progress_json, 完成后用 summary_json.stage_4_5 兜底
    intent_summary: Optional[Dict] = None
    _progress_has_intent = current_is_intent_stage and ('classified' in progress or 'failed' in progress or 'total' in progress)
    if _is_terminal and stage_4_5_from_summary:
        intent_summary = stage_4_5_from_summary
    elif _progress_has_intent:
        intent_summary = {
            'classified': int(progress.get('classified') or 0),
            'failed': int(progress.get('failed') or 0),
            'total': int(progress.get('total') or 0),
            'model': progress.get('model'),
            'error_samples': progress.get('error_samples') or [],
        }
    elif stage_4_5_from_summary:
        intent_summary = stage_4_5_from_summary
    elif articles_intent_classified > 0 or cur_idx >= 5:
        intent_summary = {
            'classified': articles_intent_classified,
            'failed': max(0, articles_cleaned - articles_intent_classified) if cur_idx >= 5 else 0,
            'total': articles_cleaned,
            'model': None,
        }

    # 整体 percent 粗算:按真实阶段顺序算,stage_4_5 是独立阶段
    overall_pct = 0
    if base.get('status') == 'completed':
        overall_pct = 100
    else:
        pos = _stage_order_position(cur_stage)
        if pos > 0:
            overall_pct = min(100, int(((pos - (0 if cur_done else 0.5)) / len(_STAGE_ORDER)) * 100))

    # 心跳健康
    from datetime import datetime as _dt, timezone as _tz
    hb_age = None
    try:
        hb = base.get('last_heartbeat_at')
        if hb:
            now = _dt.now(_tz.utc)
            hb_dt = hb if hasattr(hb, 'tzinfo') else None
            if hb_dt is not None:
                if hb_dt.tzinfo is None:
                    hb_dt = hb_dt.replace(tzinfo=_tz.utc)
                hb_age = max(0, int((now - hb_dt).total_seconds()))
    except Exception:
        hb_age = None

    return {
        'round': {
            'round_id': round_id,
            'status': base.get('status'),
            'industry_names': industry_names,
            'current_stage': cur_stage,
            'stage_label': _STAGE_LABELS_RUNTIME.get('stage_4_5' if current_is_intent_stage else f"stage_{cur_idx}", cur_stage or '-'),
            'started_at': base.get('started_at').isoformat() if base.get('started_at') else None,
            'finished_at': base.get('finished_at').isoformat() if base.get('finished_at') else None,
            'last_heartbeat_at': base.get('last_heartbeat_at').isoformat() if base.get('last_heartbeat_at') else None,
            'heartbeat_age_seconds': hb_age,
            'error_message': base.get('error_message'),
        },
        'overall': {
            'percent': overall_pct,
            'label': _STAGE_LABELS_RUNTIME.get('stage_4_5' if current_is_intent_stage else f"stage_{cur_idx}", '准备中'),
        },
        'platforms': platforms,
        'stage_progress': stage_progress,
        'errors': [
            {
                'platform': r['platform'] if isinstance(r, dict) else r[0],
                'label': _PLATFORM_LABELS.get(
                    r['platform'] if isinstance(r, dict) else r[0],
                    r['platform'] if isinstance(r, dict) else r[0]),
                'count': int((r['c'] if isinstance(r, dict) else r[2]) or 0),
                'sample': ((r['err'] if isinstance(r, dict) else r[1]) or '')[:200],
            }
            for r in err_rows
        ],
        'articles_done': articles_done,
        # P14.1 C3+: stage 3 reasons + error_samples (running 实时 / 完成态持久兜底)
        'stage_3_summary': stage_3_summary,
        'intent_summary': intent_summary,
    }


# ==================== 2. GET /rounds/{round_id} 详情 ====================

@router.get("/rounds/{round_id}")
async def get_round_detail(round_id: str, request: Request) -> Dict:
    """
    拉 round 完整详情:
      - round 基础字段(get_round_status)
      - snapshot_json
      - call_count / article_count / cost_total 统计
    """
    _require_admin(request)

    base = get_round_status(round_id)
    if not base:
        raise HTTPException(status_code=404, detail=f"round {round_id} 不存在")

    snapshot = get_round_snapshot(round_id)

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM geo_research_round_call WHERE round_id = %s",
            (round_id,),
        )
        call_count = int((cur.fetchone() or {}).get("cnt") or 0)

        cur.execute(
            "SELECT COUNT(*) AS cnt FROM geo_research_articles WHERE first_seen_round_id = %s",
            (round_id,),
        )
        article_count = int((cur.fetchone() or {}).get("cnt") or 0)

        cur.execute(
            """
            SELECT COALESCE(SUM(amount_yuan), 0) AS total
              FROM geo_research_cost_log
             WHERE round_id = %s
            """,
            (round_id,),
        )
        cost_total = float((cur.fetchone() or {}).get("total") or 0)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            f"[round 详情] 失败 round_id={round_id}: "
            f"{type(e).__name__}: {e}"
        )
        raise HTTPException(status_code=500, detail="查询 round 详情失败")
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()

    detail = _row_to_dict(base)
    # JSONB 字段确保是 dict
    detail["progress_json"] = _parse_jsonb(detail.get("progress_json"))
    detail["summary_json"] = _parse_jsonb(detail.get("summary_json"))
    detail["snapshot_json"] = _parse_jsonb(snapshot)
    detail["call_count"] = call_count
    detail["article_count"] = article_count
    detail["cost_total_yuan"] = cost_total

    return detail


# ==================== 3. POST /rounds/manual-trigger 手动触发 ====================

class ManualTriggerRequest(BaseModel):
    # max_length=100 防止恶意传万条让 SQL ANY(%s) 拖慢
    industry_ids: Optional[List[int]] = Field(
        default=None, max_length=100,
        description="None=全 active 行业, 最多 100 个 id",
    )
    note: Optional[str] = Field(default=None, max_length=500, description="admin 备注,写 round.summary_json.manual_note")


@router.post("/rounds/manual-trigger")
async def manual_trigger_round(
    payload: ManualTriggerRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> Dict:
    """
    管理员手动触发一轮跑批。
    流程: 月预算检查 → 拉行业+prompts → create_round_with_snapshot → BackgroundTask 跑 run_round
    立即返 round_id, 不阻塞 HTTP 响应。
    """
    admin = _require_admin(request)

    # 1. 月度预算检查
    month_check = check_month_budget()
    if not month_check.get("ok"):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "month_budget_exhausted",
                "message": month_check.get("reason") or "本月预算已超支,无法启动新跑批",
                "spent": month_check.get("spent"),
                "limit": month_check.get("limit"),
            },
        )

    # 2. 拉行业列表 + 各行业 prompts
    industry_ids = payload.industry_ids or []
    conn = get_connection()
    try:
        cur = conn.cursor()
        if industry_ids:
            cur.execute(
                """
                SELECT id, name, slug
                  FROM geo_research_industries
                 WHERE active = TRUE AND id = ANY(%s)
                 ORDER BY sort_order, id
                """,
                (industry_ids,),
            )
        else:
            cur.execute(
                """
                SELECT id, name, slug
                  FROM geo_research_industries
                 WHERE active = TRUE
                 ORDER BY sort_order, id
                """
            )
        industries = [_row_to_dict(r) for r in (cur.fetchall() or [])]

        if not industries:
            raise HTTPException(status_code=400, detail="没有可用 active 行业,无法触发跑批")

        ids_for_prompts = [ind["id"] for ind in industries]
        cur.execute(
            """
            SELECT id, industry_id, prompt_text, sort_order, is_sensitive
              FROM geo_research_prompts
             WHERE active = TRUE AND industry_id = ANY(%s)
             ORDER BY industry_id, sort_order, id
            """,
            (ids_for_prompts,),
        )
        prompts_rows = cur.fetchall() or []
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            f"[manual-trigger 拉行业] 失败: {type(e).__name__}: {e}"
        )
        raise HTTPException(status_code=500, detail="拉行业/prompts 失败")
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()

    prompts_by_industry: Dict[int, List[Dict]] = {}
    for row in prompts_rows:
        d = _row_to_dict(row)
        ind_id = d["industry_id"]
        prompts_by_industry.setdefault(ind_id, []).append({
            "id": d["id"],
            "text": d["prompt_text"],
            "sort_order": d.get("sort_order"),
            "is_sensitive": d.get("is_sensitive"),
        })
    prompts_count = sum(len(v) for v in prompts_by_industry.values())
    if prompts_count <= 0:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "no_active_prompts",
                "message": "所选 active 行业下没有 active prompts, 无法启动空跑批",
            },
        )

    # 3. create round + snapshot
    try:
        round_id = create_round_with_snapshot(
            triggered_by="manual",
            industries=industries,
            prompts_by_industry=prompts_by_industry,
            triggered_user_id=current_user_id(admin),
            enforce_single_active=True,
        )
    except RoundAlreadyRunningError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "round_already_running",
                "message": str(e),
            },
        )
    except Exception as e:
        # 内部记完整异常 (含 type + 详情); 外部只 generic 不暴露 e
        logger.exception(
            f"create_round_with_snapshot 失败: {type(e).__name__}: {e}"
        )
        raise HTTPException(status_code=500, detail="创建 round 失败")

    # 4. 记 admin 备注到 summary_json (跑完后 update_round_complete 会覆盖,
    #    所以这里只是初始备注; 真正持久化下面写一条 UPDATE)
    if payload.note:
        try:
            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    UPDATE geo_research_round
                       SET summary_json = jsonb_set(
                               COALESCE(summary_json, '{}'::jsonb),
                               '{manual_note}',
                               %s::jsonb,
                               true
                           )
                     WHERE round_id = %s
                    """,
                    (json.dumps(payload.note), round_id),
                )
                conn.commit()
            finally:
                # finally 兜底 rollback (本段事务中若 UPDATE 抛异常,
                # 外层 except 只 logger.warning 不 rollback,会污染连接池)
                try:
                    conn.rollback()
                except Exception:
                    pass
                conn.close()
        except Exception as e:
            logger.warning(
                f"[{round_id}] 写 manual_note 失败 (不阻塞跑批): "
                f"{type(e).__name__}: {e}"
            )

    # 5. BackgroundTask 后台跑
    snapshot = {
        "industries": industries,
        "prompts_by_industry": {str(k): v for k, v in prompts_by_industry.items()},
    }
    background_tasks.add_task(_run_round_sync, round_id, snapshot, None)

    logger.info(
        f"[manual-trigger] admin={current_user_id(admin)} 启动 round {round_id} "
        f"(行业 {len(industries)} 个, prompts {sum(len(v) for v in prompts_by_industry.values())} 条)"
    )

    return {
        "round_id": round_id,
        "status": "pending",
        "message": "跑批已启动,后台异步执行中",
        "industries_count": len(industries),
        "prompts_count": prompts_count,
    }


# ==================== 4. POST /rounds/{round_id}/cancel 取消 ====================

@router.post("/rounds/{round_id}/cancel")
async def cancel_round(round_id: str, request: Request) -> Dict:
    """
    取消跑批 (只能取消 running / pending)。
    实现: UPDATE status='cancelled' + summary_json 加 cancelled_by + cancelled_reason。
    注意: 不主动 kill 已发出的单个 HTTP/LLM 请求,但 run_round 会在 stage/任务边界读取
          cancelled 状态并退出,不会再把状态覆盖成 completed。
    """
    admin = _require_admin(request)

    base = get_round_status(round_id)
    if not base:
        raise HTTPException(status_code=404, detail=f"round {round_id} 不存在")

    current_status = base.get("status")
    if current_status not in CANCELLABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "not_cancellable",
                "message": f"当前状态 {current_status} 不可取消 (仅 running/pending 可取消)",
                "current_status": current_status,
            },
        )

    # 合并 summary_json 写入取消信息
    cancel_summary = {
        "cancelled_by": current_user_id(admin),
        "cancelled_at": datetime.now().isoformat(),
        "cancelled_reason": "admin_manual",
        "cancelled_from_status": current_status,
    }

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE geo_research_round
               SET status = 'cancelled',
                   finished_at = NOW(),
                   summary_json = COALESCE(summary_json, '{}'::jsonb) || %s::jsonb
             WHERE round_id = %s
               AND status IN ('running', 'pending')
            """,
            (json.dumps(cancel_summary), round_id),
        )
        affected = cur.rowcount
        conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            f"[cancel round] 失败 round_id={round_id}: "
            f"{type(e).__name__}: {e}"
        )
        raise HTTPException(status_code=500, detail="取消 round 失败")
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()

    if affected == 0:
        # race: 刚才查到 running, 这一刻已变 (例: run_round 自然完成)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "race_status_changed",
                "message": "状态在取消瞬间已变化,请刷新后再试",
            },
        )

    logger.info(f"[cancel] admin={current_user_id(admin)} 标记 round {round_id} 为 cancelled (from {current_status})")

    # DR-3 backlog · UX 文案: 让 admin 知道 cancel 不是即时生效
    return {
        "round_id": round_id,
        "status": "cancelled",
        "cancelled_from": current_status,
        "message": (
            "已请求取消跑批。状态已标记为 cancelled。"
            "runner 会在下一个 stage/任务边界停止,不会再覆盖为 completed。"
            "不会立即停止当前 LLM/HTTP 调用。已发出去的请求成本仍会产生。"
        ),
    }


# ==================== 5. POST /rounds/{round_id}/resume 续跑 ====================

@router.post("/rounds/{round_id}/resume")
async def resume_round(
    round_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
) -> Dict:
    """
    续跑 failed_resumable / cancelled 状态的 round。
    已支持断点续跑 (P14-v9): 依 round.current_stage 经 _infer_resume_from_stage 推算续跑起点,
    已完成 stage 跳过、中断 stage 重跑; 仍可用 ?from_stage=stage_N 手动覆盖。
    """
    admin = _require_admin(request)

    base = get_round_status(round_id)
    if not base:
        raise HTTPException(status_code=404, detail=f"round {round_id} 不存在")

    current_status = base.get("status")
    if current_status not in RESUMABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "not_resumable",
                "message": f"当前状态 {current_status} 不可续跑 (仅 failed_resumable 可续跑)",
                "current_status": current_status,
            },
        )

    snapshot = get_round_snapshot(round_id)
    if not snapshot:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "snapshot_missing",
                "message": "round snapshot 丢失,无法续跑",
            },
        )

    month_check = check_month_budget()
    if not month_check.get("ok"):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "month_budget_exhausted",
                "message": month_check.get("reason") or "本月预算已超支,无法续跑",
                "spent": month_check.get("spent"),
                "limit": month_check.get("limit"),
            },
        )

    try:
        claimed = mark_round_resume_requested(round_id, requested_by=current_user_id(admin))
    except RoundAlreadyRunningError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "round_already_running",
                "message": str(e),
            },
        )
    except Exception as e:
        logger.exception(
            f"[resume claim] 失败 round_id={round_id}: "
            f"{type(e).__name__}: {e}"
        )
        raise HTTPException(status_code=500, detail="续跑 round 失败")

    if not claimed:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "race_status_changed",
                "message": "round 状态在续跑瞬间已变化,请刷新后再试",
                "current_status": current_status,
            },
        )

    # P14-v9 (2026-05-27 老板反馈): 续跑必须真断点续 · 不能每次从 stage_1 重跑
    # 之前已成功的 stage 数据(raw + round_call + articles)都在 DB 里 · 跳过它们
    # 规则:
    #   - stage_N_xxx_done (该 stage 已完成) → 从 stage_(N+1) 续
    #   - stage_N_xxx (无 _done · 中断) → 重跑 stage_N
    #   - None / 不识别 / legacy_imported → fallback stage_1
    # 仍允许 admin 用 query param ?from_stage=stage_3 手动覆盖
    inferred_resume = _infer_resume_from_stage(base.get("current_stage"))
    override_from_stage = request.query_params.get("from_stage")
    if override_from_stage:
        if override_from_stage not in _STAGE_ORDER:
            raise HTTPException(
                status_code=400,
                detail=f"from_stage 必须是 {_STAGE_ORDER} 之一",
            )
        resume_from_stage = override_from_stage
    else:
        resume_from_stage = inferred_resume
    background_tasks.add_task(_run_round_sync, round_id, snapshot, resume_from_stage)

    logger.info(
        f"[resume] admin={current_user_id(admin)} 续跑 round {round_id} "
        f"from {resume_from_stage} (上次停在 {base.get('current_stage')})"
    )

    return {
        "round_id": round_id,
        "status": "pending",
        "resumed_from": resume_from_stage,
        "previous_stage": base.get("current_stage"),
        "message": f"续跑已启动 · 从 {resume_from_stage} 继续 (上次停在 {base.get('current_stage') or '未知'})",
    }


# ==================== 6. GET /rounds/{round_id}/cost 成本明细 ====================

@router.get("/rounds/{round_id}/cost")
async def get_round_cost_detail(round_id: str, request: Request) -> Dict:
    """
    本轮成本明细:
      - 总额(get_round_cost)
      - 按 item 分组聚合
      - 月度预算剩余
    """
    _require_admin(request)

    base = get_round_status(round_id)
    if not base:
        raise HTTPException(status_code=404, detail=f"round {round_id} 不存在")

    total_yuan = float(get_round_cost(round_id))

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT item,
                   SUM(amount_yuan) AS total,
                   COUNT(*) AS records
              FROM geo_research_cost_log
             WHERE round_id = %s
             GROUP BY item
             ORDER BY total DESC
            """,
            (round_id,),
        )
        rows = cur.fetchall() or []
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            f"[round 成本明细] 失败 round_id={round_id}: "
            f"{type(e).__name__}: {e}"
        )
        raise HTTPException(status_code=500, detail="查询 round 成本明细失败")
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()

    by_item = []
    for row in rows:
        d = _row_to_dict(row)
        by_item.append({
            "item": d.get("item"),
            "total": float(d.get("total") or 0),
            "records": int(d.get("records") or 0),
        })

    month_check = check_month_budget()
    month_remaining = float(month_check.get("remaining") or 0)

    return {
        "round_id": round_id,
        "total_yuan": total_yuan,
        "by_item": by_item,
        "month_budget_remaining": month_remaining,
    }
