"""
GEO 调研监测跑批主引擎

8 stage 流水线 (本文件实现 stage 1-3):
1. AI 抓取(4 平台并发, 圆形熔断保护)
2. 提取 cited URLs + 预过滤
3. Jina 爬取 + 文章去重(url_hash + content_hash)

每个 stage 独立 try/except, stage 内单条 commit (防 server restart 丢数据)。
"""
import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import urlparse
from uuid import uuid4

from db.connection import get_connection

from services.research_monitor.platforms import (
    query_doubao, query_deepseek, query_qwen, query_kimi, query_with_retry,
)
from services.research_monitor.crawler import (
    should_pre_filter_url, crawl_article, prefilter_urls_concurrent,
)
from services.research_monitor.url_normalizer import normalize_url, compute_url_hash
from services.research_monitor.content_hash import compute_content_hash
from services.research_monitor.corpus_contract import (
    CORPUS_CONTRACT_VERSION,
    RAW_BOUNDARY_VERSION,
    fingerprint_clean_body,
)
from services.research_monitor.domain_tiering import get_domain_tier
from services.research_monitor.circuit_breaker import CircuitBreaker
from services.research_monitor.oss_helper import (
    upload_markdown, download_markdown, generate_oss_key_for_article,
)
# Phase 9 (2026-05-25) · cleaner/scorer 已物理删 (P05g) · 改用规则清洗 · 删 LLM 评分
from services.research_monitor.clean_articles_rule import (
    clean_markdown as _rule_clean_markdown,
    text_length as _rule_text_length,
)
from services.research_monitor.content_classifier import get_content_type
from services.research_monitor.article_intent_classifier import (
    DEFAULT_INTENT_MODEL,
    classify_article_intent,
)
from services.research_monitor.round_state import (
    update_heartbeat,
    update_round_progress,
    update_round_complete,
    get_round_snapshot,
    get_round_status,
    mark_round_resume_requested,
    infer_resume_from_stage,
    RoundAlreadyRunningError,
)
from services.research_monitor.budget_guard import (
    BudgetExhaustedError,
    check_round_budget,
    check_month_budget,
)
from services.placement_service import PlacementService

logger = logging.getLogger("GEO-ResearchMonitor")

STAGE2_HEARTBEAT_INTERVAL_SECONDS = 30.0


class RoundCancelledError(Exception):
    """round 已被管理员标记 cancelled, runner 应在安全边界退出。"""


class CostLogWriteError(Exception):
    """外部成本已发生但 cost_log 写入失败, runner 必须停跑避免预算低估。"""


class Stage7AggregateError(Exception):
    """推荐统计聚合失败。round 不能继续标 completed, 避免飞轮输出层静默空转。"""


PLATFORM_FETCHERS = {
    'doubao': query_doubao,
    'deepseek': query_deepseek,
    'qwen': query_qwen,
    'kimi': query_kimi,
}

# 各 stage 单价 (元 · 跟 cost_estimator.UNIT_PRICES 对齐)
# Phase 9 (2026-05-25) · 按平台拆 AI 调用单价 (豆包按次 · 其余按 token 均价 · 单价为示例值)
COST_PER_AI_CALL_BY_PLATFORM = {
    'doubao':   0.10,   # doubao_app + ai_search 按次计费(火山方舟规则)
    'deepseek': 0.03,   # 阿里百炼 deepseek-v4-flash · token 均价估算
    'qwen':     0.03,   # 阿里百炼 qwen-plus-latest · token 均价估算
    'kimi':     0.04,   # 阿里百炼 kimi/kimi-k2.6 · token 均价估算
}
# 兼容老代码 / 未识别平台兜底均价 (4 平台简单平均 · 略偏低)
COST_PER_AI_CALL = sum(COST_PER_AI_CALL_BY_PLATFORM.values()) / len(COST_PER_AI_CALL_BY_PLATFORM)
COST_PER_JINA_CRAWL = 0.02
# COST_PER_LLM_CLEAN / COST_PER_LLM_SCORE (Phase 9 已废) 已删除 ·
# 全仓 grep 确认零 import/引用 (R2-4 · 2026-07-05)


def _read_config_value(key: str, default, cast):
    """读取 research_monitor 运行时配置。非预算配置读取失败时用默认值。"""
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT value_json FROM geo_research_config WHERE key = %s",
                (key,),
            )
            row = cur.fetchone()
            if not row:
                return default
            value = row.get('value_json') if isinstance(row, dict) else row[0]
            return cast(value)
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"读取运行时配置 {key} 失败, 使用默认值 {default}: {e}")
        return default


def _get_int_config(key: str, default: int) -> int:
    value = _read_config_value(key, default, int)
    return value if value > 0 else default


def _get_float_config(key: str, default: float) -> float:
    value = _read_config_value(key, default, float)
    return value if value >= 0 else default


def _cast_bool_config(value) -> bool:
    """JSONB bool 直通; 兼容 'true'/'false'/'1'/'0' 字符串与 0/1 数字; 其余抛错走默认。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ('true', '1', 'yes', 'on'):
            return True
        if s in ('false', '0', 'no', 'off'):
            return False
    raise ValueError(f"non-bool config value: {value!r}")


def _get_bool_config(key: str, default: bool) -> bool:
    return _read_config_value(key, default, _cast_bool_config)


def _log_cost(round_id: str, item: str, request_count: int, unit_price: float) -> None:
    """
    写一行 cost_log · stage 末尾汇总调用 · fail-closed 防预算低估
    request_count <= 0 跳过(防写空记录)
    """
    if request_count <= 0:
        return
    amount = round(request_count * unit_price, 4)
    conn = None
    cur = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO geo_research_cost_log
                (round_id, item, amount_yuan, request_count)
            VALUES (%s, %s, %s, %s)
            """,
            (round_id, item, amount, request_count),
        )
        conn.commit()
    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        logger.error(f"[{round_id}] cost_log 写入失败 item={item}, 停止后续跑批: {e}")
        raise CostLogWriteError(f"cost_log write failed for {item}: {e}") from e
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        if conn is not None:
            conn.close()

# [2026-07-16 提并发批] Stage1 每平台并发数 · 运行时配置 geo_research_config.fetch_concurrency_per_platform
#   默认 8 (老板口径: LLM 供应商支持 200+ 并发, 8/平台=全局 32 很保守)。
#   旧常量 PLATFORM_CONCURRENCY=5 只造了 semaphore(5*4) 但循环结构组间串行, 全程最多 4 个
#   在飞任务、semaphore 从未收束 → 已随并发化重写移除(全仓零外部引用, 亲证)。
DEFAULT_FETCH_CONCURRENCY_PER_PLATFORM = 8
# 防手滑配置上限: 64/平台 = 全局 256, 仍在供应商 200+/平台的并发口径内
MAX_FETCH_CONCURRENCY_PER_PLATFORM = 64


def _get_stage1_concurrency() -> int:
    """每平台并发数: env RESEARCH_FETCH_CONCURRENCY(可选) > geo_research_config > 默认 8。

    _get_int_config 已保证非法/≤0 回落默认; 这里再 clamp 上限防误配巨值。
    每轮 stage1 开头读一次(改配置下一轮生效, 不做轮内热更)。
    """
    env_v = _safe_int_env('RESEARCH_FETCH_CONCURRENCY', 0, minimum=0)
    if env_v > 0:
        return min(env_v, MAX_FETCH_CONCURRENCY_PER_PLATFORM)
    v = _get_int_config(
        'fetch_concurrency_per_platform', DEFAULT_FETCH_CONCURRENCY_PER_PLATFORM
    )
    return min(v, MAX_FETCH_CONCURRENCY_PER_PLATFORM)


# P14.4 C1 (老板复核): env 非数字防呆
# 之前 int(os.getenv(...)) / float(os.getenv(...)) 直接调内置 · env 写 "abc" 会 ValueError
# 模块 import 期触发 → backend 起不来 · 日志只见 stack trace 难定位
# 这里包成 helper · 非法值 warn + 走默认 · clamp 下限可选
def _safe_int_env(name: str, default: int, minimum: int | None = None) -> int:
    raw = os.getenv(name, '').strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except (ValueError, TypeError):
        logger.warning(f"env {name}={raw!r} 非整数 · 走默认 {default}")
        return default
    if minimum is not None and v < minimum:
        logger.warning(f"env {name}={v} 小于下限 {minimum} · clamp 到 {minimum}")
        return minimum
    return v


def _safe_float_env(name: str, default: float, minimum: float | None = None) -> float:
    raw = os.getenv(name, '').strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except (ValueError, TypeError):
        logger.warning(f"env {name}={raw!r} 非浮点 · 走默认 {default}")
        return default
    if minimum is not None and v < minimum:
        logger.warning(f"env {name}={v} 小于下限 {minimum} · clamp 到 {minimum}")
        return minimum
    return v


# Jina 爬取并发 (免费档 5 req/s)
# P14-v14 (2026-05-28 老板): JINA_CONCURRENCY 智能默认 (跟旧独立工具 platforms.py 一致)
#   有 JINA_API_KEY: 默认 2 并发 (Jina 收费版 100 RPM 但限 2 并发)
#   无 JINA_API_KEY: 默认 5 并发 (Jina 免费版 ~20 RPM 但允许 5 并发)
#   env JINA_CONCURRENCY 非 0 时覆盖默认 (跑得快可设 50 等 · 撞限速 Jina 自己 429 重试)
_jina_has_key = bool(os.getenv('JINA_API_KEY', '').strip())
JINA_CONCURRENCY = _safe_int_env('JINA_CONCURRENCY', 0, minimum=0) or (2 if _jina_has_key else 5)

# P14.3 C3 (老板 round_20260529_014854 反馈): Jina 瞬时网络抖动加轻重试
#   12/17 URL 一次 ConnectError → 1 小时后同 URL 全 OK → retry 大概率救回大部分
#   1 次 retry (共 2 attempts) · 间隔 2s · 仍在同 semaphore 槽内 · 不新增无限并发
#   env 可覆盖 · 测试 patch asyncio.sleep 防拖测试时长
# P14.3 C3.1 防呆 (老板复核): env 误配 0/负数时 · clamp 到安全下限 (现走 _safe_int_env minimum)
JINA_RETRY_MAX_ATTEMPTS = _safe_int_env('JINA_RETRY_MAX_ATTEMPTS', 2, minimum=1)

# [2026-07-16 Stage2 并发预筛] 网络预筛(DNS/robots)全局并发上限 · 默认 16(硬要求 1)
# env 只许调小(clamp 上限=16, 与硬要求"全局并发上限 16"字面一致 · 出口审核收紧);
# 要更高并发需改代码经审。该并发只作用于 stage2 预筛线程池,
# 与 Jina(JINA_CONCURRENCY, Stage3, 走 Xray 代理)完全无关(硬要求 9)。
STAGE2_PREFILTER_CONCURRENCY = min(
    _safe_int_env('RESEARCH_STAGE2_PREFILTER_CONCURRENCY', 16, minimum=1), 16,
)

# [2026-07-16 心跳普查] 通用 30s 时间型心跳节拍(stage2 既有修复同拍 · stage4/7 复用)
HEARTBEAT_TICK_INTERVAL_SECONDS = 30.0


async def _heartbeat_ticker(round_id: str, interval_seconds: float = HEARTBEAT_TICK_INTERVAL_SECONDS):
    """[2026-07-16 心跳普查] 伴飞心跳协程: 供 to_thread 长阻塞(重 SQL)期间使用 —
    阻塞卸到线程池后 event loop 空闲, 本协程每 interval 刷一次 round 心跳。
    DB 瞬断只 log 不抛(绝不因心跳失败改变 stage 结果)。由调用方 cancel 收尾。"""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            update_heartbeat(round_id)
        except Exception as e:
            logger.warning(f"[{round_id}] 伴飞心跳失败(忽略继续): {e}")


async def _to_thread_with_heartbeat(round_id: str, fn, *args, interval_seconds: Optional[float] = None):
    """[2026-07-16 心跳普查] asyncio.to_thread + 伴飞心跳: 包住 >10 分钟风险的
    同步长阻塞调用(stage7 聚合/重算 SQL), 阻塞期间每 30s 刷心跳, 防僵尸 sweep
    心跳分支误收活轮。结果/异常语义与裸 to_thread 完全一致。
    interval_seconds 缺省取 HEARTBEAT_TICK_INTERVAL_SECONDS(调用时读, 测试可注入)。"""
    ticker = asyncio.create_task(
        _heartbeat_ticker(round_id, interval_seconds or HEARTBEAT_TICK_INTERVAL_SECONDS)
    )
    try:
        return await asyncio.to_thread(fn, *args)
    finally:
        ticker.cancel()
        try:
            await ticker
        except asyncio.CancelledError:
            # [出口审核修] 区分"ticker 自身取消"与"外层取消恰落在本 await 上":
            # try 体正常返回后的微小窗口内, 4h wait_for 对本 task 的取消会在这里
            # 抛出 — 吞掉它 = 该次超时静默丢失, 轮越过 4h 跑到自然结束。
            # cancelling()>0 说明外层取消在途, 必须复抛。
            if asyncio.current_task().cancelling():
                raise
        except Exception:
            pass
JINA_RETRY_DELAY_SECONDS = _safe_float_env('JINA_RETRY_DELAY_SECONDS', 2.0, minimum=0.0)

# 文章最少字数(crawl 阶段就过滤短回答页 / 错误页)
MIN_ARTICLE_CHARS = 500


# ==================== Stage 1 ====================

