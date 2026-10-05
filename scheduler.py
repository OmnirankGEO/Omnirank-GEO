"""
定时任务调度器模块
使用APScheduler实现定时任务
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Callable, Any
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from services.sched_claim import sched_claim  # [FF5] per-job tick 级 claim

logger = logging.getLogger(__name__)

# 全局调度器实例
scheduler: Optional[BackgroundScheduler] = None


def init_scheduler() -> BackgroundScheduler:
    """初始化调度器"""
    global scheduler
    if scheduler is None:
        scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        logger.info("📅 定时任务调度器初始化完成")
    return scheduler


def start_scheduler():
    """启动调度器"""
    global scheduler
    # Deploy-CTO 2026-04-27: SCHEDULER_ENABLED=false 跳过 start
    # 用于 staging 容器避免和 active 容器重复跑 daily_monitoring/db_backup/order_sync 等
    import os as _os
    if _os.environ.get("SCHEDULER_ENABLED", "1").lower() in ("0", "false", "no"):
        logger.warning("✋ SCHEDULER_ENABLED=false · 跳过启动 (staging 模式)")
        return
    # [WORKERS=4 · SPEC §1/§2] 根调度器同受 cron 触发闸控制:
    # 仅 ROLE=cron 且选主成功的进程真正 start(web/backup 不触发根 cron)。
    # cron_gate 不可用 → 保守不 start(fail-closed · 宁可不跑不可 4× 双跑)。
    try:
        from services.cron_gate import cron_should_fire
        if not cron_should_fire():
            logger.info("[Scheduler-root] 非 cron leader · 跳过根调度器启动")
            return
    except Exception:
        return
    if scheduler and not scheduler.running:
        scheduler.start()
        logger.info("✅ 定时任务调度器已启动")


def shutdown_scheduler():
    """停止调度器"""
    global scheduler
    if scheduler and scheduler.running:
        scheduler.shutdown()
        logger.info("⏹️ 定时任务调度器已停止")


def add_job(
    func: Callable,
    trigger: str = "interval",
    job_id: Optional[str] = None,
    **trigger_args
) -> str:
    """
    添加定时任务
    
    Args:
        func: 要执行的函数
        trigger: 触发器类型 ('interval', 'cron', 'date')
        job_id: 任务ID
        **trigger_args: 触发器参数
            - interval: seconds, minutes, hours, days
            - cron: hour, minute, day_of_week 等
    
    Returns:
        job_id: 任务ID
    """
    global scheduler
    if scheduler is None:
        init_scheduler()

    # BackgroundScheduler 不能直接 await async 函数，用 asyncio.run() 包装
    import asyncio
    import inspect
    if inspect.iscoroutinefunction(func):
        _orig = func
        def _sync_wrapper(*args, **kwargs):
            try:
                return asyncio.run(_orig(*args, **kwargs))
            except RuntimeError as e:
                # 如果已有 event loop 在运行，fallback 到新建 loop
                if "already running" in str(e) or "this loop" in str(e):
                    loop = asyncio.new_event_loop()
                    try:
                        return loop.run_until_complete(_orig(*args, **kwargs))
                    finally:
                        loop.close()
                raise
        _sync_wrapper.__name__ = _orig.__name__
        func = _sync_wrapper

    job = scheduler.add_job(func, trigger=trigger, id=job_id, **trigger_args)
    logger.info(f"📌 添加定时任务: {job_id or job.id}")
    return job.id


def remove_job(job_id: str):
    """移除定时任务"""
    global scheduler
    if scheduler:
        scheduler.remove_job(job_id)
        logger.info(f"🗑️ 移除定时任务: {job_id}")


def get_jobs():
    """获取所有任务列表"""
    global scheduler
    if scheduler:
        return [{
            "id": job.id,
            "name": job.name,
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            "trigger": str(job.trigger)
        } for job in scheduler.get_jobs()]
    return []


# =====================================================
#  预定义的定时任务
# =====================================================

async def job_daily_monitoring():
    """
    每日监测任务 - 检查所有活跃订阅的关键词(消费一次扣一次模型)

    [CTO-15.23 2026-05-09 fix-scheduler-async-block 重写]
    反 INCIDENT-20260509-01/03 复演 · memory feedback_async_event_loop_block
    旧版 5 个 sync psycopg2 call 在 async fn 直调 · WORKERS=1 阻塞 event-loop · 累积 hang

    [CTO-15.23 2026-05-09 monitor-billing 集成]
    老板拍板:消费一次扣一次 · 用 charge_on_success(A 类完成才扣)
    - 数据源从 get_active_client_keywords → list_active_subscriptions(严格按 active 订阅)
    - 跑前 _filter_keywords_with_balance 过滤余额不足 → pause subscription
    - 历史实现跑完直接扣款并写订阅累计；现改为 durable freeze settlement

    修法:
      A. 5 个 sync DB call 全 wrap asyncio.to_thread
      B. Semaphore(8) 限并发 · 老板拍板 · DB pool maxconn=30 容得下
      C. asyncio.gather 跑每 client 内 keyword · 跨 client 串行(避免 task_id 共享 race)
      D. 进度埋点:每 100 keyword + 每 client done + 整个 job 完成
      E. charge_on_success 包 LLM 调用 · 失败不扣 · 成功扣 130 + 流水
    """
    from db.monitoring_db import (
        list_active_subscriptions,
        list_paused_subscriptions_for_resume,
        pause_keyword_monitor_subscription,
        resume_keyword_monitor_subscription,
        claim_subscription_for_today_with_settlement,
        release_subscription_claim,
        update_keyword_monitor_state,
        claim_monitoring_run_cell,
        create_monitoring_run_cells,
        create_monitoring_task,
        finish_monitoring_cell_error,
        mark_monitoring_cell_dispatched,
        refresh_monitoring_task_from_cells,
        save_monitoring_result,
        create_monitoring_keyword_settlements,
        record_monitoring_keyword_settlement_freeze,
        mark_monitoring_keyword_settlement_dispatched,
        settle_monitoring_keyword_reference,
        record_monitoring_subscription_charge_for_settlement,
        update_task_status,
        save_trend_stat,
        get_monitoring_config,
        DEFAULT_MONITORING_PLATFORMS,
    )
    from middleware.billing import (
        check_balance_only, commit_freeze, freeze_points, release_freeze,
    )
    from tools.monitoring.batch_monitor import PlatformAdapter, resolve_monitoring_query
    from services.monitoring_identity_review import is_pending_identity_result
    from datetime import datetime
    from fastapi import HTTPException

    start_ts = datetime.now()
    logger.info("🔍 开始执行每日监测任务...")

    # [audit P0-4 2026-06-10] 订阅计费主体锚 brands.owner_user_id(兜存量错位订阅):
    # prod 18/19 在跑订阅 user_id=1(admin 帮客户点的·admin 免扣)→ 白烧 4 引擎 + 漏收 + 假流水。
    # 写入侧已锚 owner(server.py enable/batch-enable);这里是 daily 防御层:余额恢复/预检/实扣
    # 三段统一按解析后主体(预检与实扣必须同主体,否则查 admin 余额、扣 owner 钱包)。
    def _load_brand_owner_map(brand_ids):
        ids = [int(b) for b in brand_ids if b]
        if not ids:
            return {}
        try:
            from db.connection import get_connection as _gc
            conn = _gc()
            try:
                cur = conn.cursor()
                cur.execute("SELECT id, owner_user_id FROM brands WHERE id = ANY(%s)", (ids,))
                return {r["id"]: r["owner_user_id"] for r in cur.fetchall() if r.get("owner_user_id")}
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"[P0-4] brand owner 批查失败,回落订阅原主体: {e}")
            return {}

    def _billing_uid_for_sub(sub, owner_map):
        """解析这条订阅**从谁的钱包**扣。

        [WO_MONITORING_PLATFORM_COVERED 2026-08-16 §2] 加了 platform 分支。
        🔴 这是**加法**:billing_mode 缺省 'brand_owner' 时,下面的 P0-4 覆盖
           **一字不改**(含那条 warning 日志)。2026-06-10 audit 特意把计费主体
           从"操作者"改成 brands.owner_user_id,防的是 C 端 token-only 场景下
           扣费落到错的人头上 —— 禁止用"运行时改回认 sub.user_id"实现平台承担,
           那等于把审计修复倒回去。

        🔴 platform 分支 **fail-closed**:平台账户没配 / 配成非正整数 / 解析失败
           → 抛 PlatformDirectUnavailable,由调用方跳过这条订阅。
           **绝不回落到品牌归属人** —— 「以为平台付、实际扣了服务商」正是本单
           要消灭的那个形态,回落等于把它换个地方重演一遍。
        """
        mode = (sub.get("billing_mode") or "brand_owner").strip()
        if mode == "platform":
            from services.commercial_service_routing import get_platform_direct_service_user_id
            uid = int(get_platform_direct_service_user_id())   # 未配置直接抛,不兜底
            logger.info(
                f"[platform-covered] sub#{sub.get('id')} 记平台账 → user_id={uid} "
                f"(brand={sub.get('brand_id')} · 不做 brand owner 覆盖)"
            )
            return uid

        owner = owner_map.get(sub.get("brand_id"))
        if owner and int(owner) != int(sub["user_id"]):
            logger.warning(
                f"[P0-4] sub#{sub.get('id')} 计费主体错位修正: sub.user_id={sub['user_id']} → brand owner={owner} (brand={sub.get('brand_id')})"
            )
            return int(owner)
        return sub["user_id"]

    try:
        # ==============================================================
        # 余额恢复:充值后自动恢复被 pause 的订阅(02:30 跑 · 这里是 03:00)
        # 简化设计:整合在 daily_monitoring 顶部 · 不另开 cron
        # ==============================================================
        try:
            paused_subs = await asyncio.to_thread(list_paused_subscriptions_for_resume)
            _paused_owner_map = await asyncio.to_thread(
                _load_brand_owner_map, {s.get("brand_id") for s in paused_subs}
            ) if paused_subs else {}
            resumed_count = 0
            for sub in paused_subs:
                try:
                    # [P0-4] 恢复预检也按计费主体(否则 admin 余额恒过 → 恢复后主链再 pause 振荡)
                    await check_balance_only(_billing_uid_for_sub(sub, _paused_owner_map), sub["feature_code"])
                    # 余额够 → 恢复
                    await asyncio.to_thread(resume_keyword_monitor_subscription, sub["id"])
                    await asyncio.to_thread(
                        update_keyword_monitor_state,
                        keyword_id=sub["keyword_id"],
                        is_monitored=True,
                        subscription_id=sub["id"],
                    )
                    resumed_count += 1
                except HTTPException:
                    # 还是不够 · 维持 paused
                    pass
                except Exception as e:
                    logger.warning(f"resume sub#{sub['id']} 失败: {e}")
            if resumed_count > 0:
                logger.info(f"  💰 充值恢复 {resumed_count}/{len(paused_subs)} 订阅")
        except Exception as e:
            logger.warning(f"resume_check 失败 · 不阻塞主流程: {e}")

        # ==============================================================
        # 拉 active 订阅(严格按 status='active' AND is_monitored=TRUE 过滤)
        # ==============================================================
        active_subs = await asyncio.to_thread(list_active_subscriptions)
        total = len(active_subs)

        if total == 0:
            logger.info("📭 无 active 监测订阅")
            return

        # [audit P0-4 2026-06-10] 批量解析计费主体(brand owner)· 预检与实扣全用同一主体
        _owner_map = await asyncio.to_thread(
            _load_brand_owner_map, {s.get("brand_id") for s in active_subs}
        )
        # 🔴 [WO_MONITORING_PLATFORM_COVERED 2026-08-16 §2] 逐条捕获,不许一条拖垮全场。
        #   platform 分支是 fail-closed(平台账户没配就抛),但**"对这条订阅 fail-closed"
        #   不等于"对所有人 fail-closed"** —— 这个循环原来裸跑,一条平台订阅配错
        #   会让当天**所有客户**的监测都不跑。那是把一个配置问题升级成全站事故。
        #   处置:跳过这一条(不解析 = 不预检 = 不扣费 = 不跑),其余照常。
        _billing_unresolved = []
        for sub in active_subs:
            try:
                sub["_billing_user_id"] = _billing_uid_for_sub(sub, _owner_map)
            except Exception as _e:      # noqa: BLE001
                sub["_billing_user_id"] = None
                _billing_unresolved.append(sub.get("id"))
                logger.error(
                    f"[platform-covered] sub#{sub.get('id')} 计费主体解析失败,本轮跳过该订阅: {_e}"
                )
        if _billing_unresolved:
            logger.error(
                f"[platform-covered] 本轮有 {len(_billing_unresolved)} 条订阅因计费主体解析失败被跳过: "
                f"{_billing_unresolved} —— 检查 PLATFORM_DIRECT_SERVICE_USER_ID 是否已配置"
            )
        active_subs = [s for s in active_subs if s.get("_billing_user_id")]

        # 每个订阅按品牌现行配置与购买权益取交集。必须在余额预检、claim、扣费
        # 和 provider 调用之前完成，避免仅配置不可采集表面时擅自回落到全局平台。
        _config_platform_cache = {}
        eligible_subs = []
        skipped_for_platform = 0
        for sub in active_subs:
            config_key = (sub.get("brand_id"), str(sub.get("quote_id") or "_global_"))
            if config_key not in _config_platform_cache:
                config = await asyncio.to_thread(
                    get_monitoring_config,
                    brand_id=sub.get("brand_id"),
                    client_id=config_key[1],
                )
                _config_platform_cache[config_key] = config.get(
                    "default_platforms", DEFAULT_MONITORING_PLATFORMS
                )
            platforms = PlatformAdapter.eligible_monitoring_platforms(
                _config_platform_cache[config_key],
                sub.get("entitlement_platforms") or [],
            )
            if not platforms:
                skipped_for_platform += 1
                logger.warning(
                    "skip subscription %s: no purchased and collectable monitoring platform",
                    sub.get("id"),
                )
                continue
            sub["_eligible_monitoring_platforms"] = platforms
            eligible_subs.append(sub)

        if skipped_for_platform:
            logger.warning(
                "daily monitoring skipped %s subscriptions with no eligible platform",
                skipped_for_platform,
            )
        if not eligible_subs:
            logger.info("📭 所有订阅均无可执行的已购监测平台")
            return

        # ==============================================================
        # 跑前预检:余额不足 → pause subscription · 不进 LLM 调用
        # ==============================================================
        runnable_subs = []
        paused_for_balance = 0
        for sub in eligible_subs:
            try:
                await check_balance_only(sub["_billing_user_id"], sub["feature_code"])
                runnable_subs.append(sub)
            except HTTPException as he:
                if getattr(he, "status_code", None) == 402:
                    # 余额不足 → pause subscription + is_monitored=FALSE
                    await asyncio.to_thread(pause_keyword_monitor_subscription, sub["id"], "insufficient_balance")
                    # 🔴 [WO_MONITORING_PLATFORM_COVERED 2026-08-16 §4 · N3] 通知计费主体。
                    #   病史:岱林 2026-08-11 因余额不足暂停,**静默 5 天** ——
                    #   这里当时只有一行 logger.warning,服务商不知道、客户更不知道。
                    #   这不是岱林一家,是所有服务商都会踩。
                    #   平台承担的订阅通知管理员,**不通知那个服务商**(他没付钱,
                    #   收到"你的余额不足"是错的,还会让他去充一笔本不该他出的钱)。
                    try:
                        from services.monitor_billing_notify import notify_monitor_paused_low_balance
                        await asyncio.to_thread(
                            notify_monitor_paused_low_balance,
                            subscription_id=sub["id"],
                            billing_user_id=sub["_billing_user_id"],
                            billing_mode=sub.get("billing_mode") or "brand_owner",
                            brand_id=sub.get("brand_id"),
                            brand_name=sub.get("brand_name") or "",
                            keyword=sub.get("keyword") or "",
                            daily_points=int(sub.get("daily_points") or 130),
                        )
                    except Exception as _ne:      # noqa: BLE001
                        # fail-open:通知失败不许拖住暂停本身(暂停是保护性动作)
                        logger.error(f"[notify] N3 暂停通知失败 sub#{sub['id']}: {_ne}")
                    await asyncio.to_thread(
                        update_keyword_monitor_state,
                        keyword_id=sub["keyword_id"],
                        is_monitored=False,
                    )
                    paused_for_balance += 1
                else:
                    runnable_subs.append(sub)  # 非 402 当可跑(其他 error 让 LLM 跑时再 fail)
            except Exception:
                runnable_subs.append(sub)

        if paused_for_balance > 0:
            logger.warning(f"  ⚠️ 余额不足暂停 {paused_for_balance} 订阅")

        if not runnable_subs:
            logger.info("📭 所有订阅余额不足 · 已 pause")
            return

        # 按客户分组(为每个客户创建一个监测任务)
        client_keywords = {}
        for sub in runnable_subs:
            quote_id = sub.get('quote_id')
            if quote_id not in client_keywords:
                client_keywords[quote_id] = []
            # 把 sub 标准化成 kw shape · 兼容下面 _process_keyword
            # [Deploy-CTO 2026-05-26 P0 根因修] 旧版 brand_name 没传 + target_brand="" 硬空
            # → LLM 检测拿空 brand 比对永远 False → 自动监测 7 天 0/176 detected
            # 修:list_active_subscriptions 新 JOIN quotes 拿 sub.brand_name · 这里直传
            client_keywords[quote_id].append({
                "id": sub["keyword_id"],
                "keyword": sub.get("keyword") or "",
                "monitoring_query": sub.get("monitoring_query"),
                "quote_id": sub.get("quote_id"),
                "brand_id": sub.get("brand_id"),
                "brand_name": sub.get("brand_name") or "",  # ← P0 修 · 跟 _process_keyword line 275 配对
                # [audit P0-4 2026-06-10] 实扣主体 = 解析后的 brand owner(与上方预检同主体)
                "user_id": sub.get("_billing_user_id") or sub["user_id"],
                "subscription_id": sub["id"],
                "feature_code": sub.get("feature_code") or "monitoring_keyword_daily",
                "eligible_platforms": sub["_eligible_monitoring_platforms"],
                "entitlement_platforms": (
                    sub.get("entitlement_platforms")
                    or sub["_eligible_monitoring_platforms"]
                ),
                "monitoring_product_version": sub.get("monitoring_product_version"),
                # [WO_MANUAL_KEYWORD_PARITY 2026-08-16 P0-3] 原来写死 "contract"。
                #   写死之所以一直没出事,是因为取数臂只有 confirmed 一条;本单加了 extra 臂之后,
                #   继续写死会把手动词当合同词落库 —— save_monitoring_result 按 cell.keyword_source
                #   决定结果写 confirmed_keyword_id 还是 keyword_id,判错就是把手动词的结果
                #   记到**同 id 的合同词**名下(两表 id 撞车时会记到别的客户头上)。
                #   回落 "contract" 保留旧行为:list_active_subscriptions 若因故没带 keyword_source,
                #   行为与本单之前逐字一致,不会静默改成 extra。
                "source": sub.get("keyword_source") or "contract",
                # [Deploy-CTO 2026-05-26 P0] _process_keyword:275 取 kw.get('target_brand') or kw.get('brand_name','')
                # target_brand 留空 · 让它 fallback 走 brand_name(上面新加 · 已从 sub.brand_name 拿到)
                "target_brand": sub.get("brand_name") or "",
            })

        logger.info(
            f"📋 daily_monitoring 待跑 {len(runnable_subs)} 订阅 · {len(client_keywords)} client · 并发 8"
        )

        # Semaphore(8):老板拍板 · DB pool maxconn=30 + 各引擎 RPM 60-200 · 8 安全
        sem = asyncio.Semaphore(8)
        total_completed = 0
        progress_lock = asyncio.Lock()

        async def _process_keyword(task_id: int, kw: dict, cell_lookup: dict) -> dict:
            """单 keyword 处理 · async with sem 限 8 并发 · sync DB 全 to_thread
            charge_on_success 包 LLM 调用 · 完成才扣 · 失败不扣
            """
            nonlocal total_completed
            async with sem:
                local_saved = 0
                local_detected = 0
                durable_results = []
                claim_state = None
                charge_committed = False
                freeze_handle = None
                dispatch_flags = []
                try:
                    keyword = kw['keyword']
                    target_brand = kw.get('target_brand') or kw.get('brand_name', '')
                    keyword_id = kw.get('id', 0)
                    user_id = kw.get('user_id')
                    feature_code = kw.get('feature_code') or 'monitoring_keyword_daily'
                    subscription_id = kw.get('subscription_id')
                    brand_id = kw.get('brand_id')
                    eligible_platforms = kw.get("eligible_platforms") or []
                    settlement_reference = str(kw.get("_settlement_reference") or "")

                    # [CTO-15.23 2026-05-19 P0 fix] atomic claim 今天扣费 slot · 防蓝绿+多scheduler race 重复扣
                    # 现象:1 keyword 同时间被扣 4 次 130(蓝绿 2x × scheduler.py + api/scheduler.py 2x = 4x)
                    # 修法:DB 层 INTERVAL '23 hours' atomic UPDATE · 只 1 个 instance 能 claim 成功
                    if user_id and subscription_id:
                        claim_state = await asyncio.to_thread(
                            claim_subscription_for_today_with_settlement,
                            subscription_id,
                            settlement_reference,
                        )
                        if not claim_state:
                            logger.info(
                                f"  跳过 sub#{subscription_id} keyword={keyword} "
                                f"23h 内已扣过(race detected · 蓝绿/多scheduler)"
                            )
                            for platform in eligible_platforms:
                                cell = cell_lookup[(kw["source"], int(keyword_id), platform)]
                                cell_claim = await asyncio.to_thread(
                                    claim_monitoring_run_cell,
                                    cell_id=int(cell["id"]),
                                    task_id=task_id,
                                    brand_id=brand_id,
                                    allowed_state="queued",
                                    lease_seconds=60,
                                )
                                await asyncio.to_thread(
                                    finish_monitoring_cell_error,
                                    cell_id=int(cell["id"]),
                                    claim_token=str(cell_claim["claim_token"]),
                                    state="failed",
                                    error_code="subscription_already_claimed",
                                    error_message="该订阅本周期已由另一执行器领取",
                                )
                            await asyncio.to_thread(
                                settle_monitoring_keyword_reference,
                                settlement_reference,
                                "released",
                            )
                            return {"saved": 0, "detected": 0}
                    if user_id:
                        freeze_handle = await freeze_points(
                            user_id=user_id,
                            feature_code=feature_code,
                            task_ref=settlement_reference,
                            brand_id=brand_id,
                            reason="每日监测关键词履约预留",
                        )
                        await asyncio.to_thread(
                            record_monitoring_keyword_settlement_freeze,
                            settlement_reference,
                            freeze_handle,
                        )

                    async def _run_cell(platform: str) -> dict:
                        cell = cell_lookup[(kw["source"], int(keyword_id), platform)]
                        claim = None
                        dispatched = False
                        try:
                            claim = await asyncio.to_thread(
                                claim_monitoring_run_cell,
                                cell_id=int(cell["id"]),
                                task_id=task_id,
                                brand_id=brand_id,
                                allowed_state="queued",
                                lease_seconds=300,
                            )
                            # The durable dispatch fence is adjacent to the paid
                            # provider call. A crash after this point is never
                            # treated as a safe no-charge retry.
                            await asyncio.to_thread(
                                mark_monitoring_cell_dispatched,
                                cell_id=int(cell["id"]),
                                claim_token=str(claim["claim_token"]),
                            )
                            dispatched = True
                            await asyncio.to_thread(
                                mark_monitoring_keyword_settlement_dispatched,
                                settlement_reference,
                            )
                            dispatch_flags.append(platform)
                            platform_result = await PlatformAdapter.query(
                                platform=platform,
                                question=resolve_monitoring_query(kw),
                                target_brand=target_brand,
                                search_mode="enhanced",
                                caller="monitoring",
                                brand_id=brand_id,
                                quote_id=kw.get("quote_id"),
                                user_id=user_id,
                                monitoring_task_id=task_id,
                                keyword_id=keyword_id,
                                keyword=keyword,
                            )
                            platform_result.update({
                                "task_id": task_id,
                                "keyword_id": keyword_id,
                                "keyword": keyword,
                                "keyword_source": kw["source"],
                                "platform": platform,
                                "sent_question_snapshot": resolve_monitoring_query(kw),
                                "target_brand": target_brand,
                                "provider_dispatched": True,
                                "cell_id": int(cell["id"]),
                            })
                            if platform_result.get("status") == "error":
                                error_code = str(
                                    platform_result.get("error_code") or "platform_unavailable"
                                )
                                cell_state = (
                                    "pending_provider_confirmation"
                                    if error_code in {
                                        "platform_unavailable", "provider_outcome_unknown"
                                    }
                                    else (
                                        "unavailable"
                                        if error_code == "platform_not_supported" else "failed"
                                    )
                                )
                                await asyncio.to_thread(
                                    finish_monitoring_cell_error,
                                    cell_id=int(cell["id"]),
                                    claim_token=str(claim["claim_token"]),
                                    state=cell_state,
                                    error_code=error_code,
                                    error_message=str(
                                        platform_result.get("error")
                                        or "该平台本次未返回可用结果"
                                    ),
                                )
                                platform_result["cell_state"] = cell_state
                                return platform_result

                            result_id = await asyncio.to_thread(
                                save_monitoring_result,
                                task_id=task_id,
                                keyword_id=int(keyword_id),
                                keyword=keyword,
                                platform=platform,
                                is_detected=bool(platform_result.get("is_detected")),
                                mention_type=platform_result.get("mention_type") or "none",
                                response_snippet=platform_result.get("response_snippet") or "",
                                full_response=platform_result.get("full_response") or "",
                                search_citations=platform_result.get("search_citations") or "",
                                competitors_mentioned=(
                                    platform_result.get("competitors_mentioned") or []
                                ),
                                lineage=platform_result,
                                identity_brand_id=brand_id,
                                identity_candidates=(
                                    platform_result.get("identity_candidates") or []
                                ),
                                identity_evidence_snippet=(
                                    platform_result.get("identity_evidence_snippet") or ""
                                ),
                                identity_review_state=(
                                    "pending"
                                    if platform_result.get("status") == "pending_identity"
                                    else "not_required"
                                ),
                                cell_id=int(cell["id"]),
                                cell_claim_token=str(claim["claim_token"]),
                            )
                            platform_result["result_id"] = result_id
                            return platform_result
                        except BaseException as exc:
                            if claim:
                                try:
                                    await asyncio.to_thread(
                                        finish_monitoring_cell_error,
                                        cell_id=int(cell["id"]),
                                        claim_token=str(claim["claim_token"]),
                                        state=(
                                            "pending_provider_confirmation"
                                            if dispatched else "failed"
                                        ),
                                        error_code=(
                                            "provider_outcome_unknown"
                                            if dispatched else "worker_lost_before_dispatch"
                                        ),
                                        error_message=str(exc)[:2000] or "监测执行中断",
                                    )
                                except Exception as settle_err:
                                    logger.error(
                                        "daily cell#%s 中断状态写入失败: %s",
                                        cell.get("id"), settle_err,
                                    )
                            if isinstance(exc, asyncio.CancelledError):
                                raise
                            return {
                                "status": "error",
                                "error_code": (
                                    "provider_outcome_unknown"
                                    if dispatched else "worker_lost_before_dispatch"
                                ),
                                "provider_dispatched": dispatched,
                                "keyword_id": keyword_id,
                                "keyword": keyword,
                                "keyword_source": kw["source"],
                                "platform": platform,
                                "cell_id": int(cell["id"]),
                            }

                    async def _execute_keyword_cells() -> list:
                        return await asyncio.gather(
                            *[_run_cell(platform) for platform in eligible_platforms]
                        )

                    # Daily is an asynchronous multi-provider job. Bind the existing
                    # freeze chain to the durable keyword settlement before dispatch;
                    # a hard crash is later reconciled from the cell dispatch fence.
                    durable_results = await _execute_keyword_cells()
                    local_saved = sum(
                        1 for item in durable_results if item.get("result_id") is not None
                    )
                    local_detected = sum(
                        1 for item in durable_results
                        if item.get("result_id") is not None
                        and not is_pending_identity_result(item)
                        and item.get("is_detected")
                    )
                    if not dispatch_flags:
                        raise RuntimeError("平台请求发送前执行失败 · 本关键词不扣费")
                    if user_id and freeze_handle and freeze_handle.get("freeze_id"):
                        settled = await commit_freeze(
                            freeze_id=freeze_handle.get("freeze_id"),
                            task_ref=settlement_reference,
                            user_id=user_id,
                            freeze_table=freeze_handle.get("freeze_table"),
                            reason="每日监测关键词已发送供应商请求",
                        )
                        if settled.get("success") is False:
                            raise RuntimeError(
                                f"daily monitoring freeze commit failed: {settled.get('reason')}"
                            )
                        charge_committed = True
                        await asyncio.to_thread(
                            settle_monitoring_keyword_reference,
                            settlement_reference,
                            "committed",
                        )
                    elif user_id:
                        # Admin/zero-price exemption is still durably explicit.
                        charge_committed = True
                        await asyncio.to_thread(
                            settle_monitoring_keyword_reference,
                            settlement_reference,
                            "admin_covered",
                        )
                    else:
                        await asyncio.to_thread(
                            settle_monitoring_keyword_reference,
                            settlement_reference,
                            "admin_covered",
                        )
                    if user_id:
                        await asyncio.to_thread(
                            record_monitoring_subscription_charge_for_settlement,
                            settlement_reference,
                        )

                except asyncio.CancelledError:
                    if dispatch_flags and not charge_committed:
                        try:
                            await asyncio.to_thread(
                                mark_monitoring_keyword_settlement_dispatched,
                                settlement_reference,
                            )
                        except Exception as state_err:
                            logger.error("daily cancel coverage_unknown 写入失败: %s", state_err)
                    elif charge_committed:
                        try:
                            await asyncio.to_thread(
                                settle_monitoring_keyword_reference,
                                settlement_reference,
                                "committed",
                            )
                        except Exception as state_err:
                            logger.error("daily cancel covered 写入失败: %s", state_err)
                    if claim_state and not charge_committed and not dispatch_flags:
                        try:
                            if freeze_handle and freeze_handle.get("freeze_id"):
                                released = await release_freeze(
                                    freeze_id=freeze_handle.get("freeze_id"),
                                    task_ref=settlement_reference,
                                    user_id=user_id,
                                    freeze_table=freeze_handle.get("freeze_table"),
                                    reason="每日监测取消且尚未发送供应商请求",
                                )
                                if released.get("success") is False:
                                    raise RuntimeError(released.get("reason") or "freeze release failed")
                            await asyncio.to_thread(
                                release_subscription_claim,
                                subscription_id,
                                claim_state["claim_token"],
                                claim_state["previous_value"],
                            )
                            await asyncio.to_thread(
                                settle_monitoring_keyword_reference,
                                settlement_reference,
                                "released",
                            )
                        except Exception as state_err:
                            logger.error("daily cancel release 写入失败: %s", state_err)
                    raise
                except HTTPException as he:
                    if dispatch_flags and not charge_committed:
                        try:
                            await asyncio.to_thread(
                                mark_monitoring_keyword_settlement_dispatched,
                                settlement_reference,
                            )
                        except Exception as state_err:
                            logger.error("daily coverage_unknown 写入失败: %s", state_err)
                    elif charge_committed:
                        try:
                            await asyncio.to_thread(
                                settle_monitoring_keyword_reference,
                                settlement_reference,
                                "committed",
                            )
                        except Exception as state_err:
                            logger.error("daily committed settlement 写入失败: %s", state_err)
                    if claim_state and not charge_committed and not dispatch_flags:
                        try:
                            if freeze_handle and freeze_handle.get("freeze_id"):
                                released = await release_freeze(
                                    freeze_id=freeze_handle.get("freeze_id"),
                                    task_ref=settlement_reference,
                                    user_id=user_id,
                                    freeze_table=freeze_handle.get("freeze_table"),
                                    reason="每日监测失败且尚未发送供应商请求",
                                )
                                if released.get("success") is False:
                                    raise RuntimeError(released.get("reason") or "freeze release failed")
                            await asyncio.to_thread(
                                release_subscription_claim,
                                subscription_id,
                                claim_state["claim_token"],
                                claim_state["previous_value"],
                            )
                            await asyncio.to_thread(
                                settle_monitoring_keyword_reference,
                                settlement_reference,
                                "released",
                            )
                        except Exception as state_err:
                            logger.error("daily released 写入失败: %s", state_err)
                    # charge_on_success 内部预检失败(余额不足 race condition)→ 跳过
                    if getattr(he, "status_code", None) == 402:
                        logger.warning(f"  跳过 keyword={kw.get('keyword')} 余额不足(race)")
                    else:
                        logger.error(f"  监测失败 {kw.get('keyword', 'unknown')}: {he}")
                except Exception as e:
                    if dispatch_flags and not charge_committed:
                        try:
                            await asyncio.to_thread(
                                mark_monitoring_keyword_settlement_dispatched,
                                settlement_reference,
                            )
                        except Exception as state_err:
                            logger.error("daily coverage_unknown 写入失败: %s", state_err)
                    elif charge_committed:
                        try:
                            await asyncio.to_thread(
                                settle_monitoring_keyword_reference,
                                settlement_reference,
                                "committed",
                            )
                        except Exception as state_err:
                            logger.error("daily committed settlement 写入失败: %s", state_err)
                    if claim_state and not charge_committed and not dispatch_flags:
                        try:
                            if freeze_handle and freeze_handle.get("freeze_id"):
                                released = await release_freeze(
                                    freeze_id=freeze_handle.get("freeze_id"),
                                    task_ref=settlement_reference,
                                    user_id=user_id,
                                    freeze_table=freeze_handle.get("freeze_table"),
                                    reason="每日监测异常且尚未发送供应商请求",
                                )
                                if released.get("success") is False:
                                    raise RuntimeError(released.get("reason") or "freeze release failed")
                            await asyncio.to_thread(
                                release_subscription_claim,
                                subscription_id,
                                claim_state["claim_token"],
                                claim_state["previous_value"],
                            )
                            await asyncio.to_thread(
                                settle_monitoring_keyword_reference,
                                settlement_reference,
                                "released",
                            )
                        except Exception as state_err:
                            logger.error("daily released 写入失败: %s", state_err)
                    logger.error(f"  监测失败 {kw.get('keyword', 'unknown')}: {e}")

                # 进度 log 每 100 keyword 一次(防 log 风暴 + 排查 hang)
                async with progress_lock:
                    total_completed += 1
                    if total_completed % 100 == 0 or total_completed == total:
                        elapsed = (datetime.now() - start_ts).total_seconds()
                        logger.info(
                            f"  daily_monitoring 进度 {total_completed}/{total} · "
                            f"耗时 {elapsed:.0f}s"
                        )

                pending = sum(
                    1 for item in durable_results
                    if is_pending_identity_result(item)
                )
                return {"saved": local_saved, "eligible": local_saved - pending,
                        "pending": pending, "detected": local_detected}

        total_saved = 0
        total_detected = 0

        # 按 client 串行 · 每 client 内 keyword 8 并发(sem 跨 client 共享)
        for quote_id, kws in client_keywords.items():
            # The task plan is durable before any provider starts. Each keyword
            # keeps its own charge-on-success fulfillment credential.
            import uuid as _daily_uuid
            task_keyword_ids = [int(kw["id"]) for kw in kws]
            task_brand_ids = {int(kw["brand_id"]) for kw in kws if kw.get("brand_id")}
            if len(task_brand_ids) != 1:
                logger.error("daily quote#%s brand scope invalid: %s", quote_id, task_brand_ids)
                continue
            task_brand_id = task_brand_ids.pop()
            planned_test_count = sum(len(kw["eligible_platforms"]) for kw in kws)
            planned_platforms = []
            for kw in kws:
                kw["_eligible_monitoring_platforms"] = list(kw["eligible_platforms"])
                kw["_fulfillment_credential"] = str(_daily_uuid.uuid4())
                kw["_fulfillment_state"] = (
                    "reserved" if kw.get("user_id") else "admin_covered"
                )
                kw["_retry_coverage"] = {
                    "policy_version": "monitoring-retry-v1",
                    "coverage": "included",
                    "max_attempts": 1,
                }
                for platform in kw["eligible_platforms"]:
                    if platform not in planned_platforms:
                        planned_platforms.append(platform)
            task_id = await asyncio.to_thread(
                create_monitoring_task,
                client_id=str(quote_id),
                brand_id=task_brand_id,
                keyword_ids=task_keyword_ids,
                task_name=f"每日自动监测_{datetime.now().strftime('%Y%m%d')}",
                trigger_type="scheduled",
                planned_test_count=planned_test_count,
                planned_platform_count=len(planned_platforms),
            )
            for kw in kws:
                kw["_settlement_reference"] = (
                    f"monitoring_daily:{task_id}:{kw['source']}:{int(kw['id'])}:"
                    f"{kw['_fulfillment_credential']}"
                )
            await asyncio.to_thread(
                create_monitoring_keyword_settlements,
                task_id=task_id,
                brand_id=task_brand_id,
                keywords=kws,
            )
            durable_cells = await asyncio.to_thread(
                create_monitoring_run_cells,
                task_id=task_id,
                brand_id=task_brand_id,
                keywords=kws,
                search_mode="enhanced",
                fulfillment_credential=str(_daily_uuid.uuid4()),
                fulfillment_state="reserved",
                retry_coverage={
                    "policy_version": "monitoring-retry-v1",
                    "coverage": "unknown",
                    "max_attempts": 0,
                },
            )
            cell_lookup = {
                (cell["keyword_source"], int(cell["keyword_id"]), cell["platform"]): cell
                for cell in durable_cells if cell["is_planned"]
            }
            await asyncio.to_thread(update_task_status, task_id, "running")

            # gather 限 8 并发(sem 全局共享)
            results = await asyncio.gather(
                *[_process_keyword(task_id, kw, cell_lookup) for kw in kws],
                return_exceptions=True
            )
            cancelled = next(
                (result for result in results if isinstance(result, asyncio.CancelledError)),
                None,
            )
            if cancelled is not None:
                try:
                    await asyncio.to_thread(refresh_monitoring_task_from_cells, task_id)
                finally:
                    raise cancelled

            # 聚合本 client 结果
            client_saved = sum(r["saved"] for r in results if isinstance(r, dict))
            client_eligible = sum(r.get("eligible", r["saved"]) for r in results if isinstance(r, dict))
            client_pending = sum(r.get("pending", 0) for r in results if isinstance(r, dict))
            client_detected = sum(r["detected"] for r in results if isinstance(r, dict))

            # 完成本 client task(to_thread)
            detection_rate = round(client_detected / client_eligible * 100, 1) if client_eligible > 0 else 0
            await asyncio.to_thread(
                update_task_status,
                task_id,
                "completed",
                completed_tests=client_saved,
                result_summary={
                    "attempted_tests": planned_test_count,
                    "total_tests": client_eligible,
                    "error_count": planned_test_count - client_saved,
                    "pending_identity_count": client_pending,
                    "detected_count": client_detected,
                    "detection_rate": detection_rate
                }
            )
            engines_per_kw = planned_test_count // max(len(kws), 1)
            logger.info(
                f"  client {quote_id} done · {len(kws)} kw × ~{engines_per_kw} engine · "
                f"saved={client_saved} detected={client_detected} ({detection_rate}%)"
            )

            # [CTO-15.23 2026-05-19 P0 fix] 当日趋势统计 · 从 monitoring_results 真实聚合
            # 旧版 detected_count=1 if target_brand else 0 + test_count=4 写死 = 假数据
            # ON CONFLICT UPSERT 覆盖了用户手动跑监测时的真数据 · 前端 trend chart 显示假 25%
            # 修法:复用 api/monitoring_api.py 同款 SQL 聚合真实 is_detected · 写 ground truth
            today = datetime.now().strftime("%Y-%m-%d")
            try:
                from db.monitoring_db import calculate_rate_change, get_connection as _get_mon_conn

                # 闭包变量:task_id / kws / today
                def _aggregate_and_save_trend() -> int:
                    """sync helper · 一次性 SUM(monitoring_results) + UPSERT trend_stats"""
                    from services.monitoring_identity_review import aggregate_eligible_sql
                    conn_t = _get_mon_conn()
                    try:
                        cur_t = conn_t.cursor()
                        cur_t.execute(
                            f"""
                            SELECT COALESCE(confirmed_keyword_id, keyword_id) AS keyword_id,
                                   COUNT(*) AS tests,
                                   SUM(CASE WHEN is_detected = 1 THEN 1 ELSE 0 END) AS detected
                            FROM monitoring_results
                            WHERE task_id = %s AND {aggregate_eligible_sql()}
                            GROUP BY COALESCE(confirmed_keyword_id, keyword_id)
                            """,
                            (task_id,)
                        )
                        rows_t = cur_t.fetchall()
                        written = 0
                        for row_t in rows_t:
                            kid = row_t["keyword_id"]
                            tests = row_t["tests"] or 0
                            detected_cnt = row_t["detected"] or 0
                            if tests == 0:
                                continue
                            # source 找原 kw · 默认 contract(daily 走 keyword_monitor_subscriptions)
                            source = 'contract'
                            for kw_t in kws:
                                if kw_t.get('id') == kid:
                                    source = kw_t.get('source', 'contract')
                                    break
                            current_rate = round(detected_cnt / tests * 100, 1)
                            rate_change = calculate_rate_change(kid, source, current_rate, 'daily')
                            save_trend_stat(
                                keyword_id=kid,
                                keyword_source=source,
                                period_type='daily',
                                period_date=today,
                                test_count=tests,
                                detected_count=detected_cnt,
                                rate_change=rate_change
                            )
                            written += 1
                        # [CTO-15.23 2026-05-19 补漏 patch · Deploy-CTO 复查发现]
                        # 8ca7605d 漏了 trend_synced=1 update · 跟 api/scheduler.py:848 对齐
                        # 不影响 trend 数据准确性 · 仅修 monitoring_tasks.trend_synced flag 一致性
                        if written > 0:
                            cur_t.execute(
                                "UPDATE monitoring_tasks SET trend_synced = 1 WHERE id = %s",
                                (task_id,)
                            )
                            conn_t.commit()
                        return written
                    finally:
                        try:
                            conn_t.close()
                        except Exception:
                            pass

                written = await asyncio.to_thread(_aggregate_and_save_trend)
                if written > 0:
                    logger.info(f"  client {quote_id} trend 写真实聚合 {written} 条")
            except Exception as e:
                logger.warning(f"daily_monitoring 趋势聚合失败 client={quote_id}: {e}")

            total_saved += client_saved
            total_detected += client_detected

        # 整个 job 完成 log
        elapsed_min = (datetime.now() - start_ts).total_seconds() / 60
        overall_rate = round(total_detected / total_saved * 100, 1) if total_saved > 0 else 0
        logger.info(
            f"✅ daily_monitoring 全部完成 · 总耗时 {elapsed_min:.1f} min · "
            f"DB writes={total_saved} · detected={total_detected} ({overall_rate}%) · "
            f"client {len(client_keywords)} · keyword {total}"
        )

    except Exception as e:
        logger.error(f"❌ 每日监测任务出错: {e}")
        import traceback
        traceback.print_exc()


async def job_token_expiry_check():
    """Token过期检查 - 检查并标记即将过期的Token"""
    from db.monitoring_db import get_expiring_tokens, deactivate_expired_tokens
    
    logger.info("🔑 检查Token过期状态...")
    
    try:
        # 获取即将在7天内过期的Token
        expiring = get_expiring_tokens(days=7)
        if expiring:
            logger.warning(f"⚠️ 发现 {len(expiring)} 个Token即将过期")
            # 可以在这里添加通知逻辑
        
        # 停用已过期的Token
        deactivated = deactivate_expired_tokens()
        if deactivated > 0:
            logger.info(f"🚫 已停用 {deactivated} 个过期Token")
        else:
            logger.info("✅ 无过期Token")
    except Exception as e:
        logger.error(f"❌ Token过期检查出错: {e}")


def job_backup_databases():
    """
    每日SQLite数据库备份
    保留最近30天的备份，超过30天自动清理
    """
    import shutil
    import glob
    import os
    from pathlib import Path
    
    logger.info("💾 开始数据库备份...")
    
    try:
        db_dir = Path(__file__).parent / "db"
        backup_dir = Path(__file__).parent / "data" / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        backed_up = 0
        
        # 备份所有 .db 文件
        for db_file in db_dir.glob("*.db"):
            backup_name = f"{db_file.stem}.{ts}.bak"
            backup_path = backup_dir / backup_name
            shutil.copy2(str(db_file), str(backup_path))
            size_mb = backup_path.stat().st_size / (1024 * 1024)
            logger.info(f"  📦 已备份: {db_file.name} -> {backup_name} ({size_mb:.1f}MB)")
            backed_up += 1
        
        # 清理30天前的备份
        cutoff = datetime.now() - timedelta(days=30)
        cleaned = 0
        for bak_file in backup_dir.glob("*.bak"):
            if datetime.fromtimestamp(bak_file.stat().st_mtime) < cutoff:
                bak_file.unlink()
                cleaned += 1
        
        logger.info(f"✅ 数据库备份完成: {backed_up} 个文件, 清理旧备份: {cleaned} 个")
    except Exception as e:
        logger.error(f"❌ 数据库备份失败: {e}")


def _brand_name_for_log(brand_id) -> str:
    """取品牌名,**只用于日志**。任何失败都返回占位,绝不向外抛。

    🔴 [#120] 存在理由不是"复用",是**隔离**:调用方那一步唯一的用途是拼一行
       logger.info。它以前跟 `enqueue` 共用一个 try,于是它一抛异常就把整个提醒
       吞掉了 —— 而它确实一直在抛。现在失败只损失日志里的一个名字。

    🔴 这个定义必须在 `@sched_claim` 装饰器**之前**。第一版我插在装饰器与
       被修饰函数之间,于是 claim 套到了本函数头上,而 `job_payment_overdue_check`
       **丢了并发保护** —— ast.parse 过、import 过、7 条判据里 5 条绿,
       只有"连接必须被关"那一条抓住了它。
    """
    if not brand_id:
        return "客户"
    conn = None
    try:
        from db.diagnosis_db import get_connection
        conn = get_connection()
        cursor = conn.cursor()          # 🔴 连接对象没有 .execute,必须取 cursor
        cursor.execute("SELECT name FROM brands WHERE id = %s", (int(brand_id),))
        row = cursor.fetchone()
        return (dict(row).get("name") if row else None) or "客户"
    except Exception:
        logger.warning("[#120] 取品牌名失败 brand_id=%s —— 仅影响日志文案", brand_id)
        return "客户"
    finally:
        # 🔴 finally,不是顺序执行:原版 close 写在可能抛出的那行**后面**,
        #    一旦抛异常就永远关不掉 —— 那正是这次的连接泄漏。
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# [#151 · 2026-09-08] 图文成品的发布终态收敛。
#   `publish_status='publishing'` 今天**没有出口**:`bind_publish_result` 全仓
#   只有两个调用点,一个写非终态线索值、一个写 publishing,**没人写 published**
#   (那处注释说的「既有回调链」从不调它)。生产实证 post 24 卡了 29 天。
#   本任务只读 mhz item 的终态来收敛,**不编造超时结论**(见服务模块 docstring)。
@sched_claim("geo_douyin_publish_converge", 3600)
def job_geo_douyin_publish_converge():
    """把能判的收敛掉,把判不了的喊出来。

    🔴 [WO_213] 这个 job **故意不认总闸**,和 `run_tick` 里那五个收敛器同一条口径:
       闸的语义是「不再开始新的、不再花新的钱」,**不是**「把已经花出去的丢在半路」。
       本函数只读 mhz item 的终态、写回作品终态 —— 不下单、不冻结、不扣费
       (逐行核过:只有 `_order_items` 读库与 `bind_publish_result` 写回)。
       把它也关掉,关闸那一刻在途的 `publishing` 作品就永远卡在那儿,
       连带 #184 那批冻结结不了 —— 那比"图文还在跑"糟得多。
    """
    try:
        from services.geo_douyin.publish_convergence import converge_publishing_posts

        stats = converge_publishing_posts()
        logger.info("[publish-converge] 扫 %s 条:成 %s / 败 %s / 在途 %s / 久滞 %s",
                    stats["scanned"], stats["published"], stats["failed"],
                    stats["in_flight"], stats["stale"])
    except Exception as e:  # noqa: BLE001 - 收敛失败不该拖垮调度器
        logger.error("[publish-converge] 收敛失败: %s", e, exc_info=True)


@sched_claim("payment_overdue_check", 86400)  # [FF5] tick claim(audit NEEDS_CLAIM · daily 待收款/overdue 提醒)
def job_payment_overdue_check():
    """
    每日检查待收款订单，超期提醒
    - 1/3/7/14/21/30 天推送通知
    - 超过30天自动标记 payment_overdue
    """
    import json as _json
    logger.info("💰 开始待收款订单检查...")

    try:
        from db.diagnosis_db import get_connection, update_session
        from services.notification_events import NotificationEventType
        from services.notification_outbox import enqueue_brand_owner_notification_event_durable

        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT token, brand_id, quote_id, sales_confirmed_at, payment_overdue_notified_days, status "
                "FROM keyword_selection_sessions WHERE status IN ('pending_payment', 'payment_overdue')"
            )
            rows = cursor.fetchall()
        finally:
            conn.close()

        reminder_days = {1, 3, 7, 14, 21, 30}
        now = datetime.now()

        for row in rows:
            row = dict(row)
            if not row.get("sales_confirmed_at"):
                continue

            confirmed_dt = datetime.fromisoformat(row["sales_confirmed_at"])
            days_elapsed = (now - confirmed_dt).days
            notified = set(_json.loads(row.get("payment_overdue_notified_days") or "[]"))

            # 超过30天标记为 payment_overdue
            if days_elapsed >= 30 and row["status"] == "pending_payment":
                update_session(row["token"], status="payment_overdue")
                logger.info(f"  ⚠️ {row['token']} 已超过30天未付款，标记为 payment_overdue")

            # 发送提醒通知
            for d in reminder_days:
                if days_elapsed >= d and d not in notified:
                    # 🔴 [#120 · 2026-09-06] 原来这四行长在下面那个 `try:` **里面**:
                    #      conn2 = get_connection()
                    #      brand_row = conn2.execute(...).fetchone()  ← psycopg2 的**连接**没有 .execute
                    #      brand_name = ... ; conn2.close()
                    #
                    #    第二行必抛 AttributeError ⇒ 被下面那个 except 接住 ⇒
                    #    **enqueue 从来没有被调用过**,`notified` 也从不增长 ⇒ 1/3/7/14/21/30 天
                    #    的提醒每天重算、每天失败,一条都没发出去。日志写「写入 outbox 失败」——
                    #    而它根本没走到 outbox(错的具体消息比笼统的更坏:它把人指向 outbox)。
                    #    🔴 且 `conn2.close()` 在抛出点**之后** ⇒ 每次还漏一个连接。
                    #
                    #    🔴 结构修法:`brand_name` **只喂下面那行日志**,enqueue 根本不用它。
                    #    只喂日志的东西不该有能力中止真正干活的那步 —— 取名挪出 try 并自带兜底。
                    brand_name = _brand_name_for_log(row.get("brand_id"))
                    try:

                        enqueue_brand_owner_notification_event_durable(
                            brand_id=int(row["brand_id"]),
                            event_type=NotificationEventType.PAYMENT_REMINDER,
                            business_id=f"quote:{int(row['quote_id'])}:reminder:{int(d)}",
                            terminal_state=f"overdue_day_{int(d)}",
                            facts={
                                "business_no": f"QUOTE-{int(row['quote_id'])}",
                                "status": f"已等待收款 {days_elapsed} 天",
                                "occurred_at": now.isoformat(timespec="seconds"),
                                "summary": "请在客户报价页面跟进收款；查询本身不会改变订单状态。",
                            },
                        )
                        notified.add(d)
                        logger.info(f"  📨 已发送 {d} 天收款提醒: {brand_name}")
                    except Exception as e:
                        logger.exception("待收款提醒写入 outbox 失败: %s", e)

            # 更新已通知天数
            update_session(row["token"], payment_overdue_notified_days=_json.dumps(sorted(notified)))

        logger.info(f"✅ 待收款检查完成，共 {len(rows)} 个待收款订单")
    except Exception as e:
        logger.error(f"❌ 待收款检查出错: {e}")


def job_weekly_report():
    """每周一自动为所有付费客户生成周报"""
    logger.info("📊 开始生成周报...")
    try:
        from db.monitoring_db import get_paid_clients
        from api.monitoring_api import api_generate_weekly_report
        clients = get_paid_clients(limit=200)
        success_count = 0
        for client in clients:
            brand_id = client.get("brand_id")
            if not brand_id:
                continue
            try:
                result = api_generate_weekly_report(brand_id=brand_id)
                if result.get("status") == "success":
                    success_count += 1
                    logger.info(f"  ✅ 周报生成: {client.get('brand_name', brand_id)}")
                else:
                    logger.info(f"  ⏭️ 跳过: {client.get('brand_name', brand_id)} - {result.get('message', '')}")
            except Exception as e:
                logger.warning(f"  ❌ 周报失败: {client.get('brand_name', brand_id)} - {e}")
        logger.info(f"📊 周报生成完成，成功 {success_count}/{len(clients)}")
    except Exception as e:
        logger.error(f"❌ 周报定时任务出错: {e}")


def job_monthly_report():
    """每月1日自动为所有付费客户生成月报"""
    logger.info("📊 开始生成月报...")
    try:
        from db.monitoring_db import get_paid_clients
        from api.monitoring_api import api_generate_monthly_report
        clients = get_paid_clients(limit=200)
        success_count = 0
        for client in clients:
            brand_id = client.get("brand_id")
            if not brand_id:
                continue
            try:
                result = api_generate_monthly_report(brand_id=brand_id)
                if result.get("status") == "success":
                    success_count += 1
                    logger.info(f"  ✅ 月报生成: {client.get('brand_name', brand_id)}")
                else:
                    logger.info(f"  ⏭️ 跳过: {client.get('brand_name', brand_id)} - {result.get('message', '')}")
            except Exception as e:
                logger.warning(f"  ❌ 月报失败: {client.get('brand_name', brand_id)} - {e}")
        logger.info(f"📊 月报生成完成，成功 {success_count}/{len(clients)}")
    except Exception as e:
        logger.error(f"❌ 月报定时任务出错: {e}")


async def job_publish_order_sync():
    """
    每 10 分钟：使用 StatusSyncer 同步代发订单状态（按 order_sn 精确匹配）
    """
    logger.info("📤 开始代发订单状态同步...")
    try:
        from db.publish_db import (
            get_mhz_session, get_submitted_items, get_pending_items,
            update_item_submitted, update_item_published,
            update_item_rejected, update_item_refunded, update_item_queued,
            recalculate_order_status, recalculate_batch_status, get_order,
        )
        from services.meijiehezi_client import MeiJieHeZiClient, StatusSyncer, SessionExpiredError

        session_id = get_mhz_session()
        if not session_id:
            logger.info("  无媒介盒子 Session 配置，跳过")
            return

        async with MeiJieHeZiClient(session_id) as client:
            session_valid = await client.check_session()

            if session_valid:
                # 1. 处理 queued 订单（Session 恢复）
                pending = get_pending_items()
                queued = [i for i in pending if i["status"] == "queued"]
                if queued:
                    logger.info(f"  Session 恢复，处理 {len(queued)} 条排队订单")
                    try:
                        from datetime import datetime, timezone
                        from db.meijiehezi_db import get_config, set_config
                        from services.notification_events import NotificationEventType
                        from services.notification_outbox import enqueue_admin_notification_events_durable

                        failed_since = get_config("mhz_session_failed_since") or "legacy-sync"
                        enqueue_admin_notification_events_durable(
                            event_type=NotificationEventType.EXTERNAL_CHANNEL_RECOVERED,
                            business_id=f"mhz-session:{failed_since}",
                            terminal_state="recovered",
                            facts={
                                "business_no": "CHANNEL-MHZ",
                                "status": "外部发布通道已恢复",
                                "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                "summary": "排队任务将按既有调度规则继续处理。",
                            },
                        )
                        set_config("mhz_session_failed_since", "")
                        set_config("mhz_session_failed_count", "0")
                    except Exception as exc:
                        logger.exception("外部发布通道恢复 outbox 写入失败: %s", exc)
                    for item in queued:
                        try:
                            from services.article_publish_dispatch import dispatch_article_to_provider
                            result = await dispatch_article_to_provider(
                                client=client, dispatch_kind="publish", article_id=item["article_id"],
                                source_title=item["article_title"],
                                source_content=item.get("article_content") or "",
                                outgoing_title=item["article_title"],
                                outgoing_content=item.get("article_content") or "",
                                source="legacy_scheduler_queue:softarticle",
                                provider_kwargs={"media_ids": [item["media_id"]]},
                            )
                            update_item_submitted(item["id"], mhz_order_id=str(result.success_count))
                        except (SessionExpiredError, Exception) as e:
                            logger.warning(f"  排队订单提交失败: {e}")
                        await asyncio.sleep(0.5)

                # 2. 使用 StatusSyncer 同步已提交订单
                async def _get_pending():
                    items = get_submitted_items()
                    return [{"mhz_order_id": i.get("mhz_order_id", ""), "order_item_id": i["id"],
                             "order_id": i["order_id"], "user_id": i["user_id"],
                             "media_name": i.get("media_name", ""), "article_title": i.get("article_title", "")}
                            for i in items]

                async def _update_status(item_id, status, publish_url, reject_reason, publish_time):
                    if status == "published":
                        update_item_published(item_id, publish_url)
                        # [CTO-15.23 2026-05-25] 取消旧 add_publication 垃圾写入(quote_id=0 + 空字段)
                        # 真相源是 mhz_synced_orders · 监测中心读路径已直接读那张表 by brand_id
                        # 历史这里写的脏数据永远不会被读到(quote_id=0 不命中任何 quote)
                    elif status == "rejected":
                        update_item_rejected(item_id, reject_reason)

                async def _on_rejected(item_id, reject_reason):
                    try:
                        # 查找 user_id + 文章标题
                        from db.connection import get_connection as _gc
                        conn = _gc()
                        cur = conn.cursor()
                        cur.execute("""
                            SELECT po.user_id, poi.article_title, poi.media_name
                            FROM publish_order_items poi
                            JOIN publish_orders po ON poi.order_id=po.id
                            WHERE poi.id=%s
                        """, (item_id,))
                        row = cur.fetchone()
                        conn.close()
                        if row:
                            from middleware.billing import refund_points
                            from services.notification_events import publication_refund_context
                            refund_result = await refund_points(
                                row["user_id"],
                                "media_proxy_publish",
                                "代发拒稿退款",
                                notification=publication_refund_context(
                                    f"publication_item:{int(item_id)}"
                                ),
                            )
                            update_item_refunded(item_id)
                    except Exception as e:
                        logger.warning(f"  退款失败: {e}")

                syncer = StatusSyncer(client, _get_pending, _update_status, _on_rejected)
                result = await syncer.sync()

                # 重算批次状态
                if result.published > 0 or result.rejected > 0:
                    submitted = get_submitted_items()
                    batch_ids = set()
                    for item in submitted:
                        order = get_order(item["order_id"])
                        if order and order.get("batch_id"):
                            batch_ids.add(order["batch_id"])
                    for bid in batch_ids:
                        recalculate_batch_status(bid)

            else:
                # Session 失效
                pending = get_pending_items()
                new_pending = [i for i in pending if i["status"] == "pending"]
                for item in new_pending:
                    update_item_queued(item["id"])
                if new_pending:
                    logger.warning(f"  Session 失效，{len(new_pending)} 条订单进入排队")
                try:
                    from datetime import datetime, timezone
                    from db.meijiehezi_db import get_config, set_config
                    from services.notification_events import NotificationEventType
                    from services.notification_outbox import enqueue_admin_notification_events_durable

                    failed_since = get_config("mhz_session_failed_since") or datetime.now(
                        timezone.utc
                    ).isoformat(timespec="seconds")
                    set_config("mhz_session_failed_since", failed_since)
                    enqueue_admin_notification_events_durable(
                        event_type=NotificationEventType.EXTERNAL_CHANNEL_FAILED,
                        business_id=f"mhz-session:{failed_since}",
                        terminal_state="failed",
                        facts={
                            "business_no": "CHANNEL-MHZ",
                            "status": "外部发布通道不可用",
                            "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "summary": "发布任务已暂停或等待恢复，请在运维页面处理通道状态。",
                        },
                    )
                except Exception as exc:
                    logger.exception("外部发布通道失效 outbox 写入失败: %s", exc)

        logger.info("✅ 代发订单同步完成")
    except Exception as e:
        logger.error(f"❌ 代发订单同步出错: {e}")
        import traceback
        traceback.print_exc()


async def job_publish_media_sync():
    """
    每天 02:00：从媒介盒子同步全量媒体列表
    """
    logger.info("📋 开始媒体列表同步...")
    try:
        from db.publish_db import get_mhz_session
        from api.publish_api import _sync_media

        session_id = get_mhz_session()
        if not session_id:
            logger.info("  无媒介盒子 Session 配置，跳过")
            return

        await _sync_media(session_id)
        logger.info("✅ 媒体列表同步完成")
    except Exception as e:
        logger.error(f"❌ 媒体列表同步出错: {e}")


def setup_default_jobs():
    """设置默认的定时任务"""
    global scheduler
    if scheduler is None:
        init_scheduler()
    
    # Automatic paid monitoring is registered by api.scheduler only. Keep
    # job_daily_monitoring here as the implementation/manual trigger, but never
    # start a second automatic control plane from the root scheduler.
    
    # Token过期检查：每天上午9点执行
    add_job(
        job_token_expiry_check,
        trigger="cron",
        job_id="token_expiry_check",
        hour=9,
        minute=0,
        replace_existing=True
    )
    
    # 🆕 数据库备份：每天凌晨2点执行
    add_job(
        job_backup_databases,
        trigger="cron",
        job_id="daily_db_backup",
        hour=2,
        minute=0,
        replace_existing=True
    )

    # [#151] 发布终态收敛:每小时一次(它只读 + 幂等,频繁跑无害)
    add_job(
        job_geo_douyin_publish_converge,
        trigger="interval",
        job_id="geo_douyin_publish_converge",
        hours=1,
        replace_existing=True
    )

    # 待收款订单检查：每天上午10点执行
    add_job(
        job_payment_overdue_check,
        trigger="cron",
        job_id="payment_overdue_check",
        hour=10,
        minute=0,
        replace_existing=True
    )

    # 周报自动生成：每周一上午8点为所有付费客户生成周报
    add_job(
        job_weekly_report,
        trigger="cron",
        job_id="weekly_report",
        day_of_week="mon",
        hour=8,
        minute=0,
        replace_existing=True
    )

    # 月报自动生成：每月1日上午9点为所有付费客户生成月报
    add_job(
        job_monthly_report,
        trigger="cron",
        job_id="monthly_report",
        day=1,
        hour=9,
        minute=0,
        replace_existing=True
    )

    # [E3 第 0 片] 社媒两个 cron(daily_inspiration 今日灵感 / event_driven_checks 事件驱动检查)已摘除,
    # 锁 tests/oss_e3s0_live_breakpoints_2026_09_27 守它们不回来。

    # [2026-04-30] 老的 publish_order_sync / publish_media_sync 已下线
    # 新链路在 api/scheduler.py 的 _setup_mhz_jobs 注册（mhz_status_sync /
    # mhz_toutiao_order_sync / mhz_media_sync / mhz_wemedia_sync 等）
    # 不在此处重复注册，避免两套任务并发竞争 mhz session

    logger.info("📋 默认定时任务已配置")


# =====================================================
#  API函数供server.py调用
# =====================================================

def api_get_scheduler_status():
    """获取调度器状态（合并根 scheduler + api/scheduler v32 两个实例的 jobs）

    2026-04-17: 历史原因系统有两个独立 BackgroundScheduler：
    - scheduler.py (本模块): non-monitoring default jobs (publish_order_sync 等)
    - api/scheduler.py: monitoring + v3.2/v3.3 核心任务
    此函数合并两侧 jobs 统一返回，方便 admin /api/scheduler/status 一次看完。
    """
    global scheduler
    default_jobs = get_jobs()

    # 追加 api/scheduler.py v32 核心任务
    v32_jobs = []
    try:
        from api.scheduler import get_scheduler as _get_v32_scheduler
        v32_scheduler = _get_v32_scheduler()
        for job in v32_scheduler.get_jobs():
            v32_jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger": str(job.trigger),
                "source": "v32_core",  # 标记来源便于运维
            })
    except Exception as e:
        logger.warning(f"合并 v32 scheduler jobs 失败: {e}")

    return {
        "running": scheduler.running if scheduler else False,
        "jobs": default_jobs + v32_jobs,
        "total_count": len(default_jobs) + len(v32_jobs),
        "default_count": len(default_jobs),
        "v32_core_count": len(v32_jobs),
    }


async def api_trigger_job(job_id: str):
    """手动触发任务"""
    if job_id == "daily_monitoring":
        await job_daily_monitoring()
    elif job_id == "token_expiry_check":
        await job_token_expiry_check()
    else:
        raise ValueError(f"未知任务: {job_id}")
    return {"status": "success", "message": f"任务 {job_id} 已执行"}
