"""一键蒸馏选题 · 后台任务(WO-DISTILL-TIMEOUT-ASYNC-2026-08-05)

## 为什么改异步

同步版在生产上**成功率 0/5**,5 次全部精确顶到 42s 窗口上限。
放开窗口独立复测:真实耗时 **58.0s** > 窗口 42.0s ——
不是"偶尔慢",是在窗口内根本出不来。

原作者自己在 `topic_distiller.py` 里写了正解:

> 若生产上失败率偏高,正解是把蒸馏改成异步任务(和生产链一样),
> **而不是把这个数字调大 —— 调大只会调回 504**。

调大不是选项:nginx `proxy_read_timeout 60s` 是硬墙,58s 真实耗时 + 前置查询
+ 序列化,任何同步方案都贴着墙走 —— 今天能过明天就不能。

## 🔴 改异步最容易改坏的一件事:节流锁的释放时机

同步版把 in-flight 占位放在**请求的 finally** 里释放,那时请求刚好跑完蒸馏。
改异步后请求 2 秒就返回了 —— 占位若仍跟着请求释放,用户连点两下就是
**两次真调用、两次 130**。所以:

  · 进程内占位改由**后台任务**结束时释放(本模块 `_finish` 的 finally);
  · 再加一条 **DB 部分唯一索引**兜底(`uq_geo_douyin_distill_inflight`),
    WORKERS 被调回 4、或两个请求真并发时仍然挡得住。
  · 加超龄回收(`reap_stale_distill_tasks`):进程被杀时 finally 不执行,
    留下的 running 行会被唯一索引认成"在飞" → 那个客户永远蒸不了,且静默。

## 🔴 扣费语义一个字都没改

`charge_on_success` 原样搬进后台任务里包着 `distill_topics`:
成功走完才扣,LLM 挂 / JSON 坏 / 选题全被串味丢光 → 抛异常 → **一分不扣**。
**没有**改成"提交即扣"(工单红线)。提交时只做一次 `check_balance_only`,
那是纯读、不扣费,目的是让"余额不足"仍能在提交那一刻就告诉用户,
而不是让他等 60 秒才发现。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("GEO-Douyin-DistillTask")

# 终态:到这两个就停轮询
TERMINAL_STATES = ("succeeded", "failed")

# 阶段 → 给人看的话(元指令:工程术语全站翻人话,不出现 LLM/prompt/fewshot)
STAGE_LABELS: Dict[str, str] = {
    "queued": "排队中",
    "gathering": "正在读这个客户的资料",
    "distilling": "正在想选题",
    "done": "完成",
}

# 阶段占整条进度的区间。distilling 占大头是因为它真的占大头(58s 里 ~53s)。
STAGE_BANDS: Dict[str, tuple] = {
    "queued": (0, 5),
    "gathering": (5, 15),
    "distilling": (15, 97),
    "done": (100, 100),
}

# 生产实测:三次前置查询 ~2.1s,LLM ~58s。用于给前端一个不瞎编的倒计时。
GATHER_SECONDS_ESTIMATE = 3
DISTILL_SECONDS_ESTIMATE = 60

# 进程内在飞集合(第一道锁)。DB 部分唯一索引是第二道。
_RUNNING_DISTILLS: set = set()


def running_distill_count() -> int:
    """当前进程里在跑的蒸馏任务数(给自检/排查用)。"""
    return len(_RUNNING_DISTILLS)


def stale_after_seconds() -> float:
    """超龄回收阈值。跟着蒸馏预算走,那边调了这里自动跟上 —— 不写死数字。"""
    from services.geo_douyin.topic_distiller import DISTILL_LLM_TIMEOUT_S
    return float(DISTILL_LLM_TIMEOUT_S) + 60.0


def describe_distill_progress(task: Optional[Dict[str, Any]], *,
                              now: Optional[float] = None) -> Dict[str, Any]:
    """把一行任务记录翻成前端直接能渲染的进度。

    纯函数:输入靠参数进、结果靠返回值出,测试可以直接喂 dict 断行为 ——
    不用 mock 时钟,也不用"断言源码里含某个词"那种一改写法就假绿的锁。
    (同 task_progress.describe_task_progress 的理由。)
    """
    from datetime import datetime, timezone

    now_ts = float(now) if now is not None else datetime.now(timezone.utc).timestamp()
    if not task:
        # 任务行查不到 —— 可能是被回收了,或进程重启前的旧 id。
        # 🔴 必须给终态,不能给 queued:给 queued 前端会永远转圈。
        return {"state": "failed", "stage": "done", "stage_label": "没做成",
                "percent": 0, "eta_seconds": None, "elapsed_seconds": None,
                "active": False, "error_code": "task_not_found",
                "message": "这次的任务找不到了，重新蒸一次"}

    t = dict(task)
    state = str(t.get("status") or "pending")
    stage = str(t.get("stage") or "queued")

    started = _as_epoch(t.get("started_at")) or _as_epoch(t.get("created_at"))
    elapsed = int(now_ts - started) if started is not None else None

    if state == "succeeded":
        percent, eta = 100, 0
    elif state == "failed":
        percent, eta = _band_low(stage), None
    else:
        percent = _band_low(stage)
        eta = (DISTILL_SECONDS_ESTIMATE if stage == "distilling"
               else GATHER_SECONDS_ESTIMATE + DISTILL_SECONDS_ESTIMATE)
        if elapsed is not None and eta is not None:
            eta = max(0, eta - elapsed)

    return {
        "state": state if state in TERMINAL_STATES else ("running" if state == "running" else "queued"),
        "stage": stage,
        "stage_label": ("完成" if state == "succeeded" else
                        "没做成" if state == "failed" else
                        STAGE_LABELS.get(stage, "正在想选题")),
        "percent": percent,
        "eta_seconds": eta,
        "elapsed_seconds": elapsed,
        # active=True → 前端继续轮询
        "active": state not in TERMINAL_STATES,
        "error_code": str(t.get("error_code") or ""),
        "message": str(t.get("error_msg") or ""),
    }


def _band_low(stage: str) -> int:
    lo, hi = STAGE_BANDS.get(str(stage or ""), (15, 97))
    return 100 if stage == "done" else int(lo)


def _as_epoch(value: Any) -> Optional[float]:
    from datetime import datetime, timezone

    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def humanize_distill_failure(error_code: str) -> str:
    """失败原因翻人话。**只此一处** —— 同一事实不写两处(弱锁四型之一)。"""
    return {
        # 撞额度天花板:重试同一条 prompt 结构上无效,话术要给能改变结果的动作。
        "llm_truncated": "这次想得太多没收住；把核心词写短一点、或少选几条再试",
        "llm_unavailable": "写作服务暂时不可用，稍后再试",
        "llm_bad_json": "这次没蒸出可用的选题，再试一次",
        "llm_incomplete": "这次没蒸出可用的选题，再试一次",
        "all_contaminated": "这次没蒸出可用的选题，再试一次",
        "no_keywords": "这个客户还没有确认的关键词，先在报价里选词，或者自己填一个",
        "stale_reaped": "这次等太久没做完，重新蒸一次",
        "task_not_found": "这次的任务找不到了，重新蒸一次",
    }.get(str(error_code or ""), "这次没蒸出选题，再试一次")


async def run_distill_task(*, task_id: int, user_id: int, brand_id: int,
                           keywords: List[str], brand_name: str, city: str,
                           industry_key: str, want: int,
                           feature_code: str) -> None:
    """后台跑一次蒸馏,全程把状态写进任务行。**本函数不返回结果,结果落库。**

    🔴 扣费:`charge_on_success` 原样包着 `distill_topics` —— 与同步版逐字同义。
       成功走完才扣;任何失败路径抛异常 → 一分不扣。
    """
    from db import geo_douyin_db as ddb
    from middleware.billing import charge_on_success
    from services.geo_douyin.topic_distiller import distill_topics

    charged = False
    try:
        await asyncio.to_thread(ddb.update_distill_task, task_id,
                                status="running", stage="gathering", mark_started=True)
        async with charge_on_success(int(user_id), feature_code, brand_id=int(brand_id)):
            await asyncio.to_thread(ddb.update_distill_task, task_id, stage="distilling")
            result = await distill_topics(
                brand_id=brand_id, keywords=keywords, brand_name=brand_name,
                city=city, industry_key=industry_key, want=want)
            if not result.ok:
                # 🔴 抛在 with 体**内** = 这次不扣费。挪到外面就变成"失败了照样扣"。
                #    串味被全丢也走这里:宁可"没蒸出来、再试一次",
                #    也不给用户一条抄了别人品牌名的选题。
                await asyncio.to_thread(
                    ddb.update_distill_task, task_id, status="failed", stage="done",
                    error_code=result.error or "distill_failed",
                    error_msg=humanize_distill_failure(result.error),
                    result=result.to_dict(), mark_finished=True)
                raise _DistillFailed(result.error or "distill_failed")
            payload = result.to_dict()
            payload["keywords_used"] = list(keywords)

            # ── [WO_204 §1.2] 逐条落选题表 ────────────────────────────────
            # 🔴 放在 `with` **体内**是有意的:落不进去就不扣费。
            #    挪到 with 外面 = 「扣了 130、任务显示成功、列表里一条都没有」——
            #    而那种坏法从任务状态上完全看不出来(result jsonb 里明明有 topics)。
            #    代价是一次 LLM 白跑,但用户没被扣钱,重来一次就好;
            #    与上面「串味全丢也走失败路径」的取舍是同一条。
            # 🔴 city 取本次任务的入参(蒸馏是按「词 × 城市」跑的,一次一个城市);
            #    `confirmed_keyword_id` 留空 —— 蒸馏器只拿到关键词**字符串**,
            #    按串回查 id 是"同名即同物"的猜法(同一品牌的同一个词
            #    可以出现在多张报价里),猜错了交付就记到别的词头上。
            inserted = await asyncio.to_thread(
                ddb.insert_distilled_topics,
                brand_id=int(brand_id), created_by=int(user_id),
                distill_task_id=int(task_id),
                topics=[dict(t, city=city) for t in payload.get("topics") or []])
            payload["topics_persisted"] = int(inserted)
        # 走到这里 = with 正常退出 = 已扣费
        charged = True
        await asyncio.to_thread(ddb.update_distill_task, task_id,
                                status="succeeded", stage="done",
                                result=payload, charged=True, mark_finished=True)
    except _DistillFailed:
        pass  # 任务行已在上面标好失败,这里只是让 with 体异常退出以免扣费
    except Exception as e:  # noqa: BLE001
        # 余额不足(402)/ 价目缺失 / 落库失败都会到这里。任务行必须落终态,
        # 否则前端永远转圈,而且那行会被唯一索引认成"在飞",锁死这个客户。
        code, msg = _classify_runner_error(e)
        logger.warning("[douyin-distill] 后台任务失败 task=%s brand=%s: %s: %s",
                       task_id, brand_id, type(e).__name__, str(e)[:200])
        try:
            await asyncio.to_thread(
                ddb.update_distill_task, task_id, status="failed", stage="done",
                error_code=code, error_msg=msg, mark_finished=True)
        except Exception:  # noqa: BLE001
            logger.error("[douyin-distill] 落失败态也失败 task=%s(将由超龄回收兜底)", task_id)
    finally:
        # 🔴 进程内占位在**任务**结束时释放,不是请求结束时。
        #    跟着请求释放 = 用户连点两下就是两次 130(见模块 docstring)。
        _release_inflight(brand_id)
        if not charged:
            logger.info("[douyin-distill] task=%s 未扣费(失败或异常)", task_id)


class _DistillFailed(Exception):
    """业务判定失败 —— 用来让 charge_on_success 的 with 体异常退出(即不扣费)。"""


def _classify_runner_error(e: Exception) -> tuple:
    """把后台异常翻成 (error_code, 人话)。**不猜**:分不出来就给通用文案。"""
    from fastapi import HTTPException

    if isinstance(e, HTTPException):
        detail = e.detail if isinstance(e.detail, dict) else {}
        if e.status_code == 402:
            return ("insufficient_points",
                    str(detail.get("message") or "算力不足，充值后再试（这次没有扣费）"))
        return (str(detail.get("code") or "distill_failed"),
                str(detail.get("message") or "这次没蒸出选题，再试一次"))
    return ("pricing_unavailable" if "pricing" in str(e).lower() else "distill_failed",
            "这次没蒸出选题，再试一次")


# ── 进程内在飞占位(第一道锁;DB 部分唯一索引是第二道)──
_INFLIGHT_BRANDS: dict = {}


def try_acquire_inflight(brand_id: int) -> bool:
    """判定 + 占位。🔴 中间不许有 await —— 有了就等于没锁。"""
    import time

    now = time.monotonic()
    started = _INFLIGHT_BRANDS.get(int(brand_id))
    if started is not None:
        if now - started < stale_after_seconds():
            return False
        _INFLIGHT_BRANDS.pop(int(brand_id), None)   # 超龄自愈,别永久锁死
    _INFLIGHT_BRANDS[int(brand_id)] = now
    return True


def _release_inflight(brand_id: int) -> None:
    _INFLIGHT_BRANDS.pop(int(brand_id), None)


def release_inflight(brand_id: int) -> None:
    """给提交失败(任务还没起来)的路径用 —— 那时后台 finally 不会跑。"""
    _release_inflight(brand_id)


def dispatch_distill(**kwargs) -> "asyncio.Task":
    """把一次蒸馏丢到后台跑,立刻返回。

    🔴 这里**不碰任何计费** —— 扣费全在 run_distill_task 里,
       与同步版逐字同义。本函数只负责"在哪跑"(同 dispatch_production 的分工)。
    """
    async def _runner():
        try:
            return await run_distill_task(**kwargs)
        except Exception as e:  # noqa: BLE001
            # run_distill_task 自己有兜底 except,走到这里说明是它兜不住的
            logger.error("[douyin-distill] 后台任务异常 task=%s: %s: %s",
                         kwargs.get("task_id"), type(e).__name__, e)
            _release_inflight(kwargs.get("brand_id") or 0)
            return None

    task = asyncio.create_task(_runner())
    _RUNNING_DISTILLS.add(task)
    task.add_done_callback(_RUNNING_DISTILLS.discard)
    return task