async def stage1_ai_fetch_all(round_id: str, plan: List[Dict]):
    """
    Stage 1: 4 平台并发 AI 抓取(2026-07-16 组间并发化)

    plan: [{'industry_id', 'industry_name', 'prompt_id', 'prompt_text'}, ...]

    并发模型: 每平台独立队列 + 平台内 Semaphore(N) 消化全量 prompt, 4 平台 gather
    并行 → 全局有效并发 = 4×N(N = geo_research_config.fetch_concurrency_per_platform,
    默认 8)。旧版为组间串行(for prompt: await gather(4 平台)), 每组耗时 =
    max(4 平台) ≈ kimi 50.6s, 409 组 ≈ 5.75h 必撞 4h 硬超时; 新版最慢平台
    409/8×50.6s ≈ 43min。调用总量/模型/预算口径不变。

    失败重试 3 次, 每条 round_call 单独 commit。

    熔断: breaker 计数在单 event loop 内共享(record_*/is_tripped 无 await, task 间
    不可分割 → task-safe, 无跨线程), 每个调用起跑前判 is_tripped, 触发后停发新调用
    (在飞调用自然收尾, 上限 4×N 个), 与旧版"熔断后停止后续 prompt"口径一致,
    检查频率(每调用 1 次)高于旧版(每 prompt 1 次)。

    取消: 每个调用起跑前查 round 状态; 命中 cancelled 先置停发旗标让排队任务快速
    退出, 收尾时把 RoundCancelledError 原样上抛(不许被 _raise_gather_exceptions 包成
    RuntimeError, 否则 cancelled 终态会被误归为 failed)。

    基建级异常(DB 连接/INSERT 失败等)与旧版对齐: 置停发旗标 fail-fast 上抛, 防止
    "带着坏 DB 把 1636 个供应商调用打完却整轮记 failed"的钱损。
    """
    breaker = CircuitBreaker(
        consecutive_threshold=_get_int_config('circuit_breaker_consecutive', 50),
        rate_threshold=_get_float_config('circuit_breaker_rate', 0.5),
        min_processed=_get_int_config('circuit_breaker_min_processed', 100),
    )

    total_calls = len(plan) * len(PLATFORM_FETCHERS)
    processed = 0
    per_platform_concurrency = _get_stage1_concurrency()
    # 停发旗标(熔断/取消/基建异常后不再起新调用, 在飞调用自然收尾)。
    # 与 breaker/processed 一样只在单 event loop 内读写、读写间无 await → 无锁安全。
    stop_dispatch = False
    breaker_trip_logged = False
    cancelled_errors: List[Exception] = []

    logger.info(
        f"[{round_id}] Stage 1 并发配置: {len(PLATFORM_FETCHERS)} 平台 × "
        f"{per_platform_concurrency}/平台 = 全局 {len(PLATFORM_FETCHERS) * per_platform_concurrency}, "
        f"总调用 {total_calls}"
    )

    async def call_one_platform(platform: str, fetcher, item: Dict):
        nonlocal processed, stop_dispatch, breaker_trip_logged
        if stop_dispatch:
            return
        # 熔断检查 (每个调用起跑前判 · 频率高于旧版每 prompt 前 1 次)
        tripped, reason = breaker.is_tripped()
        if tripped:
            stop_dispatch = True
            if not breaker_trip_logged:
                breaker_trip_logged = True
                logger.error(
                    f"[{round_id}] Stage 1 熔断 (原因: {reason}), 已处理 {processed}/{total_calls}"
                )
            return
        # 取消检查 (旧版在组循环头, 现移到每个调用起跑前; 这里不上抛, 置旗标+收集,
        # 由 stage 收尾统一原样 raise → run_round 归档 cancelled)
        try:
            _raise_if_round_cancelled(round_id)
        except RoundCancelledError as ce:
            stop_dispatch = True
            cancelled_errors.append(ce)
            return

        try:
            # [GEO-R6-CAN-014] resume 幂等: 若本 (round, prompt, platform) 已有 success 记录,
            #   跳过重复的 provider 调用 — 否则 admin resume 会重跑已成功的引擎, 重复扣供应商
            #   成本并向 geo_research_raw 灌重复引用/答案行。纯防重, 不改任何积分金额/守恒。
            #   首跑时无 success 行 → 该守卫不触发, 正常行为字节不变。
            _pid = item.get('prompt_id')
            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT 1
                      FROM geo_research_round_call
                     WHERE round_id = %s
                       AND platform = %s
                       AND prompt_id IS NOT DISTINCT FROM %s
                       AND status = 'success'
                     LIMIT 1
                    """,
                    (round_id, platform, _pid),
                )
                _already_done = cur.fetchone() is not None
            finally:
                conn.close()
            if _already_done:
                processed += 1
                return

            # 1. INSERT round_call status='pending' (单独 commit)
            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO geo_research_round_call
                        (round_id, industry_id, prompt_id, prompt_text, platform,
                         status, attempts, started_at)
                    VALUES (%s, %s, %s, %s, %s, 'pending', 0, NOW())
                    RETURNING id
                    """,
                    (
                        round_id,
                        item.get('industry_id'),
                        item.get('prompt_id'),
                        item.get('prompt_text', ''),
                        platform,
                    ),
                )
                call_id = cur.fetchone()['id']
                conn.commit()
            finally:
                conn.close()

            # 2. 真调用 + 重试
            try:
                result = await query_with_retry(
                    fetcher,
                    prompt_id=item.get('prompt_id') or 0,
                    prompt=item.get('prompt_text', ''),
                    max_retries=3,
                )

                citations = result.get('citations', []) or []
                answer = result.get('answer', '') or ''
                # [GEO-R6-CAN-006 消费侧 · 2026-07-16] attempts 写真实尝试次数。
                #   query_with_retry 已在返回体挂真实 attempts(生产侧 CAN-006 早已落地,
                #   但消费侧一直硬编码 3 → 历史"成功 call 全部 attempts=3"是口径失真非真重试)。
                #   失败路径仍写 3(= max_retries 耗尽; 非重试型异常拿不到真实次数, 保守记满)。
                try:
                    _attempts_real = int(result.get('attempts') or 3)
                except (TypeError, ValueError):
                    _attempts_real = 3

                # 3. 写 geo_research_raw (一行一引用) + 更新 round_call
                conn = get_connection()
                try:
                    cur = conn.cursor()
                    raw_citations = citations or [{
                        'url': '',
                        'title': '',
                        'rank': 0,
                        'is_answer_cited': False,
                        'adoption_rank': None,
                    }]
                    # [GEO-R1-CAN-124] answer_text 列本身是无界 TEXT, 旧代码把答案硬裁到 16000
                    #   字丢失分析完整性且无标记, 下游(flywheel_bridge/answer_entity)只
                    #   消费被裁前缀却无法感知丢尾。改为持久化完整答案; 仅对病态超长(> 200000)
                    #   保留上界并追加显式截断标记, 做到 loss-aware。不涉及金额/权限。
                    _ANSWER_PERSIST_MAX = 200000
                    if len(answer) > _ANSWER_PERSIST_MAX:
                        _omitted = len(answer) - _ANSWER_PERSIST_MAX
                        _answer_persist = (
                            answer[:_ANSWER_PERSIST_MAX]
                            + f"\n\n[GEO-R1-CAN-124 truncated: {_omitted} chars omitted]"
                        )
                    else:
                        _answer_persist = answer
                    for c in raw_citations:
                        # C-1 修复: cited_platform 应为被引网站 domain (zhihu.com / 36kr.com),
                        # 不是完整 URL。urlparse 失败兜底空字符串,不抛异常。
                        _cite_url = c.get('url') or ''
                        try:
                            _cited_platform = (urlparse(_cite_url).netloc or '')[:200]
                        except Exception:
                            _cited_platform = ''
                        cur.execute(
                            """
                            INSERT INTO geo_research_raw
                                (industry, query, engine, cited_platform,
                                 cite_position, cite_url, cite_title,
                                 cite_excerpt, answer_text, is_answer_cited,
                                 adoption_rank, batch_id, researcher,
                                 provider, model, model_revision, surface,
                                 search_mode, prompt_snapshot)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '',
                                    %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                item.get('industry_name', '') or '',
                                item.get('prompt_text', '') or '',
                                platform,
                                _cited_platform,
                                c.get('rank', 0) or 0,
                                _cite_url[:2000],
                                (c.get('title') or '')[:500],
                                (c.get('excerpt') or c.get('snippet') or '')[:1000],
                                _answer_persist,  # [GEO-R1-CAN-124] 完整答案(病态超长带截断标记)
                                bool(c.get('is_answer_cited')),
                                c.get('adoption_rank'),
                                f"batch_{round_id}",
                                result.get('provider') or 'unknown',
                                result.get('model') or 'unknown',
                                result.get('model_revision') or 'unknown',
                                result.get('surface') or 'unknown',
                                result.get('search_mode') or 'unknown',
                                item.get('prompt_text', '') or '',
                            ),
                        )
                    cur.execute(
                        """
                        UPDATE geo_research_round_call
                           SET status = 'success',
                               citations_count = %s,
                               attempts = %s,
                               finished_at = NOW()
                         WHERE id = %s
                        """,
                        (len(citations), _attempts_real, call_id),
                    )
                    conn.commit()
                finally:
                    conn.close()

                breaker.record_success()
            except Exception as e:
                # 失败: 更新 round_call status='failed', 不阻塞下一 prompt/platform
                conn = get_connection()
                try:
                    cur = conn.cursor()
                    cur.execute(
                        """
                        UPDATE geo_research_round_call
                           SET status = 'failed',
                               error_message = %s,
                               attempts = 3,
                               finished_at = NOW()
                         WHERE id = %s
                        """,
                        (str(e)[:500], call_id),
                    )
                    conn.commit()
                finally:
                    conn.close()

                breaker.record_failure()
                logger.warning(
                    f"[{round_id}] {platform} prompt={item.get('prompt_id')} 失败: {e}"
                )

            processed += 1
            # 每 5 个调用刷一次心跳 + 进度
            # P14-v8b (2026-05-27 老板反馈): 旧 % 30 阈值在小行业(< 30 prompt)永触发不到
            #   导致 status 一直停 pending · UI 看不到任何中间进度. 改 5 让小行业也有反馈.
            if processed % 5 == 0:
                update_heartbeat(round_id)
                update_round_progress(
                    round_id,
                    stage='stage_1_ai_fetch',
                    progress={'processed': processed, 'total': total_calls},
                )
        except Exception:
            # 基建级异常(CAN-014 SELECT / INSERT pending / 失败落库 / 心跳等 DB 故障;
            # 供应商调用失败已被内层 except 消化不会走到这): 与旧版 fail-fast 对齐 —
            # 旧版单条基建异常经 _raise_gather_exceptions 立即终止整个 stage;
            # 并发版置停发旗标后上抛, 防"带着坏 DB 把 1636 个供应商调用打完却整轮 failed"。
            stop_dispatch = True
            raise

    async def run_platform_queue(platform: str, fetcher):
        # 每平台独立信号量: 单一供应商最多 N 个在飞, 平台之间互不占额度
        # (kimi 慢不阻塞其他平台; 也不会出现"别的平台跑完后单平台并发超 N 撞限流")
        sem = asyncio.Semaphore(per_platform_concurrency)

        async def bounded_call(item: Dict):
            async with sem:
                await call_one_platform(platform, fetcher, item)

        results = await asyncio.gather(
            *[bounded_call(item) for item in plan],
            return_exceptions=True,
        )
        _raise_gather_exceptions(results, f'stage_1_ai_fetch:{platform}')

    # 4 平台 fan-out (return_exceptions 防一个 platform 崩了把其余平台拽掉)
    platform_results = await asyncio.gather(
        *[run_platform_queue(p, f) for p, f in PLATFORM_FETCHERS.items()],
        return_exceptions=True,
    )
    # 取消原样上抛(优先级最高, 不许被下面包成 RuntimeError → run_round 归档 cancelled)
    if cancelled_errors:
        raise cancelled_errors[0]
    _raise_gather_exceptions(platform_results, 'stage_1_ai_fetch')

    # 写 cost_log: 按 platform 分项, 4 平台各 1 行 (含成功+失败的 attempt 都计费)
    try:
        c = get_connection()
        try:
            cc = c.cursor()
            cc.execute(
                """
                SELECT platform, COUNT(*) AS cnt
                  FROM geo_research_round_call
                 WHERE round_id = %s
                 GROUP BY platform
                """,
                (round_id,),
            )
            for r in cc.fetchall():
                # Phase 9: 按 platform 区分单价 (豆包 ¥0.2/次 · 其余 ¥0.05-0.08)
                platform_name = r['platform']
                unit_price = COST_PER_AI_CALL_BY_PLATFORM.get(platform_name, COST_PER_AI_CALL)
                _log_cost(round_id, platform_name, int(r['cnt'] or 0), unit_price)
        finally:
            c.close()
    except CostLogWriteError:
        raise
    except Exception as e:
        logger.error(f"[{round_id}] Stage 1 cost_log 汇总失败, 停止后续跑批: {e}")
        raise CostLogWriteError(f"stage1 cost aggregation failed: {e}") from e

    update_round_progress(
        round_id,
        stage='stage_1_ai_fetch_done',
        progress={
            'processed': processed,
            'total': total_calls,
            'breaker_tripped': breaker.is_tripped()[0],
            'stats': breaker.get_stats(),
        },
    )
    logger.info(f"[{round_id}] Stage 1 完成 {processed}/{total_calls}")


# ==================== Stage 2 ====================

def stage2_extract_and_prefilter_urls(
    round_id: str,
    batch_id: str,
    plan: Optional[List[Dict]] = None,
) -> List[Dict]:
    """
    Stage 2: 从 geo_research_raw 提取本轮所有 cited url, 归一化 + 预过滤

    P14.1 C2 (2026-05-28 老板真实跑批反馈): citation 上下文恢复
      - 旧版 industry_id/prompt_id 留 None · 注释说"stage 3 再反查" · 实际没反查
      - 导致 article_citations 三字段全 NULL · 引用明细 / 矩阵丢上下文
      - raw 表无 industry_id/prompt_id 列 (只有 industry 名 + query 文本)
      - 用 plan = _flatten_plan(snapshot) 构建 lookup maps · 无 N+1

    plan: run_round 已经平铺好的 [{industry_id, industry_name, prompt_id, prompt_text}, ...]
      - 若 None (老调用方/续跑兜底), 不带 industry_id/prompt_id (维持旧行为不破)
      - 否则用 (industry_name) → industry_id + (industry_name, prompt_text) → prompt_id 反查

    返回: [{'url', 'normalized_url', 'industry_id', 'industry_name',
            'prompt_id', 'prompt_text', 'platform',
            'raw_id', 'rank_in_response'}, ...]
    """
    # 构建 plan lookup maps · 同 industry 多个 prompts 合 key 时 industry_id 一致 (设计上)
    industry_id_by_name: Dict[str, int] = {}
    prompt_id_by_key: Dict[tuple, int] = {}
    for p in (plan or []):
        ind_name = p.get('industry_name') or ''
        if ind_name and p.get('industry_id') is not None:
            industry_id_by_name.setdefault(ind_name, p['industry_id'])
        prompt_text = p.get('prompt_text') or ''
        if ind_name and prompt_text and p.get('prompt_id') is not None:
            prompt_id_by_key.setdefault((ind_name, prompt_text), p['prompt_id'])

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id AS raw_id, cite_url, industry, query, engine, cite_position
              FROM geo_research_raw
             WHERE batch_id = %s
               AND cite_url IS NOT NULL
               AND cite_url <> ''
             ORDER BY id ASC
            """,
            (batch_id,),
        )
        raw_urls = cur.fetchall()
    finally:
        conn.close()

    # ===== [2026-07-16 Stage2 并发预筛返工] 三段式: 去重 → 按域并发网络预筛 → 顺序装配 =====
    # 旧版逐 URL 串行 should_pre_filter_url(check_robots=True): 每 URL 阻塞 DNS(同
    # host 最多解析 2 次)+ 每新域 robots 抓取(5s 超时), 数千域名 → 小时级。
    # 新版语义不变: 输出 = 首现去重后通过全部过滤的 URL, 按原始 raw_id 顺序,
    # raw_id/rank/industry/prompt 上下文原样(硬要求 2/3)。
    # 去重语义订正说明: 旧版只把"通过预筛"的 URL 加进 seen, 被过滤 URL 的后续
    # 重复会重跑预筛但裁定相同(确定性)仍被过滤 → 先去重后裁定一次, 输出逐条等价。

    # Phase A: 归一化 + 首现去重(纯 CPU 串行 · 保留既有 30s 时间型心跳节拍, 不覆盖 cd09e88a 修复)
    def _stage2_beat() -> None:
        """best-effort 心跳: DB/Redis 瞬断只 log 绝不打死 stage(心跳是辅助信号;
        stage2 全程零终态写入, 瞬断也不可能把活轮误标 completed)。"""
        try:
            update_heartbeat(round_id)
        except Exception as e:
            logger.warning(f"[{round_id}] Stage 2 心跳失败(忽略继续): {e}")

    candidates: List[tuple] = []  # [(raw 行, normalized)] 按原始 raw_id 顺序
    seen_normalized = set()
    last_heartbeat_at = time.monotonic()
    _stage2_beat()

    for r in raw_urls:
        now = time.monotonic()
        if now - last_heartbeat_at >= STAGE2_HEARTBEAT_INTERVAL_SECONDS:
            _stage2_beat()
            last_heartbeat_at = now

        normalized = normalize_url(r['cite_url'])
        if not normalized or normalized in seen_normalized:
            continue
        seen_normalized.add(normalized)
        candidates.append((r, normalized))

    # Phase B: 按域合并的有界并发网络预筛(形状/SSRF/robots · 全局并发 ≤16)
    def _stage2_should_stop() -> bool:
        """非抛版取消探测(硬要求 8)。DB 瞬断按未取消处理 + log —— 绝不因
        探测失败中断预筛, 更不在任何异常路径写终态(活轮不可能被误标 completed)。"""
        try:
            st = get_round_status(round_id) or {}
            return (st.get('status') or '').lower() == 'cancelled'
        except Exception as e:
            logger.warning(f"[{round_id}] Stage 2 取消探测失败(按未取消继续): {e}")
            return False

    verdicts, stopped = prefilter_urls_concurrent(
        [norm for (_r, norm) in candidates],
        max_workers=STAGE2_PREFILTER_CONCURRENCY,
        heartbeat_cb=_stage2_beat,
        should_stop_cb=_stage2_should_stop,
        heartbeat_interval_seconds=STAGE2_HEARTBEAT_INTERVAL_SECONDS,
    )
    if stopped:
        # 取消: 未开跑任务已撤、在飞任务不再等待(正常 ≤~15s 自然收敛; tarpit
        # 钉死线程遗留为已知残余, WARN 可见)→ 立即交 run_round 归档 cancelled
        # (与其他 stage 的 _raise_if_round_cancelled 同一约定)
        raise RoundCancelledError(f"round {round_id} 已被管理员取消 (stage2 预筛中止)")

    # Phase C: 按原始顺序装配通过项(上下文字段与旧版逐字一致)
    kept: List[Dict] = []
    missing_verdicts = 0
    for r, normalized in candidates:
        if normalized not in verdicts:
            missing_verdicts += 1  # 理论不可达(非 stopped 时全量有裁定) · fail-closed 跳过
            continue
        if verdicts[normalized] is not None:
            continue
        # platform 取首次见到的 engine (citation 表 CHECK 限定 4 平台)
        # 默认 'doubao' 兜底防 engine 字段异常
        platform = r.get('engine') if r.get('engine') in PLATFORM_FETCHERS else 'doubao'
        ind_name = r.get('industry') or ''
        prompt_text = r.get('query') or ''
        # P14.1 C2: 用 plan map 反查 industry_id / prompt_id · 拉不到留 None 不报错
        kept.append({
            'url': r['cite_url'],
            'normalized_url': normalized,
            'industry_id': industry_id_by_name.get(ind_name),
            'industry_name': ind_name,
            'prompt_id': prompt_id_by_key.get((ind_name, prompt_text)),
            'prompt_text': prompt_text,
            'platform': platform,
            # P14.1 C2: raw_id + rank_in_response 透传到 stage 3 写 citation 用
            'raw_id': r.get('raw_id'),
            'rank_in_response': r.get('cite_position'),
        })
    if missing_verdicts:
        logger.warning(
            f"[{round_id}] Stage 2 有 {missing_verdicts} 条候选缺裁定, 已 fail-closed 过滤"
        )

    _stage2_beat()

    update_round_progress(
        round_id,
        stage='stage_2_prefilter_done',
        progress={
            'raw_url_count': len(raw_urls),
            'kept_url_count': len(kept),
            'unique_url_count': len(candidates),
            'prefilter_concurrency': STAGE2_PREFILTER_CONCURRENCY,
        },
    )
    logger.info(
        f"[{round_id}] Stage 2 完成 {len(raw_urls)} -> 去重 {len(candidates)} -> 通过 {len(kept)}"
    )

    return kept


# ==================== Stage 3 ====================

# ==================== Stage 3 URL 质量预算(P1 返工 2026-07-16) ====================

# Jina 吞吐常量在 url_budget(SSOT)· 本处 import 别名避免两处漂移
from services.research_monitor.url_budget import JINA_THROUGHPUT_URL_PER_MIN  # noqa: E402
STAGE3_HEARTBEAT_INTERVAL_SECONDS = 30.0


def _load_stage3_budget_config():
    """从 geo_research_config 读预算参数(读不到/非法回落代码默认)。"""
    from services.research_monitor.url_budget import BudgetConfig
    # budget_max: 用 _read_config_value 而非 _get_int_config —— 后者对 0 回落默认,
    # 但 0 在本 key 是合法语义(=不限=旧行为全量抓取)。缺省/非法 → 1500。
    raw_max = _read_config_value('stage3_url_budget_max', 1500, int)
    budget_max = raw_max if isinstance(raw_max, int) and raw_max >= 0 else 1500
    return BudgetConfig(
        budget_max=budget_max,
        min_urls_per_industry=_get_int_config('stage3_min_urls_per_industry', 40),
        min_urls_per_platform=_get_int_config('stage3_min_urls_per_platform', 50),
        max_urls_per_domain=_get_int_config('stage3_max_urls_per_domain', 30),
    )


def ensure_stage3_url_budget(round_id: str, batch_id: str, kept_urls: List[Dict]) -> List[Dict]:
    """Stage3 前置: 质量排序 + 配额选出 URL 预算, 持久化到 geo_research_url_budget,
    返回待爬 pending 列表(耐久断点 · 幂等续跑)。

    幂等: 本 round 预算表已有行 → 续跑, 不重算(读回 pending); 无 → 首次计算+持久化。
    budget_max<=0 → 不限(旧行为), 全部 kept 进预算。
    """
    from db.research_url_budget_db import (
        count_budget_rows, load_batch_raw_rows_for_signals,
        persist_url_budget, load_pending_budget, load_existing_article_url_hashes,
    )
    from services.research_monitor.url_budget import (
        normalize_and_aggregate_signals, score_candidate, select_url_budget,
    )

    if count_budget_rows(round_id) == 0:
        # 首次: 聚合 raw 信号 + 打分 + 配额选择 + 持久化
        raw_rows = load_batch_raw_rows_for_signals(batch_id)
        signals = normalize_and_aggregate_signals(raw_rows)
        cfg = _load_stage3_budget_config()

        candidates: List[Dict] = []
        for u in kept_urls:
            norm = u.get('normalized_url') or ''
            if not norm:
                continue
            try:
                dom = (urlparse(norm).netloc or '')[:200]
            except Exception:
                dom = ''
            tier = get_domain_tier(dom)
            score = score_candidate(tier, signals.get(norm))
            candidates.append({
                **u,
                'url_hash': compute_url_hash(norm),
                'domain': dom,
                'score': score,
            })

        existing: set = set()  # DB 命中(免费)url_hash 集; budget_max<=0 分支不用
        if cfg.budget_max <= 0:
            # 不限: 全部进预算(旧行为 · budget_rank 按原始顺序)
            selected = [dict(c, budget_rank=i + 1) for i, c in enumerate(candidates)]
        else:
            # [轮1审核 must_fix] DB 命中免费项(url_hash 已在 articles → stage3 走
            # reused_existing/crawled_dup, 零 Jina 抓取)豁免出 budget_max 计数, 全保留
            # (它们不消耗 Jina 分钟, 裁掉只会无谓丢 citation 覆盖率); budget_max 上限
            # 只施加于真爬 Jina 的全新 URL(paid)。
            existing = load_existing_article_url_hashes([c['url_hash'] for c in candidates])
            free = [c for c in candidates if c['url_hash'] in existing]
            paid = [c for c in candidates if c['url_hash'] not in existing]
            paid_selected = select_url_budget(paid, cfg)
            # free 全保留(budget_rank 接在 paid 之后 · 质量高的免费项也会走 reused 很快)
            free_sorted = sorted(free, key=lambda c: (-float(c.get('score') or 0.0),
                                                      str(c.get('url_hash') or '')))
            base_rank = len(paid_selected)
            free_ranked = [dict(c, budget_rank=base_rank + i + 1)
                           for i, c in enumerate(free_sorted)]
            selected = paid_selected + free_ranked
            logger.info(
                f"[{round_id}] Stage3 预算分流: 免费(DB 命中) {len(free)} 全保留 + "
                f"付费(全新) {len(paid)} → 选中 {len(paid_selected)}(Jina 上限 {cfg.budget_max})"
            )

        persist_url_budget(round_id, selected)
        # 全轮 ≤1.5h 只受【真爬 Jina 数】约束(免费项零耗时)。existing 在 budget_max<=0
        # 分支为空集 → 该分支 paid_count == len(selected)(旧行为全量都算真爬, 保守)。
        paid_count = sum(1 for c in selected if c['url_hash'] not in existing)
        est_min = (paid_count / JINA_THROUGHPUT_URL_PER_MIN) if selected else 0.0
        logger.info(
            f"[{round_id}] Stage3 URL 预算: {len(kept_urls)} candidate → 落库 {len(selected)}"
            f"(预计 Jina ≈ {est_min:.0f}min)"
        )

    return load_pending_budget(round_id)


def _count_stage3_paid_urls(budget_urls: List[Dict]) -> int:
    """Return the pending URLs that can still require a Jina request.

    Existing article-library hits are deliberately retained outside the URL
    budget because they are free citation reuse.  They must also stay outside
    the pre-stage cost estimate; otherwise a mature library can trip the
    monetary circuit before Stage 3 despite requiring no provider call.
    """
    if not budget_urls:
        return 0

    from db.research_url_budget_db import load_existing_article_url_hashes

    hashes = []
    for item in budget_urls:
        url_hash = item.get('url_hash')
        if not url_hash:
            normalized = item.get('normalized_url') or item.get('url') or ''
            if normalized:
                url_hash = compute_url_hash(normalized)
        if url_hash:
            hashes.append(url_hash)

    existing = load_existing_article_url_hashes(hashes)
    return sum(1 for url_hash in hashes if url_hash not in existing)


async def _stage3_heartbeat_ticker(round_id: str, interval_seconds: float = STAGE3_HEARTBEAT_INTERVAL_SECONDS):
    """[P1 返工] Stage3 伴飞 30s 心跳(时间驱动, 不依赖 URL 完成节奏): Jina 并发仅 2、
    单 URL 可慢(60s×2 attempts), 靠 _flush_progress(URL 完成节流)可能 >30s 无心跳。
    本 ticker 只 update_heartbeat(liveness) 【不】写 update_round_progress —— 避免在
    stage_3_crawl_done 终态写入后又把 stage 覆盖回 stage_3_crawl(要求6 终态不被覆盖)。
    DB 瞬断只 log 不抛。由 gather 收尾 cancel。

    [回归修] update_heartbeat 是同步阻塞 DB commit; 直接在本协程调会阻塞 event loop,
    而 crawl_one 持连接 await Jina 期间被阻塞就无法推进提交 → 心跳的 commit 与 crawl_one
    未提交事务在连接池/行锁上死锁。用 to_thread 卸到线程池(stage7 同款), event loop
    保持自由让 crawl_one 提交, 心跳 commit 随后成功。"""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await asyncio.to_thread(update_heartbeat, round_id)
        except Exception as e:
            logger.warning(f"[{round_id}] Stage 3 伴飞心跳失败(忽略继续): {e}")


async def stage3_crawl_articles(round_id: str, urls: List[Dict]):
    """
    Stage 3: 并发爬 Jina, url_hash 去重 + content_hash 跨域名去重

    [P1 返工 2026-07-16] urls 现在是 ensure_stage3_url_budget 选出的预算 pending 子集
    (质量排序 + 配额, 非 stage2 kept 全量); 每 URL 处理完更新 geo_research_url_budget
    的 status(耐久断点 · 续跑只处理 pending)。加 30s 伴飞心跳。

    P14.1 (2026-05-28 老板真实跑批反馈): 全路径心跳 + skip reason 分类
      - 旧版只在成功写文章路径才 % 5 flush · existing/jina_fail/short/oss_fail 4 个 return 路径不刷
      - 实测一轮 2 分钟无心跳直到全跑完 · 一刷新就完
      - 改成统一 try/finally · 每个 URL 处理完(任何 path)必经 _flush_progress
      - 节流: 每 5 个 processed OR 距上次 flush > 10s 触发 (大概率两条都满足时一次入网)
      - 6 类 reason 计数: crawled_new / reused_existing / skipped_short / skipped_jina_failed
                          / skipped_oss_failed / failed_unknown
      - progress_json 暴露 6 类 + processed + total · 让 live-status / RunningMonitorView 显示分类

    - 同 url_hash 已在 articles 表 → 不重爬, 只插 citation (reused_existing)
    - 新爬 → upload OSS raw + 算 content_hash 看跨 url 转载, 写 articles + citation (crawled_new)
    """
    semaphore = asyncio.Semaphore(JINA_CONCURRENCY)
    counters: Dict[str, int] = {
        'crawled_new': 0,
        # P14.2 C1 (老板): 跨行业 dup · 复用别行业的 article 内容字段 · 新插本行业 article 行
        # is_duplicate=true · primary_article_id 指向首篇 · 不重跑 Jina (省成本) · 也不算 reused_existing
        'crawled_dup': 0,
        'reused_existing': 0,
        'skipped_short': 0,
        'skipped_jina_failed': 0,
        'skipped_oss_failed': 0,
        'failed_unknown': 0,
    }
    # P14.1 C3+ (老板复核): Jina/OSS/unknown 失败时保留前 5 条 error 样本
    # 让 admin 在 live-status 看到具体失败原因 (timeout / 429 / connect / 其他)
    # 不打爆 DB · 限 5 条 · 进 progress_json + summary_json.stage_3
    ERROR_SAMPLE_LIMIT = 5
    error_samples: List[Dict] = []
    jina_request_count = 0
    total_urls = len(urls)
    _progress_lock = asyncio.Lock()
    _last_flush_ts = time.monotonic()
    FLUSH_EVERY_N = 5
    FLUSH_EVERY_S = 10.0

    def _classify_error(err: Optional[str]) -> str:
        """粗分 error 类型 · 给前端 chip 用 · 避免长 stack trace 噪声"""
        if not err:
            return 'unknown'
        e = err.lower()
        if 'timeout' in e or 'timed out' in e:
            return 'timeout'
        if 'connect' in e or 'connection' in e or 'dns' in e or 'resolve' in e:
            return 'connect'
        if '429' in e or 'rate' in e and 'limit' in e:
            return 'rate_limit'
        if '401' in e or '403' in e or 'unauthorized' in e or 'forbidden' in e:
            return 'auth'
        if '5' in e[:3] or 'server' in e:
            return 'server_5xx'
        return 'other'

    def _record_error_sample(url: str, reason: str, error_msg: Optional[str]) -> None:
        """在 ERROR_SAMPLE_LIMIT 内追加一条失败样本 · 满了直接 drop"""
        if len(error_samples) >= ERROR_SAMPLE_LIMIT:
            return
        error_samples.append({
            'url': (url or '')[:200],
            'reason': reason,
            'error_type': _classify_error(error_msg),
            'error_message': (error_msg or '')[:200],
        })

    def _progress_payload(processed: int) -> Dict:
        # progress 字段: 既保留旧 'crawled' / 'skipped' / 'total' 让老前端兼容 · 也新增 7 类计数
        # P14.2 C1: 'crawled' 兼容字段 = crawled_new + crawled_dup (新文档总数 · 跨行业 dup 算"新")
        skipped_total = (counters['reused_existing']
                         + counters['skipped_short']
                         + counters['skipped_jina_failed']
                         + counters['skipped_oss_failed']
                         + counters['failed_unknown'])
        return {
            'crawled': counters['crawled_new'] + counters['crawled_dup'],
            'skipped': skipped_total,
            'total': total_urls,
            'processed': processed,
            'reasons': dict(counters),
            'error_samples': list(error_samples),
        }

    async def _flush_progress(*, force: bool = False) -> None:
        nonlocal _last_flush_ts
        async with _progress_lock:
            processed = sum(counters.values())
            now = time.monotonic()
            if not force:
                # 节流: 每 FLUSH_EVERY_N 条 或 距上次 flush > FLUSH_EVERY_S 秒
                if processed % FLUSH_EVERY_N != 0 and (now - _last_flush_ts) < FLUSH_EVERY_S:
                    return
            _last_flush_ts = now
            try:
                update_heartbeat(round_id)
                update_round_progress(
                    round_id,
                    stage='stage_3_crawl',
                    progress=_progress_payload(processed),
                )
            except Exception:
                pass  # 心跳/进度失败不阻塞抓取

    # 入口 force flush · 确保 UI 立即从 stage_2_prefilter_done 切到 stage_3_crawl
    await _flush_progress(force=True)

    def _budget_status_for_reason(r: Optional[str]) -> str:
        """crawl reason → budget 表终态。所有处理过的 URL 移出 pending(幂等续跑不重爬)。"""
        if r in ('crawled_new', 'crawled_dup', 'reused_existing'):
            return 'done'
        if r in ('skipped_short', 'skipped_jina_failed', 'skipped_oss_failed'):
            return 'skipped'
        return 'failed'  # failed_unknown / None

    async def crawl_one(url_item: Dict) -> None:
        nonlocal jina_request_count
        async with semaphore:
            _raise_if_round_cancelled(round_id)
            reason: Optional[str] = None
            normalized = url_item['normalized_url']
            url_hash = compute_url_hash(normalized)
            current_industry = url_item.get('industry_name') or ''
            last_fetch_record: Optional[Dict] = None
            try:
                # P14.2 C1 (老板业务口径改正): 文章去重从"全局 URL"改"行业内 URL"
                # 同行业内 (url_hash, primary_industry) 已有 → reused_existing
                # 跨行业 (url_hash 别处有,本行业无) → crawled_dup (复制内容 · 新 article 行)
                # 全无 → 真爬 Jina
                conn = get_connection()
                try:
                    cur = conn.cursor()
                    # === 1a. 本行业查 ===
                    cur.execute(
                        """
                        SELECT id FROM geo_research_articles
                         WHERE url_hash = %s AND primary_industry = %s
                         LIMIT 1
                        """,
                        (url_hash, current_industry),
                    )
                    existing_same_ind = cur.fetchone()
                    if existing_same_ind:
                        # 不重爬, 只补一条 citation (reused_existing)
                        cur.execute(
                            """
                            INSERT INTO geo_research_article_citations
                                (article_id, round_id, industry_id, prompt_id,
                                 raw_id, rank_in_response, platform, cited_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                            ON CONFLICT DO NOTHING
                            """,
                            (
                                existing_same_ind['id'],
                                round_id,
                                url_item.get('industry_id'),
                                url_item.get('prompt_id'),
                                url_item.get('raw_id'),
                                url_item.get('rank_in_response'),
                                url_item.get('platform') or 'doubao',
                            ),
                        )
                        if cur.rowcount == 1:
                            cur.execute(
                                """
                                UPDATE geo_research_articles
                                   SET total_citation_count = total_citation_count + 1,
                                       last_seen_at = NOW()
                                 WHERE id = %s
                                """,
                                (existing_same_ind['id'],),
                            )
                        conn.commit()
                        reason = 'reused_existing'
                        return

                    # === 1b. 跨行业查 (别的 primary_industry 有该 url_hash) ===
                    # 老板规格 A: 复制 14 字段 · 重写 7 字段 · 不复制 5 字段(权限/审核状态污染)
                    cur.execute(
                        """
                        SELECT id, url, url_hash, domain, title,
                               oss_key_raw, oss_key_cleaned,
                               raw_char_count, cleaned_char_count, content_hash,
                               domain_tier, content_type, inline_cleaned_content,
                               clean_status, review_status, corpus_grade,
                               canonical_body_hash, body_hash_algorithm,
                               content_cluster_id, body_boundary_version,
                               label_provenance_version
                          FROM geo_research_articles
                         WHERE url_hash = %s
                         ORDER BY id ASC
                         LIMIT 1
                        """,
                        (url_hash,),
                    )
                    existing_other_ind = cur.fetchone()
                    if existing_other_ind:
                        # 复制内容字段 + 重写身份字段 + 不复制权限/审核字段(留 NULL)
                        cur.execute(
                            """
                            INSERT INTO geo_research_articles
                                (url, url_hash, domain, title,
                                 oss_key_raw, oss_key_cleaned,
                                 raw_char_count, cleaned_char_count, content_hash,
                                 domain_tier, content_type, inline_cleaned_content,
                                 clean_status, review_status,
                                 corpus_grade, canonical_body_hash, body_hash_algorithm,
                                 content_cluster_id, body_boundary_version,
                                 label_provenance_version,
                                 primary_industry,
                                 is_duplicate, primary_article_id,
                                 total_citation_count,
                                 first_seen_round_id,
                                 last_seen_at, fetched_at)
                            VALUES (%s, %s, %s, %s,
                                    %s, %s,
                                    %s, %s, %s,
                                    %s, %s, %s,
                                    %s, %s,
                                    %s, %s, %s, %s, %s, %s,
                                    %s,
                                    TRUE, %s,
                                    0,
                                    %s,
                                    NOW(), NOW())
                            ON CONFLICT DO NOTHING
                            RETURNING id
                            """,
                            (
                                existing_other_ind['url'],
                                existing_other_ind['url_hash'],
                                existing_other_ind['domain'],
                                existing_other_ind['title'],
                                existing_other_ind['oss_key_raw'],
                                existing_other_ind['oss_key_cleaned'],
                                existing_other_ind['raw_char_count'],
                                existing_other_ind['cleaned_char_count'],
                                existing_other_ind['content_hash'],
                                existing_other_ind['domain_tier'],
                                existing_other_ind['content_type'],
                                existing_other_ind['inline_cleaned_content'],
                                existing_other_ind['clean_status'],
                                existing_other_ind['review_status'],
                                existing_other_ind['corpus_grade'],
                                existing_other_ind['canonical_body_hash'],
                                existing_other_ind['body_hash_algorithm'],
                                existing_other_ind['content_cluster_id'],
                                existing_other_ind['body_boundary_version'],
                                existing_other_ind['label_provenance_version'],
                                current_industry,
                                existing_other_ind['id'],  # primary_article_id 指向首篇
                                round_id,
                            ),
                        )
                        dup_row = cur.fetchone()
                        if dup_row:
                            new_article_id = dup_row['id']
                        else:
                            # ON CONFLICT 命中: 可能是
                            #   (a) migration 前 url_hash UNIQUE 仍生效 → 行为退化 reused_existing
                            #   (b) migration 后 (url_hash, primary_industry) UNIQUE 命中 → 并发竞态
                            # 两种情况都用本行业的 id (若 a 没有则用首篇 id 兜底)
                            cur.execute(
                                """
                                SELECT id FROM geo_research_articles
                                 WHERE url_hash = %s AND primary_industry = %s
                                 LIMIT 1
                                """,
                                (url_hash, current_industry),
                            )
                            _row = cur.fetchone()
                            new_article_id = _row['id'] if _row else existing_other_ind['id']
                        # citation 指向新 article_id (或兜底首篇 id)
                        cur.execute(
                            """
                            INSERT INTO geo_research_article_citations
                                (article_id, round_id, industry_id, prompt_id,
                                 raw_id, rank_in_response, platform, cited_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                            ON CONFLICT DO NOTHING
                            """,
                            (
                                new_article_id,
                                round_id,
                                url_item.get('industry_id'),
                                url_item.get('prompt_id'),
                                url_item.get('raw_id'),
                                url_item.get('rank_in_response'),
                                url_item.get('platform') or 'doubao',
                            ),
                        )
                        if cur.rowcount == 1:
                            cur.execute(
                                """
                                UPDATE geo_research_articles
                                   SET total_citation_count = total_citation_count + 1,
                                       last_seen_at = NOW()
                                 WHERE id = %s
                                """,
                                (new_article_id,),
                            )
                        conn.commit()
                        reason = 'crawled_dup'
                        return
                finally:
                    conn.close()

                # 2. 真爬 Jina (P14.3 C3: 1 次 retry · 同 semaphore 槽内 · sleep 不释放并发)
                attempts_log: List[str] = []
                result: Dict = {}
                for attempt in range(1, JINA_RETRY_MAX_ATTEMPTS + 1):
                    result = await crawl_article(normalized)
                    jina_request_count += int(result.get('actual_attempts') or 1)
                    try:
                        from db.geo_research_fetch_db import record_fetch_result
                        _fetch_rows = record_fetch_result(
                            source_url=url_item.get('url') or normalized,
                            normalized_url=normalized,
                            url_hash=url_hash,
                            result=result,
                            round_id=round_id,
                            outer_attempt=attempt,
                        )
                        if _fetch_rows:
                            last_fetch_record = _fetch_rows[-1]
                    except Exception as fetch_exc:
                        # Migration absence must not lose the crawl, but the round's
                        # data-health gate will mark lineage incomplete.
                        logger.warning(
                            "[%s] Jina fetch event write failed url=%s: %s",
                            round_id, normalized, str(fetch_exc)[:200],
                        )
                    if result.get('ok'):
                        break
                    attempts_log.append(
                        f"attempt {attempt}: {str(result.get('error') or '?')[:80]}"
                    )
                    if attempt < JINA_RETRY_MAX_ATTEMPTS:
                        await asyncio.sleep(JINA_RETRY_DELAY_SECONDS)
                if not result.get('ok'):
                    reason = 'skipped_jina_failed'
                    # 保留每次 attempt 摘要 · 让 admin 看到是不是只第 1 次抖第 2 次也失败
                    final_err = '; '.join(attempts_log) if attempts_log else str(result.get('error') or '')
                    _record_error_sample(normalized, reason, final_err)
                    return
                if (result.get('char_count') or 0) < MIN_ARTICLE_CHARS:
                    reason = 'skipped_short'
                    return

                content = result.get('content', '') or ''
                content_hash = compute_content_hash(content)

                # 3. 上传 raw markdown 到 OSS (失败时跳过不可清洗文章)
                domain = urlparse(normalized).netloc or ''
                ym = datetime.utcnow().strftime('%Y-%m')
                fetch_version_key = (
                    (last_fetch_record or {}).get('fetch_event_key') or uuid4().hex
                )
                oss_key_planned = generate_oss_key_for_article(
                    'raw', ym, domain, url_hash, fetch_version_key
                )
                upload_result = upload_markdown(oss_key_planned, content)
                if not upload_result.get('ok'):
                    logger.warning(
                        f"[{round_id}] raw OSS 上传失败, 跳过不可清洗文章 url={normalized}"
                    )
                    reason = 'skipped_oss_failed'
                    _record_error_sample(normalized, reason, str(upload_result.get('error') or ''))
                    return
                oss_key = oss_key_planned

                # 4. 写 geo_research_articles + citation
                conn = get_connection()
                try:
                    cur = conn.cursor()

                    # 跨 url 转载识别: content_hash 已存在 → 标记 is_duplicate 指向首发
                    cur.execute(
                        """
                        SELECT id FROM geo_research_articles
                         WHERE content_hash = %s
                         ORDER BY id ASC
                         LIMIT 1
                        """,
                        (content_hash,),
                    )
                    primary = cur.fetchone()
                    is_dup = primary is not None
                    primary_id = primary['id'] if primary else None

                    article_content_type = get_content_type(
                        domain, normalized, result.get('char_count') or 0
                    )
                    cur.execute(
                        """
                        INSERT INTO geo_research_articles
                            (url, url_hash, domain, title, primary_industry,
                             oss_key_raw, raw_char_count, content_hash,
                             is_duplicate, primary_article_id,
                             domain_tier, content_type, clean_status, review_status,
                             corpus_grade, body_boundary_version, label_provenance_version,
                             first_seen_round_id, last_seen_at, fetched_at)
                        VALUES (%s, %s, %s, %s, %s,
                                %s, %s, %s,
                                %s, %s,
                                %s, %s, 'pending', 'crawled',
                                'JC1', %s, %s,
                                %s, NOW(), NOW())
                        ON CONFLICT DO NOTHING
                        RETURNING id
                        """,
                        # P14.2 C1 部署兼容: ON CONFLICT 不指定列名 · 老/新 UNIQUE 都能兜
                        # · migration 后 (url_hash, primary_industry) 不冲突 → 真插入 RETURNING id
                        # · migration 前 (url_hash) 仍在 → 别行业同 url_hash 来时 conflict → 退化 reused_existing
                        (
                            normalized,
                            url_hash,
                            domain,
                            (result.get('title') or '')[:500],
                            url_item.get('industry_name'),
                            oss_key,
                            result.get('char_count') or 0,
                            content_hash,
                            is_dup,
                            primary_id,
                            get_domain_tier(domain),
                            article_content_type,
                            RAW_BOUNDARY_VERSION,
                            CORPUS_CONTRACT_VERSION,
                            round_id,
                        ),
                    )
                    row = cur.fetchone()
                    if row:
                        article_id = row['id']
                    else:
                        # 并发竞态: 别的协程已先一步插入本行业的同 url_hash 行
                        cur.execute(
                            """
                            SELECT id FROM geo_research_articles
                             WHERE url_hash = %s AND primary_industry = %s
                            """,
                            (url_hash, url_item.get('industry_name') or ''),
                        )
                        article_id = cur.fetchone()['id']

                    # P14.1 C2: 加 raw_id + rank_in_response · industry_id/prompt_id 用 stage2 实值
                    cur.execute(
                        """
                        INSERT INTO geo_research_article_citations
                            (article_id, round_id, industry_id, prompt_id,
                             raw_id, rank_in_response, platform, cited_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT DO NOTHING
                        """,
                        (
                            article_id,
                            round_id,
                            url_item.get('industry_id'),
                            url_item.get('prompt_id'),
                            url_item.get('raw_id'),
                            url_item.get('rank_in_response'),
                            url_item.get('platform') or 'doubao',
                        ),
                    )
                    # P14.1 C2: 仅当 INSERT 真插了新行才 +1 计数 + 刷 last_seen_at
                    # (新爬路径 article INSERT ON CONFLICT 也可能命中 url_hash UNIQUE 跳过
                    # · 此时 article_id 是别协程已插的 · citation 是这轮第一次 · 仍应 +1)
                    if cur.rowcount == 1:
                        cur.execute(
                            """
                            UPDATE geo_research_articles
                               SET total_citation_count = total_citation_count + 1,
                                   last_seen_at = NOW()
                             WHERE id = %s
                            """,
                            (article_id,),
                        )
                    conn.commit()
                    if last_fetch_record:
                        try:
                            from db.geo_research_fetch_db import link_fetch_to_article
                            link_fetch_to_article(
                                int(last_fetch_record['id']),
                                article_id=int(article_id),
                                raw_object_key=oss_key,
                            )
                        except Exception as fetch_link_exc:
                            logger.warning(
                                "[%s] Jina fetch/article link failed fetch=%s article=%s: %s",
                                round_id, last_fetch_record.get('id'), article_id,
                                str(fetch_link_exc)[:200],
                            )
                    reason = 'crawled_new'
                finally:
                    conn.close()
            except asyncio.CancelledError:
                # 任务被取消 · 不计数(非"失败")· 让 gather 收到异常
                raise
            except RoundCancelledError:
                # [P1 返工] round 被管理员取消: re-raise 让 gather 收集 → stage 中止,
                # 归档 cancelled。不当失败(reason 保持 None)→ budget 保持 pending →
                # 续跑重爬未完成 URL(耐久断点正确性)。
                raise
            except Exception as e:
                logger.error(
                    f"[{round_id}] stage_3 crawl_one 未分类异常 url={normalized}: "
                    f"{type(e).__name__}: {e}"
                )
                reason = 'failed_unknown'
                _record_error_sample(
                    normalized, reason, f"{type(e).__name__}: {e}",
                )
            finally:
                if reason is not None:
                    counters[reason] += 1
                    # [P1 返工] 耐久断点: 仅在真处理完(reason 非空)时更新预算 status
                    # (取消/中途异常 reason=None → 不标终态 → 保持 pending 续跑重爬)。
                    # 终态守护在 db 层 WHERE status<>'done' 防迟到/重复覆盖。
                    # best-effort: 更新失败只 log 不阻塞(最坏续跑重爬, 幂等安全)。
                    try:
                        from db.research_url_budget_db import mark_budget_status
                        mark_budget_status(
                            round_id, url_hash, current_industry,
                            _budget_status_for_reason(reason), reason,
                        )
                    except Exception as e:
                        logger.warning(
                            f"[{round_id}] stage_3 预算 status 更新失败 url={normalized}: {e}"
                        )
                # 每个 URL 处理完都尝试 flush · 内部节流防写 DB 风暴
                await _flush_progress(force=False)

    # [P1 返工] 伴飞 30s 心跳: gather 期间即使全部 worker 卡在 Jina 也保持 liveness
    _hb_ticker = asyncio.create_task(_stage3_heartbeat_ticker(round_id))
    try:
        results = await asyncio.gather(
            *[crawl_one(u) for u in urls],
            return_exceptions=True,
        )
    finally:
        _hb_ticker.cancel()
        try:
            await _hb_ticker
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
    # [P1 返工] 取消优先原样抛: RoundCancelledError 不经 _raise_gather_exceptions
    # 包成 RuntimeError(否则 run_round 误归 failed 而非 cancelled)。
    for r in results:
        if isinstance(r, RoundCancelledError):
            raise r
    _raise_gather_exceptions(results, 'stage_3_crawl')
    # 终点 force flush · 防节流漏掉最后几条
    await _flush_progress(force=True)

    # 写 cost_log: jina 只按真实发出的 Jina 请求计费; DB/URL 去重缓存命中不计费
    _log_cost(round_id, 'jina', jina_request_count, COST_PER_JINA_CRAWL)

    processed_final = sum(counters.values())
    update_round_progress(
        round_id,
        stage='stage_3_crawl_done',
        progress=_progress_payload(processed_final),
    )
    # P14.1 C3 (2026-05-28 老板复核 round_20260528_232257 暴露):
    #   progress_json 会被 stage 4/5/7/8 后续 update_round_progress 覆盖 ·
    #   完成后历史视图只剩 {notified: 0} · stage 3 reasons 丢
    # 修:把 stage 3 摘要 jsonb merge 进 summary_json.stage_3 · 永久保留
    #   _build_summary 在 stage 7/8 算时会读回 stage_3 子字段合并 · 不被 overwrite
    import json as _json
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE geo_research_round
                   SET summary_json = COALESCE(summary_json, '{}'::jsonb) || %s::jsonb
                 WHERE round_id = %s
                """,
                (
                    _json.dumps({
                        'stage_3': {
                            'total_urls': total_urls,
                            'processed': processed_final,
                            'jina_requests': jina_request_count,
                            'error_samples': list(error_samples),
                            **counters,
                        }
                    }, ensure_ascii=False),
                    round_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        # 镜像失败不阻塞 stage 3 · 只 warning
        logger.warning(
            f"[{round_id}] stage 3 reasons 镜像 summary_json 失败 (容错降级): "
            f"{type(e).__name__}: {e}"
        )
    logger.info(
        f"[{round_id}] Stage 3 完成 processed={processed_final}/{total_urls} "
        f"reasons={counters}"
    )


# ==================== Stage 4 ====================

CLEAN_CONCURRENCY = 10
SCORE_CONCURRENCY = 10
ARTICLE_MIN_CHARS_FOR_REVIEW = 3000
# C-3 backlog · LLM 评分累计失败 N 次后 review_status 软着 auto_skipped 防卡死
SCORE_ATTEMPTS_MAX = 3


def _article_min_chars_for_review() -> int:
    return _get_int_config('article_min_chars_for_review', ARTICLE_MIN_CHARS_FOR_REVIEW)


def _clean_attempts_max() -> int:
    return _get_int_config('clean_attempts_max', 3)


async def stage4_clean_articles(round_id: str):
    """Phase 9 (2026-05-25): 规则清洗替代 LLM 清洗

    改动:
    - 不再调 qwen-turbo (cleaner.py)
    - 改用 services.research_monitor.clean_articles_rule.clean_markdown (纯 stdlib re)
    - 0 LLM 成本, 0 API 调用失败重试需求 (规则确定性)
    - 清洗后 char_count 用 text_length 算 (去 markdown 标记 + 链接 url)
    - 仍上传 cleaned 版到 OSS 给文章库用
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        # 规则清洗确定性, 不需 clean_attempts 限制, 也不区分 pending/failed
        cur.execute(
            """
            SELECT id, url, url_hash, oss_key_raw, raw_char_count, domain
              FROM geo_research_articles
             WHERE first_seen_round_id = %s
               AND clean_status = 'pending'
               AND oss_key_raw IS NOT NULL
            """,
            (round_id,),
        )
        articles = cur.fetchall()
    finally:
        conn.close()

    if not articles:
        update_round_progress(
            round_id, stage='stage_4_clean_done',
            progress={'cleaned': 0, 'failed': 0},
        )
        return

    semaphore = asyncio.Semaphore(CLEAN_CONCURRENCY)
    cleaned_count = 0
    failed_count = 0
    # [2026-07-16 心跳普查] stage4 原全程零心跳(仅收尾一次): 数千文章 × OSS
    # 下载/上传, 正常大轮 >10 分钟即会被僵尸 sweep 心跳分支(10min)误收活轮。
    # 补 30s 节流时间型心跳: 每篇文章两条完成路径(OSS 读失败 / 正常收尾)各
    # 打一次 tick, 节流后 ≈ 每 30s 一次 DB 写。单 event loop 内读写, 无锁安全。
    _s4_last_heartbeat = time.monotonic()

    def _stage4_heartbeat_tick() -> None:
        nonlocal _s4_last_heartbeat
        now = time.monotonic()
        if now - _s4_last_heartbeat < HEARTBEAT_TICK_INTERVAL_SECONDS:
            return
        _s4_last_heartbeat = now
        try:
            update_heartbeat(round_id)
        except Exception as e:
            logger.warning(f"[{round_id}] Stage 4 心跳失败(忽略继续): {e}")

    async def clean_one(art):
        nonlocal cleaned_count, failed_count
        async with semaphore:
            _raise_if_round_cancelled(round_id)
            raw = download_markdown(art['oss_key_raw'])
            if not raw:
                # OSS 读不到 raw, 极少见 (网络/权限), 计 failed 不重试
                c = get_connection()
                try:
                    cc = c.cursor()
                    cc.execute(
                        """
                        UPDATE geo_research_articles
                           SET clean_status='failed',
                               clean_attempts=clean_attempts+1,
                               last_cleaned_at=NOW()
                         WHERE id=%s
                        """,
                        (art['id'],),
                    )
                    c.commit()
                finally:
                    c.close()
                failed_count += 1
                _stage4_heartbeat_tick()
                return

            # 规则清洗 (纯 CPU · 不抛异常)
            try:
                cleaned_content = _rule_clean_markdown(raw)
                char_count = _rule_text_length(cleaned_content)
            except Exception as e:
                # 规则清洗理论上不抛异常, 但兜底
                logger.warning(f"[{round_id}] 规则清洗异常 article_id={art['id']}: {e}")
                cleaned_content = raw
                char_count = art.get('raw_char_count') or 0

            # 上传清洗后 markdown 到 OSS
            ym = datetime.utcnow().strftime('%Y-%m')
            oss_key_cleaned = generate_oss_key_for_article(
                'cleaned', ym, art['domain'], art['url_hash'],
                f"article-{art['id']}-{uuid4().hex}",
            )
            up_result = upload_markdown(oss_key_cleaned, cleaned_content)
            corpus = fingerprint_clean_body(cleaned_content)

            c = get_connection()
            try:
                cc = c.cursor()
                if up_result.get('ok'):
                    # OSS 成功 → clean_status='cleaned' · 真实 cleaned key
                    cc.execute(
                        """
                        UPDATE geo_research_articles
                           SET oss_key_cleaned=%s,
                               cleaned_char_count=%s,
                               clean_status='cleaned',
                               clean_model='rule_v1',
                               corpus_grade=%s,
                               canonical_body_hash=%s,
                               body_hash_algorithm=%s,
                               content_cluster_id=%s,
                               body_boundary_version=%s,
                               label_provenance_version=%s,
                               clean_attempts=clean_attempts+1,
                               last_cleaned_at=NOW()
                         WHERE id=%s
                        """,
                        (
                            oss_key_cleaned, char_count,
                            corpus.corpus_grade, corpus.canonical_body_hash or None,
                            corpus.body_hash_algorithm, corpus.content_cluster_id or None,
                            corpus.body_boundary_version, corpus.label_provenance_version,
                            art['id'],
                        ),
                    )
                    cleaned_count += 1
                else:
                    # Phase 9 (2026-05-25) · OSS 上传失败不再骗人:
                    # 不写 oss_key_cleaned, 不标 cleaned (避免下游读到 raw 当 cleaned)
                    # stage 5 不会把 failed 标进 in_library
                    cc.execute(
                        """
                        UPDATE geo_research_articles
                           SET clean_status='failed',
                               clean_attempts=clean_attempts+1,
                               last_cleaned_at=NOW()
                         WHERE id=%s
                        """,
                        (art['id'],),
                    )
                    failed_count += 1
                    logger.warning(
                        f"[{round_id}] cleaned OSS 上传失败 article_id={art['id']} 标 failed"
                    )
                c.commit()
            finally:
                c.close()
            if up_result.get('ok') and corpus.canonical_body_hash:
                try:
                    from db.geo_research_fetch_db import link_clean_body_to_latest_fetch
                    link_clean_body_to_latest_fetch(
                        article_id=int(art['id']),
                        canonical_body_hash=corpus.canonical_body_hash,
                        body_object_key=oss_key_cleaned,
                    )
                except Exception as fetch_link_exc:
                    logger.warning(
                        "[%s] cleaned body/fetch link failed article=%s: %s",
                        round_id, art['id'], str(fetch_link_exc)[:200],
                    )
            _stage4_heartbeat_tick()

    # Phase 9: 规则清洗 0 LLM 成本 · 删 _assert_round_budget_or_raise
    results = await asyncio.gather(
        *[clean_one(a) for a in articles],
        return_exceptions=True,
    )
    _raise_gather_exceptions(results, 'stage_4_clean')

    # Phase 9: 0 成本 · 删 _log_cost('qwen-turbo-clean', ...)

    update_round_progress(
        round_id, stage='stage_4_clean_done',
        progress={
            'cleaned': cleaned_count,
            'failed': failed_count,
            'total': len(articles),
            'method': 'rule_v1',
        },
    )
    update_heartbeat(round_id)
    logger.info(
        f"[{round_id}] Stage 4 完成 (规则清洗) {cleaned_count} OSS 失败 {failed_count}"
    )



# ==================== Stage 4.5 ====================

INTENT_CLASSIFY_CONCURRENCY = 4
# DeepSeek 文章意图分类成本估算。这里只用于 research_monitor 自己的 round 预算账本,
# 真实模型调用仍由 llm_call_tracker 记录 token 级明细。
INTENT_CLASSIFY_COST_PER_ARTICLE = _safe_float_env('INTENT_CLASSIFY_COST_PER_ARTICLE', 0.001, minimum=0.0)
INTENT_ERROR_SAMPLE_LIMIT = 5


async def stage45_classify_article_intents(round_id: str):
    """P15: 清洗后给文章打 8 类写作意图标签。

    分类使用 DeepSeek,失败软降级为 NULL,不阻塞 stage 5 入库。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, url, domain, title, oss_key_cleaned, inline_cleaned_content
              FROM geo_research_articles
             WHERE first_seen_round_id = %s
               AND clean_status = 'cleaned'
               AND intent_type IS NULL
            """,
            (round_id,),
        )
        articles = cur.fetchall()
    finally:
        conn.close()

    total = len(articles)
    def _mirror_intent_summary(payload: Dict):
        # progress_json 会被后续 stage 5/7/8 覆盖,所以镜像到 summary_json.stage_4_5 供历史和 live-status 兜底展示。
        import json as _json
        try:
            c = get_connection()
            try:
                cc = c.cursor()
                cc.execute(
                    """
                    UPDATE geo_research_round
                       SET summary_json = COALESCE(summary_json, '{}'::jsonb) || %s::jsonb
                     WHERE round_id = %s
                    """,
                    (_json.dumps({'stage_4_5': payload}, ensure_ascii=False), round_id),
                )
                c.commit()
            finally:
                c.close()
        except Exception as e:
            logger.warning(f"[{round_id}] intent 分类摘要镜像 summary_json 失败 (容错降级): {e}")

    if total == 0:
        final_progress = {'classified': 0, 'failed': 0, 'total': 0, 'model': DEFAULT_INTENT_MODEL, 'error_samples': []}
        update_round_progress(
            round_id,
            stage='stage_4_5_intent_done',
            progress=final_progress,
        )
        _mirror_intent_summary(final_progress)
        return

    _assert_round_budget_or_raise(
        round_id,
        'stage_4_5',
        estimated_increment_yuan=total * INTENT_CLASSIFY_COST_PER_ARTICLE,
    )

    update_round_progress(
        round_id,
        stage='stage_4_5_intent_classify',
        progress={'classified': 0, 'failed': 0, 'total': total},
    )
    semaphore = asyncio.Semaphore(INTENT_CLASSIFY_CONCURRENCY)
    classified_count = 0
    failed_count = 0
    intent_request_count = 0
    intent_error_samples: List[Dict] = []
    last_progress_flush = time.monotonic()

    def _add_intent_error_sample(article: Dict, error_type: str, error_message: str):
        if len(intent_error_samples) >= INTENT_ERROR_SAMPLE_LIMIT:
            return
        intent_error_samples.append({
            'article_id': article.get('id'),
            'title': (article.get('title') or '')[:120],
            'url': article.get('url') or '',
            'error_type': error_type,
            'error_message': str(error_message)[:240],
        })

    def _intent_progress_payload():
        return {
            'classified': classified_count,
            'failed': failed_count,
            'total': total,
            'model': DEFAULT_INTENT_MODEL,
            'error_samples': intent_error_samples,
        }

    def _flush_intent_progress(force: bool = False):
        nonlocal last_progress_flush
        processed = classified_count + failed_count
        now = time.monotonic()
        if not force and processed % 5 != 0 and (now - last_progress_flush) < 10:
            return
        update_round_progress(
            round_id,
            stage='stage_4_5_intent_classify',
            progress=_intent_progress_payload(),
        )
        update_heartbeat(round_id)
        last_progress_flush = now

    async def classify_one(art):
        nonlocal classified_count, failed_count, intent_request_count
        async with semaphore:
            _raise_if_round_cancelled(round_id)
            content = ''
            if art.get('oss_key_cleaned'):
                try:
                    content = download_markdown(art['oss_key_cleaned']) or ''
                except Exception as e:
                    logger.warning(f"[{round_id}] intent 分类读取 cleaned OSS 失败 article_id={art['id']}: {e}")
            if not content and art.get('inline_cleaned_content'):
                content = art['inline_cleaned_content'] or ''
            if not content:
                failed_count += 1
                _add_intent_error_sample(art, 'empty_content', '清洗后内容为空,无法分类')
                _flush_intent_progress()
                return

            try:
                intent_request_count += 1
                result = await classify_article_intent(
                    title=art.get('title') or '',
                    url=art.get('url') or '',
                    domain=art.get('domain') or '',
                    content=content,
                )
            except Exception as e:
                failed_count += 1
                _add_intent_error_sample(art, type(e).__name__, str(e))
                logger.warning(
                    f"[{round_id}] intent 分类失败 article_id={art['id']}: {type(e).__name__}: {e}"
                )
                _flush_intent_progress()
                return

            c = get_connection()
            try:
                cc = c.cursor()
                cc.execute(
                    """
                    UPDATE geo_research_articles
                       SET intent_type = %s,
                           intent_confidence = %s,
                           intent_reason = %s,
                           intent_model = %s,
                           intent_classified_at = NOW()
                     WHERE id = %s
                    """,
                    (
                        result.intent_type,
                        result.confidence,
                        result.reason,
                        result.model,
                        art['id'],
                    ),
                )
                c.commit()
                classified_count += 1
                _flush_intent_progress()
            finally:
                c.close()

    results = await asyncio.gather(
        *[classify_one(a) for a in articles],
        return_exceptions=True,
    )
    for r in results:
        if isinstance(r, Exception):
            failed_count += 1
            if len(intent_error_samples) < INTENT_ERROR_SAMPLE_LIMIT:
                intent_error_samples.append({
                    'article_id': None,
                    'title': '',
                    'url': '',
                    'error_type': type(r).__name__,
                    'error_message': str(r)[:240],
                })
            logger.warning(f"[{round_id}] intent 分类子任务异常: {type(r).__name__}: {r}")
            _flush_intent_progress()

    _flush_intent_progress(force=True)
    final_progress = _intent_progress_payload()
    update_round_progress(
        round_id,
        stage='stage_4_5_intent_done',
        progress=final_progress,
    )
    _mirror_intent_summary(final_progress)
    if intent_request_count:
        _log_cost(round_id, 'intent_classify', intent_request_count, INTENT_CLASSIFY_COST_PER_ARTICLE)
    update_heartbeat(round_id)
    logger.info(f"[{round_id}] Stage 4.5 完成 (文章意图分类) classified={classified_count} failed={failed_count}/{total}")


# ==================== Stage 5 ====================

def stage5_filter_by_char_count(round_id: str):
    """字数过滤 (纯 SQL · Phase 9 + P09-d549a0fe):
       cleaned_char_count >= min_chars(配置可调 · db seed=100) → in_library
       cleaned_char_count <  min_chars                          → auto_skipped
    (旧"pending_review"语义已废 · 审核工作流删除)"""
    # Phase 9 (2026-05-25): 不再"top 30 推审核", 所有清洗成 + 字数足 + 非 dup 的直接入文章库
    # 闭集:
    #   'crawled'      = 抓完未清洗(中间态, 不显示在文章库)
    #   'in_library'   = 入文章库(Phase 9 新值 · 文章库默认可见)
    #   'auto_skipped' = 过短或重复(管理员 toggle "显示已跳过" 才看到)
    #   'pending_review'/'approved'/'rejected' = 老审核流程历史值 (P05f migration 处理)
    #   'imported_to_reference' = 管理员加入参考文章库(P06)
    # 字数门槛 article_min_chars_for_review 从 geo_research_config 读, 管理员后台可调
    min_chars = _article_min_chars_for_review()
    conn = get_connection()
    try:
        cur = conn.cursor()
        # 1. is_duplicate = TRUE → auto_skipped (转载默认不进文章库)
        cur.execute(
            """
            UPDATE geo_research_articles
               SET review_status='auto_skipped'
             WHERE first_seen_round_id = %s
               AND is_duplicate = TRUE
               AND review_status = 'crawled'
            """,
            (round_id,),
        )
        dup_skipped = cur.rowcount

        # 2. cleaned + < 门槛字数 → auto_skipped
        cur.execute(
            """
            UPDATE geo_research_articles
               SET review_status='auto_skipped'
             WHERE first_seen_round_id = %s
               AND clean_status = 'cleaned'
               AND cleaned_char_count < %s
               AND review_status = 'crawled'
            """,
            (round_id, min_chars),
        )
        skipped_short = cur.rowcount

        # 3. cleaned + >= 门槛字数 + 非 dup → in_library (Phase 9 新值 · 不再用 crawled 兜底)
        cur.execute(
            """
            UPDATE geo_research_articles
               SET review_status='in_library'
             WHERE first_seen_round_id = %s
               AND clean_status = 'cleaned'
               AND cleaned_char_count >= %s
               AND is_duplicate = FALSE
               AND review_status = 'crawled'
            """,
            (round_id, min_chars),
        )
        in_library_count = cur.rowcount
        conn.commit()
    finally:
        conn.close()

    update_round_progress(
        round_id, stage='stage_5_filter_done',
        progress={
            'in_library': in_library_count,
            'auto_skipped_short': skipped_short,
            'auto_skipped_dup': dup_skipped,
            'min_chars_threshold': min_chars,
        },
    )
    logger.info(
        f"[{round_id}] Stage 5 完成 入文章库 {in_library_count} "
        f"自动跳过 {skipped_short + dup_skipped} (短 {skipped_short} + 重 {dup_skipped}) "
        f"· 字数门槛 {min_chars}"
    )


# ==================== Stage 6 (Phase 9 P05 已彻底删) ====================
# 原 stage6_score_articles (LLM 评分 qwen-plus · ¥0.001/篇) 已物理删。
# scorer.py 已删, 老 tests 的 stage6 case 在 P05g 测试清理一并删除。
# 文章库排序用 total_citation_count (citation 数, 见 _refresh_total_citation_count)


# ==================== Stage 6.5 (隐式 · stage 7 之前) ====================

def _refresh_total_citation_count() -> int:
    """
    全表重算 article.total_citation_count = COUNT(citations 关联).
    每轮 stage 7 之前跑一次, 保证审核界面 top N 排序数据准确.
    返回更新行数.
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE geo_research_articles a
               SET total_citation_count = COALESCE(sub.cnt, 0)
              FROM (
                  SELECT article_id, COUNT(*) AS cnt
                    FROM geo_research_article_citations
                   GROUP BY article_id
              ) sub
             WHERE a.id = sub.article_id
               AND a.total_citation_count IS DISTINCT FROM sub.cnt
            """
        )
        affected = cur.rowcount
        conn.commit()
        return affected
    finally:
        conn.close()


# ==================== Stage 7 ====================

async def stage7_aggregate_stats(round_id: str):
    """
    Stage 7: 把本轮新写入 geo_research_raw 的引用数据聚合到 geo_engine_stats。
    + 之前自动跑一次 _refresh_total_citation_count() 让 article 排序数据最新。

    复用 services.placement_service.PlacementService.aggregate_research_stats()。
    传入本轮涉及的行业名称列表, 只重算这些行业 (避免全表 DELETE)。

    推荐统计是飞轮输出层。聚合失败必须抛 Stage7AggregateError,
    由 run_round 标 failed_resumable, 避免 round completed 但后台候选为空。
    """
    try:
        # 6.5: 重算 article.total_citation_count (审核界面 top N 排序依赖)
        try:
            # [2026-07-16 心跳普查] 重 SQL 卸线程期间伴飞 30s 心跳(防 >10min 无心跳被 sweep 误收)
            refreshed = await _to_thread_with_heartbeat(round_id, _refresh_total_citation_count)
            logger.info(f"[{round_id}] 重算 total_citation_count 影响 {refreshed} 行")
        except Exception as e:
            logger.warning(f"[{round_id}] 重算 total_citation_count 失败 (不阻塞): {e}")
        snapshot = get_round_snapshot(round_id) or {}
        industries = snapshot.get('industries') or []
        industries_names = [
            (i.get('name') or '').strip()
            for i in industries
            if isinstance(i, dict) and i.get('name')
        ]

        if not industries_names:
            update_round_progress(
                round_id, stage='stage_7_aggregate_done',
                progress={'industries_count': 0, 'rows_written': 0,
                          'note': 'snapshot 中无 industries, 跳过聚合'},
            )
            update_heartbeat(round_id)
            logger.info(f"[{round_id}] Stage 7 跳过 (snapshot 无 industries)")
            return

        # PlacementService.aggregate_research_stats 是同步 IO 阻塞函数,
        # 用 to_thread 卸到线程池防阻塞 event loop。
        # [2026-07-16 心跳普查] 聚合可达分钟~十分钟级, 伴飞 30s 心跳。
        rows_written = await _to_thread_with_heartbeat(
            round_id, PlacementService().aggregate_research_stats, industries_names,
        )

        update_round_progress(
            round_id, stage='stage_7_aggregate_done',
            progress={
                'industries_count': len(industries_names),
                'rows_written': int(rows_written or 0),
            },
        )
        update_heartbeat(round_id)
        logger.info(
            f"[{round_id}] Stage 7 完成 行业数={len(industries_names)} 写入={rows_written}"
        )

        # [B3-1] 聚合成功后按各行业引用份额生成引擎权重候选(shadow · 只入 staging 表,需人工审核才生效)。
        # best-effort:任何失败都不得把一次成功的聚合变成 failed round。
        try:
            from db.engine_weight_candidates_db import generate_engine_weight_candidates
            n_cand = await _to_thread_with_heartbeat(
                round_id, generate_engine_weight_candidates, industries_names,
            )
            if n_cand:
                logger.info(f"[{round_id}] Stage 7 附带生成引擎权重候选 {n_cand} 条(待人工审核)")
        except Exception as cand_err:
            logger.warning(f"[{round_id}] 引擎权重候选生成跳过(不影响聚合): {cand_err}")
    except Exception as e:
        logger.error(f"[{round_id}] Stage 7 聚合失败: {e}")
        try:
            update_round_progress(
                round_id, stage='stage_7_aggregate_failed',
                progress={'error': str(e)[:500]},
            )
            update_heartbeat(round_id)
        except Exception as progress_error:
            logger.warning(f"[{round_id}] Stage 7 失败状态写入也失败: {progress_error}")
        raise Stage7AggregateError(str(e)[:500]) from e


# ==================== Stage 8 ====================

def stage8_notify_admins(round_id: str, summary: Dict):
    """
    Stage 8: 保留旧阶段检查点；真正站内信由 update_round_complete 在终态
    事务内写 notification_outbox，禁止在终态提交前先发“已完成”。

    Phase 9 (2026-05-25) · 文案改"新文章入库"语义:
        summary 字段: in_library_count / auto_skipped_count / total_cost_yuan
        老字段 pending_review_count 兼容 (旧 summary 里可能还有)

    此处不再调用旧 utils.notify，避免 worker 崩溃时留下假完成通知。
    """
    try:
        try:
            update_round_progress(
                round_id, stage='stage_8_notify_done',
                progress={'notification_deferred_to_terminal_outbox': True},
            )
            update_heartbeat(round_id)
        except Exception:
            pass

        logger.info("[%s] Stage 8 完成；通知将在终态事务内入 outbox", round_id)
    except Exception as e:
        logger.warning(f"[{round_id}] Stage 8 通知失败 (不影响 round 状态): {e}")
        try:
            update_round_progress(
                round_id, stage='stage_8_notify_failed',
                progress={'error': str(e)[:500]},
            )
        except Exception:
            pass


# ==================== Summary 辅助 ====================

def _build_summary(round_id: str) -> Dict:
    """
    从 DB 查本轮统计数据, 组装 summary_json。

    Phase 9 (2026-05-25) · 加 in_library_count 字段 · 旧字段保留兼容前端老 mock
    """
    summary: Dict = {
        'total_articles_seen': 0,
        'in_library_count': 0,           # Phase 9: 新文章库主指标
        'auto_skipped_count': 0,         # 短文 + 重复(总)
        'auto_skipped_short_count': 0,   # 仅短文
        'auto_skipped_dup_count': 0,     # 仅重复
        'clean_failed_count': 0,         # 清洗失败(OSS 上传等)
        'pending_review_count': 0,       # 兼容老 summary · 新跑批应=0
        'total_cost_yuan': 0.0,
        'finished_at': datetime.now().isoformat(),
    }
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT
                    COUNT(*) AS total_seen,
                    SUM(CASE WHEN review_status = 'in_library' THEN 1 ELSE 0 END) AS in_lib_cnt,
                    SUM(CASE WHEN review_status = 'auto_skipped' THEN 1 ELSE 0 END) AS skipped_cnt,
                    SUM(CASE WHEN review_status = 'auto_skipped' AND is_duplicate = TRUE THEN 1 ELSE 0 END) AS skipped_dup,
                    SUM(CASE WHEN review_status = 'auto_skipped' AND is_duplicate = FALSE THEN 1 ELSE 0 END) AS skipped_short,
                    SUM(CASE WHEN clean_status = 'failed' THEN 1 ELSE 0 END) AS clean_failed,
                    SUM(CASE WHEN review_status = 'pending_review' THEN 1 ELSE 0 END) AS pending_cnt
                  FROM geo_research_articles
                 WHERE first_seen_round_id = %s
                """,
                (round_id,),
            )
            row = cur.fetchone() or {}
            summary['total_articles_seen'] = int(row.get('total_seen') or 0)
            summary['in_library_count'] = int(row.get('in_lib_cnt') or 0)
            summary['auto_skipped_count'] = int(row.get('skipped_cnt') or 0)
            summary['auto_skipped_short_count'] = int(row.get('skipped_short') or 0)
            summary['auto_skipped_dup_count'] = int(row.get('skipped_dup') or 0)
            summary['clean_failed_count'] = int(row.get('clean_failed') or 0)
            summary['pending_review_count'] = int(row.get('pending_cnt') or 0)

            try:
                cur.execute(
                    """
                    SELECT COALESCE(SUM(amount_yuan), 0) AS total_cost
                      FROM geo_research_cost_log
                     WHERE round_id = %s
                    """,
                    (round_id,),
                )
                cost_row = cur.fetchone() or {}
                summary['total_cost_yuan'] = float(cost_row.get('total_cost') or 0)
            except Exception as e:
                # cost_log 表可能尚未挂入 ai 调用, 容错降级
                logger.debug(f"[{round_id}] cost_log 查询失败 (容错降级): {e}")
                summary['total_cost_yuan'] = 0.0

            # P14.1 C3: 把 stage 3 写入 summary_json.stage_3 的 reasons 子字段读回 ·
            # 合并到本次 _build_summary 输出 · 让 update_round_complete overwrite 时也保留
            try:
                cur.execute(
                    "SELECT summary_json FROM geo_research_round WHERE round_id = %s",
                    (round_id,),
                )
                row_existing = cur.fetchone() or {}
                existing_summary = row_existing.get('summary_json') or {}
                if isinstance(existing_summary, dict) and 'stage_3' in existing_summary:
                    summary['stage_3'] = existing_summary['stage_3']
            except Exception as e:
                logger.debug(f"[{round_id}] _build_summary 读 stage_3 子字段失败 (容错): {e}")
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"[{round_id}] _build_summary DB 查询失败: {e}")

    return summary


def _raise_if_no_round_articles(summary: Dict) -> None:
    """Fail closed before marking a research round completed with no articles.

    Resume can skip stage 3, so the empty URL guard there is not enough by
    itself.  A terminal completed round with zero article rows would suppress
    scheduler backfill and poison downstream source-signal rebuilds.
    """
    try:
        total_articles_seen = int(float((summary or {}).get('total_articles_seen') or 0))
    except (TypeError, ValueError):
        total_articles_seen = 0
    if total_articles_seen <= 0:
        raise ValueError('empty_citation_urls')


# ==================== Main: run_round ====================

ROUND_TIMEOUT_SECONDS = 4 * 3600  # 4 小时硬超时


_STAGE_ORDER = [
    'stage_1', 'stage_2', 'stage_3', 'stage_4', 'stage_4_5',
    'stage_5', 'stage_6', 'stage_7', 'stage_8',
]


def _should_skip(resume_from_stage: Optional[str], current_stage: str) -> bool:
    """续跑判断: resume_from_stage='stage_4' 时, stage_1-3 应 skip。"""
    if not resume_from_stage:
        return False
    try:
        resume_idx = _STAGE_ORDER.index(resume_from_stage)
        cur_idx = _STAGE_ORDER.index(current_stage)
        return cur_idx < resume_idx
    except ValueError:
        return False


def _flatten_plan(snapshot: Dict) -> List[Dict]:
    """把 snapshot 平铺为 stage1_ai_fetch_all 需要的 plan 结构。"""
    if snapshot.get('truncated'):
        raise ValueError('snapshot_truncated')
    industries = snapshot.get('industries') or []
    prompts_by_industry = snapshot.get('prompts_by_industry') or {}
    plan: List[Dict] = []

    for ind in industries:
        if not isinstance(ind, dict):
            continue
        ind_id = ind.get('id')
        ind_name = ind.get('name') or ''
        # snapshot 里 prompts key 是 str
        prompts = prompts_by_industry.get(str(ind_id)) or prompts_by_industry.get(ind_id) or []
        for p in prompts:
            if not isinstance(p, dict):
                continue
            try:
                ind_id_int = int(ind_id) if ind_id is not None else None
            except (TypeError, ValueError):
                ind_id_int = None
            plan.append({
                'industry_id': ind_id_int,
                'industry_name': ind_name,
                'prompt_id': p.get('id'),
                'prompt_text': p.get('text') or p.get('prompt_text') or '',
            })
    return plan


def _get_batch_id(round_id: str) -> str:
    """从 geo_research_round 读 batch_id (create_round_with_snapshot 已写入)。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT batch_id FROM geo_research_round WHERE round_id = %s",
            (round_id,),
        )
        row = cur.fetchone()
        if row and row.get('batch_id'):
            return row['batch_id']
    finally:
        conn.close()
    # fallback: 沿用 stage 1 写入 raw 时使用的 batch_{round_id} 格式
    return f"batch_{round_id}"


def _raise_gather_exceptions(results: List, stage: str) -> None:
    exceptions = [r for r in results if isinstance(r, Exception)]
    if not exceptions:
        return
    first = exceptions[0]
    raise RuntimeError(
        f"{stage} 有 {len(exceptions)} 个子任务异常, first={type(first).__name__}: {first}"
    )


def _raise_if_round_cancelled(round_id: str) -> None:
    try:
        status = get_round_status(round_id) or {}
    except Exception as e:
        logger.warning(f"[{round_id}] 读取取消状态失败, 继续由后续 DB/预算检查兜底: {e}")
        return
    if (status.get('status') or '').lower() == 'cancelled':
        raise RoundCancelledError(f"round {round_id} 已被管理员取消")


def _assert_round_budget_or_raise(
    round_id: str,
    before_stage: str,
    estimated_increment_yuan: float = 0.0,
) -> None:
    """
    A.5.4 单轮预算熔断 helper: 进入下一个有成本的 stage 前调一次。

    预算 ok → 静默返回。
    预算超支 → 抛 BudgetExhaustedError, 由 run_round 捕获归档为 failed_resumable。

    参数 before_stage 用于 log 定位是哪一步前被熔断 (例: 'stage_4')。
    """
    chk = check_round_budget(round_id)
    if not chk['ok']:
        logger.warning(
            f"[{round_id}] 单轮预算熔断 (在 {before_stage} 前): {chk['reason']}"
        )
        raise BudgetExhaustedError(
            level='round',
            spent=float(chk.get('spent') or 0),
            limit=float(chk.get('limit') or 0),
            reason=chk['reason'] or '单轮预算超支',
        )
    remaining = chk.get('remaining')
    if estimated_increment_yuan > 0 and remaining is not None and float(remaining) < estimated_increment_yuan:
        reason = (
            f"单轮预算剩余 ¥{float(remaining):.2f} 不足以启动 {before_stage} "
            f"(预计新增 ¥{estimated_increment_yuan:.2f})"
        )
        logger.warning(f"[{round_id}] {reason}")
        raise BudgetExhaustedError(
            level='round',
            spent=float(chk.get('spent') or 0),
            limit=float(chk.get('limit') or 0),
            reason=reason,
        )


async def _run_pipeline(round_id: str, snapshot: Dict, resume_from_stage: Optional[str]):
    """8 stage 流水线本体, 由 run_round 包 wait_for(timeout) 调度。

    A.5.4 单轮预算熔断: 在每个会触发外部成本的 stage 之前 (1/3/4/6/7) 检查累计成本,
    超支立即抛 BudgetExhaustedError。stage 5 / 8 是纯本地操作不收费, 不熔断。
    """
    plan = _flatten_plan(snapshot)
    if not plan:
        raise ValueError('empty_research_plan')
    batch_id = _get_batch_id(round_id)

    # Stage 1: AI 抓取 (4 平台, 有成本)
    _raise_if_round_cancelled(round_id)
    if not _should_skip(resume_from_stage, 'stage_1'):
        # Phase 9: 按 platform 拆单价预估 (豆包 ¥0.2/次,远高于其他 ¥0.05-0.08)
        _stage1_estimate = len(plan) * sum(COST_PER_AI_CALL_BY_PLATFORM.values())
        _assert_round_budget_or_raise(
            round_id,
            'stage_1',
            estimated_increment_yuan=_stage1_estimate,
        )
        await stage1_ai_fetch_all(round_id, plan)
        update_heartbeat(round_id)

    # Stage 2 (sync, 纯 SQL 提取 + 归一化, 无外部成本)
    # P14.1 C2: 传 plan 让 stage 2 反查 industry_id/prompt_id · 不再留 None
    _raise_if_round_cancelled(round_id)
    urls: List[Dict] = []
    if not _should_skip(resume_from_stage, 'stage_2'):
        urls = stage2_extract_and_prefilter_urls(round_id, batch_id, plan) or []
        update_heartbeat(round_id)
    else:
        # 续跑跳过 stage 2 的话也需要 stage 3 输入, 重新提取一次保证 urls 在
        urls = stage2_extract_and_prefilter_urls(round_id, batch_id, plan) or []

    # Stage 3: Jina 爬取 (有成本)
    # P14.4 C2 (老板复核 · 上线风险打分):预算预估必须乘 retry attempts
    # 之前 len(urls) * COST_PER_JINA_CRAWL 假设 1 attempt/URL
    # 但 P14.3 C3 加了 retry (默认 MAX_ATTEMPTS=2) · worst case 真请求 = len*MAX_ATTEMPTS
    # 否则 stage 3 进入前预算保护偏低 · 一旦 retry 全打满会超单轮预算才发现
    _raise_if_round_cancelled(round_id)
    if not _should_skip(resume_from_stage, 'stage_3'):
        if not urls:
            raise ValueError('empty_citation_urls')
        # [P1 返工] 质量排序 + 配额选出 URL 预算(持久化 · 幂等续跑), 不无差别抓全部。
        # 预算预估按【选中】数算(非 kept 全量), 与实际 Jina 抓取量对齐。
        budget_urls = ensure_stage3_url_budget(round_id, batch_id, urls)
        paid_budget_count = _count_stage3_paid_urls(budget_urls)
        _assert_round_budget_or_raise(
            round_id,
            'stage_3',
            estimated_increment_yuan=paid_budget_count * COST_PER_JINA_CRAWL * JINA_RETRY_MAX_ATTEMPTS,
        )
        if budget_urls:
            await stage3_crawl_articles(round_id, budget_urls)
        update_heartbeat(round_id)

    # Stage 4: 规则清洗 (Phase 9: 0 LLM 成本 · 无需 budget 预算)
    _raise_if_round_cancelled(round_id)
    if not _should_skip(resume_from_stage, 'stage_4'):
        await stage4_clean_articles(round_id)
        update_heartbeat(round_id)

    # Stage 4.5: DeepSeek 文章意图分类 (失败软降级, 不阻塞入库)
    _raise_if_round_cancelled(round_id)
    if not _should_skip(resume_from_stage, 'stage_4_5'):
        await stage45_classify_article_intents(round_id)
        update_heartbeat(round_id)

    # Stage 5 (sync, 纯 SQL 字数过滤, 无外部成本)
    _raise_if_round_cancelled(round_id)
    if not _should_skip(resume_from_stage, 'stage_5'):
        stage5_filter_by_char_count(round_id)
        update_heartbeat(round_id)

    # Stage 6 已废弃 (Phase 9): 文章库排序用 total_citation_count, 不再 LLM 评分

    # Stage 7: 推荐统计是飞轮输出层; 聚合失败由 run_round 标 failed_resumable
    _raise_if_round_cancelled(round_id)
    if not _should_skip(resume_from_stage, 'stage_7'):
        _assert_round_budget_or_raise(round_id, 'stage_7')
        await stage7_aggregate_stats(round_id)
        update_heartbeat(round_id)

    # Stage 8: 先 build summary 再通知 + 持久化 summary (本地通知, 无外部成本)
    _raise_if_round_cancelled(round_id)
    summary = _build_summary(round_id)
    _raise_if_no_round_articles(summary)
    if not _should_skip(resume_from_stage, 'stage_8'):
        stage8_notify_admins(round_id, summary)

    _raise_if_round_cancelled(round_id)
    update_round_complete(round_id, status='completed', summary=summary)
    # [P0-1 飞轮桥接补桥] round 完成后的下游影子层桥接由 run_round 在 wait_for 成功后 AWAIT 触发
    #   (见 _maybe_run_flywheel_bridge)。这里不做 fire-and-forget:两条 round 调用方
    #   (new_event_loop→run_until_complete→loop.close / asyncio.run)返回后立即拆 loop,
    #   create_task 会被取消而从不执行 → 必须在 run_round 里 await。


async def _maybe_run_flywheel_bridge(
    round_id: str,
    *,
    force_bridge: bool = False,
    force_answer_entity: bool = False,
) -> None:
    """[P0-1] flag-gated, AWAITED (not fire-and-forget) downstream shadow/candidate bridge.

    MUST be awaited from run_round: both round-completion callers spin up a throwaway
    event loop and tear it down right after run_round returns
    (api/research_monitor_round_api.py: new_event_loop → run_until_complete → loop.close;
    scheduler: asyncio.run), so a create_task would be cancelled before doing any work.
    The round is already DB-marked 'completed' (in _run_pipeline), so a slow or failed
    bridge never affects round status. Bounded by its own 30-min timeout; fully fail-soft.
    Flag flywheel_round_bridge default OFF → research behavior unchanged.

    force_bridge=True (paid self-serve rounds): run the bridge unconditionally even when
    the global flywheel_round_bridge flag is OFF — the agent already paid, the round must
    be visible. force_answer_entity threads through to the answer-entity stage. Both
    default False so cron/manual callers keep flag-gated behavior byte-for-byte.
    """
    try:
        from writing.feature_switches import is_feature_enabled
        if not is_feature_enabled("flywheel_round_bridge") and not force_bridge:
            return
        from services.research_monitor.flywheel_bridge import run_round_bridge
        await asyncio.wait_for(
            run_round_bridge(
                round_id,
                trigger_source="round_complete",
                dry_run=False,
                force_answer_entity=force_answer_entity,
            ),
            timeout=1800,  # 30 min hard cap; huge rounds paginate but won't hang the job forever
        )
    except Exception as _bridge_err:
        logger.warning(f"[{round_id}] flywheel bridge 失败(不影响调研已完成): {_bridge_err}")


async def run_round(
    round_id: str,
    snapshot: Dict,
    resume_from_stage: Optional[str] = None,
    *,
    force_bridge: bool = False,
    force_answer_entity: bool = False,
    skip_month_budget: bool = False,
) -> str:
    """
    按顺序跑 Stage 1→8, 4 小时硬超时, 异常归档为 round.status。

    返回 final_status: 'completed' / 'partial_success' / 'failed' / 'failed_resumable' / 'cancelled'

    snapshot 结构:
      {
        'industries': [{'id', 'name', 'slug'}, ...],
        'prompts_by_industry': {str(industry_id): [{'id', 'text', ...}, ...]},
        'snapshotted_at': iso 时间
      }

    resume_from_stage: None=从头跑; 'stage_3'/'stage_4'/... 表示从该 stage 续跑。
    """
    # === A.5.4 月度预算熔断 ===
    # 进入 try 之前先检查本月累计成本, 超 1000 元直接 cancelled, 不起新轮。
    # check_month_budget 读取失败时 fail-closed, 防配置/DB 故障时误放行外部调用。
    # skip_month_budget=True(付费自助轮): 代理已付费, 不受平台级月度熔断影响,
    #   整段完全跳过。cron/manual 走默认 False → 熔断照常生效, 行为字节不变。
    if not skip_month_budget:
        month_check = check_month_budget()
        if not month_check['ok']:
            logger.warning(
                f"[{round_id}] 月度预算熔断: {month_check['reason']}"
            )
            try:
                update_round_complete(
                    round_id,
                    status='cancelled',
                    summary={
                        'cancelled_reason': 'month_budget_exhausted',
                        'month_spent_yuan': float(month_check.get('spent') or 0),
                        'month_limit_yuan': float(month_check.get('limit') or 0),
                        'reason': month_check.get('reason') or '月度预算超支',
                        'finished_at': datetime.now().isoformat(),
                    },
                )
            except Exception as inner:
                logger.error(f"[{round_id}] 月度熔断收尾失败: {inner}")
            return 'cancelled'

    final_status = 'failed'
    try:
        await asyncio.wait_for(
            _run_pipeline(round_id, snapshot, resume_from_stage),
            timeout=ROUND_TIMEOUT_SECONDS,
        )
        # round 已在 _run_pipeline 末标 'completed'。此处 AWAIT 触发下游影子/候选层桥接:
        #   必须 await(非 create_task),否则调用方拆 loop 后任务被取消从不执行。
        #   自带超时 + fail-soft:桥接慢/失败绝不影响已 completed 的 round 状态。
        await _maybe_run_flywheel_bridge(
            round_id,
            force_bridge=force_bridge,
            force_answer_entity=force_answer_entity,
        )
        return 'completed'
    except RoundCancelledError as ce:
        logger.warning(f"[{round_id}] runner 在 stage 边界确认取消: {ce}")
        return 'cancelled'
    except ValueError as ve:
        reason = str(ve) or type(ve).__name__
        if reason in ('snapshot_truncated', 'empty_research_plan'):
            logger.error(f"[{round_id}] 跑批计划不可用 → failed: {reason}")
            try:
                update_round_complete(
                    round_id,
                    status='failed',
                    summary={
                        'reason': reason,
                        'finished_at': datetime.now().isoformat(),
                    },
                )
            except Exception as inner:
                logger.error(f"[{round_id}] invalid snapshot 收尾失败: {inner}")
            return 'failed'
        logger.error(f"[{round_id}] run_round ValueError → failed: {reason}")
        try:
            update_round_complete(
                round_id,
                status='failed',
                summary={
                    'reason': reason[:500],
                    'finished_at': datetime.now().isoformat(),
                },
            )
        except Exception as inner:
            logger.error(f"[{round_id}] ValueError 收尾失败: {inner}")
        return 'failed'
    except asyncio.TimeoutError:
        logger.error(f"[{round_id}] run_round 4h 硬超时, 标记 failed_resumable")
        final_status = 'failed_resumable'
        try:
            summary = _build_summary(round_id)
            summary['reason'] = '4h_hard_timeout'
            update_round_complete(round_id, status=final_status, summary=summary)
        except Exception as inner:
            logger.error(f"[{round_id}] timeout 收尾失败: {inner}")
        return final_status
    except BudgetExhaustedError as be:
        # A.5.4 单轮预算熔断: 归档为 failed_resumable, 后续可手工恢复或起新轮
        logger.error(
            f"[{round_id}] 单轮预算熔断 → failed_resumable: {be.reason}"
        )
        final_status = 'failed_resumable'
        try:
            summary = _build_summary(round_id)
            summary['reason'] = 'round_budget_exhausted'
            summary['budget_level'] = be.level
            summary['budget_spent_yuan'] = float(be.spent or 0)
            summary['budget_limit_yuan'] = float(be.limit or 0)
            summary['budget_message'] = be.reason
            update_round_complete(round_id, status=final_status, summary=summary)
        except Exception as inner:
            logger.error(f"[{round_id}] 单轮熔断收尾失败: {inner}")
        return final_status
    except CostLogWriteError as cle:
        # 外部调用已发生但成本日志没落库: 必须停跑, 防止后续预算检查低估花费。
        logger.error(f"[{round_id}] 成本日志写入失败 → failed_resumable: {cle}")
        final_status = 'failed_resumable'
        try:
            try:
                summary = _build_summary(round_id)
            except Exception as summary_error:
                logger.error(f"[{round_id}] cost_log 失败后 build_summary 也失败: {summary_error}")
                summary = {}
            summary['reason'] = 'cost_log_write_failed'
            summary['cost_log_error'] = str(cle)[:500]
            update_round_complete(round_id, status=final_status, summary=summary)
        except Exception as inner:
            logger.error(f"[{round_id}] cost_log 失败收尾失败: {inner}")
        return final_status
    except Stage7AggregateError as sae:
        logger.error(f"[{round_id}] Stage 7 推荐统计聚合失败 → failed_resumable: {sae}")
        final_status = 'failed_resumable'
        try:
            try:
                summary = _build_summary(round_id)
            except Exception as summary_error:
                logger.error(f"[{round_id}] stage7 失败后 build_summary 也失败: {summary_error}")
                summary = {}
            summary['reason'] = 'stage_7_aggregate_failed'
            summary['stage_7_error'] = str(sae)[:500]
            update_round_complete(round_id, status=final_status, summary=summary)
        except Exception as inner:
            logger.error(f"[{round_id}] stage7 失败收尾失败: {inner}")
        return final_status
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.warning(f"[{round_id}] run_round 被取消")
        final_status = 'cancelled'
        try:
            summary = _build_summary(round_id)
            summary['reason'] = 'cancelled'
            update_round_complete(round_id, status=final_status, summary=summary)
        except Exception as inner:
            logger.error(f"[{round_id}] cancel 收尾失败: {inner}")
        raise
    except Exception as e:
        # 区分 stage 1-3 (数据采集硬伤) vs stage 4-6 (内部加工, 部分成功容忍)
        # 通过 round 当前 current_stage 字段判断: 在 stage_1/2/3 阶段挂 → failed
        try:
            from services.research_monitor.round_state import get_round_status
            st = get_round_status(round_id) or {}
            cur_stage = (st.get('current_stage') or '').lower()
        except Exception:
            cur_stage = ''

        is_collection_phase = any(
            tag in cur_stage for tag in ('stage_1', 'stage_2', 'stage_3')
        )
        final_status = 'failed' if is_collection_phase else 'partial_success'

        logger.error(
            f"[{round_id}] run_round 异常 stage={cur_stage} → {final_status}: {e}"
        )
        try:
            summary = _build_summary(round_id)
            summary['reason'] = str(e)[:500]
            summary['failed_at_stage'] = cur_stage
            update_round_complete(round_id, status=final_status, summary=summary)
        except Exception as inner:
            logger.error(f"[{round_id}] failure 收尾失败: {inner}")
        return final_status


# ==================== 4h 超时自动续跑外壳 (2026-07-16) ====================

# 只有这个 reason 允许自动续跑; 预算熔断(round_budget_exhausted)/cost_log 失败/
# stage7 聚合失败等其他 failed_resumable 一律等人工(尊重 restart_recovery.py 老板旧拍板)。
AUTO_RESUME_REASON = '4h_hard_timeout'
# summary_json.auto_resume_history 只留最近 N 条防膨胀(truncate_large_jsonb 50KB 是兜底)
AUTO_RESUME_HISTORY_KEEP = 10


def _get_auto_resume_max() -> int:
    """round_auto_resume_max 允许 0(=等效关闭自动续跑); 负数/非法回落默认 3。

    不能复用 _get_int_config: 它对 ≤0 一律回落默认 → admin 配 0 想关闭,
    会被静默变回 3 继续续跑(判别测试当场抓到的真缺陷)。
    """
    v = _read_config_value('round_auto_resume_max', 3, int)
    return v if isinstance(v, int) and v >= 0 else 3


async def run_round_with_auto_resume(
    round_id: str,
    snapshot: Dict,
    resume_from_stage: Optional[str] = None,
    *,
    force_bridge: bool = False,
    force_answer_entity: bool = False,
    skip_month_budget: bool = False,
) -> str:
    """
    run_round 的 4h 硬超时自动续跑外壳(SPEC 2026-07-16 改动二)。

    只对 summary.reason=='4h_hard_timeout' 的 failed_resumable 自动重入续跑;
    其他 failed_resumable(round_budget_exhausted / cost_log_write_failed /
    stage_7_aggregate_failed)一律不自动续 — 尊重 restart_recovery.py:7 老板旧拍板
    的预算/风暴顾虑。重启僵尸 sweep(sweep_zombie_rounds)行为一字未动:
    重启场景仍然只标记不自动续, 本外壳只覆盖"进程活着、wait_for 超时归档"这一条路径。

    调用方约束(为什么两条路径都安全):
    - 重入全部发生在本协程 return 之前 = 调用方 event loop 存活期间
      (与 _maybe_run_flywheel_bridge 同一 await 约束, 见 run_round docstring)。
      scheduler 的 asyncio.run(...) 与 round_api _run_round_sync 的
      run_until_complete(...) 都等本协程跑完才拆 loop → throwaway loop 不是问题,
      故 SPEC §2.4 二选一取"两路都启用"。
    - 不新造触发面: 本外壳只是既有调用路径(cron leader trigger_research_round_sync /
      API BackgroundTask)内部的循环, 蓝绿双 cron 的 leader gate(cron_should_fire)
      零感知; 跨容器/跨路径防重叠靠 mark_round_resume_requested 既有
      advisory lock + 单活跃轮守卫, 抢占失败(已有活跃轮/被人抢先)→ 放弃并 log,
      不排队不重试(防风暴)。
    - auto_resume_count / auto_resume_history 通过 extra_summary 与抢占同一条
      UPDATE 原子落库(见 mark_round_resume_requested docstring); update_round_complete
      是 jsonb merge(||), 正常收尾不会抹掉计数 → 单条自动续跑链内上限不可绕过。
      已知口径([出口审核 LOW·同意保留·二轮覆审订正措辞]): 僵尸 sweep
      (restart_recovery.mark_zombie_failed_resumable, SPEC 红线一字不动)用
      jsonb_build_object 整体替换 summary → 计数被清。两条触达路径:
      (a) 进程崩溃后(心跳停更): 被 sweep 的轮 reason ≠ '4h_hard_timeout',
          不会自动复活, 重启链条必经 admin 人工点击;
      (b) 周期 15min sweep 的心跳分支(base 既有): 段内单点慢调用(如 240s
          超时 ×3 重试)造成 >10 分钟心跳空窗时, 可命中仍在跑的活轮并清计数;
          该段随后 4h 超时收尾会 merge 回 reason='4h_hard_timeout' → 同链
          count 归零后无人工点击即可再获 max 次续跑。
      故 round_auto_resume_max 的精确语义是"每条计数链的上限", 不是"每轮
      终身上限"; 极端情况兜底靠月度/单轮预算熔断(round_budget_exhausted
      不自动续)。彻底闭合需 restart_recovery 改 merge 写(红线, 需老板批)。

    config(geo_research_config · 每次续跑决策前现读, 改配置即时生效):
    - round_auto_resume_enabled 缺配置默认 false（灰度 fail-closed）
    - round_auto_resume_max     默认 3(达上限保持 failed_resumable 等人工,
      admin 页既有续跑按钮不受影响)
    """
    status = await run_round(
        round_id,
        snapshot,
        resume_from_stage=resume_from_stage,
        force_bridge=force_bridge,
        force_answer_entity=force_answer_entity,
        skip_month_budget=skip_month_budget,
    )

    while status == 'failed_resumable':
        if not _get_bool_config('round_auto_resume_enabled', False):
            break
        max_resumes = _get_auto_resume_max()

        try:
            st = get_round_status(round_id) or {}
        except Exception as e:
            logger.error(f"[{round_id}] 自动续跑读状态失败, 放弃: {e}")
            break
        # [出口审核修 · 复活缺陷] DB 权威 status 复核: run_round 返回值与 DB 终态可能
        # 分歧 — 4h 超时收尾若输给 admin cancel(update_round_complete 的 cancelled
        # 守卫使收尾 no-op), run_round 仍返回 'failed_resumable' 而 DB 已是 cancelled;
        # summary.reason 还残留着上一次超时写入的 '4h_hard_timeout'(jsonb merge 永不删键)。
        # 不复核就会把管理员刚 cancel 的轮自动抢回 pending。此处快速路径 + 下方
        # mark(allowed_statuses=('failed_resumable',)) WHERE 层原子挡, 双层防御。
        if (st.get('status') or '') != 'failed_resumable':
            logger.warning(
                f"[{round_id}] 自动续跑放弃(DB status={st.get('status')!r} ≠ failed_resumable, "
                f"疑似 admin cancel/其他路径已接管)"
            )
            break
        summary = st.get('summary_json')
        if not isinstance(summary, dict):
            summary = {}
        # 只认 4h 硬超时; 预算熔断等其他 failed_resumable 不自动续
        if (summary.get('reason') or '') != AUTO_RESUME_REASON:
            break
        try:
            count = int(summary.get('auto_resume_count') or 0)
        except (TypeError, ValueError):
            count = 0
        if count >= max_resumes:
            logger.warning(
                f"[{round_id}] 4h 超时自动续跑达上限 {max_resumes} 次, "
                f"保持 failed_resumable 等人工处理"
            )
            break

        progress = st.get('progress_json')
        if not isinstance(progress, dict):
            progress = {}
        # 抢占前先推断续跑起点(mark_round_resume_requested 会清 current_stage)
        resume_stage = infer_resume_from_stage(st.get('current_stage'))
        history = summary.get('auto_resume_history')
        if not isinstance(history, list):
            history = []
        history_entry = {
            'n': count + 1,
            'at': datetime.now().isoformat(),
            'resume_from_stage': resume_stage,
            'progress_before': {
                'processed': progress.get('processed'),
                'total': progress.get('total'),
                'stage': st.get('current_stage'),
            },
        }
        try:
            claimed = mark_round_resume_requested(
                round_id,
                requested_by=None,  # system 自动续跑, 无操作人
                extra_summary={
                    'auto_resume_count': count + 1,
                    'auto_resume_history': (history + [history_entry])[-AUTO_RESUME_HISTORY_KEEP:],
                },
                # [出口审核修 · 复活缺陷] 自动路径收窄抢占窗口: 只许 failed_resumable。
                # admin 人工按钮保留默认 ('failed_resumable','cancelled') 语义不变。
                # 上面的 status 快速复核有 TOCTOU 空窗(读后 admin 才 cancel),
                # 这里 WHERE 层原子生效才是真闸。
                allowed_statuses=('failed_resumable',),
            )
        except RoundAlreadyRunningError as e:
            # 防重叠: 已有活跃轮(别的 cron leader / admin 手动) → 放弃, 不排队不重试
            logger.warning(f"[{round_id}] 自动续跑放弃(已有活跃跑批): {e}")
            break
        except Exception as e:
            logger.error(f"[{round_id}] 自动续跑抢占异常, 放弃: {e}")
            break
        if not claimed:
            # 被人抢先(admin 点了续跑按钮)或状态已变(如 admin cancel) → 放弃
            logger.warning(f"[{round_id}] 自动续跑放弃(round 已被抢先处理或状态已变)")
            break

        # [出口审核修 · 孤儿 pending] 上一段被 4h 超时切断时, 在飞任务(≤4×N 个)
        # 已各自单独 commit 了 status='pending' 的 round_call 行, 而 CancelledError
        # 是 BaseException 不走失败落库 → 永久悬挂 'pending', 污染 admin 详情页
        # 终态展示(平台永远显示"在跑")。此刻本轮已被抢占锁定、上一段进程内任务
        # 均已被取消, pending 全是孤儿 → 幂等批量归档。best-effort: 清理失败不
        # 阻断续跑(仅展示口径问题, 成本台账按行计费不受 status 影响)。
        try:
            _conn = get_connection()
            try:
                _cur = _conn.cursor()
                _cur.execute(
                    """
                    UPDATE geo_research_round_call
                       SET status = 'failed',
                           error_message = 'superseded_by_auto_resume(4h_timeout 切断的在飞调用)',
                           finished_at = NOW()
                     WHERE round_id = %s
                       AND status = 'pending'
                    """,
                    (round_id,),
                )
                _orphans = _cur.rowcount
                _conn.commit()
            finally:
                _conn.close()
            if _orphans:
                logger.info(f"[{round_id}] 自动续跑清理孤儿 pending round_call {_orphans} 行")
        except Exception as e:
            logger.warning(f"[{round_id}] 孤儿 pending 清理失败(不阻断续跑): {e}")

        logger.warning(
            f"[{round_id}] 4h 硬超时自动续跑 第 {count + 1}/{max_resumes} 次, "
            f"resume_from={resume_stage}, 断点前进度 "
            f"processed={progress.get('processed')}/{progress.get('total')} "
            f"stage={st.get('current_stage')}"
        )
        status = await run_round(
            round_id,
            snapshot,
            resume_from_stage=resume_stage,
            force_bridge=force_bridge,
            force_answer_entity=force_answer_entity,
            skip_month_budget=skip_month_budget,
        )

    return status
